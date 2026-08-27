from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _utc_bars(bars: pd.DataFrame) -> pd.DataFrame:
    out = bars.copy().sort_index()
    index = pd.DatetimeIndex(out.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    out.index = index
    return out


def cycle859_latest(target_bars: pd.DataFrame, strategy: dict[str, Any]) -> dict[str, Any]:
    bars = _utc_bars(target_bars)
    close = bars["Close"].astype(float)
    log_return = np.log(close).diff()
    abs_return = log_return.abs()
    short_std = abs_return.rolling(30, min_periods=15).std()
    long_std = abs_return.rolling(120, min_periods=60).std()
    ret25 = np.log(close / close.shift(25))
    vol30_bps = log_return.mul(10000.0).rolling(30, min_periods=30).std()
    ratio = short_std / long_std
    bar_time = bars.index[-1]
    ret_value = float(ret25.iloc[-1])
    ratio_value = float(ratio.iloc[-1])
    threshold = float(strategy["threshold"])
    finite = np.isfinite(ret_value) and np.isfinite(ratio_value)
    eligible = finite and ret_value > 0.0 and ratio_value <= threshold
    return {
        "bar_time": bar_time,
        "side": "LONG",
        "eligible": bool(eligible),
        "ret25": ret_value,
        "absret_std_ratio30_120": ratio_value,
        "vol30_bps": float(vol30_bps.iloc[-1]),
        "threshold": threshold,
        "reason": "eligible" if eligible else ("condition_false" if finite else "feature_not_ready"),
    }


def latest_signal(
    target_bars: pd.DataFrame,
    context_bars: pd.DataFrame | None,
    strategy: dict[str, Any],
) -> dict[str, Any]:
    del context_bars
    strategy_type = str(strategy["strategy_type"])
    if strategy_type == "PV2C859":
        return cycle859_latest(target_bars, strategy)
    raise ValueError(f"unsupported strategy_type={strategy_type}")


def short_overlay_signals(target_bars: pd.DataFrame, strategy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Evaluate the two frozen completed-M1 short overlays for the latest bar."""
    bars = _utc_bars(target_bars)
    close_source = "MidClose" if "MidClose" in bars.columns and bars["MidClose"].notna().all() else "Close"
    close = bars[close_source].astype(float)
    high = bars["High"].astype(float)
    low = bars["Low"].astype(float)
    open_ = bars["Open"].astype(float)
    volume = bars["Volume"].astype(float)
    ret1 = np.log(close).diff().mul(10000.0)
    activity = volume / volume.rolling(60, min_periods=30).median()
    ema20 = close.ewm(span=20, adjust=False).mean()
    bar_range = (high - low).replace(0.0, np.nan)
    upper_wick_ratio = (high - pd.concat([open_, close], axis=1).max(axis=1)) / bar_range
    bar_time = bars.index[-1]

    activity_cfg = dict(strategy["short_overlays"]["activity"])
    activity_ready = all(
        np.isfinite(value)
        for value in (activity.iloc[-2], ret1.iloc[-2], ret1.iloc[-1], close.iloc[-1], low.iloc[-2])
    )
    activity_eligible = bool(
        activity_ready
        and activity.iloc[-2] >= float(activity_cfg["prior_activity_min"])
        and abs(ret1.iloc[-2]) <= float(activity_cfg["prior_abs_ret1_max_bps"])
        and ret1.iloc[-1] < 0.0
        and close.iloc[-1] < low.iloc[-2]
        and (low.iloc[-2] / close.iloc[-1] - 1.0) * 10000.0 >= float(activity_cfg["break_min_bps"])
    )

    vsa_cfg = dict(strategy["short_overlays"]["vsa"])
    vsa_ready = all(
        np.isfinite(value)
        for value in (activity.iloc[-1], upper_wick_ratio.iloc[-1], ret1.iloc[-1], close.iloc[-1], ema20.iloc[-1])
    )
    vsa_eligible = bool(
        vsa_ready
        and activity.iloc[-1] >= float(vsa_cfg["activity_min"])
        and upper_wick_ratio.iloc[-1] >= float(vsa_cfg["upper_wick_ratio_min"])
        and ret1.iloc[-1] < 0.0
        and close.iloc[-1] > ema20.iloc[-1]
    )
    return {
        "activity": {
            "bar_time": bar_time,
            "signal_type": "activity",
            "side": "SHORT",
            "eligible": activity_eligible,
            "prior_activity": float(activity.iloc[-2]),
            "prior_ret1_bps": float(ret1.iloc[-2]),
            "ret1_bps": float(ret1.iloc[-1]),
            "break_bps": float((low.iloc[-2] / close.iloc[-1] - 1.0) * 10000.0),
            "vol30_bps": float(ret1.rolling(30, min_periods=30).std().iloc[-1]),
            "reason": "eligible" if activity_eligible else ("condition_false" if activity_ready else "feature_not_ready"),
        },
        "vsa": {
            "bar_time": bar_time,
            "signal_type": "vsa",
            "side": "SHORT",
            "eligible": vsa_eligible,
            "activity": float(activity.iloc[-1]),
            "upper_wick_ratio": float(upper_wick_ratio.iloc[-1]),
            "ret1_bps": float(ret1.iloc[-1]),
            "close_above_ema20": bool(close.iloc[-1] > ema20.iloc[-1]),
            "vol30_bps": float(ret1.rolling(30, min_periods=30).std().iloc[-1]),
            "reason": "eligible" if vsa_eligible else ("condition_false" if vsa_ready else "feature_not_ready"),
        },
    }
