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


def cycle421_latest(target_bars: pd.DataFrame, strategy: dict[str, Any]) -> dict[str, Any]:
    bars = _utc_bars(target_bars)
    close = bars["Close"].astype(float)
    log_return = np.log(close).diff()
    rv30 = np.sqrt((log_return * log_return).rolling(30, min_periods=30).sum())
    rv120 = np.sqrt((log_return * log_return).rolling(120, min_periods=120).sum())
    ret30 = close / close.shift(30) - 1.0
    ratio = rv30 / rv120
    bar_time = bars.index[-1]
    ret_value = float(ret30.iloc[-1])
    ratio_value = float(ratio.iloc[-1])
    threshold = float(strategy["threshold"])
    finite = np.isfinite(ret_value) and np.isfinite(ratio_value)
    return {
        "bar_time": bar_time,
        "side": "LONG",
        "eligible": bool(finite and ret_value > 0.0 and ratio_value <= threshold),
        "ret30": ret_value,
        "rv_ratio_30_120": ratio_value,
        "threshold": threshold,
        "reason": "eligible" if finite and ret_value > 0.0 and ratio_value <= threshold else ("condition_false" if finite else "feature_not_ready"),
    }


def cycle520_latest(
    target_bars: pd.DataFrame,
    context_bars: pd.DataFrame,
    strategy: dict[str, Any],
) -> dict[str, Any]:
    target = _utc_bars(target_bars)
    context = _utc_bars(context_bars)
    target_frame = pd.DataFrame({"bar_start": target.index, "Close_target": target["Close"].astype(float).to_numpy()})
    context_frame = pd.DataFrame({"bar_start": context.index, "Close_context": context["Close"].astype(float).to_numpy()})
    joined = target_frame.merge(context_frame, on="bar_start", how="inner").sort_values("bar_start", ignore_index=True)
    target_latest = target.index[-1]
    if joined.empty or pd.Timestamp(joined.bar_start.iloc[-1]) != target_latest:
        return {
            "bar_time": target_latest,
            "side": "LONG",
            "eligible": False,
            "reason": "context_current_bar_missing",
        }
    target_return = np.log(joined.Close_target.astype(float)).diff(5)
    context_return = np.log(joined.Close_context.astype(float)).diff(5)
    corr = target_return.rolling(240, min_periods=180).corr(context_return.shift(1))
    median = corr.rolling(1440, min_periods=960).median()
    mad = (corr - median).abs().rolling(1440, min_periods=960).median()
    lead5_corr_z = (corr - median) / (1.4826 * mad + 1e-9)
    ret25 = np.log(joined.Close_target.astype(float)).diff(25)
    bar_time = pd.Timestamp(joined.bar_start.iloc[-1])
    reference_bar = pd.Timestamp(joined.bar_start.iloc[-2]) if len(joined) >= 2 else pd.NaT
    stale_seconds = float((bar_time - reference_bar).total_seconds()) if pd.notna(reference_bar) else float("inf")
    ret_value = float(ret25.iloc[-1])
    z_value = float(lead5_corr_z.iloc[-1])
    threshold = float(strategy["threshold"])
    stale_limit = float(strategy["context_stale_limit_seconds"])
    finite = np.isfinite(ret_value) and np.isfinite(z_value)
    stale_pass = np.isfinite(stale_seconds) and stale_seconds <= stale_limit
    return {
        "bar_time": bar_time,
        "side": "LONG",
        "eligible": bool(finite and stale_pass and ret_value > 0.0 and z_value >= threshold),
        "ret25": ret_value,
        "lead5_corr_z": z_value,
        "threshold": threshold,
        "context_reference_bar_start": reference_bar,
        "context_reference_time": reference_bar + pd.Timedelta(minutes=1) if pd.notna(reference_bar) else pd.NaT,
        "context_stale_seconds": stale_seconds,
        "context_stale_pass": bool(stale_pass),
        "reason": "context_stale_fail_closed" if finite and not stale_pass else ("eligible" if finite and ret_value > 0.0 and z_value >= threshold else ("condition_false" if finite else "feature_not_ready")),
    }


def latest_signal(
    target_bars: pd.DataFrame,
    context_bars: pd.DataFrame | None,
    strategy: dict[str, Any],
) -> dict[str, Any]:
    strategy_type = str(strategy["strategy_type"])
    if strategy_type == "PV2C421":
        return cycle421_latest(target_bars, strategy)
    if strategy_type == "PV2C520":
        if context_bars is None:
            raise ValueError("PV2C520 requires context bars")
        return cycle520_latest(target_bars, context_bars, strategy)
    raise ValueError(f"unsupported strategy_type={strategy_type}")
