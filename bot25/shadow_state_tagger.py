# -*- coding: utf-8 -*-
"""Causal completed-M5 and inventory state tags for bot25 opportunities."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from passive_evidence_io import append_durable_csv, csv_rows, dt_text

UTC = timezone.utc
TAGGER_VERSION = "s25_v24_virtual_core_state_tagger_v1"

TAG_FIELDS = [
    "timestamp_utc", "event", "tagger_version", "opportunity_id", "symbol",
    "opportunity_type", "side", "signal_bar_time", "decision_time",
    "bar_open", "bar_high", "bar_low", "bar_close", "bar_volume",
    "bar_range", "bar_range_atr", "bar_body", "body_to_range",
    "upper_wick", "lower_wick", "upper_wick_to_range", "lower_wick_to_range",
    "break_dir", "atr14", "ema200", "mid_to_ema_atr",
    "active_wave", "frontier", "frontier_distance_atr",
    "long_positions", "short_positions", "side_imbalance",
    "episode_id", "episode_age_minutes", "minutes_since_productive_close",
    "inventory_mtm_usd", "core_positions", "satellite_positions",
    "capacity_allowed", "ratio_allowed", "v23_allowed", "execution_allowed",
    "spread_price", "spread_points", "route_status_at_registration", "note",
]


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def build_causal_tag(payload: dict[str, Any]) -> dict[str, Any]:
    high = _finite(payload.get("bar_high"))
    low = _finite(payload.get("bar_low"))
    open_price = _finite(payload.get("bar_open"))
    close = _finite(payload.get("bar_close"))
    atr = _finite(payload.get("atr14"))
    bid = _finite(payload.get("entry_bid"))
    ask = _finite(payload.get("entry_ask"))
    ema = _finite(payload.get("ema200"))
    bar_range = max(0.0, high - low)
    body = abs(close - open_price)
    upper_wick = max(0.0, high - max(open_price, close))
    lower_wick = max(0.0, min(open_price, close) - low)
    mid = 0.5 * (bid + ask)
    return {
        **{field: payload.get(field, "") for field in TAG_FIELDS},
        "timestamp_utc": dt_text(payload.get("registered_at") or datetime.now(UTC)),
        "event": "registered",
        "tagger_version": TAGGER_VERSION,
        "decision_time": dt_text(payload.get("registered_at") or datetime.now(UTC)),
        "bar_range": bar_range,
        "bar_range_atr": bar_range / atr if atr > 0 else "",
        "bar_body": body,
        "body_to_range": body / bar_range if bar_range > 0 else "",
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "upper_wick_to_range": upper_wick / bar_range if bar_range > 0 else "",
        "lower_wick_to_range": lower_wick / bar_range if bar_range > 0 else "",
        "mid_to_ema_atr": (mid - ema) / atr if atr > 0 else "",
        "route_status_at_registration": "registered",
    }


class S25ShadowStateTagger:
    def __init__(self, cfg: dict[str, Any], *, log_dir: str):
        self.enabled = bool(cfg.get("enabled", False))
        self.path = Path(log_dir) / str(cfg.get("csv", "s25_shadow_state_tags.csv"))
        self.opportunity_ids = {
            str(row.get("opportunity_id") or "")
            for row in csv_rows(self.path, TAG_FIELDS)
            if row.get("event") == "registered" and row.get("opportunity_id")
        }

    def record(self, payload: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        opportunity_id = str(payload.get("opportunity_id") or "")
        if not opportunity_id:
            raise ValueError("opportunity_id is required")
        if opportunity_id in self.opportunity_ids:
            return False
        append_durable_csv(self.path, build_causal_tag(payload), TAG_FIELDS)
        self.opportunity_ids.add(opportunity_id)
        return True


__all__ = ["S25ShadowStateTagger", "TAG_FIELDS", "TAGGER_VERSION", "build_causal_tag"]
