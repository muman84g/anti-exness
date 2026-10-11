"""Lazy journal-bound controller for the private sampled-current shadow.

Construction and empty-journal recovery do not import the native replay stack.
The first fully validated quote binds the cold activation boundary.  Each
accepted row is preflighted, consumed once through StreamHarness, digested, and
then committed to QuoteJournal.  This module is not a broker or market runner.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


RUN_PREFIX = "bot30_cent_os_shadow_control_"
SYMBOL = "XAUUSD"
SCALE = 1_000_000
MAX_RECORD_BYTES = 4096
CLOCK_FIELDS = frozenset({
    "schema", "event_time_ms", "release_time_ms", "release_time_upper_bound_ms",
    "release_time_status", "broker_original_release_time_ms", "ingested_time_ms",
    "available_time_ms", "cutoff_time_ms", "source_provenance", "binding_id",
    "binding_sha256", "time_basis", "timezone", "dst_conversion", "clock_evidence_refs",
})
EVENT_FIELDS = frozenset({
    "context_row_index", "source_row_sequence", "raw_date", "raw_time", "raw_time_label",
    "assumed_utc_epoch_ms", "raw_bid", "raw_ask", "raw_last", "raw_volume", "raw_flags",
    "flags", "bid_micro", "ask_micro", "carried_sides",
})
RECORD_FIELDS = frozenset({"schema", "quote_event", "clock_evidence", "binding_proof"})
PROOF_FIELDS = frozenset({
    "schema", "verification", "binding_id", "binding_sha256", "clock_evidence_refs",
    "time_basis", "timezone", "dst_conversion", "ea_source_sha256",
})
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DECIMAL_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?$")


class PortableControlError(RuntimeError):
    """Terminal identity, native-advance, journal, or replay failure."""


class _InputRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("ascii")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _typed_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return (set(left) == set(right)
                and all(type(key) is str and _typed_equal(left[key], right[key]) for key in left))
    if type(left) in (list, tuple):
        return len(left) == len(right) and all(_typed_equal(a, b) for a, b in zip(left, right))
    return left == right


def _exact_ms(value: Any) -> bool:
    return type(value) is int and 0 < value <= 9_223_372_036_854_775_807


def _decimal_micro(value: Any) -> int:
    """Losslessly scale a bridge Decimal without using Decimal context arithmetic."""
    from decimal import Decimal

    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise _InputRejected("invalid_quote_price")
    text = str(value)
    if len(text) > 32 or not DECIMAL_TEXT.fullmatch(text):
        raise _InputRejected("invalid_quote_price_grammar")
    sign, digits, exponent = value.as_tuple()
    if len(digits) > 30 or exponent < -12 or exponent > 0:
        raise _InputRejected("quote_price_scale_out_of_range")
    coefficient = int("".join(str(digit) for digit in digits) or "0")
    scaled_exponent = exponent + 6
    if scaled_exponent >= 0:
        micro = coefficient * (10 ** scaled_exponent)
    else:
        divisor = 10 ** (-scaled_exponent)
        micro, remainder = divmod(coefficient, divisor)
        if remainder:
            raise _InputRejected("quote_price_exceeds_micro_precision")
    if sign:
        micro = -micro
    if micro <= 0 or micro > 9_223_372_036_854_775_807:
        raise _InputRejected("quote_price_out_of_range")
    return micro


def _format_micro(value: int) -> str:
    whole, fraction = divmod(value, SCALE)
    return f"{whole}.{fraction:06d}"


def _utc_label(epoch_ms: int) -> tuple[str, str, str]:
    try:
        moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=epoch_ms)
    except (OverflowError, ValueError) as exc:
        raise _InputRejected("quote_time_out_of_range") from exc
    raw_date = moment.strftime("%Y.%m.%d")
    raw_time = moment.strftime("%H:%M:%S.%f")[:-3]
    return raw_date, raw_time, f"EXNESS_MT5_SERVER_UTC0_V001:{raw_date} {raw_time}"


class PortableControl:
    """One journal owner. Runtime/native objects are created only for a valid row."""

    def __init__(self, *, journal: Any, run_identity: str, binding_config: Any):
        if type(run_identity) is not str or not run_identity.startswith(RUN_PREFIX):
            raise PortableControlError("fixed portable run identity is required")
        if type(getattr(journal, "identity", None)) is not dict:
            raise PortableControlError("journal identity must be an exact dictionary")
        identity = journal.identity
        if set(identity) != {"source_sha256", "config_sha256", "binding_sha256", "run_id"}:
            raise PortableControlError("journal identity schema mismatch")
        for name in ("source_sha256", "config_sha256", "binding_sha256"):
            if type(identity[name]) is not str or not HEX64.fullmatch(identity[name]):
                raise PortableControlError("journal identity pin is malformed")
        if type(identity["run_id"]) is not str or identity["run_id"] != run_identity:
            raise PortableControlError("run identity differs from journal identity")
        seed = {key: identity[key] for key in ("source_sha256", "config_sha256", "binding_sha256")}
        expected_run_id = RUN_PREFIX + _sha256(_canonical(seed))[:24]
        if run_identity != expected_run_id:
            raise PortableControlError("run identity is not derived from the fixed source/config/binding pins")
        for name in ("recover", "preflight", "commit"):
            if not callable(getattr(journal, name, None)):
                raise PortableControlError("journal does not expose the fixed bounded API")
        self.journal = journal
        self.run_identity = run_identity
        self.identity = dict(identity)
        self.binding_config = binding_config
        self._book: dict[str, Any] | None = None
        self._factory: Any = None
        self._bridge: Any = None
        self._harness_module: Any = None
        self._activation_ms: int | None = None
        self._accepted_count = 0
        self._last_sample_key: tuple[int, int, int] | None = None
        self._terminal_error: str | None = None
        self._recovered = False
        self._closed = False
        self._package_verified = False
        self.last_rejection: str | None = None

    @property
    def terminal(self) -> bool:
        return self._terminal_error is not None

    @property
    def activation_ms(self) -> int | None:
        return self._activation_ms

    def _require_usable(self) -> None:
        if self._closed:
            raise PortableControlError("portable control is closed")
        if self._terminal_error is not None:
            raise PortableControlError("portable control is terminal; use a fresh full-prefix recovery")
        if not self._recovered:
            raise PortableControlError("journal recovery must complete before quote admission")

    def _verify_package(self) -> str:
        if self._package_verified:
            return self.identity["source_sha256"]
        module = sys.modules.get("bot30_shadow")
        if module is None:
            try:
                module = importlib.import_module("bot30_shadow")
            except BaseException as exc:
                raise PortableControlError("portable package verifier could not be loaded") from exc
        expected = Path(__file__).resolve(strict=True).with_name("bot30_shadow.py")
        try:
            actual = Path(module.__file__).resolve(strict=True)
        except (AttributeError, OSError, RuntimeError) as exc:
            raise PortableControlError("portable package verifier has no exact local source path") from exc
        if actual != expected:
            raise PortableControlError("portable package verifier came from an unexpected source path")
        try:
            source_sha = module.verify_package()
        except BaseException as exc:
            raise PortableControlError("portable package identity verification failed") from exc
        if type(source_sha) is not str or source_sha != self.identity["source_sha256"]:
            raise PortableControlError("runtime manifest digest differs from the journal source identity")
        self._package_verified = True
        return source_sha

    def _bridge_module(self) -> Any:
        if self._bridge is not None:
            return self._bridge
        module = sys.modules.get("shadow_bridge")
        if module is None:
            try:
                module = importlib.import_module("shadow_bridge")
            except BaseException as exc:
                raise PortableControlError("pinned clock bridge could not be loaded") from exc
        expected = Path(__file__).resolve(strict=True).with_name("shadow_bridge.py")
        try:
            actual = Path(module.__file__).resolve(strict=True)
        except (AttributeError, OSError, RuntimeError) as exc:
            raise PortableControlError("clock bridge has no exact local source path") from exc
        if actual != expected:
            raise PortableControlError("clock bridge module came from an unexpected source path")
        self._bridge = module
        return module

    def _validate_binding_config(self, bridge: Any) -> None:
        binding = self.binding_config
        if type(binding) is not bridge.NonsecretBinding:
            raise _InputRejected("binding_config_type_rejected")
        if (type(binding.expected_company) is not str or not binding.expected_company
                or type(binding.expected_server) is not str or not binding.expected_server
                or type(binding.ea_source_sha256) is not str or not HEX64.fullmatch(binding.ea_source_sha256)
                or binding.binding_id != "EXNESS_MT5_SERVER_UTC0_V001"
                or tuple(binding.clock_evidence_refs) != tuple(bridge.CLOCK_EVIDENCE_REFS)
                or binding.time_basis != "UTC_EPOCH_MILLISECONDS"
                or binding.timezone != "UTC" or binding.dst_conversion != "NONE_UTC_EPOCH"
                or type(binding.binding_sha256) is not str
                or binding.binding_sha256 != self.identity["binding_sha256"]):
            raise _InputRejected("binding_config_mismatch")

    @staticmethod
    def _exact_clock(clock: Any, expected: dict[str, Any]) -> bool:
        if type(clock) is not dict or set(clock) != CLOCK_FIELDS:
            return False
        return _typed_equal(clock, expected)

    def _live_record(self, info: Any, clock: Any, verified_binding: Any) -> dict[str, Any]:
        bridge = self._bridge_module()
        self._validate_binding_config(bridge)
        if type(info) is not bridge.DecodedInfo or type(verified_binding) is not bridge.VerifiedBinding:
            raise _InputRejected("codec_decoded_info_required")
        if info.verified_binding is not verified_binding:
            raise _InputRejected("verified_binding_object_mismatch")
        if info.symbol != SYMBOL:
            raise _InputRejected("unsupported_symbol")
        if (type(info.time_msc) is not int or type(info.time_msc) is bool or not _exact_ms(info.time_msc)
                or type(info.received_ms) is not int or not _exact_ms(info.received_ms)
                or type(info.available_ms) is not int or not _exact_ms(info.available_ms)):
            raise _InputRejected("invalid_sample_clock_types")
        if (info.binding_id != self.binding_config.binding_id
                or info.binding_sha256 != self.binding_config.binding_sha256
                or verified_binding.binding_id != self.binding_config.binding_id
                or verified_binding.binding_sha256 != self.binding_config.binding_sha256
                or tuple(verified_binding.clock_evidence_refs) != tuple(self.binding_config.clock_evidence_refs)
                or verified_binding.time_basis != self.binding_config.time_basis):
            raise _InputRejected("verified_binding_mismatch")
        if type(clock) is not dict or set(clock) != CLOCK_FIELDS:
            raise _InputRejected("clock_schema_rejected")
        cutoff = clock.get("cutoff_time_ms")
        if not _exact_ms(cutoff):
            raise _InputRejected("clock_cutoff_rejected")
        try:
            clock_evidence = bridge.clock_evidence(info, cutoff).asdict()
        except Exception:
            raise _InputRejected("clock_or_codec_binding_rejected") from None
        if not self._exact_clock(clock, clock_evidence):
            raise _InputRejected("clock_evidence_mismatch")

        bid_micro = _decimal_micro(info.bid)
        ask_micro = _decimal_micro(info.ask)
        if ask_micro <= bid_micro:
            raise _InputRejected("crossed_or_equal_quote")
        raw_date, raw_time, raw_time_label = _utc_label(info.time_msc)
        sequence = self._accepted_count + 1
        event = {
            "context_row_index": sequence,
            "source_row_sequence": sequence,
            "raw_date": raw_date,
            "raw_time": raw_time,
            "raw_time_label": raw_time_label,
            "assumed_utc_epoch_ms": info.time_msc,
            "raw_bid": _format_micro(bid_micro),
            "raw_ask": _format_micro(ask_micro),
            "raw_last": "",
            "raw_volume": "0",
            "raw_flags": "6",
            "flags": 6,
            "bid_micro": bid_micro,
            "ask_micro": ask_micro,
            "carried_sides": [],
        }
        proof = {
            "schema": "bot30_shadow_binding_proof_v001",
            "verification": "CODEC_VERIFIED_AT_CAPTURE",
            "binding_id": self.binding_config.binding_id,
            "binding_sha256": self.binding_config.binding_sha256,
            "clock_evidence_refs": list(self.binding_config.clock_evidence_refs),
            "time_basis": self.binding_config.time_basis,
            "timezone": self.binding_config.timezone,
            "dst_conversion": self.binding_config.dst_conversion,
            "ea_source_sha256": self.binding_config.ea_source_sha256,
        }
        record = {
            "schema": "bot30_shadow_actual_quote_record_v001",
            "quote_event": event,
            "clock_evidence": clock_evidence,
            "binding_proof": proof,
        }
        if len(_canonical(record)) > MAX_RECORD_BYTES - 1:
            raise _InputRejected("quote_journal_record_size_rejected")
        self._validate_record(record)
        return record

    def _validate_clock(self, clock: Any, event: dict[str, Any]) -> None:
        if type(clock) is not dict or set(clock) != CLOCK_FIELDS:
            raise PortableControlError("journal clock schema mismatch")
        if clock.get("schema") != "bot30_sampled_clock_envelope_v002":
            raise PortableControlError("journal clock version mismatch")
        for name in ("event_time_ms", "ingested_time_ms", "available_time_ms", "cutoff_time_ms", "release_time_upper_bound_ms"):
            if not _exact_ms(clock.get(name)):
                raise PortableControlError("journal clock contains a non-integer or invalid time")
        if (clock.get("release_time_ms") is not None
                or clock.get("broker_original_release_time_ms") is not None
                or clock.get("release_time_status") != "consumer_observed_upper_bound"
                or clock["release_time_upper_bound_ms"] != clock["ingested_time_ms"]
                or not clock["event_time_ms"] <= clock["ingested_time_ms"] <= clock["available_time_ms"] <= clock["cutoff_time_ms"]
                or clock["event_time_ms"] != event["assumed_utc_epoch_ms"]
                or clock.get("source_provenance") != "SAMPLED_CURRENT_BID_ASK_SHADOW_V001"
                or clock.get("binding_id") != "EXNESS_MT5_SERVER_UTC0_V001"
                or clock.get("binding_sha256") != self.identity["binding_sha256"]
                or clock.get("time_basis") != "UTC_EPOCH_MILLISECONDS"
                or clock.get("timezone") != "UTC"
                or clock.get("dst_conversion") != "NONE_UTC_EPOCH"):
            raise PortableControlError("journal clock order/provenance does not match the fixed sampled contract")
        refs = clock.get("clock_evidence_refs")
        if (type(refs) is not list or len(refs) != 2
                or any(type(item) is not str or not item for item in refs)):
            raise PortableControlError("journal clock reference shape mismatch")

    def _validate_record(self, record: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if type(record) is not dict or set(record) != RECORD_FIELDS or record.get("schema") != "bot30_shadow_actual_quote_record_v001":
            raise PortableControlError("journal quote record schema mismatch")
        event = record.get("quote_event")
        if type(event) is not dict or set(event) != EVENT_FIELDS:
            raise PortableControlError("journal QuoteEvent schema mismatch")
        int_fields = ("context_row_index", "source_row_sequence", "assumed_utc_epoch_ms", "flags", "bid_micro", "ask_micro")
        if any(type(event.get(name)) is not int for name in int_fields):
            raise PortableControlError("journal QuoteEvent integer type mismatch")
        str_fields = ("raw_date", "raw_time", "raw_time_label", "raw_bid", "raw_ask", "raw_last", "raw_volume", "raw_flags")
        if any(type(event.get(name)) is not str for name in str_fields):
            raise PortableControlError("journal QuoteEvent string type mismatch")
        if (event["context_row_index"] != self._accepted_count + 1
                or event["source_row_sequence"] != self._accepted_count + 1
                or not _exact_ms(event["assumed_utc_epoch_ms"])
                or not _exact_ms(event["bid_micro"]) or not _exact_ms(event["ask_micro"])
                or event["ask_micro"] <= event["bid_micro"]
                or event["flags"] != 6 or event["raw_flags"] != "6"
                or event["raw_last"] != "" or event["raw_volume"] != "0"
                or type(event["carried_sides"]) is not list or event["carried_sides"]):
            raise PortableControlError("journal QuoteEvent value contract mismatch")
        try:
            raw_date, raw_time, raw_label = _utc_label(event["assumed_utc_epoch_ms"])
        except _InputRejected as exc:
            raise PortableControlError("journal QuoteEvent time is out of range") from exc
        if (event["raw_date"], event["raw_time"], event["raw_time_label"]) != (raw_date, raw_time, raw_label):
            raise PortableControlError("journal QuoteEvent raw UTC label mismatch")
        if (event["raw_bid"] != _format_micro(event["bid_micro"])
                or event["raw_ask"] != _format_micro(event["ask_micro"])):
            raise PortableControlError("journal QuoteEvent raw prices differ from integer micro prices")
        clock = record.get("clock_evidence")
        self._validate_clock(clock, event)
        proof = record.get("binding_proof")
        if type(proof) is not dict or set(proof) != PROOF_FIELDS:
            raise PortableControlError("journal binding proof schema mismatch")
        bridge = self._bridge_module()
        self._validate_binding_config(bridge)
        expected_proof = {
            "schema": "bot30_shadow_binding_proof_v001",
            "verification": "CODEC_VERIFIED_AT_CAPTURE",
            "binding_id": self.binding_config.binding_id,
            "binding_sha256": self.binding_config.binding_sha256,
            "clock_evidence_refs": list(self.binding_config.clock_evidence_refs),
            "time_basis": self.binding_config.time_basis,
            "timezone": self.binding_config.timezone,
            "dst_conversion": self.binding_config.dst_conversion,
            "ea_source_sha256": self.binding_config.ea_source_sha256,
        }
        if not _typed_equal(proof, expected_proof):
            raise PortableControlError("persisted binding proof does not match the fixed configuration")
        return event, clock

    def _make_tick(self, event_row: dict[str, Any]) -> Any:
        if self._book is None:
            raise PortableControlError("native quote event construction preceded the control book")
        h2 = self._book["runtime"]["h2"]
        values = dict(event_row)
        values["carried_sides"] = tuple(values["carried_sides"])
        try:
            return h2.QuoteEvent(**values)
        except (TypeError, ValueError) as exc:
            raise PortableControlError("pinned native QuoteEvent constructor schema mismatch") from exc

    def _ensure_book(self, activation_ms: int) -> None:
        if self._book is not None:
            if activation_ms != self._activation_ms:
                raise PortableControlError("online activation changed after first accepted row")
            return
        self._verify_package()
        factory = importlib.import_module("portable_factory")
        expected_factory = Path(__file__).resolve(strict=True).with_name("portable_factory.py")
        if Path(factory.__file__).resolve(strict=True) != expected_factory:
            raise PortableControlError("portable factory module came from an unexpected path")
        identity = factory.verify_portable_identity()
        if (type(identity) is not dict
                or identity.get("status") != "PORTABLE_SOURCE_IDENTITY_VERIFIED_NO_BOOK"
                or identity.get("market_authority") is not False
                or identity.get("h2_p2_first_installed") is not False):
            raise PortableControlError("portable factory identity or control-only profile mismatch")
        runtime = factory._load_runtime()
        self._harness_module = importlib.import_module("stream_harness")
        boundary_type = runtime["boundary_module"].Boundary
        boundary = boundary_type(activation_ms, activation_ms, activation_ms)
        book = factory.build_control_book(
            activation_epoch_ms=activation_ms,
            boundary=boundary,
            stream_harness_type=self._harness_module,
            run_identity=self.run_identity,
        )
        harness = book.get("harness") if type(book) is dict else None
        if (type(harness) is not self._harness_module.StreamHarness
                or harness.rows_processed != 0 or harness.last_tick is not None
                or book.get("activation_epoch_ms") != activation_ms
                or book.get("h2_p2_first_installed") is not False):
            factory.close_control_book(book)
            raise PortableControlError("fresh portable native book did not match the cold online boundary")
        self._factory = factory
        self._book = book
        self._activation_ms = activation_ms

    def _advance(self, event_row: dict[str, Any], clock: dict[str, Any], activation_ms: int) -> str:
        self._ensure_book(activation_ms)
        if self._book is None or self._activation_ms != activation_ms:
            raise PortableControlError("control book activation is unavailable")
        tick = self._make_tick(event_row)
        harness = self._book["harness"]
        try:
            harness.prepare_clock_record(tick, clock)
            harness.consume(tick)
            self._accepted_count += 1
            self._last_sample_key = (tick.assumed_utc_epoch_ms, tick.bid_micro, tick.ask_micro)
            if harness.rows_processed != self._accepted_count:
                raise PortableControlError("native stream cursor differs from the committed quote prefix")
            return self.business_digest()
        except BaseException as exc:
            self._terminal_error = type(exc).__name__
            raise

    def business_digest(self) -> str:
        if self._book is None or self._activation_ms is None:
            raise PortableControlError("business digest is unavailable before the first native quote")
        harness_digest = self._book["harness"].business_digest()
        summary = {
            "schema": "bot30_shadow_portable_business_digest_v001",
            "run_identity": self.run_identity,
            "activation_ms": self._activation_ms,
            "accepted_quotes": self._accepted_count,
            "last_sample_key": list(self._last_sample_key) if self._last_sample_key is not None else None,
            "native_stream_harness_sha256": harness_digest,
        }
        return _sha256(_canonical(summary))

    def _replay_quote(self, record: dict[str, Any]) -> str:
        try:
            event_row, clock = self._validate_record(record)
            activation = self.journal.activation_ms
            if not _exact_ms(activation):
                raise PortableControlError("journal activation is missing during recovery")
            event_time = event_row["assumed_utc_epoch_ms"]
            if self._accepted_count == 0 and activation != event_time:
                raise PortableControlError("journal activation does not equal the first recorded event")
            if self._last_sample_key is not None:
                last_time, last_bid, last_ask = self._last_sample_key
                if event_time <= last_time:
                    raise PortableControlError("journal replay contains a duplicate or backward source event")
            return self._advance(event_row, clock, activation)
        except BaseException as exc:
            self._terminal_error = type(exc).__name__
            raise

    def recover(self) -> dict[str, Any]:
        if self._recovered or self._closed:
            raise PortableControlError("journal recovery must run exactly once on an open control")
        if self._terminal_error is not None:
            raise PortableControlError("terminal control cannot recover in place")
        try:
            self.journal.recover(self._replay_quote)
            self._recovered = True
            if self.journal.count != self._accepted_count:
                raise PortableControlError("journal/native replay row counts differ")
            if self.journal.count == 0:
                if self._book is not None or self._activation_ms is not None:
                    raise PortableControlError("empty journal unexpectedly constructed a native book")
            else:
                if self.journal.activation_ms != self._activation_ms:
                    raise PortableControlError("journal/native activation differs after replay")
                if self.journal.business_digest != self.business_digest():
                    raise PortableControlError("journal final business digest differs after replay")
            return {
                "status": "EMPTY_PREFIX_READY" if self.journal.count == 0 else "FULL_PREFIX_REPLAYED",
                "committed_quotes": self.journal.count,
                "activation_ms": self._activation_ms,
                "business_digest": self.business_digest() if self._book is not None else None,
            }
        except BaseException as exc:
            self._terminal_error = type(exc).__name__
            raise

    def on_quote(self, decoded_info: Any, clock_record: Any, verified_binding: Any) -> bool:
        self._require_usable()
        if getattr(self.journal, "failed", False):
            self._terminal_error = "JournalError"
            raise PortableControlError("journal is terminal after a failed commit/recovery")
        try:
            self._verify_package()
            record = self._live_record(decoded_info, clock_record, verified_binding)
            event_row = record["quote_event"]
            key = (event_row["assumed_utc_epoch_ms"], event_row["bid_micro"], event_row["ask_micro"])
            if self._last_sample_key is not None:
                last_time, last_bid, last_ask = self._last_sample_key
                if key[0] == last_time:
                    if key[1:] == (last_bid, last_ask):
                        self.last_rejection = "exact_duplicate_snapshot"
                        return False
                    self.last_rejection = "equal_time_changed_quote"
                    return False
                if key[0] < last_time:
                    self.last_rejection = "backward_event_time"
                    return False
            activation = self._activation_ms if self._activation_ms is not None else key[0]
            if self.journal.activation_ms is not None and activation != self.journal.activation_ms:
                self.last_rejection = "journal_activation_mismatch"
                return False
        except _InputRejected as exc:
            self.last_rejection = exc.reason
            return False
        except Exception as exc:
            # Source verification is not a quote rejection and must not be hidden.
            if isinstance(exc, PortableControlError):
                self._terminal_error = type(exc).__name__
                raise
            self.last_rejection = "clock_or_binding_preflight_rejected"
            return False

        try:
            self.journal.preflight(record, activation)
        except BaseException as exc:
            self._terminal_error = type(exc).__name__
            raise
        try:
            digest = self._advance(record["quote_event"], record["clock_evidence"], activation)
        except BaseException as exc:
            self._terminal_error = type(exc).__name__
            # Do not translate native/post-advance failures into BridgeError; the
            # process must preserve terminal state and recover a fresh prefix.
            raise
        try:
            self.journal.commit(record, digest, activation)
        except BaseException as exc:
            self._terminal_error = type(exc).__name__
            raise
        self.last_rejection = None
        return True

    def close(self) -> None:
        if self._closed:
            return
        if self._book is not None and self._factory is not None:
            self._factory.close_control_book(self._book)
        self._closed = True

