"""Portable, inactive wrapper for the frozen bot30 native approximation.

The caller supplies QuoteEvent values and an event sink. Import does not load a
market reader, connect to MT5, place orders, read settings, or start a runner.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


CORE_DIR = Path(__file__).resolve().parent / "native_v006"
CORE_HASHES = {
    "h2_engine.py": "47da2b8b6cbe3a6fc24426e9a11ca55642096e2cf1ad55744e863cf80b9b7396",
    "lifecycle_market_adapter.py": "786f6490999379d63ffd3646a65a04b15911abd136923096160c2f5a82bb4c2a",
    "a3_lifecycle_boundary.py": "cd15541aef8b459a31ef79e905c33e32907d1c60596a20e2b47397eaac83dac8",
}
VARIANTS = ("B0_NATIVE", "E1_P1_RANGE_AND", "X1_INITIAL_DECISION_REF_BE_TRAIL")
SPEC_SHA256 = "5b66597d5692413bf58cf79b0f694fd72af6f404082b8ec26299336d8183fb43"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_native_core() -> dict[str, Any]:
    """Load exact adjacent pinned copies, rejecting mixed/stale module caches."""
    names = ("h2_engine", "a3_lifecycle_boundary", "lifecycle_market_adapter")
    cached = [name for name in names if name in sys.modules]
    if cached and len(cached) != len(names):
        raise RuntimeError(f"partial frozen-core module cache rejected: {cached}")
    modules: dict[str, Any] = {}
    created: list[str] = []
    try:
        for name in names:
            filename = f"{name}.py"
            path = (CORE_DIR / filename).resolve(strict=True)
            source_bytes = path.read_bytes()
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            if source_hash != CORE_HASHES[filename]:
                raise RuntimeError(f"frozen-core hash mismatch: {filename}")
            module = sys.modules.get(name)
            if module is None:
                spec = importlib.util.spec_from_file_location(name, path)
                if spec is None or spec.loader is None:
                    raise RuntimeError(f"cannot load frozen core: {filename}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                created.append(name)
                exec(compile(source_bytes, str(path), "exec"), module.__dict__)
                module.__verified_source_sha256__ = source_hash
            actual = Path(module.__file__).resolve(strict=True)
            if os.path.normcase(str(actual)) != os.path.normcase(str(path)):
                raise RuntimeError(f"frozen-core path mismatch: {name}")
            if _sha256(actual) != CORE_HASHES[filename]:
                raise RuntimeError(f"loaded frozen-core hash mismatch: {filename}")
            if getattr(module, "__verified_source_sha256__", None) != source_hash:
                raise RuntimeError(f"cached frozen-core execution bytes unverified: {filename}")
            modules[name] = module
    except BaseException:
        for name in created:
            sys.modules.pop(name, None)
        raise
    return modules


def build_engine(sink: Any, *, variant_id: str = "B0_NATIVE", config: Any | None = None) -> Any:
    """Build one simulated native engine or one frozen single-factor variant."""
    if variant_id not in VARIANTS:
        raise ValueError(f"unsupported frozen variant: {variant_id}")
    core = load_native_core()
    h2 = core["h2_engine"]
    resolved_config = config if config is not None else h2.ReplayConfig()
    if variant_id == "B0_NATIVE":
        engine = h2.ReplayEngine(sink, config=resolved_config)
        engine.research_variant_id = variant_id
        return engine

    class ResearchEngine(h2.ReplayEngine):
        def __init__(self, event_sink: Any, engine_config: Any):
            super().__init__(event_sink, config=engine_config)
            self.research_variant_id = variant_id
            self._x1_command_reference: dict[str, dict[str, Any]] = {}
            self._x1_position_reference: dict[str, dict[str, Any]] = {}

        def _p1_spacing_ok(self, side: str, tick: Any) -> bool:
            native_spacing_ok = super()._p1_spacing_ok(side, tick)
            if variant_id != "E1_P1_RANGE_AND":
                return native_spacing_ok
            _, open_p1, _, _ = self._position_counts()
            p2_spacing, _ = self._p2_spacing_ok(side, tick)
            preserve_native_collision = (
                native_spacing_ok
                and open_p1 > self.config.p2_start_positions_strictly_greater_than
                and p2_spacing
            )
            features = self.last_features
            evidence: dict[str, Any] = {"variant_id": variant_id, "p1_only": True}
            if not isinstance(features, dict):
                self._emit_branch(tick, "p1_range_gate", "unknown_reject", "confirmed_m1_features_missing", None, evidence)
                return preserve_native_collision
            try:
                high = int(features["last_completed_bar_high_micro"])
                low = int(features["last_completed_bar_low_micro"])
                atr14 = Decimal(features["atr14_micro"])
                current_minute = tick.assumed_utc_epoch_ms // 60_000
                source_minute = int(features["last_completed_bar_minute_index"])
                if high < low or not atr14.is_finite() or atr14 <= 0:
                    raise ValueError("invalid range/ATR")
            except (KeyError, TypeError, ValueError, ArithmeticError):
                self._emit_branch(tick, "p1_range_gate", "unknown_reject", "range_or_atr14_missing_or_invalid", features, evidence)
                return preserve_native_collision
            if source_minute != current_minute - 1:
                evidence.update({
                    "source_bar_minute_index": source_minute,
                    "current_quote_minute_index": current_minute,
                    "available_bar_policy": "require_exact_previous_minute; current/stale bars reject",
                })
                self._emit_branch(tick, "p1_range_gate", "unknown_reject", "source_bar_not_exact_previous_minute", features, evidence)
                return preserve_native_collision
            range_micro = high - low
            # Exact integer comparison for R >= 0.7*A14; no float rounding.
            meets_price = range_micro >= 600_000
            meets_atr = Decimal(10 * range_micro) >= Decimal(7) * atr14
            accepted = meets_price and meets_atr
            evidence.update({
                "last_completed_bar_minute_index": features.get("last_completed_bar_minute_index"),
                "range_micro": range_micro,
                "atr14_micro_model": str(atr14),
                "minimum_price_micro": 600_000,
                "minimum_atr_fraction": "0.7",
                "range_meets_price_minimum": meets_price,
                "range_meets_atr_minimum": meets_atr,
                "native_spacing_ok": native_spacing_ok,
                "atr14_mapping_and_gate_formula_are_research_assumptions": True,
            })
            self._emit_branch(
                tick,
                "p1_range_gate",
                "pass" if accepted else "reject",
                "both_saved_minimums_met" if accepted else "one_or_more_saved_minimums_not_met",
                features,
                evidence,
            )
            if not native_spacing_ok:
                return False
            # Keep the frozen native simultaneous-P1/P2 refusal intact. E1
            # cannot make the independent P2 branch win by removing P1 before
            # the native collision check.
            if preserve_native_collision:
                return True
            return accepted

        def _new_order(self, tick: Any, **kwargs: Any) -> dict[str, Any]:
            command = super()._new_order(tick, **kwargs)
            if variant_id == "X1_INITIAL_DECISION_REF_BE_TRAIL" and command.get("command_type") == "entry" and command.get("role") == "initial":
                if int(command.get("retry_count", 0)) != 0 or command.get("parent_order_id"):
                    self.execution_degraded = True
                    raise RuntimeError("X1 native retry mapping is unsupported; preserved failure required")
                order_id = str(command["order_id"])
                if order_id in self._x1_command_reference:
                    self.execution_degraded = True
                    raise RuntimeError("X1 duplicate command identity")
                side = str(command["side"])
                ref = tick.ask_micro if side == "BUY" else tick.bid_micro
                self._x1_command_reference[order_id] = {
                    "order_id": order_id,
                    "side": side,
                    "executable_side": "ASK" if side == "BUY" else "BID",
                    "command_reference_micro": ref,
                    "source_row_sequence": tick.source_row_sequence,
                    "event_time_assumed_utc_ms": tick.assumed_utc_epoch_ms,
                    "cutoff_time_model_assumed_utc_ms": tick.assumed_utc_epoch_ms,
                    "clock_provenance": "event/cutoff equal only under provisional native model; actual clocks unknown",
                }
            return command

        def _process_command_fill(self, command: dict[str, Any], tick: Any) -> None:
            before = len(self.sink.current_fills)
            order_id = str(command.get("order_id", ""))
            captured = self._x1_command_reference.get(order_id)
            super()._process_command_fill(command, tick)
            new_fills = self.sink.current_fills[before:]
            matched_initial = [row for row in new_fills if row.get("fill_kind") == "entry" and row.get("position_role") == "initial"]
            if variant_id == "X1_INITIAL_DECISION_REF_BE_TRAIL" and command.get("command_type") == "entry" and command.get("role") == "initial":
                if len(matched_initial) > 1:
                    self.execution_degraded = True
                    raise RuntimeError("X1 duplicate initial fill mapping")
                if matched_initial:
                    fill = matched_initial[0]
                    if captured is None or fill.get("side") != captured["side"] or int(fill["volume_steps"]) != int(command["initial_volume_steps"]):
                        self.execution_degraded = True
                        raise RuntimeError("X1 missing/mismatched/fullfill-only command reference mapping")
                    position_id = str(fill["position_id"])
                    if position_id in self._x1_position_reference:
                        self.execution_degraded = True
                        raise RuntimeError("X1 position reference already bound")
                    self._x1_position_reference[position_id] = captured
                    self._x1_command_reference.pop(order_id, None)
                    self._emit_state("x1_initial_reference_bound", tick, "command_time_executable_side", {
                        **captured,
                        "position_id": position_id,
                        "fill_source_row_sequence": tick.source_row_sequence,
                        "actual_fill_price_micro_preserved": int(fill["fill_price_micro"]),
                        "initial_fullfill_only": True,
                    })
                else:
                    if not any(str(item.get("order_id")) == order_id for item in self.pending_orders):
                        self._x1_command_reference.pop(order_id, None)
            removed = [position_id for position_id in list(self._x1_position_reference) if position_id not in self.inventory]
            for position_id in removed:
                self._x1_position_reference.pop(position_id, None)

        def _manage_initial_protection(self, tick: Any) -> None:
            if variant_id != "X1_INITIAL_DECISION_REF_BE_TRAIL":
                return super()._manage_initial_protection(tick)
            if self.close_batch is not None:
                return
            for position in list(self.inventory.values()):
                if position.role != "initial" or position.close_pending_order_id:
                    continue
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
                if protection_favorable >= 70 * point:
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
                            "variant_id": variant_id,
                        })
                if protection_favorable >= 150 * point:
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
                            "variant_id": variant_id,
                        })
                if position.stop_price_micro is not None:
                    hit = tick.bid_micro <= position.stop_price_micro if position.side == "BUY" else tick.ask_micro >= position.stop_price_micro
                    if hit:
                        reason = "initial_trailing_stop" if position.trailing_armed else "initial_breakeven_stop"
                        self._begin_single_close(tick, position, reason)

    return ResearchEngine(sink, resolved_config)


def build_adapter(engine: Any, *, boundary: Any | None, lifecycle_mode: str, trace: Any | None = None) -> Any:
    if lifecycle_mode not in {"L_CARRY", "L_RESET"}:
        raise ValueError("lifecycle mode must be explicit L_CARRY or L_RESET")
    adapter_module = load_native_core()["lifecycle_market_adapter"]
    return adapter_module.LifecycleMarketAdapter(engine, boundary=boundary, mode=lifecycle_mode, trace=trace)
