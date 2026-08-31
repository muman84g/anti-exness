"""Frozen JST13:00-to-pre-Europe M5 signal policy for bot23."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from eu_entry_admission_clock import classify_entry_admission


POLICY_ID = "jst1300_pre_eu30_squeeze45_double60_rsi45_cap3_dst_v001"
POLICY_PARAMS_HASH = "71722f0f90a34f7c265007cc8522f12b6b8769c7c81a1f3bf9988497956c9f0c"
ADMISSION_BLOCK_ID = "jst1300_pre_eu30"
SIGNAL_IDS = (
    "c4_bollinger_squeeze_release_direction_control",
    "c4_double_sweep_resolution_primary",
    "c4_rsi_extreme_reversal_direction_control",
)


def in_entry_session(at_utc: datetime | pd.Timestamp) -> bool:
    stamp = pd.Timestamp(at_utc)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    block = classify_entry_admission(stamp.to_pydatetime())
    return block is not None and block.id == ADMISSION_BLOCK_ID


def _completed_m5(bars: pd.DataFrame) -> pd.DataFrame:
    if bars.empty:
        return pd.DataFrame()
    frame = bars.copy()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(frame.columns):
        return pd.DataFrame()
    decision_time = frame.index[-1] + pd.Timedelta(minutes=1)
    m5 = frame[["Open", "High", "Low", "Close"]].resample(
        "5min", label="left", closed="left"
    ).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    return m5[(m5.index + pd.Timedelta(minutes=5)) <= decision_time]


def signal_sides(bars: pd.DataFrame) -> dict[str, str | None]:
    """Return one-shot sides at the latest completed M5 release.

    The two ``direction_control`` lanes intentionally invert their primitive;
    the double-sweep lane preserves its primary direction. This is the exact
    identity frozen by the DEV/leak/forward audit.
    """
    result = {signal_id: None for signal_id in SIGNAL_IDS}
    m5 = _completed_m5(bars)
    if m5.empty:
        return result
    release_time = m5.index[-1] + pd.Timedelta(minutes=5)
    if not in_entry_session(release_time):
        return result
    source_minute_utc = int(m5.index[-1].hour) * 60 + int(m5.index[-1].minute)
    if source_minute_utc < 4 * 60:
        return result

    close = m5["Close"].astype(float)
    open_ = m5["Open"].astype(float)
    high = m5["High"].astype(float)
    low = m5["Low"].astype(float)
    rng = (high - low).replace(0.0, np.nan)
    body = close - open_
    loc = (close - low) / rng
    active = pd.Series(
        [
            (int(stamp.hour) * 60 + int(stamp.minute) >= 4 * 60)
            and in_entry_session(stamp)
            for stamp in m5.index
        ],
        index=m5.index,
        dtype=bool,
    )

    mean20 = close.shift(1).rolling(20, min_periods=20).mean()
    sd20 = close.shift(1).rolling(20, min_periods=20).std()
    width = 4.0 * sd20 / mean20.replace(0.0, np.nan)
    squeeze = width < width.shift(1).rolling(48, min_periods=36).quantile(0.25)
    squeeze_raw = pd.Series(
        np.where(active & squeeze & (close > mean20 + 2.0 * sd20), 1,
                 np.where(active & squeeze & (close < mean20 - 2.0 * sd20), -1, 0)),
        index=m5.index,
        dtype=int,
    )
    squeeze_pulse = squeeze_raw.where(squeeze_raw.ne(squeeze_raw.shift(1).fillna(0)), 0)
    squeeze_latest = -int(squeeze_pulse.iloc[-1])
    if squeeze_latest:
        result[SIGNAL_IDS[0]] = "LONG" if squeeze_latest > 0 else "SHORT"

    hi10 = high.shift(1).rolling(10, min_periods=10).max()
    lo10 = low.shift(1).rolling(10, min_periods=10).min()
    swept_hi_recent = high.shift(1).rolling(3).max() > hi10.shift(3)
    swept_lo_recent = low.shift(1).rolling(3).min() < lo10.shift(3)
    sweep_raw = pd.Series(
        np.where(active & swept_hi_recent & swept_lo_recent & (close > open_) & (loc >= 0.70), 1,
                 np.where(active & swept_hi_recent & swept_lo_recent & (close < open_) & (loc <= 0.30), -1, 0)),
        index=m5.index,
        dtype=int,
    )
    sweep_pulse = sweep_raw.where(sweep_raw.ne(sweep_raw.shift(1).fillna(0)), 0)
    sweep_latest = int(sweep_pulse.iloc[-1])
    if sweep_latest:
        result[SIGNAL_IDS[1]] = "LONG" if sweep_latest > 0 else "SHORT"

    delta = close.diff()
    gain = delta.clip(lower=0.0).shift(1).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0.0)).shift(1).rolling(14, min_periods=14).mean()
    rsi = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    rsi_raw = pd.Series(
        np.where(active & (rsi < 25.0) & (body > 0.0) & (loc >= 0.65), 1,
                 np.where(active & (rsi > 75.0) & (body < 0.0) & (loc <= 0.35), -1, 0)),
        index=m5.index,
        dtype=int,
    )
    rsi_pulse = rsi_raw.where(rsi_raw.ne(rsi_raw.shift(1).fillna(0)), 0)
    rsi_latest = -int(rsi_pulse.iloc[-1])
    if rsi_latest:
        result[SIGNAL_IDS[2]] = "LONG" if rsi_latest > 0 else "SHORT"
    return result
