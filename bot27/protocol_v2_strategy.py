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
