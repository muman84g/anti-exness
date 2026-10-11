"""Single-row native/ledger/R3 stream harness for the portable control book.

This intentionally does not contain a feed reader, cutoff scheduler, or market
loop.  It preserves the accepted ReplayHarness consume ordering while allowing
the caller to own feed clocks, activation, and resource observation.
"""
from __future__ import annotations

import hashlib
import json
import math
import types
from collections import Counter, deque
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum
from types import MethodType
from typing import Any, Mapping


MAX_DIAGNOSTIC_ROWS = 4096
MAX_DIAGNOSTIC_BYTES = 32 * 1024 * 1024
ADAPTER_TRACE_LIMIT = 64
MAX_ORDER_DEADLINES = 100_000
CLOCK_FIELDS = frozenset({
    "schema", "event_time_ms", "release_time_ms", "release_time_upper_bound_ms",
    "release_time_status", "broker_original_release_time_ms", "ingested_time_ms",
    "available_time_ms", "cutoff_time_ms", "source_provenance", "binding_id",
    "binding_sha256", "time_basis", "timezone", "dst_conversion", "clock_evidence_refs",
})


class StreamHarnessError(RuntimeError):
    """A stream row was rejected or the harness entered a terminal state."""


def _typed_value(value: Any, *, allowed_modules: frozenset[str]) -> Any:
    """Project only explicit primitive/container/native-state types for hashing."""
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is Decimal:
        if not value.is_finite():
            raise StreamHarnessError("non-finite Decimal in native state")
        return {"$decimal": str(value)}
    if type(value) is float:
        if not math.isfinite(value):
            raise StreamHarnessError("non-finite float in native state")
        return {"$float_hex": value.hex()}
    if isinstance(value, Enum):
        enum_type = type(value)
        if enum_type.__module__ not in allowed_modules:
            raise StreamHarnessError("unapproved enum in native state")
        return {
            "$enum": f"{enum_type.__module__}.{enum_type.__qualname__}",
            "value": _typed_value(value.value, allowed_modules=allowed_modules),
        }
    if type(value) is dict or isinstance(value, Counter):
        result: dict[str, Any] = {}
        for key in sorted(value):
            if type(key) is not str:
                raise StreamHarnessError("native state mapping key is not an exact string")
            result[key] = _typed_value(value[key], allowed_modules=allowed_modules)
        return result
    if type(value) is set or type(value) is frozenset:
        if any(type(item) is not str for item in value):
            raise StreamHarnessError("native state set contains a non-string identity")
        return sorted(value)
    if type(value) in (list, tuple, deque):
        return [_typed_value(item, allowed_modules=allowed_modules) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        value_type = type(value)
        if value_type.__module__ not in allowed_modules:
            raise StreamHarnessError("unapproved dataclass in native state")
        return {
            "$dataclass": f"{value_type.__module__}.{value_type.__qualname__}",
            "fields": {
                field.name: _typed_value(getattr(value, field.name), allowed_modules=allowed_modules)
                for field in fields(value)
            },
        }
    raise StreamHarnessError(f"unsupported native state type: {type(value).__module__}.{type(value).__qualname__}")


def _canonical_bytes(value: Any, *, allowed_modules: frozenset[str]) -> bytes:
    projected = _typed_value(value, allowed_modules=allowed_modules)
    return json.dumps(
        projected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class BoundedEventCapture:
    """Keep a bounded diagnostic tail while preserving the native sink digests."""

    def __init__(self, sink: Any, canonical_json: Any):
        if not callable(getattr(sink, "emit", None)):
            raise StreamHarnessError("event sink has no emit method")
        self.sink = sink
        self.canonical_json = canonical_json
        self.rows: deque[tuple[str, dict[str, Any]]] = deque(maxlen=MAX_DIAGNOSTIC_ROWS)
        self._row_sizes: deque[int] = deque(maxlen=MAX_DIAGNOSTIC_ROWS)
        self.rows_seen = 0
        self.bytes_seen = 0
        self.retained_bytes = 0
        self.digest = hashlib.sha256()
        self._original_emit = sink.emit
        self._installed_emit = None

        def emit(kind: str, row: dict[str, Any]) -> None:
            if type(kind) is not str or type(row) is not dict:
                raise StreamHarnessError("native event schema is not exact str/dict")
            payload = canonical_json(row)
            if type(payload) is not bytes or len(payload) > MAX_DIAGNOSTIC_BYTES:
                raise StreamHarnessError("native diagnostic record exceeds the retention byte cap")
            # Call the frozen native sink first. If it fails, do not claim capture.
            self._original_emit(kind, row)
            copied = dict(row)
            if len(self.rows) == MAX_DIAGNOSTIC_ROWS:
                self.rows.popleft()
                self.retained_bytes -= self._row_sizes.popleft()
            while self.rows and self.retained_bytes + len(payload) > MAX_DIAGNOSTIC_BYTES:
                self.rows.popleft()
                self.retained_bytes -= self._row_sizes.popleft()
            self.rows.append((kind, copied))
            self._row_sizes.append(len(payload))
            self.retained_bytes += len(payload)
            self.digest.update(payload)
            self.bytes_seen += len(payload)
            self.rows_seen += 1

        self._installed_emit = emit
        sink.emit = emit

    def restore(self) -> None:
        if self.sink.emit is not self._installed_emit:
            raise StreamHarnessError("native event capture hook identity changed")
        self.sink.emit = self._original_emit

    def summary(self) -> dict[str, Any]:
        return {
            "retained_rows": len(self.rows),
            "rows_seen": self.rows_seen,
            "retention_policy": "last_4096_diagnostic_rows",
            "bytes_seen": self.bytes_seen,
            "retained_bytes": self.retained_bytes,
            "sha256": self.digest.hexdigest(),
        }


class BoundedTradeWriter:
    """Bound model-ledger diagnostic rows; cumulative totals remain in ModelLedger."""

    def __init__(self):
        self.rows: deque[str] = deque(maxlen=MAX_DIAGNOSTIC_ROWS)
        self._row_sizes: deque[int] = deque(maxlen=MAX_DIAGNOSTIC_ROWS)
        self.rows_seen = 0
        self.bytes_seen = 0
        self.retained_bytes = 0
        self.digest = hashlib.sha256()

    def write(self, text: str) -> int:
        if type(text) is not str:
            raise StreamHarnessError("model ledger output must be exact text")
        payload = text.encode("utf-8")
        if len(payload) > MAX_DIAGNOSTIC_BYTES:
            raise StreamHarnessError("model-ledger diagnostic record exceeds the retention byte cap")
        if len(self.rows) == MAX_DIAGNOSTIC_ROWS:
            self.rows.popleft()
            self.retained_bytes -= self._row_sizes.popleft()
        while self.rows and self.retained_bytes + len(payload) > MAX_DIAGNOSTIC_BYTES:
            self.rows.popleft()
            self.retained_bytes -= self._row_sizes.popleft()
        self.rows.append(text)
        self._row_sizes.append(len(payload))
        self.rows_seen += 1
        self.bytes_seen += len(payload)
        self.retained_bytes += len(payload)
        self.digest.update(payload)
        return len(text)

    def summary(self) -> dict[str, Any]:
        return {
            "retained_rows": len(self.rows),
            "rows_seen": self.rows_seen,
            "retention_policy": "last_4096_diagnostic_rows",
            "bytes_seen": self.bytes_seen,
            "retained_bytes": self.retained_bytes,
            "sha256": self.digest.hexdigest(),
        }


class StreamHarness:
    """One accepted quote → native adapter → independent ledger/R3 consume."""

    def __init__(
        self,
        *,
        run_main: Any,
        engine: Any,
        adapter: Any,
        boundary: Any,
        event_sink: Any,
        ledger: Any,
        trade_writer: BoundedTradeWriter,
        trace_buffer: deque[Any],
        r3_memory: Any,
        clock_formatter: Any,
        allowed_state_modules: tuple[str, ...],
    ):
        if type(trace_buffer) is not deque or trace_buffer.maxlen != ADAPTER_TRACE_LIMIT:
            raise StreamHarnessError("adapter trace must be the fixed maxlen=64 deque")
        if event_sink.output_dir is not None or type(event_sink.current_fills) is not list:
            raise StreamHarnessError("portable stream harness requires an in-memory native sink")
        if ledger.trade_writer is not trade_writer:
            raise StreamHarnessError("model ledger must use the bounded trade diagnostic writer")
        self.run_main = run_main
        self.engine = engine
        self.adapter = adapter
        self.boundary = boundary
        self.event_sink = event_sink
        self.ledger = ledger
        self.trade_writer = trade_writer
        self.trace_buffer = trace_buffer
        self.r3_memory = r3_memory
        if not callable(clock_formatter):
            raise StreamHarnessError("native assumed-UTC formatter is required")
        self.clock_formatter = clock_formatter
        if (type(allowed_state_modules) is not tuple or not allowed_state_modules
                or any(type(name) is not str or not name for name in allowed_state_modules)):
            raise StreamHarnessError("full-state digest requires the exact pinned runtime module set")
        self.event_capture = BoundedEventCapture(event_sink, run_main._canonical_json)
        self._captured_emit = event_sink.emit
        self._current_clock: dict[str, Any] | None = None
        self._current_row_context: dict[str, Any] | None = None
        self._prepared_clock: dict[str, Any] | None = None
        self._market_order_deadlines: dict[str, dict[str, Any]] = {}
        self._pending_stop_deadlines: dict[str, dict[str, Any]] = {}
        self._deadline_emit = None
        self._original_pending_stop = engine._process_pending_stop
        if "_process_pending_stop" in vars(engine):
            self.event_capture.restore()
            raise StreamHarnessError("native pending-stop processor was already replaced")
        self._install_deadline_capture()
        self._install_pending_stop_gate()
        allowed_modules = {
            engine.__class__.__module__, ledger.__class__.__module__,
            adapter.__class__.__module__, r3_memory.__class__.__module__,
            boundary.__class__.__module__,
        }
        # QuoteEvent, ReplayConfig, ExecutionPlan, and native position/bar
        # dataclasses live in the verified base modules of the factory-created
        # engine, not necessarily in its dynamic ResearchEngine module.
        allowed_modules.update(allowed_state_modules)
        allowed_modules.update(cls.__module__ for cls in engine.__class__.__mro__)
        for candidate in (getattr(engine, "config", None), getattr(engine, "execution_plan", None)):
            if candidate is not None:
                allowed_modules.add(type(candidate).__module__)
        if adapter.lifecycle is not None:
            allowed_modules.add(adapter.lifecycle.__class__.__module__)
        self.allowed_state_modules = frozenset(allowed_modules)
        self.last_tick: Any | None = None
        self.last_minute: int | None = None
        self.last_sample: dict[str, Any] | None = None
        self.last_trace_rows: deque[dict[str, Any]] = deque(maxlen=ADAPTER_TRACE_LIMIT)
        self.trace_sequence = 0
        self.trace_hash = hashlib.sha256()
        self.row_envelope_hash = hashlib.sha256()
        self.state_hash = hashlib.sha256()
        self.rows_processed = 0
        self.row_event_count = 0
        self.equity_hash = hashlib.sha256()
        self.equity_samples = 0
        self._terminal_error: str | None = None
        self._closed = False
        self._last_clock: dict[str, Any] | None = None

    @staticmethod
    def _exact_ms(value: Any, name: str) -> int:
        if type(value) is not int or value <= 0:
            raise StreamHarnessError(f"{name} must be a positive exact millisecond integer")
        return value

    @classmethod
    def validate_clock_record(cls, tick: Any, clock_record: Any) -> dict[str, Any]:
        if type(clock_record) is not dict or set(clock_record) != CLOCK_FIELDS:
            raise StreamHarnessError("clock evidence does not match the exact v002 envelope")
        if clock_record.get("schema") != "bot30_sampled_clock_envelope_v002":
            raise StreamHarnessError("unsupported sampled clock envelope")
        event = cls._exact_ms(clock_record.get("event_time_ms"), "event_time_ms")
        ingested = cls._exact_ms(clock_record.get("ingested_time_ms"), "ingested_time_ms")
        available = cls._exact_ms(clock_record.get("available_time_ms"), "available_time_ms")
        cutoff = cls._exact_ms(clock_record.get("cutoff_time_ms"), "cutoff_time_ms")
        upper = cls._exact_ms(clock_record.get("release_time_upper_bound_ms"), "release_time_upper_bound_ms")
        if (clock_record.get("release_time_ms") is not None
                or clock_record.get("broker_original_release_time_ms") is not None
                or clock_record.get("release_time_status") != "consumer_observed_upper_bound"
                or upper != ingested or not event <= ingested <= available <= cutoff
                or event != getattr(tick, "assumed_utc_epoch_ms", None)):
            raise StreamHarnessError("sampled clock order/provisional release fields are inconsistent")
        if (clock_record.get("source_provenance") != "SAMPLED_CURRENT_BID_ASK_SHADOW_V001"
                or clock_record.get("binding_id") != "EXNESS_MT5_SERVER_UTC0_V001"
                or clock_record.get("time_basis") != "UTC_EPOCH_MILLISECONDS"
                or clock_record.get("timezone") != "UTC"
                or clock_record.get("dst_conversion") != "NONE_UTC_EPOCH"):
            raise StreamHarnessError("clock evidence is not the fixed sampled Exness UTC binding")
        binding_sha = clock_record.get("binding_sha256")
        refs = clock_record.get("clock_evidence_refs")
        if (type(binding_sha) is not str or len(binding_sha) != 64
                or any(c not in "0123456789abcdef" for c in binding_sha)
                or type(refs) is not list or len(refs) != 2
                or any(type(ref) is not str or not ref for ref in refs)):
            raise StreamHarnessError("clock binding proof/reference shape is invalid")
        return dict(clock_record)

    def prepare_clock_record(self, tick: Any, clock_record: Any) -> dict[str, Any]:
        """Pure row preflight; call immediately before the single consume."""
        if self._closed or self._terminal_error is not None:
            raise StreamHarnessError("stream harness cannot accept another row")
        if self._prepared_clock is not None or self._current_clock is not None:
            raise StreamHarnessError("a prior row clock is still active")
        if (type(tick.source_row_sequence) is not int or tick.source_row_sequence <= 0
                or type(tick.context_row_index) is not int or tick.context_row_index <= 0
                or type(tick.assumed_utc_epoch_ms) is not int or tick.assumed_utc_epoch_ms <= 0
                or type(tick.bid_micro) is not int or tick.bid_micro <= 0
                or type(tick.ask_micro) is not int or tick.ask_micro <= tick.bid_micro
                or type(tick.flags) is not int or type(tick.raw_bid) is not str
                or type(tick.raw_ask) is not str or type(tick.raw_time_label) is not str
                or not tick.raw_time_label.startswith("EXNESS_MT5_SERVER_UTC0_V001:")):
            raise StreamHarnessError("native quote event does not match the actual sampled quote contract")
        if tick.source_row_sequence != self.rows_processed + 1:
            raise StreamHarnessError("actual quote source sequence is not the next committed row")
        if self.last_tick is not None:
            if tick.source_row_sequence <= self.last_tick.source_row_sequence:
                raise StreamHarnessError("source sequence must increase before row processing")
            if tick.assumed_utc_epoch_ms < self.last_tick.assumed_utc_epoch_ms:
                raise StreamHarnessError("quote epoch must not move backward")
        validated = self.validate_clock_record(tick, clock_record)
        self._verify_pending_deadlines(tick)
        self._prepared_clock = {
            "source_row_sequence": tick.source_row_sequence,
            "event_time_ms": tick.assumed_utc_epoch_ms,
            "record": validated,
        }
        return validated

    def _install_deadline_capture(self) -> None:
        harness = self

        def emit(kind: str, row: dict[str, Any]) -> None:
            if kind == "order":
                harness._bind_order_deadline(row)
            harness._captured_emit(kind, row)

        self._deadline_emit = emit
        self.event_sink.emit = emit

    def _bind_order_deadline(self, command: dict[str, Any]) -> None:
        context = self._current_clock
        row_context = self._current_row_context
        if context is None or row_context is None or type(command) is not dict:
            raise StreamHarnessError("native order was emitted outside an accepted quote row")
        order_id = command.get("order_id")
        command_type = command.get("command_type")
        if type(order_id) is not str or not order_id or order_id in self._market_order_deadlines or order_id in self._pending_stop_deadlines:
            raise StreamHarnessError("native order identity is missing or was reused")
        if len(self._market_order_deadlines) + len(self._pending_stop_deadlines) >= MAX_ORDER_DEADLINES:
            raise StreamHarnessError("immutable order deadline sidecar reached its fixed cap")
        decision_sequence = command.get("decision_source_row_sequence")
        creation_sequence = row_context.get("creation_source_row_sequence")
        if (type(creation_sequence) is not int or creation_sequence <= 0
                or type(decision_sequence) is not int or decision_sequence != creation_sequence):
            raise StreamHarnessError("native order decision row differs from the accepted creation cursor")
        if command_type == "pending_stop":
            if (command.get("simulated_eligibility_basis") != "resting_stop_cross_after_ack"
                    or command.get("simulated_eligible_after_epoch_ms_assumed_utc") is not None
                    or command.get("simulated_eligible_after_raw_utc_assumption") is not None):
                raise StreamHarnessError("native pending stop has unexpected market-latency eligibility")
            self._pending_stop_deadlines[order_id] = {
                "order_id": order_id,
                "request_cutoff_time_ms": context["cutoff_time_ms"],
                "creation_source_row_sequence": creation_sequence,
                "binding_id": context["binding_id"],
                "binding_sha256": context["binding_sha256"],
                "kind": "pending_stop",
            }
            return
        if command_type not in {"entry", "exit"}:
            raise StreamHarnessError("native emitted an unsupported order type")
        if command.get("simulated_eligibility_basis") != "market_latency_after_command":
            raise StreamHarnessError("native market order has an unexpected eligibility basis")
        cutoff = context["cutoff_time_ms"]
        deadline = cutoff + 100
        if deadline > 9_223_372_036_854_775_807:
            raise StreamHarnessError("market order deadline overflows the supported epoch range")
        command["simulated_eligible_after_epoch_ms_assumed_utc"] = deadline
        command["simulated_eligible_after_raw_utc_assumption"] = self.clock_formatter(deadline)
        self._market_order_deadlines[order_id] = {
            "order_id": order_id,
            "request_cutoff_time_ms": cutoff,
            "eligible_after_epoch_ms": deadline,
            "creation_source_row_sequence": creation_sequence,
            "decision_source_row_sequence": decision_sequence,
            "binding_id": context["binding_id"],
            "binding_sha256": context["binding_sha256"],
            "command_type": command_type,
        }

    def _install_pending_stop_gate(self) -> None:
        engine = self.engine
        original = self._original_pending_stop
        harness = self

        def process_pending_stop(this: Any, tick: Any) -> Any:
            if this is not engine:
                raise StreamHarnessError("pending-stop processor was rebound to another engine")
            pending = this.pending_stop
            if pending is None:
                return original(tick)
            order_id = getattr(pending, "order_id", None)
            record = harness._pending_stop_deadlines.get(order_id) if type(order_id) is str else None
            if record is None:
                raise StreamHarnessError("native pending stop has no immutable request-clock sidecar")
            if (type(tick.source_row_sequence) is not int
                    or type(tick.assumed_utc_epoch_ms) is not int):
                raise StreamHarnessError("pending-stop quote cursor/clock is not exact")
            if (tick.source_row_sequence <= record["creation_source_row_sequence"]
                    or tick.assumed_utc_epoch_ms <= record["request_cutoff_time_ms"]):
                return None
            return original(tick)

        self._pending_stop_gate = MethodType(process_pending_stop, engine)
        engine._process_pending_stop = self._pending_stop_gate

    def _verify_pending_deadlines(self, tick: Any) -> None:
        for command in self.engine.pending_orders:
            if type(command) is not dict:
                raise StreamHarnessError("native pending market order is not an exact dict")
            if command.get("command_type") == "pending_stop":
                raise StreamHarnessError("native pending stop was inserted into the market-order queue")
            order_id = command.get("order_id")
            record = self._market_order_deadlines.get(order_id) if type(order_id) is str else None
            if record is None:
                raise StreamHarnessError("native pending market order has no immutable deadline sidecar")
            if (command.get("simulated_eligibility_basis") != "market_latency_after_command"
                    or command.get("simulated_eligible_after_epoch_ms_assumed_utc") != record["eligible_after_epoch_ms"]
                    or command.get("simulated_eligible_after_raw_utc_assumption") != self.clock_formatter(record["eligible_after_epoch_ms"])):
                raise StreamHarnessError("native pending market eligibility changed after creation")
        pending = self.engine.pending_stop
        if pending is not None:
            order_id = getattr(pending, "order_id", None)
            record = self._pending_stop_deadlines.get(order_id) if type(order_id) is str else None
            if record is None:
                raise StreamHarnessError("native pending stop has no immutable request-clock sidecar")
            if getattr(pending, "created_source_row_sequence", None) != record["creation_source_row_sequence"]:
                raise StreamHarnessError("native pending stop creation cursor differs from its sidecar")

    def _verify_market_fills(self, tick: Any) -> None:
        for fill in self.event_sink.current_fills:
            if fill.get("fill_kind") not in {"entry", "exit"}:
                continue
            order_id = fill.get("order_id")
            record = self._market_order_deadlines.get(order_id) if type(order_id) is str else None
            if record is None:
                raise StreamHarnessError("market fill has no immutable order-deadline record")
            if (tick.source_row_sequence <= record["creation_source_row_sequence"]
                    or tick.assumed_utc_epoch_ms <= record["eligible_after_epoch_ms"]
                    or fill.get("source_row_sequence") != tick.source_row_sequence
                    or fill.get("assumed_utc_epoch_ms_provisional") != tick.assumed_utc_epoch_ms):
                raise StreamHarnessError("native market fill did not follow its strict-later cutoff deadline")

    def _verify_stop_fills(self, tick: Any) -> None:
        for fill in self.event_sink.current_fills:
            if fill.get("fill_kind") != "resting_stop":
                continue
            order_id = fill.get("order_id")
            record = self._pending_stop_deadlines.get(order_id) if type(order_id) is str else None
            if record is None:
                raise StreamHarnessError("resting-stop fill has no immutable placement-clock record")
            if (tick.source_row_sequence <= record["creation_source_row_sequence"]
                    or tick.assumed_utc_epoch_ms <= record["request_cutoff_time_ms"]
                    or fill.get("source_row_sequence") != tick.source_row_sequence
                    or fill.get("assumed_utc_epoch_ms_provisional") != tick.assumed_utc_epoch_ms):
                raise StreamHarnessError("resting-stop fill did not follow its placement quote")

    def _state_roles(self) -> dict[int, str]:
        roles = {
            id(self): "stream_harness",
            id(self.run_main): "fixed_run_main_module",
            id(self.engine): "native_engine",
            id(self.adapter): "lifecycle_adapter",
            id(self.boundary): "native_boundary",
            id(self.event_sink): "native_event_sink",
            id(self.ledger): "model_ledger",
            id(self.trade_writer): "bounded_trade_writer",
            id(self.r3_memory): "r3_memory",
            id(self.r3_memory.helper): "fixed_quantity_helper",
            id(self.r3_memory.record_sink): "bounded_r3_sink",
            id(self.adapter.lifecycle): "native_lifecycle_boundary",
            id(self.event_capture): "bounded_event_capture",
        }
        return roles

    @staticmethod
    def _code_contract(function: Any) -> dict[str, Any]:
        code = getattr(function, "__code__", None)
        if type(code) is not types.CodeType:
            raise StreamHarnessError("native callable has no Python code identity")
        return {
            "module": getattr(function, "__module__", None),
            "qualname": getattr(function, "__qualname__", None),
            "code_sha256": hashlib.sha256(code.co_code).hexdigest(),
            "names": list(code.co_names),
            "freevars": list(code.co_freevars),
        }

    def _closure_role(self, value: Any, roles: dict[int, str]) -> Any:
        role = roles.get(id(value))
        if role is not None:
            return {"object_role": role}
        if type(value) in (type(None), bool, int, str, Decimal, float):
            return _typed_value(value, allowed_modules=self.allowed_state_modules)
        if type(value) is types.MethodType:
            owner = roles.get(id(value.__self__))
            if owner is None:
                raise StreamHarnessError("callable closure points to an unowned object")
            return {"bound_owner": owner, **self._code_contract(value.__func__)}
        if type(value) is types.FunctionType:
            return self._code_contract(value)
        raise StreamHarnessError("callable closure contains an unsupported mutable/object reference")

    def _callable_state(self, value: Any, roles: dict[int, str]) -> dict[str, Any]:
        if type(value) is types.MethodType:
            owner = roles.get(id(value.__self__))
            if owner is None:
                raise StreamHarnessError("native method is bound to an unowned object")
            function = value.__func__
            contract = self._code_contract(function)
            closure = []
        elif type(value) is types.FunctionType:
            owner = None
            function = value
            contract = self._code_contract(function)
            closure = []
            if function.__closure__ is not None:
                for name, cell in zip(function.__code__.co_freevars, function.__closure__):
                    try:
                        content = cell.cell_contents
                    except ValueError as exc:
                        raise StreamHarnessError("native callable has an empty closure cell") from exc
                    closure.append({"name": name, "value": self._closure_role(content, roles)})
        else:
            raise StreamHarnessError("unsupported callable stored in mutable state")
        defaults = function.__defaults__
        kwdefaults = function.__kwdefaults__
        return {
            "owner": owner,
            **contract,
            "closure": closure,
            "defaults": _typed_value(defaults, allowed_modules=self.allowed_state_modules),
            "kwdefaults": _typed_value(kwdefaults, allowed_modules=self.allowed_state_modules),
        }

    def _project_object(self, obj: Any, *, name: str, roles: dict[int, str]) -> dict[str, Any]:
        if type(obj) not in (self.engine.__class__, self.adapter.__class__, self.ledger.__class__, self.r3_memory.__class__, self.adapter.lifecycle.__class__, self.__class__):
            raise StreamHarnessError(f"unexpected mutable state object for {name}")
        result: dict[str, Any] = {}
        hash_fields = {
            "trace_hash", "row_envelope_hash", "state_hash", "equity_hash",
            "qualifying_out_hash", "decision_hash", "digest",
        }
        for field_name, value in vars(obj).items():
            role = roles.get(id(value))
            if role is not None:
                result[field_name] = {"object_role": role}
            elif field_name in hash_fields:
                hexdigest = getattr(value, "hexdigest", None)
                if not callable(hexdigest):
                    raise StreamHarnessError("named digest state has no hexdigest method")
                result[field_name] = {"sha256": hexdigest()}
            elif field_name == "execution_plan" and obj is self.engine:
                outcomes = value.outcomes
                if type(outcomes) is not dict:
                    raise StreamHarnessError("native execution outcomes have an unsupported container")
                rows = []
                for key, pending in outcomes.items():
                    if type(key) is not tuple or len(key) != 2 or any(type(part) is not str for part in key):
                        raise StreamHarnessError("native execution outcome key is not the fixed reason/role tuple")
                    rows.append({"reason": key[0], "role": key[1], "outcomes": pending})
                result[field_name] = {"outcomes": _typed_value(sorted(rows, key=lambda row: (row["reason"], row["role"])), allowed_modules=self.allowed_state_modules)}
            elif callable(value):
                result[field_name] = {"callable": self._callable_state(value, roles)}
            else:
                result[field_name] = _typed_value(value, allowed_modules=self.allowed_state_modules)
        return result

    def _native_sink_state(self) -> dict[str, Any]:
        return {
            "output_dir": None,
            "schemas": self.event_sink.schemas,
            "row_counts": self.event_sink.row_counts,
            "current_fills": self.event_sink.current_fills,
            "current_event_count": self.event_sink.current_event_count,
            "total_rows": self.event_sink.total_rows,
            "digest_summary": self.event_sink.digest_summary(),
        }

    def _full_business_state(self) -> dict[str, Any]:
        roles = self._state_roles()
        memory = self.r3_memory
        return {
            "engine": self._project_object(self.engine, name="engine", roles=roles),
            "adapter": self._project_object(self.adapter, name="adapter", roles=roles),
            "lifecycle": self._project_object(self.adapter.lifecycle, name="lifecycle", roles=roles),
            "ledger": self._project_object(self.ledger, name="ledger", roles=roles),
            "r3_memory": self._project_object(memory, name="r3_memory", roles=roles),
            "harness": self._project_object(self, name="harness", roles=roles),
            "native_event_sink": self._native_sink_state(),
            "event_capture": self.event_capture.summary(),
            "trade_writer": self.trade_writer.summary(),
            "r3_record_sink": self.r3_memory.record_sink.summary(),
            "market_order_deadlines": self._market_order_deadlines,
            "pending_stop_deadlines": self._pending_stop_deadlines,
            "last_clock": self._last_clock,
        }

    def business_digest(self) -> str:
        state_bytes = _canonical_bytes(self._full_business_state(), allowed_modules=self.allowed_state_modules)
        return hashlib.sha256(state_bytes).hexdigest()

    @property
    def terminal(self) -> bool:
        return self._terminal_error is not None

    def _sample_record(self, tick: Any) -> dict[str, Any]:
        account = self.ledger.update_tick(tick, self.engine)
        usd = self.run_main._usd
        return {
            "epoch_ms": tick.assumed_utc_epoch_ms,
            "realized_gross_usd": usd(self.ledger.realized_gross_micro),
            "realized_fee_usd": usd(self.ledger.realized_fee_micro),
            "realized_net_usd": usd(self.ledger.realized_gross_micro - self.ledger.realized_fee_micro),
            "net_equity_usd": usd(account["net_equity_micro"]),
            "open_count": account["open_count"],
            "pending_count": account["pending_count"],
            "_net_equity_micro": account["net_equity_micro"],
            "_unrealized_gross_micro": account["unrealized_gross_micro"],
            "_open_fee_reserve_micro": account["open_fee_reserve_micro"],
        }

    def _drain_trace(self, source_sequence: int) -> None:
        if len(self.trace_buffer) >= self.trace_buffer.maxlen:
            raise StreamHarnessError("adapter trace reached capacity before row drain")
        rows: deque[dict[str, Any]] = deque(maxlen=ADAPTER_TRACE_LIMIT)
        for item in self.trace_buffer:
            self.trace_sequence += 1
            row = {
                "trace_sequence": self.trace_sequence,
                "source_row_sequence": source_sequence,
                "phase": item[0] if type(item) is tuple and item and type(item[0]) is str else None,
                "event": item,
            }
            payload = _canonical_bytes(row, allowed_modules=self.allowed_state_modules)
            self.trace_hash.update(payload)
            rows.append(row)
        self.last_trace_rows = rows
        self.trace_buffer.clear()

    def consume(self, tick: Any, *, after_adapter: Any | None = None) -> None:
        if self._closed:
            raise StreamHarnessError("stream harness is closed")
        if self._terminal_error is not None:
            raise StreamHarnessError("stream harness is terminal after a prior row failure") from RuntimeError(self._terminal_error)
        prepared = self._prepared_clock
        if (prepared is None or prepared["source_row_sequence"] != tick.source_row_sequence
                or prepared["event_time_ms"] != tick.assumed_utc_epoch_ms):
            raise StreamHarnessError("consume requires the exact preflighted quote clock")
        clock_record = prepared["record"]

        row_started = False
        try:
            self._verify_pending_deadlines(tick)
            self._current_clock = clock_record
            # Keep the quote cursor outside the exact 16-field clock envelope.
            self._current_row_context = {
                "creation_source_row_sequence": tick.source_row_sequence,
            }
            # Keep the accepted order: clear row, native adapter, R3 caller
            # callback, independent ledger, parity, then post-consume wrapper.
            self.event_sink.begin_row()
            self.trace_buffer.clear()
            row_started = True
            before_fill_total = self.event_sink.row_counts["fill"]
            self.adapter.on_tick(tick)
            if after_adapter is not None:
                after_adapter(self, tick)
            self._verify_market_fills(tick)
            self._verify_stop_fills(tick)
            self.ledger.apply_fills(self.event_sink.current_fills)
            engine_positions = {
                key: (pos.side, pos.role, pos.volume_steps, pos.entry_price_micro)
                for key, pos in self.engine.inventory.items()
            }
            ledger_positions = {
                key: (pos.side, pos.role, pos.volume_steps, pos.average_entry_micro)
                for key, pos in self.ledger.positions.items()
            }
            if engine_positions != ledger_positions:
                raise StreamHarnessError("native/independent-ledger open-position parity failed")

            sample = self._sample_record(tick)
            sample["_source_row_sequence"] = tick.source_row_sequence
            self.last_sample = sample
            self.rows_processed += 1
            self.row_event_count += self.event_sink.current_event_count
            state = self.engine.diagnostic_state_snapshot()
            state_payload = _canonical_bytes(state, allowed_modules=self.allowed_state_modules)
            self.state_hash.update(len(state_payload).to_bytes(8, "big"))
            self.state_hash.update(state_payload)
            envelope = {
                "source_row_sequence": tick.source_row_sequence,
                "assumed_utc_epoch_ms": tick.assumed_utc_epoch_ms,
                "bid_micro": tick.bid_micro,
                "ask_micro": tick.ask_micro,
                "flags": tick.flags,
                "carried_sides": list(tick.carried_sides),
                "clock_evidence": clock_record,
                "vendor_event_count": self.event_sink.current_event_count,
                "fill_count_delta": self.event_sink.row_counts["fill"] - before_fill_total,
                "state_sha256": hashlib.sha256(state_payload).hexdigest(),
            }
            envelope_payload = _canonical_bytes(envelope, allowed_modules=self.allowed_state_modules)
            self.row_envelope_hash.update(len(envelope_payload).to_bytes(8, "big"))
            self.row_envelope_hash.update(envelope_payload)
            self._drain_trace(tick.source_row_sequence)
            minute = tick.assumed_utc_epoch_ms // 60_000
            if self.last_minute != minute or self.event_sink.current_event_count:
                fields = self.run_main.EQUITY_FIELDS
                equity_row = {field: sample[field] for field in fields}
                self.equity_hash.update(self.run_main._canonical_json(equity_row))
                self.equity_samples += 1
            self.last_minute = minute
            self.last_tick = tick
            self._last_clock = dict(clock_record)
        except BaseException as exc:
            if row_started:
                self._terminal_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._current_clock = None
            self._current_row_context = None
            self._prepared_clock = None

    def checkpoint(self) -> dict[str, Any]:
        state = self.engine.diagnostic_state_snapshot()
        state_bytes = _canonical_bytes(state, allowed_modules=self.allowed_state_modules)
        return {
            "rows_processed": self.rows_processed,
            "event_streams": self.event_sink.digest_summary(),
            "event_capture": self.event_capture.summary(),
            "trace_rows": self.trace_sequence,
            "trace_sha256": self.trace_hash.hexdigest(),
            "equity_samples": self.equity_samples,
            "equity_samples_sha256": self.equity_hash.hexdigest(),
            "row_envelope_sha256": self.row_envelope_hash.hexdigest(),
            "engine_state_sha256": hashlib.sha256(state_bytes).hexdigest(),
            "ledger_realized_gross_micro": self.ledger.realized_gross_micro,
            "ledger_realized_fee_micro": self.ledger.realized_fee_micro,
            "trade_diagnostics": self.trade_writer.summary(),
            "r3_memory_summary": self.r3_memory.summary(),
            "r3_sink_records_seen": self.r3_memory.record_sink.rows_seen,
            "r3_sink_sha256": self.r3_memory.record_sink.digest.hexdigest(),
            "terminal": self.terminal,
        }

    def close(self) -> None:
        if self._closed:
            return
        if self.event_sink.emit is not self._deadline_emit:
            raise StreamHarnessError("native order-deadline hook identity changed")
        # Restore hooks in installation order: the deadline wrapper was
        # layered on top of BoundedEventCapture's emitter.
        self.event_sink.emit = self._captured_emit
        self.event_capture.restore()
        self._closed = True
