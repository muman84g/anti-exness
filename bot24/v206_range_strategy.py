from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


EMA_SPAN = 60
ATR_WINDOW = 30
ER_WINDOW = 30
CONTAINMENT_WINDOW = 20
TOUCH_LOOKBACK = 10
PATH_BARS = 4
CENTER_DRIFT_BARS = 10
BAND_MULTIPLIER = 2.5
ER_MAX = 0.35
CONTAINMENT_MIN = 0.80
WIDTH_ATR_MIN = 2.5
WIDTH_ATR_MAX = 12.0
CENTER_DRIFT_ATR_MAX = 0.75
STOP_ATR = 0.5
TIMEOUT_MINUTES_FROM_ENTRY = 30
COOLDOWN_MINUTES = 5


@dataclass(frozen=True)
class V206Signal:
    side: str
    signal_bar_time: pd.Timestamp
    lower: float
    center: float
    upper: float
    atr30: float
    stop: float
    timeout_at: pd.Timestamp


def _series(frame: pd.DataFrame, live_name: str, research_name: str) -> pd.Series:
    if live_name in frame:
        return frame[live_name].astype(float)
    if research_name in frame:
        return frame[research_name].astype(float)
    raise KeyError(f"missing price column: {live_name}/{research_name}")


def _utc_index(frame: pd.DataFrame) -> pd.DatetimeIndex:
    raw: Any = frame.index
    if "minute" in frame:
        raw = frame["minute"]
    index = pd.DatetimeIndex(raw)
    if index.tz is None:
        index = index.tz_localize("UTC")
    return index.tz_convert("UTC")


def rolling_containment(
    close: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    window: int = CONTAINMENT_WINDOW,
) -> np.ndarray:
    out = np.full(len(close), np.nan, dtype=np.float64)
    for i in range(window - 1, len(close)):
        if not np.isfinite(lower[i]) or not np.isfinite(upper[i]):
            continue
        values = close[i - window + 1 : i + 1]
        out[i] = float(np.count_nonzero((values >= lower[i]) & (values <= upper[i]))) / window
    return out


