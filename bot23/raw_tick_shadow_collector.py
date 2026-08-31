# -*- coding: utf-8 -*-
"""Standalone, read-only raw tick shadow collector for bot23.

This module is deliberately not imported by ``live_s23_bot.py``.  It reads
bounded pages through the bridge ``TICKS`` command and appends validated rows
to an audit CSV.  It never sends OPEN, CLOSE, MODIFY, CANCEL, or PENDING.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ea_bridge import ea_bridge


SCHEMA = [
    "recipe_version", "run_id", "batch_id", "source_sequence",
    "event_time", "release_time", "ingested_time", "available_time",
    "cutoff_time", "recorded_at", "broker_time_msc", "bid", "ask",
    "last", "volume", "flags", "next_from_msc", "next_skip_at_from_msc",
]
READ_ONLY_COMMAND = "TICKS"
EXPECTED_RECIPE_VERSION = "s23_raw_tick_shadow_v1"
MAX_TICK_FLAGS = (1 << 32) - 1
EXPECTED_CONFIG_KEYS = {
    "enabled", "recipe_version", "page_rows", "lookback_seconds_on_first_run",
    "max_catchup_seconds_per_run", "csv", "state_file",
}


class Bridge(Protocol):
    def send_command(self, cmd_str: str, timeout: float = 10) -> str: ...


@dataclass(frozen=True)
class Cursor:
    from_msc: int
    skip_at_from_msc: int
    source_sequence: int


@dataclass(frozen=True)
class RecoveryEvidence:
    cursor: Cursor
    run_id: str
    last_available_time: datetime


@dataclass(frozen=True)
class Tick:
    time_msc: int
    bid: float
    ask: float
    last: float
    volume: float
    flags: int


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def load_collector_config(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    params = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_json_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(params, dict) or params.get("mt5_symbol") != "XAUUSD":
        raise ValueError("invalid raw-tick collector symbol contract")
    cfg = params.get("raw_tick_shadow_collector")
    if not isinstance(cfg, dict) or set(cfg) != EXPECTED_CONFIG_KEYS:
        raise ValueError("invalid raw-tick collector config schema")
    if not isinstance(cfg["enabled"], bool):
        raise ValueError("invalid raw-tick collector enabled flag")
    if cfg["recipe_version"] != EXPECTED_RECIPE_VERSION:
        raise ValueError("invalid raw-tick collector recipe identity")
    for key, lower, upper in (
        ("page_rows", 1, 2000),
        ("lookback_seconds_on_first_run", 1, 86400),
        ("max_catchup_seconds_per_run", 1, 86400),
    ):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(f"invalid raw-tick collector integer: {key}={value!r}")
    for key in ("csv", "state_file"):
        value = cfg[key]
        if (
            not isinstance(value, str)
            or not value
            or value != Path(value).name
            or any(char in value for char in ("/", "\\", "\r", "\n"))
        ):
            raise ValueError(f"invalid raw-tick collector output name: {key}={value!r}")
    return params, cfg


def iso_msc(value: int) -> str:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat(timespec="milliseconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_not_before(floor: datetime) -> datetime:
    return max(_utc_now(), floor)


def parse_page(response: str) -> tuple[list[Tick], bool]:
    if not response.startswith("OK|META,"):
        raise RuntimeError(f"raw tick bridge failure: {response[:160]}")
    parts = response.split("|")
    meta = parts[1].split(",")
    if (
        len(meta) != 5
        or meta[0] != "META"
        or any(field != field.strip() for field in meta)
        or any(re.fullmatch(r"[0-9]+", field) is None for field in meta[1:])
    ):
        raise ValueError("malformed TICKS metadata")
    expected = int(meta[1])
    raw_has_more = int(meta[2])
    meta_last_msc = int(meta[3])
    meta_last_count = int(meta[4])
    if (
        raw_has_more not in {0, 1}
        or expected > 2000
        or meta_last_count > expected
        or (raw_has_more == 1 and expected == 0)
    ):
        raise ValueError("invalid TICKS metadata values")
    has_more = bool(raw_has_more)
    rows: list[Tick] = []
    prior_msc = -1
    for raw in parts[2:]:
        fields = raw.split(",")
        if len(fields) != 6 or any(field != field.strip() for field in fields):
            raise ValueError("malformed TICKS row")
        if re.fullmatch(r"[0-9]+", fields[0]) is None or re.fullmatch(r"[0-9]+", fields[5]) is None:
            raise ValueError("malformed TICKS integer field")
        tick = Tick(int(fields[0]), float(fields[1]), float(fields[2]), float(fields[3]), float(fields[4]), int(fields[5]))
        if tick.time_msc < prior_msc:
            raise ValueError("TICKS page is not ordered")
        if (
            tick.time_msc <= 0
            or not all(math.isfinite(value) for value in (tick.bid, tick.ask, tick.last, tick.volume))
            or tick.bid <= 0
            or tick.ask <= 0
            or tick.ask < tick.bid
            or tick.last < 0
            or tick.volume < 0
            or tick.flags < 0
            or tick.flags > MAX_TICK_FLAGS
        ):
            raise ValueError("invalid executable quote")
        prior_msc = tick.time_msc
        rows.append(tick)
    if len(rows) != expected:
        raise ValueError(f"TICKS count mismatch expected={expected} actual={len(rows)}")
    if rows:
        trailing_count = 0
        for tick in reversed(rows):
            if tick.time_msc != rows[-1].time_msc:
                break
            trailing_count += 1
        if meta_last_msc != rows[-1].time_msc or meta_last_count != trailing_count:
            raise ValueError("TICKS metadata does not match emitted rows")
    elif meta_last_msc != 0 or meta_last_count != 0:
        raise ValueError("empty TICKS page has nonzero tail metadata")
    return rows, has_more


def next_cursor(cursor: Cursor, tick: Tick) -> Cursor:
    skip = cursor.skip_at_from_msc + 1 if tick.time_msc == cursor.from_msc else 1
    return Cursor(tick.time_msc, skip, cursor.source_sequence + 1)


def _load_cursor_from_handle(
    handle: Any, *, path: Path, expected_recipe_version: str | None = None,
    checkpoint: RecoveryEvidence | None = None,
) -> RecoveryEvidence | None:
    handle.seek(0)
    reader = csv.reader(handle)
    try:
        header = next(reader)
    except StopIteration:
        return None
    if header != SCHEMA:
        raise RuntimeError(f"raw tick CSV schema mismatch: {path}")

    prior: Cursor | None = None
    recipe_version: str | None = None
    current_run_id: str | None = None
    current_batch_number = 0
    seen_run_ids: set[str] = set()
    current_batch_key: tuple[str, int] | None = None
    current_batch_signature: tuple[datetime, datetime] | None = None
    current_batch_available_time: datetime | None = None
    checkpoint_matched = False
    for line_number, values in enumerate(reader, start=2):
        if len(values) != len(SCHEMA):
            raise ValueError(f"invalid raw-tick CSV row width at line {line_number}")
        row = dict(zip(SCHEMA, values))
        row_recipe = row["recipe_version"]
        if (
            not row_recipe
            or row_recipe != row_recipe.strip()
            or (expected_recipe_version is not None and row_recipe != expected_recipe_version)
            or (recipe_version is not None and row_recipe != recipe_version)
        ):
            raise ValueError(f"invalid raw-tick CSV recipe identity at line {line_number}")
        recipe_version = row_recipe
        if (
            not row["run_id"]
            or row["run_id"] != row["run_id"].strip()
            or any(char in row["run_id"] for char in (",", "\r", "\n"))
            or not row["batch_id"].startswith(row["run_id"] + ":")
            or re.fullmatch(r"[1-9][0-9]*", row["batch_id"][len(row["run_id"]) + 1:]) is None
        ):
            raise ValueError(f"invalid raw-tick CSV batch identity at line {line_number}")
        batch_number = int(row["batch_id"][len(row["run_id"]) + 1:])
        if row["run_id"] != current_run_id:
            if row["run_id"] in seen_run_ids or batch_number != 1:
                raise ValueError(f"invalid raw-tick CSV run transition at line {line_number}")
            seen_run_ids.add(row["run_id"])
            current_run_id = row["run_id"]
            current_batch_number = 1
        elif batch_number not in {current_batch_number, current_batch_number + 1}:
            raise ValueError(f"invalid raw-tick CSV batch sequence at line {line_number}")
        else:
            current_batch_number = batch_number

        integer_fields = (
            "source_sequence", "broker_time_msc", "flags",
            "next_from_msc", "next_skip_at_from_msc",
        )
        if any(re.fullmatch(r"[0-9]+", row[field]) is None for field in integer_fields):
            raise ValueError(f"invalid raw-tick CSV integer at line {line_number}")
        source_sequence = int(row["source_sequence"])
        broker_time_msc = int(row["broker_time_msc"])
        flags = int(row["flags"])
        cursor = Cursor(
            int(row["next_from_msc"]),
            int(row["next_skip_at_from_msc"]),
            source_sequence,
        )
        try:
            bid, ask, last, volume = (
                float(row[field]) for field in ("bid", "ask", "last", "volume")
            )
        except ValueError as exc:
            raise ValueError(f"invalid raw-tick CSV quote at line {line_number}") from exc
        if any(
            not row[field] or row[field] != row[field].strip()
            for field in ("bid", "ask", "last", "volume")
        ):
            raise ValueError(f"invalid raw-tick CSV quote text at line {line_number}")
        try:
            ingested_time = datetime.fromisoformat(row["ingested_time"])
            available_time = datetime.fromisoformat(row["available_time"])
            cutoff_time = datetime.fromisoformat(row["cutoff_time"])
            recorded_at = datetime.fromisoformat(row["recorded_at"])
        except ValueError as exc:
            raise ValueError(f"invalid raw-tick CSV timestamp at line {line_number}") from exc
        timestamps = (ingested_time, available_time, cutoff_time, recorded_at)
        event_time = datetime.fromtimestamp(broker_time_msc / 1000.0, tz=timezone.utc)
        batch_key = (row["run_id"], batch_number)
        batch_signature = (ingested_time, cutoff_time)
        same_batch = batch_key == current_batch_key
        if same_batch:
            if batch_signature != current_batch_signature:
                raise ValueError(f"raw-tick CSV batch evidence mismatch at line {line_number}")
        else:
            current_batch_key = batch_key
            current_batch_signature = batch_signature
        if current_batch_available_time is not None and available_time < current_batch_available_time:
            raise ValueError(f"raw-tick CSV available time reversed at line {line_number}")
        current_batch_available_time = available_time
        if (
            cursor.from_msc <= 0
            or cursor.skip_at_from_msc <= 0
            or cursor.source_sequence <= 0
            or broker_time_msc != cursor.from_msc
            or flags < 0
            or flags > MAX_TICK_FLAGS
            or not all(math.isfinite(value) for value in (bid, ask, last, volume))
            or bid <= 0
            or ask <= 0
            or ask < bid
            or last < 0
            or volume < 0
            or row["event_time"] != iso_msc(broker_time_msc)
            or row["release_time"] != iso_msc(broker_time_msc)
            or any(value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(None) for value in timestamps)
            or not event_time <= cutoff_time <= ingested_time <= available_time
            or recorded_at != available_time
        ):
            raise ValueError(f"invalid raw-tick CSV evidence at line {line_number}")
        if prior is None:
            if source_sequence != 1 or cursor.skip_at_from_msc != 1:
                raise ValueError("raw-tick CSV evidence does not start at sequence/skip 1")
        else:
            expected_skip = (
                prior.skip_at_from_msc + 1
                if broker_time_msc == prior.from_msc else 1
            )
            if (
                source_sequence != prior.source_sequence + 1
                or broker_time_msc < prior.from_msc
                or cursor.skip_at_from_msc != expected_skip
            ):
                raise ValueError(f"raw-tick CSV continuity failure at line {line_number}")
        if checkpoint is not None and source_sequence == checkpoint.cursor.source_sequence:
            actual_checkpoint = RecoveryEvidence(cursor, row["run_id"], available_time)
            if actual_checkpoint != checkpoint:
                raise RuntimeError(
                    "raw tick CSV checkpoint conflicts with persisted state evidence"
                )
            checkpoint_matched = True
        prior = cursor
    if prior is None:
        return None
    assert current_run_id is not None
    assert current_batch_available_time is not None
    if (
        checkpoint is not None
        and checkpoint.cursor.source_sequence <= prior.source_sequence
        and not checkpoint_matched
    ):
        raise RuntimeError("raw tick CSV does not contain persisted state checkpoint")
    return RecoveryEvidence(prior, current_run_id, current_batch_available_time)


def load_csv_recovery_evidence(
    path: Path, *, expected_recipe_version: str | None = None,
    checkpoint: RecoveryEvidence | None = None,
) -> RecoveryEvidence | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("r", encoding="utf-8", newline="") as handle:
        return _load_cursor_from_handle(
            handle, path=path, expected_recipe_version=expected_recipe_version,
            checkpoint=checkpoint,
        )


def load_cursor_from_csv(
    path: Path, *, expected_recipe_version: str | None = None,
) -> Cursor | None:
    evidence = load_csv_recovery_evidence(
        path, expected_recipe_version=expected_recipe_version,
    )
    return evidence.cursor if evidence is not None else None


STATE_KEYS = {
    "schema_version", "recipe_version", "run_id", "from_msc",
    "skip_at_from_msc", "source_sequence", "last_available_time",
}


def load_state_recovery_evidence(
    path: Path, *, expected_recipe_version: str,
) -> RecoveryEvidence | None:
    if not path.exists():
        return None
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_json_pairs,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(payload, dict) or set(payload) != STATE_KEYS:
        raise ValueError("invalid raw-tick state schema")
    if (
        payload["schema_version"] != "s23_raw_tick_collector_state_v1"
        or payload["recipe_version"] != expected_recipe_version
        or not isinstance(payload["run_id"], str)
        or not payload["run_id"]
        or payload["run_id"] != payload["run_id"].strip()
        or any(char in payload["run_id"] for char in (",", "\r", "\n"))
    ):
        raise ValueError("invalid raw-tick state identity")
    integer_values = (
        payload["from_msc"], payload["skip_at_from_msc"],
        payload["source_sequence"],
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values):
        raise ValueError("invalid raw-tick state cursor type")
    cursor = Cursor(*integer_values)
    if cursor.from_msc <= 0 or cursor.skip_at_from_msc <= 0 or cursor.source_sequence <= 0:
        raise ValueError("invalid raw-tick state cursor value")
    if not isinstance(payload["last_available_time"], str):
        raise ValueError("invalid raw-tick state available time")
    try:
        available_time = datetime.fromisoformat(payload["last_available_time"])
    except ValueError as exc:
        raise ValueError("invalid raw-tick state available time") from exc
    if available_time.tzinfo is None or available_time.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("invalid raw-tick state available timezone")
    return RecoveryEvidence(cursor, payload["run_id"], available_time)


def load_state_cursor(path: Path, *, expected_recipe_version: str) -> Cursor | None:
    evidence = load_state_recovery_evidence(
        path, expected_recipe_version=expected_recipe_version,
    )
    return evidence.cursor if evidence is not None else None


def reconcile_recovery_cursors(csv_cursor: Cursor | None, state_cursor: Cursor | None) -> None:
    if state_cursor is None:
        return
    if csv_cursor is None:
        raise RuntimeError("raw tick CSV is missing or empty while state evidence exists")
    if state_cursor.source_sequence > csv_cursor.source_sequence:
        raise RuntimeError("raw tick CSV is behind persisted state evidence")
    if state_cursor.source_sequence == csv_cursor.source_sequence and state_cursor != csv_cursor:
        raise RuntimeError("raw tick CSV cursor conflicts with persisted state evidence")


def reconcile_recovery_evidence(
    csv_evidence: RecoveryEvidence | None,
    state_evidence: RecoveryEvidence | None,
) -> None:
    reconcile_recovery_cursors(
        csv_evidence.cursor if csv_evidence is not None else None,
        state_evidence.cursor if state_evidence is not None else None,
    )
    if (
        csv_evidence is not None
        and state_evidence is not None
        and csv_evidence.cursor.source_sequence == state_evidence.cursor.source_sequence
        and (
            csv_evidence.run_id != state_evidence.run_id
            or csv_evidence.last_available_time != state_evidence.last_available_time
        )
    ):
        raise RuntimeError("raw tick CSV tail identity conflicts with persisted state evidence")


def acquire_collector_lock(path: Path) -> Any | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (OSError, IOError):
        handle.close()
        return None


def collector_lock_path(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.name + ".lock")


def release_collector_lock(handle: Any) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


class RawTickShadowCollector:
    def __init__(self, *, bridge: Bridge, symbol: str, csv_path: Path, state_path: Path,
                 recipe_version: str, page_rows: int = 1000, run_id: str | None = None,
                 available_time_floor: datetime | None = None):
        if isinstance(page_rows, bool) or not isinstance(page_rows, int) or not (1 <= page_rows <= 2000):
            raise ValueError("page_rows must be between 1 and 2000")
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.strip()
            or any(char in symbol for char in ("|", ",", "\r", "\n"))
        ):
            raise ValueError("invalid raw-tick symbol")
        if (
            not isinstance(recipe_version, str)
            or not recipe_version
            or recipe_version != recipe_version.strip()
            or any(char in recipe_version for char in (",", "\r", "\n"))
        ):
            raise ValueError("invalid raw-tick recipe version")
        if run_id is not None and (
            not isinstance(run_id, str)
            or not run_id
            or run_id != run_id.strip()
            or any(char in run_id for char in (",", "\r", "\n"))
        ):
            raise ValueError("invalid raw-tick run ID")
        if (
            available_time_floor is not None
            and (
                available_time_floor.tzinfo is None
                or available_time_floor.utcoffset() != timezone.utc.utcoffset(None)
            )
        ):
            raise ValueError("available_time_floor must be UTC")
        self.bridge = bridge
        self.symbol = symbol
        self.csv_path = csv_path
        self.state_path = state_path
        self.recipe_version = recipe_version
        self.page_rows = page_rows
        self.run_id = run_id if run_id is not None else uuid.uuid4().hex
        self._csv_identity: tuple[int, int, int, int] | None = None
        self._validated_cursor: Cursor | None = None
        self._batch_sequence = 0
        self._last_available_time = available_time_floor

    @staticmethod
    def _file_identity(handle: Any) -> tuple[int, int, int, int]:
        stat = os.fstat(handle.fileno())
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _append(
        self, rows: list[dict[str, Any]], *, expected_cursor: Cursor,
        resulting_cursor: Cursor,
    ) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.csv_path.exists() and self.csv_path.stat().st_size > 0
        mode = "a+" if exists else "w+"
        with self.csv_path.open(mode, encoding="utf-8", newline="") as handle:
            if exists:
                current_identity = self._file_identity(handle)
                if self._csv_identity is None:
                    actual_evidence = _load_cursor_from_handle(
                        handle,
                        path=self.csv_path,
                        expected_recipe_version=self.recipe_version,
                    )
                    actual_cursor = (
                        actual_evidence.cursor if actual_evidence is not None else None
                    )
                else:
                    if current_identity != self._csv_identity:
                        raise RuntimeError("raw tick CSV changed before append")
                    actual_cursor = self._validated_cursor
                empty_csv_start = (
                    actual_cursor is None
                    and expected_cursor.source_sequence == 0
                    and expected_cursor.skip_at_from_msc == 0
                )
                if not empty_csv_start and actual_cursor != expected_cursor:
                    raise RuntimeError("raw tick CSV changed before append")
            handle.seek(0, os.SEEK_END)
            writer = csv.DictWriter(handle, fieldnames=SCHEMA, extrasaction="raise")
            if not exists:
                writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
            self._csv_identity = self._file_identity(handle)
            self._validated_cursor = resulting_cursor

    def collect_until(self, cursor: Cursor, cutoff_msc: int, *, max_pages: int = 1000) -> tuple[Cursor, int]:
        total = 0
        page_count = 0
        while cursor.from_msc <= cutoff_msc and page_count < max_pages:
            page_count += 1
            page_start_cursor = cursor
            command = f"{READ_ONLY_COMMAND}|{self.symbol}|{cursor.from_msc}|{cutoff_msc}|{self.page_rows}|{cursor.skip_at_from_msc}"
            response = self.bridge.send_command(command, timeout=15)
            ticks, has_more = parse_page(response)
            if (
                len(ticks) > self.page_rows
                or any(
                    tick.time_msc < cursor.from_msc or tick.time_msc > cutoff_msc
                    for tick in ticks
                )
            ):
                raise ValueError("TICKS page violates the requested cursor window")
            if not ticks:
                break
            cutoff_time = datetime.fromtimestamp(cutoff_msc / 1000.0, tz=timezone.utc)
            receipt_floor = max(
                cutoff_time,
                self._last_available_time or cutoff_time,
            )
            ingested_time = _utc_now_not_before(receipt_floor)
            ingested = ingested_time.isoformat()
            self._batch_sequence += 1
            batch_id = f"{self.run_id}:{self._batch_sequence}"
            output: list[dict[str, Any]] = []
            prior_available_time = ingested_time
            for tick in ticks:
                cursor = next_cursor(cursor, tick)
                prior_available_time = _utc_now_not_before(prior_available_time)
                available = prior_available_time.isoformat()
                output.append({
                    "recipe_version": self.recipe_version,
                    "run_id": self.run_id,
                    "batch_id": batch_id,
                    "source_sequence": cursor.source_sequence,
                    "event_time": iso_msc(tick.time_msc),
                    "release_time": iso_msc(tick.time_msc),
                    "ingested_time": ingested,
                    "available_time": available,
                    "cutoff_time": iso_msc(cutoff_msc),
                    "recorded_at": available,
                    "broker_time_msc": tick.time_msc,
                    "bid": f"{tick.bid:.10f}",
                    "ask": f"{tick.ask:.10f}",
                    "last": f"{tick.last:.10f}",
                    "volume": f"{tick.volume:.8f}",
                    "flags": tick.flags,
                    "next_from_msc": cursor.from_msc,
                    "next_skip_at_from_msc": cursor.skip_at_from_msc,
                })
            self._append(
                output,
                expected_cursor=page_start_cursor,
                resulting_cursor=cursor,
            )
            self._last_available_time = prior_available_time
            total += len(output)
            save_state(self.state_path, {
                "schema_version": "s23_raw_tick_collector_state_v1",
                "recipe_version": self.recipe_version,
                "run_id": self.run_id,
                "from_msc": cursor.from_msc,
                "skip_at_from_msc": cursor.skip_at_from_msc,
                "source_sequence": cursor.source_sequence,
                "last_available_time": output[-1]["available_time"],
            })
            if not has_more:
                break
        return cursor, total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--params", type=Path, default=Path(__file__).with_name("s23_params.json"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true", help="allow a manual run while config enabled=false")
    args = parser.parse_args()
    params, cfg = load_collector_config(args.params)
    if not cfg.get("enabled", False) and not args.force:
        raise SystemExit("raw tick shadow collector is disabled; use --force for an explicit manual no-order run")
    root = Path(__file__).resolve().parent
    csv_path = root / "logs" / cfg["csv"]
    state_path = root / "state" / cfg["state_file"]
    lock_path = collector_lock_path(csv_path)
    collector_lock = acquire_collector_lock(lock_path)
    if collector_lock is None:
        raise SystemExit(f"raw tick shadow collector is already running: {lock_path}")
    try:
        now_msc = int(time.time() * 1000)
        state_evidence = load_state_recovery_evidence(
            state_path, expected_recipe_version=cfg["recipe_version"],
        )
        csv_evidence = load_csv_recovery_evidence(
            csv_path,
            expected_recipe_version=cfg["recipe_version"],
            checkpoint=state_evidence,
        )
        reconcile_recovery_evidence(csv_evidence, state_evidence)
        csv_cursor = csv_evidence.cursor if csv_evidence is not None else None
        cursor = csv_cursor or Cursor(
            now_msc - int(cfg["lookback_seconds_on_first_run"]) * 1000, 0, 0,
        )
        collector = RawTickShadowCollector(bridge=ea_bridge, symbol=params["mt5_symbol"], csv_path=csv_path,
                                           state_path=state_path, recipe_version=cfg["recipe_version"],
                                           page_rows=int(cfg["page_rows"]),
                                           available_time_floor=(
                                               csv_evidence.last_available_time
                                               if csv_evidence is not None else None
                                           ))
        ea_bridge.start_server()
        while True:
            now_msc = int(time.time() * 1000)
            max_cutoff = cursor.from_msc + int(cfg["max_catchup_seconds_per_run"]) * 1000
            cursor, count = collector.collect_until(cursor, min(now_msc, max_cutoff))
            print(json.dumps({"status": "ok", "rows": count, "cursor": cursor.__dict__}, ensure_ascii=False))
            if args.once:
                return 0
            time.sleep(1.0)
    finally:
        release_collector_lock(collector_lock)


if __name__ == "__main__":
    raise SystemExit(main())
