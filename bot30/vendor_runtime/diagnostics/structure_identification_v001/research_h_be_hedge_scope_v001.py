from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path
from typing import Any


BASE_COMPONENT_SHA256 = "713e3478d76874b64989362c260159c67b6a04723fba7635e26e984a295cd166"
EXPECTED_X1_WRAPPER_SHA256 = "16c0c2b368c9864f7c06338c6db02d6f10368557dc177193d8e2a557d374b475"
EXPECTED_NATIVE_HASHES = {
    "h2_engine": "47da2b8b6cbe3a6fc24426e9a11ca55642096e2cf1ad55744e863cf80b9b7396",
    "lifecycle_market_adapter": "786f6490999379d63ffd3646a65a04b15911abd136923096160c2f5a82bb4c2a",
    "a3_lifecycle_boundary": "cd15541aef8b459a31ef79e905c33e32907d1c60596a20e2b47397eaac83dac8",
}
X1_VARIANT_ID = "X1_INITIAL_DECISION_REF_BE_TRAIL"
HEDGE_SCOPE_X1_VARIANT_ID = X1_VARIANT_ID
BASE_VARIANT_ID = "X1_P2_BREAKOUT_COUNT_ONLY_V001"
HEDGE_SCOPE_VARIANT_ID = "H_BE_HEDGE_SCOPE01_V001"
TRACE_LIMIT = 8192


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_pinned_count_component() -> tuple[Any, str]:
    if sys.dont_write_bytecode is not True:
        raise RuntimeError("synthetic code load requires sys.dont_write_bytecode=True")
    path = (Path(__file__).resolve().parents[1] / "mechanism_selection_v001" / "research_p2_count_v001.py").resolve(strict=True)
    raw = path.read_bytes()
    digest = _sha256_bytes(raw)
    if digest != BASE_COMPONENT_SHA256:
        raise RuntimeError("pinned P2 count component hash mismatch")
    name = "_task_h_be_hedge_scope_p2_count_v001"
    cached = sys.modules.get(name)
    if cached is not None:
        if getattr(cached, "__verified_source_sha256__", None) != digest:
            raise RuntimeError("unverified pinned component cache rejected")
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
    return any(
        getattr(position, "role", None) == "hedge"
        and type(getattr(position, "volume_steps", None)) is int
        and position.volume_steps > 0
        for position in engine.inventory.values()
    )


def _record_hedge_scope_trace(engine: Any, tick: Any, position: Any, hedge_active: bool) -> None:
    engine._hedge_scope_calls += 1
    item = {
        "source_row_sequence": getattr(tick, "source_row_sequence", None),
        "raw_time": getattr(tick, "raw_time_label", None),
        "initial_position_id": str(position.position_id),
        "positive_live_hedge": hedge_active,
        "initial_stop_before_micro": position.stop_price_micro,
        "be_armed_before": bool(position.be_armed),
        "trailing_armed_before": bool(position.trailing_armed),
    }
    if len(engine._hedge_scope_trace) < engine._hedge_scope_trace_limit:
        engine._hedge_scope_trace.append(item)
    else:
        engine._hedge_scope_trace_dropped += 1


