# -*- coding: utf-8 -*-
"""Passive causal state tags for bot23 forward opportunities.

The tagger has no broker, bridge, executor, or order dependency. It writes one
row per already-produced raw ZA opportunity. Outcomes remain in the existing
shadow markout file and are joined later by opportunity_id.
"""

from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc
TAGGER_VERSION = "s27_shadow_state_tagger_v1"

TAG_FIELDS = [
    "timestamp_utc", "tagger_version", "opportunity_id", "symbol",
    "raw_side", "effective_side", "event_time", "release_time", "decision_time",
    "bar_time", "bar_open", "bar_high", "bar_low", "bar_close", "bar_volume",
    "entry_bid", "entry_ask", "spread_price",
    "prior20_high", "prior20_low", "prior20_mid", "prior20_range",
    "swept_high", "swept_low", "rejected_high", "rejected_low",
    "rejection_alignment", "close_location_prior20", "close_to_mid_atr",
    "quote_to_mid_atr", "bar_body", "upper_wick", "lower_wick",
    "body_to_bar_range", "upper_wick_to_bar_range", "lower_wick_to_bar_range",
    "true_range", "atr30", "true_range_to_atr30",
    "activity_ratio_prior30", "activity_percentile_prior_history",
    "ret1", "ret5", "ret10", "ret30",
    "ret1_atr", "ret5_atr", "ret10_atr", "ret30_atr",
    "path_efficiency5", "path_efficiency10", "path_efficiency30",
    "portfolio_positions", "long_positions", "short_positions",
    "side_imbalance", "lane_positions_json", "lane_pending_json",
]


def _utc(value: Any) -> datetime:
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


def _dt_text(value: Any) -> str:
    return _utc(value).isoformat()


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _ratio(numerator: float, denominator: float) -> float | str:
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator <= 0:
        return ""
    return numerator / denominator


def _validate_header(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("r", newline="", encoding="utf-8") as handle:
        observed = next(csv.reader(handle), [])
    if observed != TAG_FIELDS:
        raise RuntimeError(f"shadow state tag CSV schema mismatch: {path}")


def _append_csv(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    _validate_header(path)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TAG_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in TAG_FIELDS})


