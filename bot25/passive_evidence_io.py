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
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _archive_schema_mismatch(path: Path, fields: list[str]) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("r", newline="", encoding="utf-8") as handle:
        observed = next(csv.reader(handle), [])
    if observed == fields:
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
