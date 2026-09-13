"""Causal M15-compression/M5-release Long overlay with NY terminal guards."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd


POLICY_ID = "m15_compression_release_long_only_ny1400_1600_h45_hard1650_v001"
POLICY_PARAMS = {
    "timezone": "America/New_York",
    "entry_start": "14:00",
    "entry_end_exclusive": "16:05",
    "last_completed_m5_release": "16:00",
    "hard_flat": "16:50",
    "hold_minutes": 45,
    "compression_ratio": 0.60,
    "compression_reference_m15_bars": 4,
    "release_valid_m5_bars": 3,
    "direction": "LONG_ONLY",
}
POLICY_PARAMS_HASH = hashlib.sha256(
    json.dumps(POLICY_PARAMS, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
NY = ZoneInfo(POLICY_PARAMS["timezone"])


def _as_utc(value: datetime | pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("M15 terminal clock requires a timezone-aware timestamp")
    return stamp.tz_convert("UTC")


def _ny_minutes(value: datetime | pd.Timestamp) -> int:
    local = _as_utc(value).tz_convert(NY)
    return int(local.hour) * 60 + int(local.minute)


def in_entry_window(value: datetime | pd.Timestamp) -> bool:
    """True for NY [14:00, 16:05); completed M5 releases end at 16:00."""
    minute = _ny_minutes(value)
    return 14 * 60 <= minute < 16 * 60 + 5


def hard_flat_due_at(entry_time: datetime | pd.Timestamp) -> pd.Timestamp:
    """Return NY 16:50 on the local calendar date of a confirmed fill."""
    local = _as_utc(entry_time).tz_convert(NY)
    return local.normalize().replace(hour=16, minute=50).tz_convert("UTC")


def entry_deadline_at(reference_time: datetime | pd.Timestamp) -> pd.Timestamp:
    """Return the exclusive NY 16:05 submit boundary for the local date."""
    local = _as_utc(reference_time).tz_convert(NY)
    return local.normalize().replace(hour=16, minute=5).tz_convert("UTC")


def long_signal_series(bars: pd.DataFrame) -> pd.Series:
    """Return the causal Long pulse aligned to each completed M1 open time."""
    if bars.empty or not {"High", "Low", "Close"}.issubset(bars.columns):
        return pd.Series(0, index=bars.index, dtype="int8")
    frame = bars.copy()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    close = frame["Close"].astype(float)
    source = pd.DataFrame({"High": high, "Low": low, "Close": close})
    m5 = source.resample("5min", label="left", closed="left").agg(
        High=("High", "max"), Low=("Low", "min"), Close=("Close", "last"),
        Count=("Close", "count"),
    )
    m5.loc[m5["Count"] != 5, ["High", "Low", "Close"]] = float("nan")
    m15 = source.resample("15min", label="left", closed="left").agg(
        High=("High", "max"), Low=("Low", "min"), Count=("Close", "count"),
    )
    m15.loc[m15["Count"] != 15, ["High", "Low"]] = float("nan")
    if m5.empty or m15.empty:
        return pd.Series(0, index=frame.index, dtype="int8")
    width = (m15["High"] - m15["Low"]).clip(lower=0.001)
    reference = width.shift(1).rolling(4, min_periods=4).median()
    compressed = width <= 0.60 * reference
    comp_high = m15["High"].where(compressed).copy()
    comp_high.index = comp_high.index + pd.Timedelta(minutes=15)
    m5_clock = m5.index + pd.Timedelta(minutes=5)
    comp_high = comp_high.reindex(m5_clock, method="ffill", limit=3).set_axis(m5.index)
    state = (m5["Close"] > comp_high).fillna(False).astype("int8")
    state.index = state.index + pd.Timedelta(minutes=5)
    aligned = state.reindex(
        frame.index + pd.Timedelta(minutes=1), method="ffill"
    ).set_axis(frame.index).fillna(0).astype("int8")
    pulse = aligned.where(aligned.ne(aligned.shift(1)), 0)
    return pulse.where(pulse.gt(0), 0).astype("int8")


def latest_long_signal(bars: pd.DataFrame) -> bool:
    """Evaluate only information available after the latest completed M1."""
    if bars.empty:
        return False
    latest = pd.Timestamp(bars.index[-1])
    # A research M5 bar is observable only after all five constituent M1 bars
    # have closed. Polling on minutes 0..3 must never turn a partial live M5
    # aggregate into an earlier signal.
    if latest.second != 0 or latest.microsecond != 0 or latest.minute % 5 != 4:
        return False
    pulse = long_signal_series(bars)
    return bool(not pulse.empty and int(pulse.iloc[-1]) > 0)