class ShadowStateTagger:
    """Write causal state descriptors without changing strategy decisions."""

    def __init__(
        self,
        config: dict[str, Any] | None,
        *,
        log_dir: str | os.PathLike[str],
        symbol: str,
    ) -> None:
        cfg = dict(config or {})
        self.enabled = bool(cfg.get("enabled", False))
        self.symbol = str(symbol)
        self.path = Path(log_dir) / str(cfg.get("csv", "s23_shadow_state_tags.csv"))
        self._ids: set[str] = set()
        if self.enabled:
            self._ids = self._read_ids()

    def _read_ids(self) -> set[str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return set()
        _validate_header(self.path)
        with self.path.open("r", newline="", encoding="utf-8") as handle:
            return {str(row.get("opportunity_id") or "") for row in csv.DictReader(handle)} - {""}

    @staticmethod
    def _path_efficiency(closes: list[float], window: int) -> float | str:
        if len(closes) <= window:
            return ""
        path = closes[-(window + 1):]
        travelled = sum(abs(path[index] - path[index - 1]) for index in range(1, len(path)))
        return _ratio(abs(path[-1] - path[0]), travelled)

    def tag_opportunity(
        self,
        opportunity: dict[str, Any],
        *,
        at: Any,
        bars: Any,
        bid: float,
        ask: float,
        context: dict[str, Any] | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        opportunity_id = str(opportunity.get("opportunity_id") or "")
        if not opportunity_id:
            raise ValueError("shadow state tagger requires opportunity_id")
        if opportunity_id in self._ids:
            return False
        if bars is None or len(bars) < 31:
            raise ValueError("shadow state tagger requires at least 31 completed M1 bars")

        frame = bars.iloc[-240:]
        current = frame.iloc[-1]
        history = frame.iloc[:-1]
        prior20 = history.iloc[-20:]
        prior30 = history.iloc[-30:]
        closes = [_finite(value) for value in frame["Close"].tolist()]
        if any(value is None for value in closes):
            raise ValueError("shadow state tagger requires finite closes")
        close_values = [float(value) for value in closes if value is not None]

        open_price = _finite(current.get("Open"))
        high = _finite(current.get("High"))
        low = _finite(current.get("Low"))
        close = _finite(current.get("Close"))
        bid_value = _finite(bid)
        ask_value = _finite(ask)
        if None in {open_price, high, low, close, bid_value, ask_value}:
            raise ValueError("shadow state tagger requires finite OHLC and Bid/Ask")
        assert open_price is not None and high is not None and low is not None and close is not None
        assert bid_value is not None and ask_value is not None
        if ask_value < bid_value or low > high:
            raise ValueError("shadow state tagger received invalid prices")

        prior_high = max(float(value) for value in prior20["High"].tolist())
        prior_low = min(float(value) for value in prior20["Low"].tolist())
        prior_mid = (prior_high + prior_low) / 2.0
        prior_range = prior_high - prior_low
        previous_close = float(history.iloc[-1]["Close"])
        true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        atr30 = _finite(current.get("atr30"))
        if atr30 is None:
            ranges: list[float] = []
            hist_tail = frame.iloc[-31:]
            for index in range(1, len(hist_tail)):
                row = hist_tail.iloc[index]
                prev_close = float(hist_tail.iloc[index - 1]["Close"])
                ranges.append(max(float(row["High"]) - float(row["Low"]), abs(float(row["High"]) - prev_close), abs(float(row["Low"]) - prev_close)))
            atr30 = sum(ranges) / len(ranges) if ranges else 0.0

        bar_range = high - low
        body = abs(close - open_price)
        upper_wick = high - max(open_price, close)
        lower_wick = min(open_price, close) - low
        swept_high = high > prior_high
        swept_low = low < prior_low
        rejected_high = swept_high and close < prior_high
        rejected_low = swept_low and close > prior_low
        effective_side = str(opportunity.get("effective_side") or opportunity.get("side") or "").upper()
        if rejected_low and effective_side == "LONG":
            alignment = "aligned_low_rejection_long"
        elif rejected_high and effective_side == "SHORT":
            alignment = "aligned_high_rejection_short"
        elif rejected_low or rejected_high:
            alignment = "opposed_or_unrouted_rejection"
        else:
            alignment = "no_range_rejection"

        volumes = [_finite(value) for value in prior30.get("Volume", []).tolist()] if "Volume" in prior30 else []
        current_volume = _finite(current.get("Volume"), 0.0) or 0.0
        usable_volumes = [float(value) for value in volumes if value is not None]
        volume_mean = sum(usable_volumes) / len(usable_volumes) if usable_volumes else 0.0
        history_volumes = [_finite(value) for value in history.get("Volume", []).tolist()] if "Volume" in history else []
        usable_history_volumes = [float(value) for value in history_volumes if value is not None]
        activity_percentile = (
            sum(value <= current_volume for value in usable_history_volumes) / len(usable_history_volumes)
            if usable_history_volumes else ""
        )

        returns: dict[int, float | str] = {}
        for window in (1, 5, 10, 30):
            returns[window] = close_values[-1] - close_values[-1 - window] if len(close_values) > window else ""
        context_data = dict(context or {})
        long_positions = int(context_data.get("long_positions") or 0)
        short_positions = int(context_data.get("short_positions") or 0)
        row = {
            "timestamp_utc": _dt_text(at), "tagger_version": TAGGER_VERSION,
            "opportunity_id": opportunity_id, "symbol": self.symbol,
            "raw_side": str(opportunity.get("raw_side") or opportunity.get("side") or "").upper(),
            "effective_side": effective_side,
            "event_time": str(opportunity.get("event_time") or ""),
            "release_time": str(opportunity.get("release_time") or ""),
            "decision_time": str(opportunity.get("decision_time") or _dt_text(at)),
            "bar_time": str(current.name), "bar_open": open_price, "bar_high": high,
            "bar_low": low, "bar_close": close, "bar_volume": current_volume,
            "entry_bid": bid_value, "entry_ask": ask_value, "spread_price": ask_value - bid_value,
            "prior20_high": prior_high, "prior20_low": prior_low, "prior20_mid": prior_mid,
            "prior20_range": prior_range, "swept_high": int(swept_high), "swept_low": int(swept_low),
            "rejected_high": int(rejected_high), "rejected_low": int(rejected_low),
            "rejection_alignment": alignment,
            "close_location_prior20": _ratio(close - prior_low, prior_range),
            "close_to_mid_atr": _ratio(close - prior_mid, atr30),
            "quote_to_mid_atr": _ratio(((bid_value + ask_value) / 2.0) - prior_mid, atr30),
            "bar_body": body, "upper_wick": upper_wick, "lower_wick": lower_wick,
            "body_to_bar_range": _ratio(body, bar_range),
            "upper_wick_to_bar_range": _ratio(upper_wick, bar_range),
            "lower_wick_to_bar_range": _ratio(lower_wick, bar_range),
            "true_range": true_range, "atr30": atr30,
            "true_range_to_atr30": _ratio(true_range, atr30),
            "activity_ratio_prior30": _ratio(current_volume, volume_mean),
            "activity_percentile_prior_history": activity_percentile,
            "ret1": returns[1], "ret5": returns[5], "ret10": returns[10], "ret30": returns[30],
            "ret1_atr": _ratio(float(returns[1]), atr30) if returns[1] != "" else "",
            "ret5_atr": _ratio(float(returns[5]), atr30) if returns[5] != "" else "",
            "ret10_atr": _ratio(float(returns[10]), atr30) if returns[10] != "" else "",
            "ret30_atr": _ratio(float(returns[30]), atr30) if returns[30] != "" else "",
            "path_efficiency5": self._path_efficiency(close_values, 5),
            "path_efficiency10": self._path_efficiency(close_values, 10),
            "path_efficiency30": self._path_efficiency(close_values, 30),
            "portfolio_positions": long_positions + short_positions,
            "long_positions": long_positions, "short_positions": short_positions,
            "side_imbalance": long_positions - short_positions,
            "lane_positions_json": json.dumps(context_data.get("lane_positions", {}), sort_keys=True),
            "lane_pending_json": json.dumps(context_data.get("lane_pending", {}), sort_keys=True),
        }
        _append_csv(self.path, row)
        self._ids.add(opportunity_id)
        return True


__all__ = ["ShadowStateTagger", "TAG_FIELDS", "TAGGER_VERSION"]
