# -*- coding: utf-8 -*-
"""Strategy signal adapters for template live bots.

Replace or extend `build_signal` when creating botNN. Keep the return contract:
`{"side": "long"|"short", "bar_time": str, ...}` or `None`.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def _atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    high = bars["High"].astype(float)
    low = bars["Low"].astype(float)
    close = bars["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def ehlers_cross_signal(bars: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    period = int(params.get("period", 21))
    cycle_atr = float(params.get("cycle_atr", 0.5))
    if len(bars) < max(period + 5, 25):
        return None
    close = bars["Close"].astype(float)
    trendline = close.ewm(span=period, adjust=False).mean()
    atr = _atr(bars, 14)
    i = len(bars) - 1
    if pd.isna(atr.iloc[i]) or pd.isna(trendline.iloc[i]) or pd.isna(trendline.iloc[i - 1]):
        return None
    cycle = abs(close.iloc[i] - trendline.iloc[i])
    long_sig = close.iloc[i - 1] <= trendline.iloc[i - 1] and close.iloc[i] > trendline.iloc[i] and cycle > atr.iloc[i] * cycle_atr
    short_sig = close.iloc[i - 1] >= trendline.iloc[i - 1] and close.iloc[i] < trendline.iloc[i] and cycle > atr.iloc[i] * cycle_atr
    if not long_sig and not short_sig:
        return None
    return {
        "side": "long" if long_sig else "short",
        "bar_time": str(bars.index[i]),
        "close": float(close.iloc[i]),
        "trendline": float(trendline.iloc[i]),
        "atr14": float(atr.iloc[i]),
    }


def bollinger_squeeze_pullback_signal(bars: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any] | None:
    bb_period = int(params.get("bb_period", 20))
    width_lookback = int(params.get("width_lookback", 120))
    pullback_window = int(params.get("pullback_window", 6))
    if len(bars) < bb_period + width_lookback + pullback_window + 2:
        return None
    open_price = bars["Open"].astype(float)
    high = bars["High"].astype(float)
    low = bars["Low"].astype(float)
    close = bars["Close"].astype(float)
    ma = close.rolling(bb_period).mean()
    std = close.rolling(bb_period).std()
    upper = ma + float(params.get("std_mult", 2.0)) * std
    lower = ma - float(params.get("std_mult", 2.0)) * std
    width = (upper - lower) / ma.replace(0.0, float("nan"))
    squeeze_level = width.rolling(width_lookback).quantile(float(params.get("squeeze_quantile", 0.2))).shift(1)
    squeezed = width.shift(1) <= squeeze_level
    recent_long = (squeezed & (close > upper)).shift(1).rolling(pullback_window).max().fillna(0).astype(bool)
    recent_short = (squeezed & (close < lower)).shift(1).rolling(pullback_window).max().fillna(0).astype(bool)
    i = len(bars) - 1
    if any(pd.isna(series.iloc[i]) for series in (ma, upper, lower, width, squeeze_level)):
        return None
    long_sig = bool(recent_long.iloc[i]) and low.iloc[i] <= ma.iloc[i] and close.iloc[i] > ma.iloc[i] and close.iloc[i] > open_price.iloc[i]
    short_sig = bool(recent_short.iloc[i]) and high.iloc[i] >= ma.iloc[i] and close.iloc[i] < ma.iloc[i] and close.iloc[i] < open_price.iloc[i]
    if not long_sig and not short_sig:
        return None
    return {
        "side": "long" if long_sig else "short",
        "bar_time": str(bars.index[i]),
        "close": float(close.iloc[i]),
        "ma": float(ma.iloc[i]),
    }


def build_signal(bars: pd.DataFrame, spec: dict[str, Any]) -> dict[str, Any] | None:
    mode = str(spec.get("signal_adapter", "NONE")).upper()
    signal_params = dict(spec.get("signal_params") or {})
    if mode in {"NONE", "CUSTOM"}:
        return None
    if mode == "EHLERS_CROSS":
        return ehlers_cross_signal(bars, signal_params)
    if mode == "BOLLINGER_SQUEEZE_PULLBACK":
        return bollinger_squeeze_pullback_signal(bars, signal_params)
    raise ValueError(f"Unsupported signal_adapter: {mode}")