def build_v206_features(bars: pd.DataFrame) -> pd.DataFrame:
    if bars.empty:
        return pd.DataFrame(index=_utc_index(bars))
    index = _utc_index(bars)
    open_ = _series(bars, "Open", "bid_open").reset_index(drop=True)
    high = _series(bars, "High", "bid_high").reset_index(drop=True)
    low = _series(bars, "Low", "bid_low").reset_index(drop=True)
    close = _series(bars, "Close", "bid_close").reset_index(drop=True)

    prior_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prior_close).abs(), (low - prior_close).abs()], axis=1
    ).max(axis=1)
    atr30 = true_range.rolling(ATR_WINDOW, min_periods=ATR_WINDOW).mean()
    center = close.ewm(span=EMA_SPAN, adjust=False, min_periods=EMA_SPAN).mean()
    absdev = (close - center).abs().ewm(
        span=EMA_SPAN, adjust=False, min_periods=EMA_SPAN
    ).mean()
    lower = center - BAND_MULTIPLIER * absdev
    upper = center + BAND_MULTIPLIER * absdev

    close_np = close.to_numpy(dtype=np.float64)
    lower_np = lower.to_numpy(dtype=np.float64)
    center_np = center.to_numpy(dtype=np.float64)
    upper_np = upper.to_numpy(dtype=np.float64)
    atr_np = atr30.to_numpy(dtype=np.float64)
    diff_sum = close.diff().abs().rolling(ER_WINDOW).sum().to_numpy(dtype=np.float64)
    displacement = np.abs(close_np - close.shift(ER_WINDOW).to_numpy(dtype=np.float64))
    efficiency = np.divide(
        displacement,
        diff_sum,
        out=np.full_like(displacement, np.nan),
        where=diff_sum > 0.0,
    )
    containment = rolling_containment(close_np, lower_np, upper_np)
    width = upper_np - lower_np
    width_atr = width / atr_np
    drift = np.abs(center_np - center.shift(CENTER_DRIFT_BARS).to_numpy(dtype=np.float64)) / atr_np
    valid = (
        np.isfinite(lower_np)
        & np.isfinite(center_np)
        & np.isfinite(upper_np)
        & np.isfinite(atr_np)
        & (upper_np > center_np)
        & (center_np > lower_np)
        & (atr_np > 0.0)
        & (efficiency <= ER_MAX)
        & (containment >= CONTAINMENT_MIN)
        & (width_atr >= WIDTH_ATR_MIN)
        & (width_atr <= WIDTH_ATR_MAX)
        & (drift <= CENTER_DRIFT_ATR_MAX)
    )

    touched_low = (
        (low <= lower).shift(1).rolling(TOUCH_LOOKBACK).max().fillna(0).astype(bool)
    ).to_numpy()
    touched_high = (
        (high >= upper).shift(1).rolling(TOUCH_LOOKBACK).max().fillna(0).astype(bool)
    ).to_numpy()
    cross_up = ((close.shift(1) <= center.shift(1)) & (close > center)).to_numpy()
    cross_down = ((close.shift(1) >= center.shift(1)) & (close < center)).to_numpy()
    path_up = (
        (close.shift(3) < close.shift(2))
        & (close.shift(2) < close.shift(1))
        & (close.shift(1) < close)
    ).to_numpy()
    path_down = (
        (close.shift(3) > close.shift(2))
        & (close.shift(2) > close.shift(1))
        & (close.shift(1) > close)
    ).to_numpy()
    signal = np.where(
        valid & touched_low & cross_up & path_up,
        1,
        np.where(valid & touched_high & cross_down & path_down, -1, 0),
    ).astype(np.int8)

    return pd.DataFrame(
        {
            "open": open_.to_numpy(dtype=np.float64),
            "high": high.to_numpy(dtype=np.float64),
            "low": low.to_numpy(dtype=np.float64),
            "close": close_np,
            "atr30": atr_np,
            "lower": lower_np,
            "center": center_np,
            "upper": upper_np,
            "efficiency": efficiency,
            "containment": containment,
            "width_atr": width_atr,
            "center_drift_atr": drift,
            "valid": valid,
            "signal": signal,
        },
        index=index,
    )


def latest_v206_signal(bars: pd.DataFrame) -> V206Signal | None:
    features = build_v206_features(bars)
    if features.empty:
        return None
    row = features.iloc[-1]
    raw_side = int(row["signal"])
    if raw_side == 0:
        return None
    side = "LONG" if raw_side == 1 else "SHORT"
    atr30 = float(row["atr30"])
    lower = float(row["lower"])
    upper = float(row["upper"])
    stop = lower - STOP_ATR * atr30 if side == "LONG" else upper + STOP_ATR * atr30
    signal_bar = pd.Timestamp(features.index[-1])
    timeout_at = signal_bar + pd.Timedelta(minutes=1 + TIMEOUT_MINUTES_FROM_ENTRY)
    return V206Signal(
        side=side,
        signal_bar_time=signal_bar,
        lower=lower,
        center=float(row["center"]),
        upper=upper,
        atr30=atr30,
        stop=float(stop),
        timeout_at=timeout_at,
    )


def target_from_actual_fill(side: str, actual_fill: float, fixed_stop: float) -> float:
    fill = float(actual_fill)
    stop = float(fixed_stop)
    risk = abs(fill - stop)
    if not np.isfinite(fill) or not np.isfinite(stop) or risk <= 0.0:
        raise ValueError("invalid v206 fill/stop")
    if side == "LONG":
        if stop >= fill:
            raise ValueError("v206 LONG stop must be below fill")
        return fill + risk
    if side == "SHORT":
        if stop <= fill:
            raise ValueError("v206 SHORT stop must be above fill")
        return fill - risk
    raise ValueError(f"invalid v206 side: {side}")
