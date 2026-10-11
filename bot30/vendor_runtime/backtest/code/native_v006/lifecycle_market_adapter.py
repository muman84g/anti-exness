"""Connect the frozen lifecycle boundary to the frozen H2 replay on-tick path.

The adapter splits the existing B engine process into its same input-clock,
feature preload, existing-order broker, and strategy phases. It does not read
market files or synthesize quote values.
"""

from __future__ import annotations

from typing import Any

from a3_lifecycle_boundary import Boundary, LifecycleBoundary
from h2_engine import QuoteEvent, QuoteFault, ReplayEngine, fmt_price_micro


class LifecycleMarketAdapter:
    def __init__(
        self,
        engine: ReplayEngine,
        *,
        boundary: Boundary | None = None,
        mode: str = "L_CARRY",
        trace: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.engine = engine
        self.boundary = boundary
        self.mode = mode
        self.lifecycle = LifecycleBoundary(engine, boundary, mode) if boundary is not None else None
        self.trace = trace if trace is not None else []
        self.preloaded_features: dict[str, Any] | None = None
        self._new_minute = False
        self._last_preload_sequence: int | None = None
        self.terminal_kind_by_order: dict[str, str] = {}
        if self.lifecycle is not None:
            original_ready_followups = self.lifecycle._ready_followups
            original_terminal_unfilled = engine._terminal_unfilled_order

            def capture_terminal_unfilled(command, tick, *, outcome):
                # Preserve the explicit broker terminal state across A3's
                # deferred ready callback. The immutable A3 helper currently
                # drops this keyword when it invokes _retry_or_degrade.
                if self.boundary is not None and (
                    self.boundary.removed_ms <= tick.assumed_utc_epoch_ms < self.boundary.ready_ms
                ):
                    self.terminal_kind_by_order[command["order_id"]] = outcome
                return original_terminal_unfilled(command, tick, outcome=outcome)

            engine._terminal_unfilled_order = capture_terminal_unfilled

            def traced_ready_followups(tick: QuoteEvent) -> None:
                self.trace.append(("ready_followups", tick.source_row_sequence, tick.assumed_utc_epoch_ms))
                retry_method = engine._retry_or_degrade

                def retry_with_terminal(command, retry_tick, *, position, **kwargs):
                    terminal_kind = self.terminal_kind_by_order.pop(command["order_id"], None)
                    if terminal_kind is not None:
                        kwargs["terminal_kind"] = terminal_kind
                    return retry_method(command, retry_tick, position=position, **kwargs)

                engine._retry_or_degrade = retry_with_terminal
                try:
                    original_ready_followups(tick)
                finally:
                    engine._retry_or_degrade = retry_method

            # Bridge explicit broker terminal state across the read-only A3 hook.
            self.lifecycle._ready_followups = traced_ready_followups

    def _begin_source_row(self, tick: QuoteEvent) -> None:
        engine = self.engine
        if engine.last_source_row_sequence is not None and tick.source_row_sequence <= engine.last_source_row_sequence:
            raise QuoteFault("source row sequence did not increase")
        if engine.last_epoch_ms is not None:
            if tick.assumed_utc_epoch_ms < engine.last_epoch_ms:
                raise QuoteFault("source timestamp reversed; input is never sorted")
            gap = tick.assumed_utc_epoch_ms - engine.last_epoch_ms
            if gap == 0:
                engine.source_equal_timestamp_pairs += 1
            engine.max_gap_ms = max(engine.max_gap_ms, gap)
            if gap > 60_000:
                engine.long_gap_gt_60s += 1
        engine.last_epoch_ms = tick.assumed_utc_epoch_ms
        engine.last_source_row_sequence = tick.source_row_sequence
        engine.context_rows += 1
        if tick.carried_sides:
            engine.metrics["carried_quote_sides"] += len(tick.carried_sides)
            for side in tick.carried_sides:
                engine.sink.emit("quote_carry", {
                    "source_row_sequence": tick.source_row_sequence,
                    "context_row_index": tick.context_row_index,
                    "raw_time": tick.raw_time_label,
                    "raw_bid": tick.raw_bid,
                    "raw_ask": tick.raw_ask,
                    "raw_flags": tick.raw_flags,
                    "carried_side": side,
                    "effective_bid": fmt_price_micro(tick.bid_micro),
                    "effective_ask": fmt_price_micro(tick.ask_micro),
                    "carry_policy": "prior raw quote field; missing-side update bit must be unset",
                    "event_time": "",
                    "release_time": "",
                    "ingested_time": "",
                    "available_time": "",
                    "cutoff_time": "",
                })

    def _preload(self, tick: QuoteEvent, *, active: bool) -> None:
        engine = self.engine
        if self.lifecycle is not None and self.lifecycle.events:
            last = self.lifecycle.events[-1]
            if last.get("event") == "ready_applied" and last.get("source_row_sequence") == tick.source_row_sequence:
                self.trace.append(("boundary_applied", tick.source_row_sequence, tick.assumed_utc_epoch_ms))
        self.trace.append(("preload", tick.source_row_sequence, tick.assumed_utc_epoch_ms))
        self._new_minute = engine._update_bar(tick)
        if not engine.started and tick.assumed_utc_epoch_ms >= engine.config.activation_epoch_ms:
            engine.started = True
            engine.activation_source_row_sequence = tick.source_row_sequence
            engine.start_raw_time = tick.raw_time_label
            engine.shock_locked = False  # same explicitly unverified B diagnostic bootstrap
            engine.shock_lock_started_ms = None
            engine.shock_normal_bars = 0
            engine._emit_state("diagnostic_activation", tick, engine.config.activation_boundary_label, {
                "bootstrap": engine.config.start_state_assumption,
                "shock_lock_assumed_unlocked": True,
                "source_timezone": None,
                "assumed_timezone": "UTC provisional",
            })
        self.preloaded_features = engine._features()
        if not engine.started:
            return
        if self.preloaded_features is not None:
            # Data/feature cache refresh is external preload and continues while detached.
            engine.last_features = self.preloaded_features
        if self._new_minute and active:
            engine._on_completed_bar(tick, self.preloaded_features)
        if self._new_minute and self.preloaded_features is not None and active:
            features = self.preloaded_features
            engine._emit_branch(tick, "feature_snapshot", "evaluated", "new_completed_m1_boundary", features, {
                "completed_bars": features["completed_bar_count"],
                "shock_atr_short_ratio": str(features["atr_short_ratio"]),
                "shock_latest_tr_ratio": str(features["latest_tr_ratio"]),
            })
        self._last_preload_sequence = tick.source_row_sequence

    def _broker_events(self, tick: QuoteEvent) -> None:
        self.trace.append(("broker", tick.source_row_sequence, tick.assumed_utc_epoch_ms))
        if not self.engine.started:
            self.trace.append(("broker_done", tick.source_row_sequence, 0, 0))
            return
        self.engine._process_due_market_orders(tick)
        self.engine._process_pending_stop(tick)
        self.trace.append((
            "broker_done",
            tick.source_row_sequence,
            len(self.engine.sink.rows.get("fill", [])) if hasattr(self.engine.sink, "rows") else None,
            len(self.engine.inventory),
        ))

    def _decide(self, tick: QuoteEvent) -> None:
        engine = self.engine
        self.trace.append((
            "decide",
            tick.source_row_sequence,
            tick.assumed_utc_epoch_ms,
            len(engine.inventory),
            len(engine.pending_orders),
        ))
        if not engine.started:
            return
        features = self.preloaded_features
        engine._manage_initial_protection(tick)
        engine._evaluate_recovery_and_basket(tick, features)
        engine._evaluate_entries(tick, features, self._new_minute)

    def on_tick(self, tick: QuoteEvent) -> bool:
        """Run one ordered quote through the B engine and optional A3 boundary."""
        self._begin_source_row(tick)
        active = True
        if self.boundary is not None:
            active = tick.assumed_utc_epoch_ms < self.boundary.removed_ms or tick.assumed_utc_epoch_ms >= self.boundary.ready_ms

        if self.lifecycle is None:
            self._preload(tick, active=True)
            self._broker_events(tick)
            self._decide(tick)
            return True

        return self.lifecycle.process(
            tick,
            preload=lambda row: self._preload(row, active=active),
            broker_events=self._broker_events,
            decide=self._decide,
        )
