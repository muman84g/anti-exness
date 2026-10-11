"""Synthetic lifecycle hook contract; not a connected market replay adapter.

Only shock/cooldown state changes between CARRY and RESET. External preload
and broker events continue while the EA's decision callback is suspended.
No strategy formula, fee, clock provenance or broker fill policy is inferred.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Boundary:
    removed_ms: int
    loaded_ms: int
    ready_ms: int

    def __post_init__(self):
        if not self.removed_ms <= self.loaded_ms <= self.ready_ms:
            raise ValueError("remove/load/ready order invalid")


class LifecycleBoundary:
    def __init__(self, engine, boundary: Boundary, mode: str):
        if mode not in {"L_CARRY", "L_RESET"}:
            raise ValueError("unsupported lifecycle mode")
        self.engine = engine
        self.boundary = boundary
        self.mode = mode
        self.ready_applied = False
        self.last_ms = None
        self.events = []
        self.deferred_followups = []

    def _broker_phase(self, tick, callback, active):
        if active:
            callback(tick)
            return
        # The old gateway mixes fill reconciliation and strategy followups.
        # Keep the fill, and defer known sends/cancel/flat completion until ready.
        methods = ("_make_hedge_stop", "_retry_or_degrade", "_issue_next_batch_close",
                   "_cancel_pending_stop", "_finalize_flat")
        saved = {name: getattr(self.engine, name) for name in methods}
        saved_new_order = self.engine._new_order
        def forbidden_send(*args, **kwargs):
            raise RuntimeError("detached broker callback attempted unseparated order send")
        def defer(name):
            def deferred(*args, **kwargs):
                self.deferred_followups.append((name, args, kwargs))
            return deferred
        try:
            for name in methods:
                setattr(self.engine, name, defer(name))
            self.engine._new_order = forbidden_send
            callback(tick)
        finally:
            for name, method in saved.items():
                setattr(self.engine, name, method)
            self.engine._new_order = saved_new_order

    def _ready_followups(self, tick):
        deferred, self.deferred_followups = self.deferred_followups, []
        for name, args, kwargs in deferred:
            if name == "_make_hedge_stop":
                position = self.engine.inventory.get(args[0].position_id)
                if position is not None and position.volume_steps > 0:
                    self.engine._make_hedge_stop(position, tick)
            elif name == "_retry_or_degrade":
                command = args[0]
                position = self.engine.inventory.get(command["target_position_id"]) if command["command_type"] == "exit" else None
                # Reject caller can pass position=None even for an exit. The
                # gateway must reconcile the current target or degrade; never
                # consume and silently drop the deferred exit retry.
                self.engine._retry_or_degrade(command, tick, position=position)
            elif name == "_issue_next_batch_close":
                if self.engine.close_batch is not None:
                    self.engine._issue_next_batch_close(tick)
            elif name == "_cancel_pending_stop":
                self.engine._cancel_pending_stop(tick, kwargs.get("reason", args[1] if len(args) > 1 else "deferred_cancel"))
            elif name == "_finalize_flat":
                if not self.engine.inventory:
                    self.engine._finalize_flat(tick, kwargs.get("reason", args[1] if len(args) > 1 else "deferred_flat"))

    def process(self, tick, *, preload, broker_events, decide):
        now = tick.assumed_utc_epoch_ms
        if self.last_ms is not None and now < self.last_ms:
            raise ValueError("lifecycle clock reversed")
        self.last_ms = now
        active = now < self.boundary.removed_ms or now >= self.boundary.ready_ms
        if now >= self.boundary.ready_ms and not self.ready_applied:
            if self.mode == "L_RESET":
                self.engine.shock_locked = False
                self.engine.shock_lock_started_ms = None
                self.engine.shock_normal_bars = 0
                self.engine.last_full_settlement_ms = None
            self.ready_applied = True
            self.events.append({"event": "ready_applied", "mode": self.mode,
                                "effective_ms": now,
                                "source_row_sequence": tick.source_row_sequence})
        # Deterministic chosen artificial priority: boundary -> preload ->
        # broker events -> strategy. Existing broker orders remain independent
        # of whether the EA is attached. Broker callback must not submit orders.
        preload(tick)
        self._broker_phase(tick, broker_events, active)
        if active:
            self._ready_followups(tick)
            decide(tick)
        return active
