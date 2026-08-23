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


def cycle560_latest(target_bars: pd.DataFrame, strategy: dict[str, Any]) -> dict[str, Any]:
    bars = _utc_bars(target_bars)
    close = bars["Close"].astype(float)
    log_return = np.log(close).diff()
    squared_return = log_return.pow(2)
    feature = squared_return.rolling(60, min_periods=30).corr(squared_return.shift(1))
    ret25 = np.log(close).diff(25)
    bar_time = bars.index[-1]
    ret_value = float(ret25.iloc[-1])
    feature_value = float(feature.iloc[-1])
    threshold = float(strategy["threshold"])
    finite = np.isfinite(ret_value) and np.isfinite(feature_value)
    eligible = finite and ret_value > 0.0 and feature_value >= threshold
    return {
        "bar_time": bar_time,
        "side": "LONG",
        "eligible": bool(eligible),
        "ret25": ret_value,
        "sqret_ac_l1_w60": feature_value,
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
    if strategy_type == "PV2C560":
        return cycle560_latest(target_bars, strategy)
    raise ValueError(f"unsupported strategy_type={strategy_type}")
