"""Bounded R3 no-reset adapter for the private paired PRIORITY runner."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from types import MethodType
from typing import Any, Callable


MAX_OWNED_POSITIONS = 500


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=str,
    ).encode("utf-8")


def _positive_native_steps(value: Any) -> bool:
    return type(value) is int and value > 0


def _identity_matches(position: Any, ref: dict[str, Any], *, mapped_position_id: str) -> bool:
    if position is None or mapped_position_id != ref["position_id"]:
        return False
    if getattr(position, "role", None) != "p2" or getattr(position, "side", None) != ref["side"]:
        return False
    # Native Position carries stable broker/parent identity. ModelLedger's
    # LedgerPosition intentionally carries only side, role, and volume; its
    # dictionary key supplies the position ID and consume parity binds its
    # accounting state to the native fill stream.
    for field, expected in (
        ("position_id", ref["position_id"]),
        ("broker_order_id", ref["broker_order_id"]),
        ("strategy_parent_id", ref["strategy_parent_id"]),
    ):
        if hasattr(position, field) and getattr(position, field) != expected:
            return False
    return True


class R3Memory:
    """Keep one qualifying OUT witness and bounded live P2 opening references."""

    def __init__(
        self, helper: Any, namespace: str, start_epoch_ms: int,
        record_sink: Callable[[bytes], None],
    ) -> None:
        self.helper = helper
        self.namespace = namespace
        self.start_epoch_ms = start_epoch_ms
        self.fresh_complete = False
        self.prefix_started = False
        self.prefix_invalid = False
        self.record_sink = record_sink
        self.qualifying_out_witness: dict[str, Any] | None = None
        self.owned_p2_openings: dict[str, dict[str, Any]] = {}
        self.qualifying_out_count = 0
        self.qualifying_out_hash = hashlib.sha256()
        self.decision_count = 0
        self.decision_hash = hashlib.sha256()
        self.rejected = Counter()
        self.active_ref_peak = 0
        self.last_source_row_sequence: int | None = None
        self.last_quote_epoch_ms: int | None = None

    def _reject(self, reason: str) -> None:
        self.rejected[reason] += 1
        self.prefix_invalid = True
        self.fresh_complete = False

    def observed(self, cutoff_ms: int) -> bool | None:
        witness = self.qualifying_out_witness
        if witness is not None:
            event_ms = witness.get("event_assumed_ms")
            available_ms = witness.get("model_available_ms")
            if type(event_ms) is int and type(available_ms) is int:
                if event_ms < cutoff_ms and available_ms <= cutoff_ms:
                    return True
                if event_ms >= cutoff_ms:
                    return None
        if self.fresh_complete and witness is None:
            return False
        return None

    def _emit(self, record_type: str, record: dict[str, Any]) -> None:
        self.record_sink(_canonical_bytes({"record_type": record_type, **record}) + b"\n")

    def _check_prior_refs(
        self,
        pre_engine_positions: dict[str, Any],
        pre_ledger_positions: dict[str, Any],
    ) -> None:
        for position_id, ref in self.owned_p2_openings.items():
            engine_position = pre_engine_positions.get(position_id)
            ledger_position = pre_ledger_positions.get(position_id)
            if (
                not _identity_matches(engine_position, ref, mapped_position_id=position_id)
                or not _identity_matches(ledger_position, ref, mapped_position_id=position_id)
                or engine_position.volume_steps != ref["live_volume_steps"]
                or ledger_position.volume_steps != ref["live_volume_steps"]
            ):
                self._reject("prior_owned_position_identity_or_volume_mismatch")
                raise RuntimeError("tracked live P2 reference disagrees with pre-consume native/ledger state")

    def _entry_fill(self, fill: dict[str, Any]) -> None:
        if fill.get("position_role") != "p2":
            return
        position_id = fill.get("position_id")
        broker_id = fill.get("broker_order_id")
        parent_id = fill.get("strategy_parent_id")
        side = fill.get("side")
        steps = fill.get("volume_steps")
        before = fill.get("position_volume_before_steps")
        after = fill.get("position_volume_after_steps")
        if (
            not isinstance(position_id, str) or not position_id
            or not isinstance(broker_id, str) or not broker_id
            or not isinstance(parent_id, str) or not parent_id
            or side not in {"BUY", "SELL"}
            or not _positive_native_steps(steps)
            or type(before) is not int or type(after) is not int
            or after - before != steps
        ):
            self._reject("p2_entry_fill_identity_or_volume_unknown")
            return
        ref = self.owned_p2_openings.get(position_id)
        if ref is None:
            if before != 0 or after != steps:
                self._reject("untracked_p2_entry_did_not_open_from_zero")
                return
            if len(self.owned_p2_openings) >= MAX_OWNED_POSITIONS:
                raise RuntimeError("active P2 opening reference bound exceeded")
            ref = {
                "namespace": self.namespace,
                "position_id": position_id,
                "broker_order_id": broker_id,
                "strategy_parent_id": parent_id,
                "side": side,
                "role": "p2",
                "opening_order_id": fill.get("order_id", ""),
                "opening_source_row_sequence": fill.get("source_row_sequence"),
                "live_volume_steps": after,
            }
            if not isinstance(ref["opening_order_id"], str) or not ref["opening_order_id"]:
                self._reject("p2_opening_order_id_unknown")
                return
            self.owned_p2_openings[position_id] = ref
            self.active_ref_peak = max(self.active_ref_peak, len(self.owned_p2_openings))
            return
        if (
            ref["namespace"] != self.namespace
            or broker_id != ref["broker_order_id"]
            or parent_id != ref["strategy_parent_id"]
            or side != ref["side"]
            or before != ref["live_volume_steps"]
        ):
            self._reject("p2_partial_entry_identity_or_volume_transition_mismatch")
            return
        ref["live_volume_steps"] = after

    def _exit_fill(
        self,
        fill: dict[str, Any],
        *,
        tick: Any,
        pre_engine_positions: dict[str, Any],
        pre_ledger_positions: dict[str, Any],
    ) -> None:
        if fill.get("position_role") != "p2":
            return
        position_id = fill.get("position_id")
        ref = self.owned_p2_openings.get(position_id) if isinstance(position_id, str) else None
        steps = fill.get("volume_steps")
        before = fill.get("position_volume_before_steps")
        after = fill.get("position_volume_after_steps")
        event_ms = fill.get("assumed_utc_epoch_ms_provisional")
        if ref is None:
            self._reject("p2_exit_without_direct_opening_reference")
            return
        if (
            ref["namespace"] != self.namespace
            or position_id != ref["position_id"]
            or fill.get("side") != ("SELL" if ref["side"] == "BUY" else "BUY")
            or not isinstance(fill.get("order_id"), str) or not fill.get("order_id")
            or not isinstance(fill.get("broker_order_id"), str) or not fill.get("broker_order_id")
            or not isinstance(fill.get("strategy_parent_id"), str) or not fill.get("strategy_parent_id")
            or not _positive_native_steps(steps)
            or type(before) is not int or type(after) is not int
            or before != ref["live_volume_steps"]
            or before - after != steps
            or after < 0
            or type(event_ms) is not int
            or event_ms != tick.assumed_utc_epoch_ms
        ):
            self._reject("p2_exit_identity_or_ordered_volume_transition_mismatch")
            return
        opened_this_consume = ref.get("opening_source_row_sequence") == tick.source_row_sequence
        if not opened_this_consume and position_id not in pre_engine_positions:
            self._reject("p2_exit_position_not_present_in_preconsume_state")
            return
        if not opened_this_consume and position_id not in pre_ledger_positions:
            self._reject("p2_exit_position_not_present_in_preconsume_ledger")
            return
        event = {
            "namespace": self.namespace,
            "position_id": position_id,
            "opening_order_id": ref["opening_order_id"],
            "broker_order_id": ref["broker_order_id"],
            "strategy_parent_id": ref["strategy_parent_id"],
            "side": ref["side"],
            "exit_order_id": fill.get("order_id", ""),
            "exit_broker_order_id": fill.get("broker_order_id", ""),
            "exit_strategy_parent_id": fill.get("strategy_parent_id", ""),
            "fill_sequence": fill.get("fill_sequence"),
            "source_row_sequence": fill.get("source_row_sequence"),
            "event_assumed_ms": event_ms,
            "model_available_ms": tick.assumed_utc_epoch_ms,
            "volume_steps": steps,
            "actual_five_clocks": {key: None for key in (
                "event_time", "release_time", "ingested_time", "available_time", "cutoff_time"
            )},
            "actual_five_clocks_status": "UNKNOWN_NOT_EMITTED_BY_NATIVE_FILL",
            "pnl_used": False,
        }
        self.qualifying_out_count += 1
        self.qualifying_out_hash.update(_canonical_bytes(event) + b"\n")
        if self.qualifying_out_witness is None:
            event["witness_id"] = hashlib.sha256(_canonical_bytes(event)).hexdigest()
            self.qualifying_out_witness = event
        self._emit("confirmed_p2_out", event)
        ref["live_volume_steps"] = after
        if after == 0:
            self.owned_p2_openings.pop(position_id, None)

    def after_confirmed_consume(
        self,
        harness: Any,
        tick: Any,
        pre_engine_positions: dict[str, Any],
        pre_ledger_positions: dict[str, Any],
    ) -> None:
        self._check_prior_refs(pre_engine_positions, pre_ledger_positions)
        source_sequence = tick.source_row_sequence
        quote_epoch_ms = tick.assumed_utc_epoch_ms
        if type(source_sequence) is not int or type(quote_epoch_ms) is not int:
            self._reject("source_sequence_or_quote_clock_unknown")
            raise RuntimeError("R3 prefix identity requires integer source sequence and quote clock")
        if not self.prefix_started:
            if quote_epoch_ms < self.start_epoch_ms:
                self._reject("first_processed_quote_precedes_fixed_window_start")
                raise RuntimeError("R3 fresh prefix began before the fixed window start")
            self.prefix_started = True
            self.fresh_complete = not self.prefix_invalid
        elif (
            self.last_source_row_sequence is None
            or source_sequence != self.last_source_row_sequence + 1
            or self.last_quote_epoch_ms is None
            or quote_epoch_ms < self.last_quote_epoch_ms
        ):
            self._reject("source_sequence_or_time_not_monotonic_contiguous")
            raise RuntimeError("R3 source prefix is not monotonic and contiguous")
        self.last_source_row_sequence = source_sequence
        self.last_quote_epoch_ms = quote_epoch_ms
        current_fills = list(harness.event_sink.current_fills)
        previous_fill_sequence: int | None = None
        for fill in current_fills:
            sequence = fill.get("fill_sequence")
            if type(sequence) is not int or (
                previous_fill_sequence is not None and sequence <= previous_fill_sequence
            ):
                self._reject("native_fill_order_unknown_or_nonincreasing")
                raise RuntimeError("native fill sequence is not strictly increasing within consume")
            previous_fill_sequence = sequence
            if fill.get("fill_kind") == "entry":
                self._entry_fill(fill)
            elif fill.get("fill_kind") == "exit":
                self._exit_fill(
                    fill, tick=tick, pre_engine_positions=pre_engine_positions,
                    pre_ledger_positions=pre_ledger_positions,
                )
        for position_id, ref in self.owned_p2_openings.items():
            position = harness.engine.inventory.get(position_id)
            ledger_position = harness.ledger.positions.get(position_id)
            if position is None or ledger_position is None:
                self._reject("postconsume_live_p2_position_missing_after_fill_walk")
                raise RuntimeError("ordered fill walk leaves a live P2 reference absent from final state")
            if (
                not _identity_matches(position, ref, mapped_position_id=position_id)
                or not _identity_matches(ledger_position, ref, mapped_position_id=position_id)
                or position.volume_steps != ref["live_volume_steps"]
                or ledger_position.volume_steps != ref["live_volume_steps"]
            ):
                self._reject("postconsume_native_ledger_p2_state_mismatch")
                raise RuntimeError("ordered P2 fill walk disagrees with post-consume native/ledger state")
        reserved_slots = int(harness.engine._reserved_position_slots())
        if reserved_slots > MAX_OWNED_POSITIONS or len(self.owned_p2_openings) > reserved_slots:
            self._reject("active_reference_or_native_reserved_slot_bound_mismatch")
            raise RuntimeError("live P2 reference set exceeds native position/reserved-slot bounds")

    def install_first_p2_wrapper(self, engine: Any) -> None:
        original_new_order = engine._new_order
        memory = self

        def new_order(this: Any, tick: Any, **kwargs: Any) -> dict[str, Any]:
            first_p2_entry = (
                kwargs.get("command_type") == "entry"
                and kwargs.get("role") == "p2"
                and kwargs.get("retry_count", 0) == 0
                and not kwargs.get("parent_order_id", "")
            )
            if not first_p2_entry:
                return original_new_order(tick, **kwargs)
            if kwargs.get("target_position_id", "") or kwargs.get("batch_id", ""):
                raise RuntimeError("initial P2 order unexpectedly carries close/batch identity")
            supplied = kwargs.get("volume_steps")
            observed = memory.observed(tick.assumed_utc_epoch_ms)
            decision = memory.helper.rule_r3_prior_observed_p2_out(observed)
            kwargs["volume_steps"] = decision.selected_steps
            decision_row = {
                "namespace": memory.namespace,
                "source_row_sequence": tick.source_row_sequence,
                "decision_ms": tick.assumed_utc_epoch_ms,
                "observed_prior_positive_p2_out": observed,
                "witness_id": memory.qualifying_out_witness.get("witness_id")
                if memory.qualifying_out_witness else None,
                "selected_steps": decision.selected_steps,
                "possible_steps": list(decision.possible_steps),
                "fallback_used": decision.fallback_used,
                "reason": decision.reason,
                "native_requested_before_private_binding": supplied,
                "retry_count": kwargs.get("retry_count", 0),
                "parent_order_id": kwargs.get("parent_order_id", ""),
            }
            command = original_new_order(tick, **kwargs)
            decision_row["native_created_initial_volume_steps"] = command["initial_volume_steps"]
            decision_row["native_created_order_id"] = command["order_id"]
            if command["initial_volume_steps"] != decision.selected_steps:
                raise RuntimeError("native initial P2 volume differs from the R3 helper decision")
            memory.decision_count += 1
            memory.decision_hash.update(_canonical_bytes(decision_row) + b"\n")
            memory._emit("initial_p2_quantity_decision", decision_row)
            return command

        engine._new_order = MethodType(new_order, engine)

    def wrap_harness(self, harness: Any) -> None:
        original_consume = harness.consume
        memory = self

        def consume_after_parity(this: Any, tick: Any, *, after_adapter: Any | None = None) -> None:
            pre_engine = {key: copy.copy(value) for key, value in this.engine.inventory.items()}
            pre_ledger = {key: copy.copy(value) for key, value in this.ledger.positions.items()}
            original_consume(tick, after_adapter=after_adapter)
            memory.after_confirmed_consume(this, tick, pre_engine, pre_ledger)

        harness.consume = MethodType(consume_after_parity, harness)

    def summary(self) -> dict[str, Any]:
        return {
            "status": "R3_STREAMED_MEMORY_SUMMARY",
            "namespace": self.namespace,
            "fresh_complete": self.fresh_complete,
            "qualifying_out_count": self.qualifying_out_count,
            "qualifying_out_sha256": self.qualifying_out_hash.hexdigest(),
            "qualifying_out_witness": self.qualifying_out_witness,
            "decision_count": self.decision_count,
            "decision_sha256": self.decision_hash.hexdigest(),
            "rejected_or_unknown_counts": dict(sorted(self.rejected.items())),
            "active_owned_p2_reference_count": len(self.owned_p2_openings),
            "active_owned_p2_reference_peak": self.active_ref_peak,
            "active_reference_limit": MAX_OWNED_POSITIONS,
            "actual_five_clocks_status": "UNKNOWN_NOT_EMITTED_BY_NATIVE_FILL",
            "restart_restore_status": "UNSUPPORTED_NOT_TESTED",
        }
