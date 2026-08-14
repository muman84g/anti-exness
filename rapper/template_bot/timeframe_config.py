# -*- coding: utf-8 -*-
"""Timeframe selection helpers for template live bots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "H1": 60,
}

MT5_TIMEFRAMES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "H1": 16385,
}

RESAMPLE_RULES = {
    "M5": "5min",
    "M15": "15min",
    "H1": "1h",
}


@dataclass(frozen=True)
class TimeframeProfile:
    signal_timeframe: str
    execution_timeframe: str
    history_source: str
    hist_timeframe: int
    hist_bars: int
    signal_minutes: int
    drop_latest_signal_bar: bool
    max_signal_delay_minutes: float


def load_timeframe_profile(params: dict[str, Any]) -> TimeframeProfile:
    cfg = dict(params.get("timeframe_profile") or {})
    signal_tf = str(cfg.get("signal_timeframe", "H1")).upper()
    execution_tf = str(cfg.get("execution_timeframe", "M1")).upper()
    history_source = str(cfg.get("history_source", "DIRECT_HIST")).upper()
    if signal_tf not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unsupported signal_timeframe: {signal_tf}")
    if execution_tf not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unsupported execution_timeframe: {execution_tf}")
    if history_source not in {"DIRECT_HIST", "M1_RESAMPLE"}:
        raise ValueError(f"Unsupported history_source: {history_source}")
    default_hist_tf = "M1" if history_source == "M1_RESAMPLE" else signal_tf
    hist_tf_name = str(cfg.get("hist_timeframe_name", default_hist_tf)).upper()
    if hist_tf_name not in MT5_TIMEFRAMES:
        raise ValueError(f"Unsupported hist_timeframe_name: {hist_tf_name}")
    return TimeframeProfile(
        signal_timeframe=signal_tf,
        execution_timeframe=execution_tf,
        history_source=history_source,
        hist_timeframe=int(cfg.get("hist_timeframe", MT5_TIMEFRAMES[hist_tf_name])),
        hist_bars=int(cfg.get("hist_bars", 300)),
        signal_minutes=int(TIMEFRAME_MINUTES[signal_tf]),
        drop_latest_signal_bar=bool(cfg.get("drop_latest_signal_bar", True)),
        max_signal_delay_minutes=float(cfg.get("max_signal_delay_minutes", params.get("max_signal_delay_minutes", 5))),
    )


def build_signal_bars(raw_bars: pd.DataFrame, profile: TimeframeProfile) -> pd.DataFrame:
    if profile.history_source == "DIRECT_HIST" or profile.signal_timeframe == "M1":
        return raw_bars
    rule = RESAMPLE_RULES.get(profile.signal_timeframe)
    if not rule:
        raise ValueError(f"Cannot resample to {profile.signal_timeframe}")
    agg = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    bars = raw_bars.resample(rule, label="left", closed="left").agg(agg).dropna()
    return bars
