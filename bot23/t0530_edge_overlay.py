# -*- coding: utf-8 -*-
"""Frozen NY 05:30 edge-break fade signal used by the bot23 overlay."""

from __future__ import annotations

import hashlib
import json
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np


POLICY_ID = "ny0530_edge_break_fade_w15_h15_cap4_v001"
POLICY_SPEC: dict[str, Any] = {
    "signal_id": "t0530_edge_break_fade",
    "price_basis": "bid_m1_dev_event_parity_with_mid_20260831",
    "edge_lookback_completed_m1_bars": 15,
    "session_timezone": "America/New_York",
    "release_window_start": "05:30",
    "release_window_end": "06:00",
    "onset_only": True,
    "onset_condition_scope": "release_window_and_edge_break",
    "upper_break_action": "SHORT",
    "lower_break_action": "LONG",
    "hold_minutes_from_confirmed_fill": 15,
    "max_signal_delay_minutes": 5,
    "max_positions": 4,
}
POLICY_PARAMS_HASH = hashlib.sha256(
    json.dumps(POLICY_SPEC, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

NY = ZoneInfo("America/New_York")


def _utc_index(bars: pd.DataFrame) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(bars.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    return index


def in_release_session(value: Any) -> bool:
    """Return whether an availability time is in the frozen NY window."""
    at = pd.Timestamp(value)
    if pd.isna(at):
        return False
    if at.tzinfo is None:
        at = at.tz_localize("UTC")
    local = at.tz_convert(NY)
    return time(5, 30) <= local.time().replace(tzinfo=None) < time(6, 0)


def signal_series(bars: pd.DataFrame, lookback: int = 15) -> pd.Series:
    """Build onset-only fade sides indexed by completed M1 event time.

    Edges exclude the current bar.  The release time is one minute after the
    index and the New York window is evaluated on that release time.
    """
    if not isinstance(bars, pd.DataFrame) or bars.empty:
        return pd.Series(dtype="object")
    if lookback != 15:
        raise ValueError("the frozen edge lookback must be 15")
    missing = {"High", "Low", "Close"} - set(bars.columns)
    if missing:
        raise ValueError(f"missing M1 columns: {sorted(missing)}")
    frame = bars.loc[:, ["High", "Low", "Close"]].copy()
    frame.index = _utc_index(frame)
    if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError("M1 index must be unique and increasing")
    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    prior_high = frame["High"].shift(1).rolling(lookback, min_periods=lookback).max()
    prior_low = frame["Low"].shift(1).rolling(lookback, min_periods=lookback).min()
    raw = pd.Series(None, index=frame.index, dtype="object")
    raw.loc[frame["Close"] > prior_high] = "SHORT"
    raw.loc[frame["Close"] < prior_low] = "LONG"
    releases = frame.index + pd.Timedelta(minutes=1)
    in_window = pd.Series([in_release_session(value) for value in releases], index=frame.index)
    condition = raw.notna() & in_window
    onset = condition & (~condition.shift(1, fill_value=False) | raw.ne(raw.shift(1)))
    return raw.where(onset)


def latest_signal(bars: pd.DataFrame) -> str | None:
    """Return the side only when the newest completed M1 is a frozen event."""
    if not isinstance(bars, pd.DataFrame) or len(bars) < 17:
        return None
    recent_index = _utc_index(bars.iloc[-17:])
    if not (recent_index.to_series().diff().iloc[1:] == pd.Timedelta(minutes=1)).all():
        raise ValueError("latest edge decision requires 17 contiguous completed M1 bars")
    missing = {"High", "Low", "Close"} - set(bars.columns)
    if missing:
        raise ValueError(f"missing M1 columns: {sorted(missing)}")
    recent = bars.iloc[-17:].loc[:, ["High", "Low", "Close"]].apply(
        pd.to_numeric, errors="coerce",
    )
    if not np.isfinite(recent.to_numpy(dtype=float)).all():
        raise ValueError("latest edge decision contains nonfinite M1 prices")
    if not (
        recent["High"].ge(recent["Low"])
        & recent["Close"].le(recent["High"])
        & recent["Close"].ge(recent["Low"])
    ).all():
        raise ValueError("latest edge decision contains invalid M1 OHLC")
    sides = signal_series(bars)
    if sides.empty:
        return None
    value = sides.iloc[-1]
    return str(value) if value in {"LONG", "SHORT"} else None
