from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path
from typing import Any


BASELINE_COMPONENT_SHA256 = "fa6e78a907cbbcee766f993f25dd8c24df02d503517abf9ca30a5f3d6cb84981"
BASELINE_VARIANT_ID = "H_BE_HEDGE_SCOPE01_V001"
PRIORITY_VARIANT_ID = "H_BE_P2_PRIORITY01_V001"
HEDGE_SCOPE_VARIANT_ID = BASELINE_VARIANT_ID
TRACE_LIMIT = 8192


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_pinned_hbe_component() -> tuple[Any, str]:
    if sys.dont_write_bytecode is not True:
        raise RuntimeError("synthetic code load requires sys.dont_write_bytecode=True")
    path = (Path(__file__).resolve().parents[1] / "structure_identification_v001" / "research_h_be_hedge_scope_v001.py").resolve(strict=True)
    raw = path.read_bytes()
    digest = _sha256_bytes(raw)
    if digest != BASELINE_COMPONENT_SHA256:
        raise RuntimeError("pinned H_BE baseline component hash mismatch")
    name = "_task_hbe_p2_priority_baseline_v001"
    cached = sys.modules.get(name)
    if cached is not None:
        if getattr(cached, "__verified_source_sha256__", None) != digest:
            raise RuntimeError("unverified pinned H_BE component cache rejected")
        return cached, digest
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    try:
        exec(compile(raw, str(path), "exec"), module.__dict__)
        module.__verified_source_sha256__ = digest
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module, digest


def has_positive_live_hedge(engine: Any) -> bool:
    baseline, _ = load_pinned_hbe_component()
    return baseline.has_positive_live_hedge(engine)


def manage_initial_protection_hedge_scope(self: Any, tick: Any) -> None:
    baseline, _ = load_pinned_hbe_component()
    return baseline.manage_initial_protection_hedge_scope(self, tick)


def drain_hedge_scope_trace(engine: Any) -> dict[str, Any]:
    baseline, _ = load_pinned_hbe_component()
    return baseline.drain_hedge_scope_trace(engine)