def manage_initial_protection_hedge_scope(self: Any, tick: Any) -> None:
    """Pinned X1 initial protection with only the two new-arm gates added."""
    if self.close_batch is not None:
        return
    hedge_active = has_positive_live_hedge(self)
    for position in list(self.inventory.values()):
        if position.role != "initial" or position.close_pending_order_id:
            continue
        _record_hedge_scope_trace(self, tick, position, hedge_active)
        reference = self._x1_position_reference.get(position.position_id)
        if reference is None or reference["side"] != position.side:
            self.execution_degraded = True
            raise RuntimeError("X1 active initial position lacks matching command reference")
        # TP and all accounting stay fill-anchored and native.
        fill_favorable = (
            tick.bid_micro - position.entry_price_micro
            if position.side == "BUY"
            else position.entry_price_micro - tick.ask_micro
        )
        ref_micro = int(reference["command_reference_micro"])
        protection_favorable = (
            tick.bid_micro - ref_micro
            if position.side == "BUY"
            else ref_micro - tick.ask_micro
        )
        exit_quote = tick.bid_micro if position.side == "BUY" else tick.ask_micro
        if position.side == "BUY":
            position.best_favorable_micro = max(position.best_favorable_micro, exit_quote)
        else:
            position.best_favorable_micro = min(position.best_favorable_micro, exit_quote)
        point = self.config.point_micro
        if fill_favorable >= 1_000 * point:
            self._begin_single_close(tick, position, "initial_take_profit_1000_points")
            continue
        if not hedge_active and protection_favorable >= 70 * point:
            be_stop = ref_micro + 30 * point if position.side == "BUY" else ref_micro - 30 * point
            improves = position.stop_price_micro is None or (
                be_stop > position.stop_price_micro if position.side == "BUY" else be_stop < position.stop_price_micro
            )
            if improves:
                position.stop_price_micro = be_stop
                position.be_armed = True
                self._emit_state("breakeven_stop_armed", tick, "70_30_FROM_INITIAL_COMMAND_REFERENCE", {
                    "position_id": position.position_id,
                    "stop_price_micro": be_stop,
                    "decision_reference_micro": ref_micro,
                    "actual_fill_entry_price_micro_unchanged": position.entry_price_micro,
                    "variant_id": X1_VARIANT_ID,
                })
        if not hedge_active and protection_favorable >= 150 * point:
            # Best favorable remains native fill-initialized and only
            # receives executable exit quotes after the confirmed fill.
            trailing_stop = (
                position.best_favorable_micro - 50 * point
                if position.side == "BUY"
                else position.best_favorable_micro + 50 * point
            )
            improves_by_step = position.stop_price_micro is None or (
                trailing_stop - position.stop_price_micro >= 10 * point
                if position.side == "BUY"
                else position.stop_price_micro - trailing_stop >= 10 * point
            )
            if improves_by_step:
                position.stop_price_micro = trailing_stop
                position.trailing_armed = True
                self._emit_state("trailing_stop_armed", tick, "150_50_10_FROM_POSTFILL_NATIVE_EXTREME", {
                    "position_id": position.position_id,
                    "stop_price_micro": trailing_stop,
                    "decision_reference_micro": ref_micro,
                    "best_favorable_micro": position.best_favorable_micro,
                    "actual_fill_entry_price_micro_unchanged": position.entry_price_micro,
                    "variant_id": X1_VARIANT_ID,
                })
        if position.stop_price_micro is not None:
            hit = tick.bid_micro <= position.stop_price_micro if position.side == "BUY" else tick.ask_micro >= position.stop_price_micro
            if hit:
                reason = "initial_trailing_stop" if position.trailing_armed else "initial_breakeven_stop"
                self._begin_single_close(tick, position, reason)


def build_x1_hedge_scope_engine(
    sink: Any,
    *,
    wrapper: Any,
    config: Any | None = None,
    trace_limit: int = TRACE_LIMIT,
) -> Any:
    if type(trace_limit) is not int or trace_limit <= 0:
        raise ValueError("trace_limit must be a positive integer")
    base_component, _ = load_pinned_count_component()
    engine = base_component.build_x1_count_only_engine(
        sink,
        wrapper=wrapper,
        config=config,
        trace_limit=trace_limit,
    )
    if engine.research_variant_id != BASE_VARIANT_ID:
        raise RuntimeError("pinned P2 count baseline variant mismatch")
    engine._hedge_scope_installed = True
    engine._hedge_scope_trace_limit = trace_limit
    engine._hedge_scope_trace = []
    engine._hedge_scope_trace_dropped = 0
    engine._hedge_scope_calls = 0
    engine._manage_initial_protection = types.MethodType(manage_initial_protection_hedge_scope, engine)
    engine.research_variant_id = HEDGE_SCOPE_VARIANT_ID
    return engine


def drain_hedge_scope_trace(engine: Any) -> dict[str, Any]:
    rows = list(engine._hedge_scope_trace)
    engine._hedge_scope_trace.clear()
    return {
        "rows": rows,
        "calls_total": engine._hedge_scope_calls,
        "dropped_total": engine._hedge_scope_trace_dropped,
        "retained_after_drain": len(engine._hedge_scope_trace),
    }
