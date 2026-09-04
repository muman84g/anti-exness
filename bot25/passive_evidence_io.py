# -*- coding: utf-8 -*-
"""Durable file helpers for bot25 passive evidence.

This module has no bridge, executor, or order dependency.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc


def utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def dt_text(value: Any) -> str:
    return utc_datetime(value).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"nonfinite JSON constant: {value}")


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle, object_pairs_hook=_strict_json_pairs, parse_constant=_reject_json_constant)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _archive_schema_mismatch(path: Path, fields: list[str]) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    raw = path.read_bytes()
    if raw and not raw.endswith((b"\n", b"\r")):
        raise RuntimeError(f"unterminated CSV row: {path}")
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    observed = rows[0] if rows else []
    if observed == fields:
        if any(len(row) != len(fields) for row in rows[1:]):
            raise RuntimeError(f"malformed CSV row width: {path}")
        return None
    old_dir = path.parent / "old"
    old_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    archived = old_dir / f"{path.stem}_schema_retired_{stamp}{path.suffix}"
    shutil.move(str(path), str(archived))
    return archived.name


def append_durable_csv(path: Path, row: dict[str, Any], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    archived_name = _archive_schema_mismatch(path, fields)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        if archived_name:
            rollover = {
                "timestamp_utc": dt_text(datetime.now(UTC)),
                "event": "schema_rollover",
                "reason": "incompatible_header_archived",
                "route_reason": "incompatible_header_archived",
                "note": f"archive={archived_name}",
            }
            writer.writerow({field: rollover.get(field, "") for field in fields})
        writer.writerow({field: row.get(field, "") for field in fields})
        handle.flush()
        os.fsync(handle.fileno())


def csv_rows(path: Path, fields: list[str]) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    archived_name = _archive_schema_mismatch(path, fields)
    if archived_name:
        append_durable_csv(
            path,
            {
                "timestamp_utc": dt_text(datetime.now(UTC)),
                "event": "schema_rollover",
                "reason": "incompatible_header_archived",
                "route_reason": "incompatible_header_archived",
                "note": f"archive={archived_name}",
            },
            fields,
        )
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