def evaluate_entries_p2_priority(self: Any, tick: Any, features: dict[str, Any] | None, new_minute: bool) -> None:
    """Pinned native admission flow with only the triple-true collision resolved to P2."""
    if features is None:
        if new_minute:
            self._emit_branch(tick, "features", "no_order", "insufficient_m1_warmup")
        return
    self.last_features = features
    if self.execution_degraded:
        if new_minute:
            self._emit_branch(tick, "admission", "no_order", "execution_degraded_fail_closed", features)
        return
    if self.close_batch is not None or any(p.close_pending_order_id for p in self.inventory.values()):
        if new_minute:
            self._emit_branch(tick, "admission", "no_order", "close_batch_or_ticket_close_pending", features)
        return
    if self.pending_orders:
        if new_minute:
            self._emit_branch(tick, "admission", "no_order", "market_command_pending_duplicate_suppression", features)
        return
    if not self.inventory:
        if self._cooldown_active(tick):
            if new_minute:
                self._emit_branch(tick, "initial", "no_order", "15_min_full_settlement_cooldown", features)
            return
        if self._reserved_position_slots() >= self.config.max_positions:
            if new_minute:
                self._emit_branch(tick, "admission", "no_order", "max_positions_reserved_slot_cap", features, {
                    "reserved_position_slots": self._reserved_position_slots(),
                    "max_positions": self.config.max_positions,
                })
            return
        side, reason, details = self._initial_signal(tick, features)
        if side is None:
            if new_minute or reason == "both_edges_true_ambiguous":
                self._emit_branch(tick, "initial", "no_order", reason, features, details)
            return
        self._emit_branch(tick, "initial", "submit_market_entry", reason, features, details)
        command = self._new_order(tick, command_type="entry", side=side, role="initial", volume_steps=self.config.initial_lot_steps, reason="initial_" + reason)
        self._queue_market_order(tick, command)
        return

    breakout_side, breakout_reason = self._breakout_side(tick, features)
    p1_candidate = breakout_side is not None and self._p1_spacing_ok(breakout_side, tick)
    p1_reason = breakout_reason if p1_candidate else (
        "same_side_p1_spacing_below_400_points" if breakout_side is not None else "no_strict_breakout"
    )
    _, open_p1, _, _ = self._position_counts()
    p2_side = breakout_side
    p2_spacing, p2_spacing_reason = self._p2_spacing_ok(p2_side, tick) if p2_side else (False, "no_breakout_side")
    p2_candidate = p2_side is not None and open_p1 > self.config.p2_start_positions_strictly_greater_than and p2_spacing
    if p2_side and open_p1 <= self.config.p2_start_positions_strictly_greater_than and p1_candidate:
        p2_gate_reason = "P1_open_count_must_be_strictly_greater_than_2"
    else:
        p2_gate_reason = p2_spacing_reason
    if (p1_candidate or p2_candidate) and self._reserved_position_slots() >= self.config.max_positions:
        self._emit_branch(tick, "admission", "no_order", "max_positions_reserved_slot_cap", features, {
            "reserved_position_slots": self._reserved_position_slots(),
            "max_positions": self.config.max_positions,
            "p1_candidate": p1_candidate,
            "p2_candidate": p2_candidate,
        })
        return
    if p1_candidate and p2_candidate:
        p1_candidate = False
        self._emit_branch(tick, "add_priority", "select_p2", "simultaneous_P1_P2_P2_priority_research", features, {
            "side": breakout_side,
            "p1_reason": p1_reason,
            "p2_reason": p2_gate_reason,
            "open_p1_count": open_p1,
        })
    if p2_candidate and not self._has_pending_entry("p2", p2_side):
        self._emit_branch(tick, "p2_add", "submit_market_entry", "P1_count_gt_2_and_400_point_same_side_spacing", features, {
            "side": p2_side,
            "open_p1_count": open_p1,
            "spacing_reason": p2_gate_reason,
        })
        command = self._new_order(tick, command_type="entry", side=p2_side, role="p2", volume_steps=self.config.p2_lot_steps, reason="p2_same_side_spacing")
        self._queue_market_order(tick, command)
        return
    if p1_candidate and not self._has_pending_entry("p1_breakout", breakout_side):
        self._emit_branch(tick, "p1_add", "submit_market_entry", p1_reason, features, {
            "side": breakout_side,
            "spacing_points": self.config.p1_breakout_spacing_points,
            "open_p1_count": open_p1,
        })
        command = self._new_order(tick, command_type="entry", side=breakout_side, role="p1_breakout", volume_steps=self.config.initial_lot_steps, reason="p1_strict_breakout_spacing")
        self._queue_market_order(tick, command)
        return
    if new_minute and breakout_side is not None:
        self._emit_branch(tick, "add_gate", "no_order", p2_gate_reason if open_p1 <= 2 else p1_reason, features, {
            "side": breakout_side,
            "open_p1_count": open_p1,
            "p1_spacing_ok": p1_candidate,
            "p2_spacing_ok": p2_spacing,
        })


def build_hbe_p2_priority_engine(
    sink: Any,
    *,
    wrapper: Any,
    config: Any | None = None,
    trace_limit: int = TRACE_LIMIT,
) -> Any:
    if type(trace_limit) is not int or trace_limit <= 0:
        raise ValueError("trace_limit must be a positive integer")
    baseline, _ = load_pinned_hbe_component()
    engine = baseline.build_x1_hedge_scope_engine(
        sink,
        wrapper=wrapper,
        config=config,
        trace_limit=trace_limit,
    )
    if engine.research_variant_id != BASELINE_VARIANT_ID:
        raise RuntimeError("pinned H_BE baseline variant mismatch")
    engine._evaluate_entries = types.MethodType(evaluate_entries_p2_priority, engine)
    engine.research_variant_id = PRIORITY_VARIANT_ID
    return engine
