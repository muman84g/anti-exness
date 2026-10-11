"""Bounded native-quote wrapper around the frozen v003 vendor baseline.

Market execution is deliberately gated by a parent launch receipt. Synthetic
fixtures import the same wrapper without reading the market copy.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator


IDENTITY = "bot30_demo_api_full_tick_native_approx_similarity_v006"
DESIGN_SHA256 = "3328c4a88e8f19a1dd90d00d7b73d9b0e6412d4fc23ed300b534afbdc849cd99"
SOURCE_SHA256 = "9515dd1a8449306659ad76890fc0de18046dbf55193316390249e4a4ca0fd9a5"
SOURCE_BYTES = 10_081_136
SOURCE_ROWS = 197_673
SOURCE_HEADER = ("<DATE>", "<TIME>", "<BID>", "<ASK>", "<LAST>", "<VOLUME>", "<FLAGS>")
MAX_LINE_BYTES = 4096
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_RSS_BYTES = 1024 * 1024 * 1024
MAX_RUNTIME_SECONDS = 3600
OBSERVATION_SECONDS = 5
FEE_PER_LOT_USD_MICRO = 11_000_000
VOLUME_STEP_DIVISOR = 100

WORKSPACE = Path(__file__).resolve().parents[2]
SOURCE_BIND_PATH = WORKSPACE / "outputs" / "discovery_v005" / "source_scope_bind_v001.json"
DESIGN_PATH = WORKSPACE / "outputs" / "discovery_v003" / "design" / "native_approx_similarity_design_v001.md"
VENDOR_PATH = WORKSPACE / "work" / "discovery_v003" / "vendor"
EXPECTED_VENDOR_HASHES = {
    "h2_engine.py": "47da2b8b6cbe3a6fc24426e9a11ca55642096e2cf1ad55744e863cf80b9b7396",
    "lifecycle_market_adapter.py": "786f6490999379d63ffd3646a65a04b15911abd136923096160c2f5a82bb4c2a",
    "a3_lifecycle_boundary.py": "cd15541aef8b459a31ef79e905c33e32907d1c60596a20e2b47397eaac83dac8",
}
_VERIFIED_VENDOR_MODULES: tuple[Any, Any, Any, dict[str, dict[str, str]]] | None = None
ACTUAL_CLOCK_FIELDS = ("event_time", "release_time", "ingested_time", "available_time", "cutoff_time")
EQUITY_FIELDS = (
    "epoch_ms",
    "realized_gross_usd",
    "realized_fee_usd",
    "realized_net_usd",
    "net_equity_usd",
    "open_count",
    "pending_count",
)
EVENT_FILES = {
    "order": "orders.jsonl",
    "fill": "fills.jsonl",
    "order_event": "order_events.jsonl",
    "state": "state_events.jsonl",
    "branch": "branch_decisions.jsonl",
    "quote_carry": "quote_carries.jsonl",
}


class NativeInputError(RuntimeError):
    """A fixed-copy, native quote, or launch receipt contract failed."""


class RunLimitError(RuntimeError):
    """The predeclared runtime, RSS, or output limit was exceeded."""


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _json_read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise NativeInputError(f"expected JSON object: {path}")
    return value


def _utc_epoch_ms(raw_date: str, raw_time: str) -> int:
    parsed = datetime.strptime(f"{raw_date} {raw_time}", "%Y.%m.%d %H:%M:%S.%f")
    if parsed.microsecond % 1000:
        raise ValueError("source timestamp finer than one millisecond")
    epoch_day = date(1970, 1, 1)
    days = parsed.date().toordinal() - epoch_day.toordinal()
    return (
        days * 86_400_000
        + parsed.hour * 3_600_000
        + parsed.minute * 60_000
        + parsed.second * 1_000
        + parsed.microsecond // 1_000
    )


def _price_micro(raw: str) -> int:
    value = Decimal(raw)
    scaled = value * 1_000_000
    if not value.is_finite() or scaled != scaled.to_integral_value():
        raise ValueError("price is not an exact micro-price")
    result = int(scaled)
    if result <= 0:
        raise ValueError("nonpositive quote")
    return result


def _stat_record(path: Path) -> dict[str, Any]:
    info = path.lstat()
    attributes = int(getattr(info, "st_file_attributes", 0))
    return {
        "path": str(path),
        "size_bytes": int(info.st_size),
        "mtime_ns": int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        "device_id": int(info.st_dev),
        "file_id": int(info.st_ino),
        "attributes": attributes,
    }


def _assert_no_reparse_components(path: Path) -> None:
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except OSError as exc:
            raise NativeInputError(f"cannot stat path component: {ancestor}") from exc
        attributes = int(getattr(info, "st_file_attributes", 0))
        if ancestor.is_symlink() or attributes & 0x400:
            raise NativeInputError(f"symlink/reparse component rejected: {ancestor}")


def _load_vendor_modules() -> tuple[Any, Any, Any, dict[str, dict[str, str]]]:
    """Import the three fixed copies and prove each loaded path/hash."""
    import importlib

    global _VERIFIED_VENDOR_MODULES

    names = ("h2_engine", "lifecycle_market_adapter", "a3_lifecycle_boundary")
    cached = [name for name in names if name in sys.modules]
    if cached and _VERIFIED_VENDOR_MODULES is None:
        raise NativeInputError(f"unexpected preloaded vendor module cache: {cached}")
    if _VERIFIED_VENDOR_MODULES is not None:
        if len(cached) != len(names):
            raise NativeInputError("verified vendor module cache became incomplete")
        modules = tuple(sys.modules[name] for name in names)
        loaded = _VERIFIED_VENDOR_MODULES[3]
        for module, filename in zip(modules, EXPECTED_VENDOR_HASHES):
            expected = (VENDOR_PATH / filename).absolute()
            actual = Path(module.__file__).absolute()
            if os.path.normcase(str(actual)) != os.path.normcase(str(expected)) or _sha256_path(actual) != EXPECTED_VENDOR_HASHES[filename]:
                raise NativeInputError(f"verified vendor module cache changed: {module.__name__}")
        return modules[0], modules[1], modules[2], loaded
    vendor_text = str(VENDOR_PATH)
    sys.path.insert(0, vendor_text)
    loaded: dict[str, dict[str, str]] = {}
    modules: dict[str, Any] = {}
    try:
        for stem, filename in (("h2_engine", "h2_engine.py"), ("lifecycle_market_adapter", "lifecycle_market_adapter.py"), ("a3_lifecycle_boundary", "a3_lifecycle_boundary.py")):
            module = importlib.import_module(stem)
            loaded_path = Path(module.__file__).absolute()
            expected_path = (VENDOR_PATH / filename).absolute()
            digest = _sha256_path(loaded_path)
            if os.path.normcase(str(loaded_path)) != os.path.normcase(str(expected_path)):
                raise NativeInputError(f"wrong loaded vendor path for {stem}: {loaded_path}")
            if digest != EXPECTED_VENDOR_HASHES[filename]:
                raise NativeInputError(f"vendor hash mismatch for {filename}: {digest}")
            loaded[filename] = {"loaded_path": str(loaded_path), "sha256": digest}
            modules[stem] = module
    except Exception:
        for name in names:
            sys.modules.pop(name, None)
        raise
    finally:
        try:
            sys.path.remove(vendor_text)
        except ValueError:
            pass
    _VERIFIED_VENDOR_MODULES = (modules["h2_engine"], modules["lifecycle_market_adapter"], modules["a3_lifecycle_boundary"], loaded)
    return _VERIFIED_VENDOR_MODULES


def _process_rss_bytes() -> int:
    try:
        import psutil  # type: ignore[import-not-found]

        return int(psutil.Process().memory_info().rss)
    except ImportError:
        if os.name != "nt":
            raise RuntimeError("RSS measurement requires psutil outside Windows")

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.wintypes.DWORD),
                ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        if not ok:
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.WorkingSetSize)


@dataclass(frozen=True)
class SourceIdentity:
    path: Path
    bytes_expected: int
    rows_expected: int
    sha256_expected: str


class NativeSourceReader:
    """Single-pass TSV codec; no R00 freshness/current-flag rules."""

    def __init__(self, identity: SourceIdentity, common_cutoff_epoch_ms: int):
        self.identity = identity
        self.common_cutoff_epoch_ms = common_cutoff_epoch_ms
        self.receipt: dict[str, Any] = {
            "identity": IDENTITY,
            "path": str(identity.path),
            "expected_bytes": identity.bytes_expected,
            "expected_rows": identity.rows_expected,
            "expected_sha256": identity.sha256_expected,
            "raw_clock_policy": "assumed UTC from source labels; actual release/ingested/available/cutoff clocks unknown",
            "carry_policy": "vendor-native: parse any nonempty Bid/Ask raw field; carry prior side only when that raw side is blank, its update bit is unset, the other side update bit is set, and prior side exists; no R00 age/current-flag filter",
            "complete": False,
            "normalized_quote_sha256": None,
            "normalized_quote_prefix_le_C_sha256": None,
            "normalized_quote_prefix_le_C_rows": 0,
        }

    def __iter__(self) -> Iterator[Any]:
        engine_module = sys.modules.get("h2_engine")
        if engine_module is None:
            raise NativeInputError("vendor h2_engine must be loaded before quote parsing")
        QuoteEvent = engine_module.QuoteEvent
        path = Path(os.path.abspath(str(self.identity.path)))
        _assert_no_reparse_components(path)
        pre = _stat_record(path)
        if pre["size_bytes"] != self.identity.bytes_expected:
            raise NativeInputError(f"source size differs from fixed receipt: {pre['size_bytes']}")
        hasher = hashlib.sha256()
        normalized = hashlib.sha256()
        prefix = hashlib.sha256()
        previous_time_ms: int | None = None
        previous_bid: int | None = None
        previous_ask: int | None = None
        flags_counts: Counter[int] = Counter()
        unknown_flag_bits_counts: Counter[int] = Counter()
        equal_adjacent = 0
        sequence = 0
        first_raw_time: str | None = None
        last_raw_time: str | None = None
        bytes_read = 0
        first_line = True
        handle_post_values: dict[str, int] | None = None
        with path.open("rb") as stream:
            handle_pre = os.fstat(stream.fileno())
            handle_file_id = (int(handle_pre.st_dev), int(handle_pre.st_ino))
            if handle_file_id != (pre["device_id"], pre["file_id"]):
                raise NativeInputError("opened handle does not match fixed-copy preflight identity")
            if int(handle_pre.st_size) != pre["size_bytes"]:
                raise NativeInputError("opened handle size differs from fixed-copy preflight")
            while True:
                raw_line = stream.readline(MAX_LINE_BYTES + 1)
                if not raw_line:
                    break
                hasher.update(raw_line)
                bytes_read += len(raw_line)
                if len(raw_line) > MAX_LINE_BYTES:
                    raise NativeInputError(f"source line exceeds {MAX_LINE_BYTES} bytes")
                try:
                    line = raw_line.decode("utf-8-sig" if first_line else "utf-8", errors="strict").rstrip("\r\n")
                except UnicodeDecodeError as exc:
                    raise NativeInputError("source encoding is not strict UTF-8") from exc
                if first_line:
                    first_line = False
                    header = tuple(line.split("\t"))
                    if header != SOURCE_HEADER:
                        raise NativeInputError(f"fixed source header mismatch: {header}")
                    self.receipt["header"] = list(header)
                    self.receipt["delimiter"] = "tab"
                    self.receipt["encoding"] = "utf-8-sig"
                    continue
                sequence += 1
                fields = line.split("\t")
                if len(fields) != len(SOURCE_HEADER):
                    raise NativeInputError(f"source column count mismatch at row {sequence}")
                raw_date, raw_time, raw_bid, raw_ask, raw_last, raw_volume, raw_flags = fields
                label = f"{raw_date} {raw_time}"
                try:
                    epoch_ms = _utc_epoch_ms(raw_date, raw_time)
                    flags_decimal = Decimal(raw_flags)
                    if not flags_decimal.is_finite() or flags_decimal != flags_decimal.to_integral_value():
                        raise ValueError("noninteger flags")
                    flags = int(flags_decimal)
                    if flags < 0:
                        raise ValueError(f"negative native quote flags {flags}")
                    bid_updated = bool(flags & 2)
                    ask_updated = bool(flags & 4)
                    if not raw_bid and not raw_ask:
                        raise ValueError("both quote sides are absent")
                    if raw_bid:
                        next_bid = _price_micro(raw_bid)
                    elif bid_updated:
                        raise ValueError("Bid update bit set with missing Bid")
                    elif not ask_updated:
                        raise ValueError("missing Bid is not paired with Ask update")
                    elif previous_bid is None:
                        raise ValueError("leading missing Bid carry")
                    else:
                        next_bid = previous_bid
                    if raw_ask:
                        next_ask = _price_micro(raw_ask)
                    elif ask_updated:
                        raise ValueError("Ask update bit set with missing Ask")
                    elif not bid_updated:
                        raise ValueError("missing Ask is not paired with Bid update")
                    elif previous_ask is None:
                        raise ValueError("leading missing Ask carry")
                    else:
                        next_ask = previous_ask
                    if next_bid is None or next_ask is None:
                        raise ValueError("leading missing-side carry")
                    if next_bid > next_ask:
                        raise ValueError("crossed quote")
                    if previous_time_ms is not None and epoch_ms < previous_time_ms:
                        raise ValueError("source timestamp reversed; source order is never sorted")
                except (ValueError, InvalidOperation) as exc:
                    raise NativeInputError(f"native quote rejected at source row {sequence}: {exc}") from exc
                if previous_time_ms == epoch_ms:
                    equal_adjacent += 1
                previous_time_ms = epoch_ms
                previous_bid, previous_ask = next_bid, next_ask
                flags_counts[flags] += 1
                if flags & ~6:
                    unknown_flag_bits_counts[flags & ~6] += 1
                if first_raw_time is None:
                    first_raw_time = label
                last_raw_time = label
                carried_sides = tuple(side for side, raw_value in (("BID", raw_bid), ("ASK", raw_ask)) if not raw_value)
                digest_record = f"{sequence}\t{epoch_ms}\t{next_bid}\t{next_ask}\t{flags}\n".encode("ascii")
                normalized.update(digest_record)
                if epoch_ms <= self.common_cutoff_epoch_ms:
                    prefix.update(digest_record)
                    self.receipt["normalized_quote_prefix_le_C_rows"] += 1
                yield QuoteEvent(
                    context_row_index=sequence,
                    source_row_sequence=sequence,
                    raw_date=raw_date,
                    raw_time=raw_time,
                    raw_time_label=label,
                    assumed_utc_epoch_ms=epoch_ms,
                    raw_bid=raw_bid,
                    raw_ask=raw_ask,
                    raw_last=raw_last,
                    raw_volume=raw_volume,
                    raw_flags=raw_flags,
                    flags=flags,
                    bid_micro=next_bid,
                    ask_micro=next_ask,
                    carried_sides=carried_sides,
                )
            handle_post = os.fstat(stream.fileno())
            handle_post_values = {
                "size_bytes": int(handle_post.st_size),
                "mtime_ns": int(getattr(handle_post, "st_mtime_ns", int(handle_post.st_mtime * 1_000_000_000))),
                "device_id": int(handle_post.st_dev),
                "file_id": int(handle_post.st_ino),
            }
        if first_line:
            raise NativeInputError("source is empty")
        post = _stat_record(path)
        actual_sha = hasher.hexdigest()
        stable = (
            pre["size_bytes"] == post["size_bytes"]
            and pre["mtime_ns"] == post["mtime_ns"]
            and pre["device_id"] == post["device_id"]
            and pre["file_id"] == post["file_id"]
            and bytes_read == self.identity.bytes_expected
            and handle_post_values is not None
            and handle_post_values["size_bytes"] == self.identity.bytes_expected
            and handle_post_values["mtime_ns"] == pre["mtime_ns"]
            and handle_post_values["device_id"] == pre["device_id"]
            and handle_post_values["file_id"] == pre["file_id"]
        )
        if sequence != self.identity.rows_expected:
            raise NativeInputError(f"source row count mismatch: {sequence}")
        if actual_sha != self.identity.sha256_expected:
            raise NativeInputError(f"source SHA256 mismatch: {actual_sha}")
        if not stable:
            raise NativeInputError("fixed source metadata changed during the one-pass read")
        self.receipt.update({
            "pre": pre,
            "post": post,
            "handle_file_id_at_open": list(handle_file_id),
            "handle_post": handle_post_values,
            "source_bytes_read_once": bytes_read,
            "source_rows_read_once": sequence,
            "source_sha256_read_once": actual_sha,
            "first_raw_time": first_raw_time,
            "last_raw_time": last_raw_time,
            "equal_adjacent_timestamps": equal_adjacent,
            "flags_counts": {str(k): v for k, v in sorted(flags_counts.items())},
            "unknown_flag_bits_counts": {str(k): v for k, v in sorted(unknown_flag_bits_counts.items())},
            "normalized_quote_sha256": normalized.hexdigest(),
            "normalized_quote_prefix_le_C_sha256": prefix.hexdigest(),
            "complete": True,
            "fixed_copy_pre_post_stable": stable,
        })


class _CountOnly:
    def __init__(self) -> None:
        self.count = 0

    def __len__(self) -> int:
        return self.count


class StreamingJsonlSink:
    """Write vendor events verbatim as bounded JSONL, or hash-only in fixtures."""

    def __init__(self, output_dir: Path | None):
        self.output_dir = output_dir
        self.streams: dict[str, Any] = {}
        self.schemas: dict[str, tuple[str, ...]] = {}
        self.row_counts: Counter[str] = Counter()
        self.row_hashes: dict[str, Any] = {}
        self.rows = {kind: _CountOnly() for kind in EVENT_FILES}
        self.current_fills: list[dict[str, Any]] = []
        self.current_event_count = 0
        self.total_rows = 0
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=False)

    def begin_row(self) -> None:
        self.current_fills.clear()
        self.current_event_count = 0

    def emit(self, kind: str, row: dict[str, Any]) -> None:
        if kind not in EVENT_FILES:
            raise RuntimeError(f"unexpected frozen engine event kind: {kind}")
        schema = tuple(row.keys())
        if kind in self.schemas and self.schemas[kind] != schema:
            raise RuntimeError(f"vendor event schema drift for {kind}")
        self.schemas.setdefault(kind, schema)
        payload = _canonical_json(row)
        if kind not in self.row_hashes:
            self.row_hashes[kind] = hashlib.sha256()
        self.row_hashes[kind].update(payload)
        self.row_counts[kind] += 1
        self.rows[kind].count += 1
        self.total_rows += 1
        self.current_event_count += 1
        if kind == "fill":
            self.current_fills.append(dict(row))
        if self.output_dir is not None:
            stream = self.streams.get(kind)
            if stream is None:
                stream = (self.output_dir / EVENT_FILES[kind]).open("x", encoding="utf-8", newline="")
                self.streams[kind] = stream
            stream.write(payload.decode("utf-8"))
        if self.total_rows % 4096 == 0:
            self.flush()

    def flush(self) -> None:
        for stream in self.streams.values():
            stream.flush()

    def close(self) -> None:
        self.flush()
        for stream in self.streams.values():
            stream.close()
        self.streams.clear()

    def digest_summary(self) -> dict[str, Any]:
        return {
            kind: {"rows": self.row_counts[kind], "sha256": self.row_hashes[kind].hexdigest() if kind in self.row_hashes else hashlib.sha256().hexdigest()}
            for kind in EVENT_FILES
        }


def _usd(micro: int) -> str:
    sign = "-" if micro < 0 else ""
    whole, fractional = divmod(abs(micro), 1_000_000)
    return f"{sign}{whole}.{fractional:06d}"


@dataclass
class LedgerPosition:
    side: str
    role: str
    volume_steps: int
    average_entry_micro: int


class ModelLedger:
    """Independent integer accounting over the immutable vendor fill stream."""

    def __init__(self, trade_writer: Any | None = None):
        self.realized_gross_micro = 0
        self.realized_fee_micro = 0
        self.positions: dict[str, LedgerPosition] = {}
        self.trade_writer = trade_writer
        self.trade_rows: list[dict[str, Any]] = []
        self.current_group: dict[str, Any] | None = None
        self.eof_group_row: dict[str, Any] | None = None
        self.group_sequence = 0
        self.peak_equity_micro = 0
        self.max_drawdown_micro = 0
        self.mtm_ticks = 0
        self.max_open_count = 0
        self.max_pending_count = 0
        self.min_group_mae_micro: int | None = None

    def _emit_group(self, row: dict[str, Any]) -> None:
        payload = _canonical_json(row)
        if self.trade_writer is not None:
            self.trade_writer.write(payload.decode("utf-8"))
        else:
            self.trade_rows.append(row)

    def _new_group(self, fill: dict[str, Any]) -> None:
        self.group_sequence += 1
        self.current_group = {
            "group_sequence": self.group_sequence,
            "group_id": f"MODEL-G{self.group_sequence:06d}",
            "opened_source_row_sequence": fill["source_row_sequence"],
            "opened_raw_time": fill["raw_time"],
            "opened_epoch_ms_assumed_utc_provisional": fill["assumed_utc_epoch_ms_provisional"],
            "gross_at_open_micro": self.realized_gross_micro,
            "fee_at_open_micro": self.realized_fee_micro,
            "position_ids": set(),
            "entry_roles_steps": Counter(),
            "exit_reasons": Counter(),
            "entry_fill_count": 0,
            "exit_fill_count": 0,
            "min_net_liquidation_micro": 0,
            "minimum_source_row_sequence": fill["source_row_sequence"],
            "minimum_raw_time": fill["raw_time"],
        }

    def _fee_micro(self, volume_steps: int) -> int:
        return FEE_PER_LOT_USD_MICRO * volume_steps // VOLUME_STEP_DIVISOR

    def apply_fills(self, fills: list[dict[str, Any]]) -> None:
        for fill in fills:
            kind = fill["fill_kind"]
            position_id = fill["position_id"]
            steps = int(fill["volume_steps"])
            before = int(fill["position_volume_before_steps"])
            after = int(fill["position_volume_after_steps"])
            price = int(fill["fill_price_micro"])
            if steps <= 0:
                raise RuntimeError("vendor emitted nonpositive fill volume")
            if kind in {"entry", "resting_stop"}:
                existing = self.positions.get(position_id)
                if before == 0:
                    if existing is not None:
                        raise RuntimeError("entry fill reuses open position id with zero before volume")
                    if self.current_group is None:
                        self._new_group(fill)
                    average = price
                    role = str(fill["position_role"])
                    side = str(fill["side"])
                else:
                    if existing is None or existing.volume_steps != before:
                        raise RuntimeError("entry fill volume does not reconcile with ledger position")
                    if existing.side != fill["side"] or existing.role != fill["position_role"]:
                        raise RuntimeError("entry fill changed a live position's side or role")
                    total = before + steps
                    average = (existing.average_entry_micro * before + price * steps + total // 2) // total
                    role = existing.role
                    side = existing.side
                if after != before + steps:
                    raise RuntimeError("entry fill volume delta mismatch")
                self.positions[position_id] = LedgerPosition(side, role, after, average)
                assert self.current_group is not None
                self.current_group["position_ids"].add(position_id)
                self.current_group["entry_roles_steps"][role] += steps
                self.current_group["entry_fill_count"] += 1
            elif kind == "exit":
                position = self.positions.get(position_id)
                if position is None or position.volume_steps != before:
                    raise RuntimeError("exit fill lacks matching open position volume")
                if after != before - steps or after < 0:
                    raise RuntimeError("exit fill volume delta mismatch")
                gross = (price - position.average_entry_micro) * steps if position.side == "BUY" else (position.average_entry_micro - price) * steps
                fee = self._fee_micro(steps)
                self.realized_gross_micro += gross
                self.realized_fee_micro += fee
                if self.current_group is None:
                    raise RuntimeError("exit fill has no flat-to-flat group")
                self.current_group["exit_reasons"][str(fill["reason"])] += 1
                self.current_group["exit_fill_count"] += 1
                if after == 0:
                    del self.positions[position_id]
                else:
                    position.volume_steps = after
            else:
                raise RuntimeError(f"unknown immutable vendor fill kind: {kind}")

    def mark_micro(self, tick: Any) -> tuple[int, int]:
        unrealized = 0
        fee_reserve = 0
        for pos in self.positions.values():
            exit_price = tick.bid_micro if pos.side == "BUY" else tick.ask_micro
            gross = (exit_price - pos.average_entry_micro) * pos.volume_steps if pos.side == "BUY" else (pos.average_entry_micro - exit_price) * pos.volume_steps
            unrealized += gross
            fee_reserve += self._fee_micro(pos.volume_steps)
        return unrealized, fee_reserve

    def equity_micro(self, tick: Any) -> tuple[int, int, int]:
        unrealized, fee_reserve = self.mark_micro(tick)
        net = self.realized_gross_micro - self.realized_fee_micro + unrealized - fee_reserve
        return net, unrealized, fee_reserve

    def update_tick(self, tick: Any, engine: Any) -> dict[str, Any]:
        net, unrealized, fee_reserve = self.equity_micro(tick)
        self.peak_equity_micro = max(self.peak_equity_micro, net)
        self.max_drawdown_micro = max(self.max_drawdown_micro, self.peak_equity_micro - net)
        self.mtm_ticks += 1
        pending_count = len(engine.pending_orders) + (1 if engine.pending_stop is not None else 0)
        self.max_open_count = max(self.max_open_count, len(engine.inventory))
        self.max_pending_count = max(self.max_pending_count, pending_count)
        if self.current_group is not None:
            group = self.current_group
            group_realized_gross = self.realized_gross_micro - group["gross_at_open_micro"]
            group_fees = self.realized_fee_micro - group["fee_at_open_micro"]
            group_mark = 0
            group_reserve = 0
            for position_id in group["position_ids"]:
                pos = self.positions.get(position_id)
                if pos is None:
                    continue
                side_exit = tick.bid_micro if pos.side == "BUY" else tick.ask_micro
                group_mark += ((side_exit - pos.average_entry_micro) if pos.side == "BUY" else (pos.average_entry_micro - side_exit)) * pos.volume_steps
                group_reserve += self._fee_micro(pos.volume_steps)
            group_equity = group_realized_gross - group_fees + group_mark - group_reserve
            if group_equity < group["min_net_liquidation_micro"]:
                group["min_net_liquidation_micro"] = group_equity
                group["minimum_source_row_sequence"] = tick.source_row_sequence
                group["minimum_raw_time"] = tick.raw_time_label
            if not engine.inventory and pending_count == 0:
                if self.positions:
                    raise RuntimeError("flat group settlement has residual ledger positions")
                if engine.close_batch is not None:
                    raise RuntimeError("flat group settlement has a live close batch")
                if engine._reserved_position_slots() != 0:
                    raise RuntimeError("flat group settlement has reserved position slots")
                self._close_group(tick, group, net, unrealized, fee_reserve)
        return {
            "net_equity_micro": net,
            "unrealized_gross_micro": unrealized,
            "open_fee_reserve_micro": fee_reserve,
            "open_count": len(engine.inventory),
            "pending_count": pending_count,
        }

    def _close_group(self, tick: Any, group: dict[str, Any], net: int, unrealized: int, reserve: int) -> None:
        row = {
            "group_sequence": group["group_sequence"],
            "group_id": group["group_id"],
            "closed": True,
            "opened_source_row_sequence": group["opened_source_row_sequence"],
            "opened_raw_time": group["opened_raw_time"],
            "opened_epoch_ms_assumed_utc_provisional": group["opened_epoch_ms_assumed_utc_provisional"],
            "closed_source_row_sequence": tick.source_row_sequence,
            "closed_raw_time": tick.raw_time_label,
            "closed_epoch_ms_assumed_utc_provisional": tick.assumed_utc_epoch_ms,
            "realized_gross_usd": _usd(self.realized_gross_micro - group["gross_at_open_micro"]),
            "realized_fee_usd": _usd(self.realized_fee_micro - group["fee_at_open_micro"]),
            "realized_net_usd": _usd((self.realized_gross_micro - group["gross_at_open_micro"]) - (self.realized_fee_micro - group["fee_at_open_micro"])),
            "mae_net_liquidation_usd": _usd(group["min_net_liquidation_micro"]),
            "mae_source_row_sequence": group["minimum_source_row_sequence"],
            "mae_raw_time": group["minimum_raw_time"],
            "entry_roles_steps": dict(sorted(group["entry_roles_steps"].items())),
            "exit_reasons": dict(sorted(group["exit_reasons"].items())),
            "entry_fill_count": group["entry_fill_count"],
            "exit_fill_count": group["exit_fill_count"],
            "terminal_unrealized_gross_usd": _usd(unrealized),
            "terminal_open_fee_reserve_usd": _usd(reserve),
            "terminal_net_equity_usd": _usd(net),
        }
        self.min_group_mae_micro = min(self.min_group_mae_micro, group["min_net_liquidation_micro"]) if self.min_group_mae_micro is not None else group["min_net_liquidation_micro"]
        self._emit_group(row)
        self.current_group = None

    def finish(self, last_tick: Any | None, engine: Any) -> dict[str, Any] | None:
        if self.current_group is None or last_tick is None:
            return None
        net, unrealized, reserve = self.equity_micro(last_tick)
        group = self.current_group
        group_realized_gross = self.realized_gross_micro - group["gross_at_open_micro"]
        group_fees = self.realized_fee_micro - group["fee_at_open_micro"]
        group_mark = 0
        group_reserve = 0
        for position_id in group["position_ids"]:
            pos = self.positions.get(position_id)
            if pos is None:
                continue
            side_exit = last_tick.bid_micro if pos.side == "BUY" else last_tick.ask_micro
            group_mark += ((side_exit - pos.average_entry_micro) if pos.side == "BUY" else (pos.average_entry_micro - side_exit)) * pos.volume_steps
            group_reserve += self._fee_micro(pos.volume_steps)
        group_equity = group_realized_gross - group_fees + group_mark - group_reserve
        if group_equity < group["min_net_liquidation_micro"]:
            group["min_net_liquidation_micro"] = group_equity
            group["minimum_source_row_sequence"] = last_tick.source_row_sequence
            group["minimum_raw_time"] = last_tick.raw_time_label
        row = {
            "group_sequence": group["group_sequence"],
            "group_id": group["group_id"],
            "closed": False,
            "terminal_reason": "EOF_UNCLOSED; no synthetic close",
            "opened_source_row_sequence": group["opened_source_row_sequence"],
            "opened_raw_time": group["opened_raw_time"],
            "opened_epoch_ms_assumed_utc_provisional": group["opened_epoch_ms_assumed_utc_provisional"],
            "eof_source_row_sequence": last_tick.source_row_sequence,
            "eof_raw_time": last_tick.raw_time_label,
            "eof_epoch_ms_assumed_utc_provisional": last_tick.assumed_utc_epoch_ms,
            "realized_gross_usd": _usd(self.realized_gross_micro - group["gross_at_open_micro"]),
            "realized_fee_usd": _usd(self.realized_fee_micro - group["fee_at_open_micro"]),
            "realized_net_usd": _usd((self.realized_gross_micro - group["gross_at_open_micro"]) - (self.realized_fee_micro - group["fee_at_open_micro"])),
            "terminal_unrealized_gross_usd": _usd(unrealized),
            "terminal_open_fee_reserve_usd": _usd(reserve),
            "terminal_net_equity_usd": _usd(net),
            "mae_net_liquidation_usd": _usd(group["min_net_liquidation_micro"]),
            "mae_source_row_sequence": group["minimum_source_row_sequence"],
            "mae_raw_time": group["minimum_raw_time"],
            "entry_roles_steps": dict(sorted(group["entry_roles_steps"].items())),
            "exit_reasons": dict(sorted(group["exit_reasons"].items())),
            "entry_fill_count": group["entry_fill_count"],
            "exit_fill_count": group["exit_fill_count"],
        }
        self._emit_group(row)
        self.eof_group_row = row
        return row

    def summary(self) -> dict[str, Any]:
        return {
            "realized_gross_usd": _usd(self.realized_gross_micro),
            "realized_fee_usd": _usd(self.realized_fee_micro),
            "realized_net_usd": _usd(self.realized_gross_micro - self.realized_fee_micro),
            "open_positions_in_ledger": len(self.positions),
            "open_volume_steps_in_ledger": sum(pos.volume_steps for pos in self.positions.values()),
            "max_open_count_all_ticks": self.max_open_count,
            "max_pending_count_all_ticks": self.max_pending_count,
            "mtm_ticks": self.mtm_ticks,
            "max_drawdown_net_liquidation_usd": _usd(self.max_drawdown_micro),
            "closed_group_count": self.group_sequence - (1 if self.current_group is not None else 0),
            "most_adverse_closed_group_mae_usd": _usd(self.min_group_mae_micro) if self.min_group_mae_micro is not None else None,
            "open_eof_group": self.eof_group_row,
            "curve_similarity_mae_usd": None,
        }


class ReplayHarness:
    """One source row in, one native adapter callback, bounded output out."""

    def __init__(self, output_dir: Path | None = None, common_cutoff_epoch_ms: int | None = None):
        h2_engine, adapter_module, boundary_module, loaded = _load_vendor_modules()
        self.h2_engine = h2_engine
        self.loaded_vendor = loaded
        self.output_dir = output_dir
        self.common_cutoff_epoch_ms = common_cutoff_epoch_ms
        self.event_sink = StreamingJsonlSink(output_dir)
        self.trade_stream = (output_dir / "trade_ledger.jsonl").open("x", encoding="utf-8", newline="") if output_dir is not None else None
        self.equity_stream = (output_dir / "equity_samples.csv").open("x", encoding="utf-8", newline="") if output_dir is not None else None
        self.trace_stream = (output_dir / "action_publication_trace.jsonl").open("x", encoding="utf-8", newline="") if output_dir is not None else None
        self.cutoff_stream = (output_dir / "common_c_snapshot.json").open("x", encoding="utf-8") if output_dir is not None else None
        self.engine = h2_engine.ReplayEngine(self.event_sink, config=h2_engine.ReplayConfig())
        removed_ms = _utc_epoch_ms("2026.10.08", "13:45:46.316")
        loaded_ms = _utc_epoch_ms("2026.10.08", "13:45:46.317")
        ready_ms = _utc_epoch_ms("2026.10.08", "13:45:57.394")
        self.boundary = boundary_module.Boundary(removed_ms=removed_ms, loaded_ms=loaded_ms, ready_ms=ready_ms)
        self.trace_buffer: deque[tuple[Any, ...]] = deque(maxlen=64)
        self.adapter = adapter_module.LifecycleMarketAdapter(
            self.engine,
            boundary=self.boundary,
            mode="L_RESET",
            trace=self.trace_buffer,
        )
        self.ledger = ModelLedger(self.trade_stream)
        self.last_tick: Any | None = None
        self.last_minute: int | None = None
        self.trace_sequence = 0
        self.trace_hash = hashlib.sha256()
        self.last_trace_rows: list[dict[str, Any]] = []
        self.equity_hash = hashlib.sha256()
        self.equity_samples = 0
        self.cutoff_snapshot: dict[str, Any] | None = None
        self.resource_start = time.monotonic()
        self.next_resource_sample = self.resource_start
        self.resource_samples = [{"elapsed_seconds": 0.0, "rss_bytes": _process_rss_bytes()}]
        self.peak_rss_bytes = self.resource_samples[0]["rss_bytes"]
        self.output_limit_peak_bytes = 0
        self.rows_processed = 0
        self.row_envelope_hash = hashlib.sha256()
        self.row_event_count = 0
        self._write_equity_header()
        self.last_sample: dict[str, Any] | None = None

    def _write_equity_header(self) -> None:
        if self.equity_stream is not None:
            writer = csv.writer(self.equity_stream, lineterminator="\n")
            writer.writerow(EQUITY_FIELDS)
            self.equity_stream.flush()

    def _sample_record(self, tick: Any, *, epoch_ms: int | None = None) -> dict[str, Any]:
        account = self.ledger.update_tick(tick, self.engine)
        return {
            "epoch_ms": tick.assumed_utc_epoch_ms if epoch_ms is None else epoch_ms,
            "realized_gross_usd": _usd(self.ledger.realized_gross_micro),
            "realized_fee_usd": _usd(self.ledger.realized_fee_micro),
            "realized_net_usd": _usd(self.ledger.realized_gross_micro - self.ledger.realized_fee_micro),
            "net_equity_usd": _usd(account["net_equity_micro"]),
            "open_count": account["open_count"],
            "pending_count": account["pending_count"],
            "_net_equity_micro": account["net_equity_micro"],
            "_unrealized_gross_micro": account["unrealized_gross_micro"],
            "_open_fee_reserve_micro": account["open_fee_reserve_micro"],
        }

    def _write_equity(self, sample: dict[str, Any]) -> None:
        row = {field: sample[field] for field in EQUITY_FIELDS}
        payload = _canonical_json(row)
        self.equity_hash.update(payload)
        self.equity_samples += 1
        if self.equity_stream is not None:
            writer = csv.writer(self.equity_stream, lineterminator="\n")
            writer.writerow([row[field] for field in EQUITY_FIELDS])

    def _capture_common_cut(self) -> None:
        if self.cutoff_snapshot is not None or self.last_tick is None or self.common_cutoff_epoch_ms is None:
            return
        if self.last_tick.assumed_utc_epoch_ms > self.common_cutoff_epoch_ms:
            return
        net_micro, unrealized_micro, reserve_micro = self.ledger.equity_micro(self.last_tick)
        sample = {
            "epoch_ms": self.common_cutoff_epoch_ms,
            "realized_gross_usd": _usd(self.ledger.realized_gross_micro),
            "realized_fee_usd": _usd(self.ledger.realized_fee_micro),
            "realized_net_usd": _usd(self.ledger.realized_gross_micro - self.ledger.realized_fee_micro),
            "net_equity_usd": _usd(net_micro),
            "open_count": len(self.engine.inventory),
            "pending_count": len(self.engine.pending_orders) + (1 if self.engine.pending_stop is not None else 0),
            "_net_equity_micro": net_micro,
            "_unrealized_gross_micro": unrealized_micro,
            "_open_fee_reserve_micro": reserve_micro,
            "_source_row_sequence": self.last_tick.source_row_sequence,
        }
        self.cutoff_snapshot = {
            "cutoff_epoch_ms_inclusive": self.common_cutoff_epoch_ms,
            "quote_source_row_sequence_included": self.last_tick.source_row_sequence,
            "quote_epoch_ms_included": self.last_tick.assumed_utc_epoch_ms,
            "quote_raw_time_included": self.last_tick.raw_time_label,
            "condition": "all rows with assumed_utc_epoch_ms <= C are included; no row after C is included",
            "account": {key: value for key, value in sample.items() if not key.startswith("_")},
            "account_exact_micro": {
                "realized_gross_micro": self.ledger.realized_gross_micro,
                "realized_fee_micro": self.ledger.realized_fee_micro,
                "realized_net_micro": self.ledger.realized_gross_micro - self.ledger.realized_fee_micro,
                "net_equity_micro": sample["_net_equity_micro"],
                "unrealized_gross_micro": sample["_unrealized_gross_micro"],
                "open_fee_reserve_micro": sample["_open_fee_reserve_micro"],
            },
            "engine_state": self.engine.summary(),
            "open_position_ids": sorted(self.engine.inventory),
            "pending_market_order_ids": [item["order_id"] for item in self.engine.pending_orders],
            "pending_stop_order_id": self.engine.pending_stop.order_id if self.engine.pending_stop else None,
        }
        self._write_equity(sample)
        if self.cutoff_stream is not None:
            self.cutoff_stream.write(json.dumps(self.cutoff_snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            self.cutoff_stream.flush()

    def _drain_trace(self, source_sequence: int) -> None:
        if len(self.trace_buffer) >= self.trace_buffer.maxlen:
            raise RuntimeError("adapter trace deque reached capacity before per-row drain")
        current_rows: list[dict[str, Any]] = []
        for item in self.trace_buffer:
            self.trace_sequence += 1
            row = {
                "trace_sequence": self.trace_sequence,
                "source_row_sequence": source_sequence,
                "phase": str(item[0]),
                "event_tuple": list(item),
                "source_row_sequence_in_tuple": item[1] if len(item) > 1 and isinstance(item[1], int) else None,
                "model_event_epoch_ms_assumed_utc_provisional": item[2] if len(item) > 2 and isinstance(item[2], int) else None,
            }
            payload = _canonical_json(row)
            self.trace_hash.update(payload)
            current_rows.append(row)
            if self.trace_stream is not None:
                self.trace_stream.write(payload.decode("utf-8"))
        self.last_trace_rows = current_rows
        self.trace_buffer.clear()

    def _observe_limits(self) -> None:
        now = time.monotonic()
        elapsed = now - self.resource_start
        if elapsed >= MAX_RUNTIME_SECONDS:
            raise RunLimitError(f"runtime exceeded {MAX_RUNTIME_SECONDS} seconds")
        if now >= self.next_resource_sample:
            rss = _process_rss_bytes()
            self.peak_rss_bytes = max(self.peak_rss_bytes, rss)
            self.resource_samples.append({"elapsed_seconds": round(elapsed, 3), "rss_bytes": rss})
            self.next_resource_sample = now + OBSERVATION_SECONDS
            if rss > MAX_RSS_BYTES:
                raise RunLimitError(f"RSS exceeded {MAX_RSS_BYTES} bytes")
            if self.output_dir is not None:
                total = sum(p.stat().st_size for p in self.output_dir.iterdir() if p.is_file())
                self.output_limit_peak_bytes = max(self.output_limit_peak_bytes, total)
                if total > MAX_OUTPUT_BYTES:
                    raise RunLimitError(f"output exceeded {MAX_OUTPUT_BYTES} bytes")

    def consume(self, tick: Any, *, after_adapter: Any | None = None) -> None:
        self._observe_limits()
        if self.common_cutoff_epoch_ms is not None and tick.assumed_utc_epoch_ms > self.common_cutoff_epoch_ms:
            self._capture_common_cut()
        self.event_sink.begin_row()
        self.trace_buffer.clear()
        before_fill_total = self.event_sink.row_counts["fill"]
        self.adapter.on_tick(tick)
        if after_adapter is not None:
            after_adapter(self, tick)
        self.ledger.apply_fills(self.event_sink.current_fills)
        engine_positions = {key: (pos.side, pos.role, pos.volume_steps, pos.entry_price_micro) for key, pos in self.engine.inventory.items()}
        ledger_positions = {key: (pos.side, pos.role, pos.volume_steps, pos.average_entry_micro) for key, pos in self.ledger.positions.items()}
        if engine_positions != ledger_positions:
            raise RuntimeError("independent ledger open position identity/volume/average does not match vendor engine")
        row_sample = self._sample_record(tick)
        row_sample["_source_row_sequence"] = tick.source_row_sequence
        self.last_sample = row_sample
        self.rows_processed += 1
        self.row_event_count += self.event_sink.current_event_count
        envelope = {
            "source_row_sequence": tick.source_row_sequence,
            "raw_time": tick.raw_time_label,
            "assumed_utc_epoch_ms_provisional": tick.assumed_utc_epoch_ms,
            "flags": tick.flags,
            "bid_micro": tick.bid_micro,
            "ask_micro": tick.ask_micro,
            "carried_sides": list(tick.carried_sides),
            "vendor_event_count": self.event_sink.current_event_count,
            "fill_count_delta": self.event_sink.row_counts["fill"] - before_fill_total,
            "state": self.engine._state_snapshot(),
        }
        self.row_envelope_hash.update(_canonical_json(envelope))
        self._drain_trace(tick.source_row_sequence)
        minute = tick.assumed_utc_epoch_ms // 60_000
        if self.last_minute != minute or self.event_sink.current_event_count:
            self._write_equity(row_sample)
        self.last_minute = minute
        self.last_tick = tick

    def checkpoint(self) -> dict[str, Any]:
        state_bytes = json.dumps(
            self.engine.diagnostic_state_snapshot(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
        return {
            "rows_processed": self.rows_processed,
            "event_streams": self.event_sink.digest_summary(),
            "trace_rows": self.trace_sequence,
            "trace_sha256": self.trace_hash.hexdigest(),
            "equity_samples": self.equity_samples,
            "equity_samples_sha256": self.equity_hash.hexdigest(),
            "row_envelope_sha256": self.row_envelope_hash.hexdigest(),
            "engine_state_sha256": hashlib.sha256(state_bytes).hexdigest(),
            "ledger_realized_net_usd": _usd(self.ledger.realized_gross_micro - self.ledger.realized_fee_micro),
        }

    def finish(self, source_receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.common_cutoff_epoch_ms is not None and self.cutoff_snapshot is None and self.last_tick is not None:
            self._capture_common_cut()
        eof_group = self.ledger.finish(self.last_tick, self.engine)
        if self.last_tick is not None:
            final_sample = self.last_sample
            if final_sample is None or final_sample.get("_source_row_sequence") != self.last_tick.source_row_sequence:
                raise RuntimeError("EOF sample is missing the final processed source row")
            self._write_equity(final_sample)
        self.event_sink.flush()
        if self.equity_stream is not None:
            self.equity_stream.flush()
        if self.trace_stream is not None:
            self.trace_stream.flush()
        if self.trade_stream is not None:
            self.trade_stream.flush()
        if self.cutoff_stream is not None:
            self.cutoff_stream.flush()
        engine_summary = self.engine.summary()
        summary = {
            "identity": IDENTITY,
            "status": "PASS_SYNTHETIC_OR_STREAM_COMPLETE",
            "design_sha256": DESIGN_SHA256,
            "source_rows_processed": self.rows_processed,
            "source_last_row_sequence": self.last_tick.source_row_sequence if self.last_tick is not None else None,
            "source_last_raw_time": self.last_tick.raw_time_label if self.last_tick is not None else None,
            "source_last_epoch_ms_assumed_utc_provisional": self.last_tick.assumed_utc_epoch_ms if self.last_tick is not None else None,
            "source_receipt": source_receipt,
            "source_sha256": source_receipt.get("source_sha256_read_once") if source_receipt else None,
            "normalized_quote_sha256": source_receipt.get("normalized_quote_sha256") if source_receipt else None,
            "normalized_quote_prefix_le_C_sha256": source_receipt.get("normalized_quote_prefix_le_C_sha256") if source_receipt else None,
            "normalized_quote_prefix_le_C_rows": source_receipt.get("normalized_quote_prefix_le_C_rows") if source_receipt else None,
            "actual_clocks": {field: None for field in ACTUAL_CLOCK_FIELDS},
            "modeled_clock": "raw timestamp labels provisionally assumed UTC; decision available/cutoff at same assumed row event time",
            "configuration": {
                "lifecycle_mode": "L_RESET",
                "activation_epoch_ms_assumed_utc": self.engine.config.activation_epoch_ms,
                "removed_epoch_ms_assumed_utc": self.boundary.removed_ms,
                "loaded_epoch_ms_assumed_utc": self.boundary.loaded_ms,
                "ready_epoch_ms_assumed_utc": self.boundary.ready_ms,
                "market_latency_ms_strict_later": self.engine.config.market_latency_ms,
                "fee_per_lot_roundtrip_usd": "11.000000",
                "fee_charge": "once on each closed volume step; open volume fee is reserved in net liquidation mark",
                "contract_size_assumed": self.engine.config.assumed_contract_size,
                "point_micro": self.engine.config.point_micro,
                "position_cap_union": self.engine.config.max_positions,
                "no_eod_force_close": True,
                "no_R00_freshness_ttl_retry": True,
            },
            "cutoff": self.cutoff_snapshot,
            "eof_state": {
                "state": engine_summary["state_final"],
                "open_positions": engine_summary["open_positions_final"],
                "open_volume_steps": engine_summary["open_volume_steps_final"],
                "reserved_position_slots": engine_summary["reserved_position_slots_final"],
                "pending_market_orders": engine_summary["pending_market_orders_final"],
                "pending_stop": engine_summary["pending_stop_final"],
                "close_batch": engine_summary["close_batch_final"],
                "execution_degraded": engine_summary["execution_degraded_final"],
                "open_position_ids": sorted(self.engine.inventory),
                "pending_market_order_ids": [item["order_id"] for item in self.engine.pending_orders],
                "pending_stop_order_id": self.engine.pending_stop.order_id if self.engine.pending_stop else None,
                "forced_close": False,
                "unfinished_trade_group": eof_group,
            },
            "vendor_engine_summary": engine_summary,
            "accounting": self.ledger.summary(),
            "event_streams": self.event_sink.digest_summary(),
            "action_publication_trace": {
                "rows": self.trace_sequence,
                "sha256": self.trace_hash.hexdigest(),
                "buffer_capacity": self.trace_buffer.maxlen,
                "drained_after_each_source_row": True,
            },
            "row_envelope": {"rows": self.rows_processed, "sha256": self.row_envelope_hash.hexdigest()},
            "equity_samples": {"rows": self.equity_samples, "sha256": self.equity_hash.hexdigest(), "fields": list(EQUITY_FIELDS)},
            "all_tick_metrics": {
                "mark_to_market_ticks": self.ledger.mtm_ticks,
                "max_drawdown_net_liquidation_usd": _usd(self.ledger.max_drawdown_micro),
                "max_open_count": self.ledger.max_open_count,
                "max_pending_count": self.ledger.max_pending_count,
                "closed_group_count": self.ledger.summary()["closed_group_count"],
                "most_adverse_closed_group_mae_usd": self.ledger.summary()["most_adverse_closed_group_mae_usd"],
            },
            "resource": {
                "elapsed_seconds": round(time.monotonic() - self.resource_start, 3),
                "rss_peak_bytes": self.peak_rss_bytes,
                "rss_samples": self.resource_samples,
                "output_size_peak_bytes_observed": self.output_limit_peak_bytes,
                "limits": {"runtime_seconds": MAX_RUNTIME_SECONDS, "rss_bytes": MAX_RSS_BYTES, "output_bytes": MAX_OUTPUT_BYTES, "sample_interval_seconds": OBSERVATION_SECONDS, "workers": 1, "retry": 0},
            },
            "limitations": [
                "source timezone, actual release/ingested/available/cutoff, and feed delay are unknown; UTC is provisional",
                "commission, swap, external ACK/fill semantics, and loaded runtime configuration are not independently established",
                "curve similarity MAE is calculated by the separate demo overlay; no claim is made here",
                "mechanics baseline only; not EA equivalence, improvement, ranking, or live readiness",
            ],
        }
        return summary

    def close(self) -> None:
        self.event_sink.close()
        for stream in (self.trade_stream, self.equity_stream, self.trace_stream, self.cutoff_stream):
            if stream is not None and not stream.closed:
                stream.close()


def _resolve_source_identity(source_bind: dict[str, Any]) -> SourceIdentity:
    if source_bind.get("identity") != IDENTITY:
        raise NativeInputError("source scope bind identity mismatch")
    if source_bind.get("design_sha256") != DESIGN_SHA256:
        raise NativeInputError("source scope bind design hash mismatch")
    if source_bind.get("source_sha256") != SOURCE_SHA256 or source_bind.get("source_bytes") != SOURCE_BYTES or source_bind.get("source_rows") != SOURCE_ROWS:
        raise NativeInputError("source scope bind is not the frozen full source identity")
    relative = source_bind.get("source_path")
    if not isinstance(relative, str) or not relative.startswith("work/discovery_v004/inputs/"):
        raise NativeInputError("source scope bind path is outside the fixed v004 input")
    path = WORKSPACE / relative
    expected = Path(os.path.abspath(str(path)))
    if os.path.normcase(str(path.absolute())) != os.path.normcase(str(expected)):
        raise NativeInputError("source path lexical identity mismatch")
    return SourceIdentity(path=expected, bytes_expected=SOURCE_BYTES, rows_expected=SOURCE_ROWS, sha256_expected=SOURCE_SHA256)


def _check_launch_go(path: Path, main_sha256: str) -> tuple[dict[str, Any], int]:
    receipt = _json_read(path)
    expected = {
        "identity": IDENTITY,
        "status": "GO_PARENT_AUTHORIZED_MARKET_REPLAY",
        "design_sha256": DESIGN_SHA256,
        "source_sha256": SOURCE_SHA256,
        "main_code_sha256": main_sha256,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise NativeInputError(f"parent launch receipt mismatch at {key}")
    cutoff = receipt.get("common_cutoff_utc_epoch_ms")
    if type(cutoff) is not int:
        raise NativeInputError("launch receipt must provide a frozen integer common cutoff")
    if cutoff > _utc_epoch_ms("2026.10.08", "16:01:59.924"):
        raise NativeInputError("common C exceeds the bound source end")
    return receipt, cutoff


def run_market_replay(launch_go_path: Path, output_dir: Path) -> dict[str, Any]:
    """One authorized source pass. This function does not create launch authority."""
    main_sha256 = _sha256_path(Path(__file__))
    launch_go, cutoff = _check_launch_go(launch_go_path, main_sha256)
    source_bind = _json_read(SOURCE_BIND_PATH)
    source = _resolve_source_identity(source_bind)
    if DESIGN_PATH.exists() and _sha256_path(DESIGN_PATH) != DESIGN_SHA256:
        raise NativeInputError("frozen design document SHA256 mismatch")
    output_root = WORKSPACE / "outputs" / "discovery_v005" / "main"
    target = Path(os.path.abspath(str(output_dir)))
    root_prefix = os.path.normcase(str(Path(os.path.abspath(str(output_root))) ) + os.sep)
    if not os.path.normcase(str(target)).startswith(root_prefix):
        raise NativeInputError("main output path must be a new directory under outputs/discovery_v005/main")
    target.parent.mkdir(parents=True, exist_ok=True)
    harness = ReplayHarness(target, cutoff)
    harness_started = True
    reader = NativeSourceReader(source, cutoff)
    failure: BaseException | None = None
    try:
        for tick in reader:
            harness.consume(tick)
        summary = harness.finish(reader.receipt)
        summary["launch_go_receipt"] = launch_go
        summary["main_code_sha256"] = main_sha256
        summary["loaded_vendor"] = harness.loaded_vendor
        if harness.rows_processed != SOURCE_ROWS:
            raise NativeInputError("runner did not consume every bound source row")
        if reader.receipt.get("source_sha256_read_once") != SOURCE_SHA256:
            raise NativeInputError("one-pass source hash did not match the parent source bind")
        harness.close()
        output_bytes = sum(p.stat().st_size for p in target.iterdir() if p.is_file())
        if output_bytes > MAX_OUTPUT_BYTES:
            raise RunLimitError(f"completed output exceeds {MAX_OUTPUT_BYTES} bytes")
        summary["resource"]["output_size_final_bytes"] = output_bytes
        (target / "source_receipt.json").write_text(json.dumps(reader.receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (target / "main_summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return summary
    except BaseException as exc:
        failure = exc
        try:
            harness.close()
        finally:
            failure_receipt = {
                "identity": IDENTITY,
                "status": "INVALID_OR_INCOMPLETE_PRESERVED_NO_RETRY",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "rows_processed": harness.rows_processed,
                "source_receipt_partial": reader.receipt,
                "main_code_sha256": main_sha256,
                "loaded_vendor": harness.loaded_vendor,
                "launch_go_receipt": launch_go,
            }
            (target / "main_failure.json").write_text(json.dumps(failure_receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        raise
    finally:
        if harness_started:
            harness.close()


def _main() -> int:
    parser = argparse.ArgumentParser(description="One-pass frozen native baseline wrapper; requires parent GO receipt")
    parser.add_argument("--launch-go", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run_market_replay(args.launch_go, args.output_dir)
    except Exception as exc:
        print(json.dumps({"status": "FAILED_PRESERVED_NO_RETRY", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": report["status"], "source_rows_processed": report["source_rows_processed"], "output_dir": str(args.output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
