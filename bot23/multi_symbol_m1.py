"""Causal, read-only multi-symbol completed-M1 support for bot23 signals."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

import pandas as pd


_HISTORY_SYMBOL_RE = re.compile(r"[A-Za-z0-9._#-]{1,32}")


@dataclass(frozen=True)
class MultiSymbolM1Snapshot:
    decision_time: pd.Timestamp
    bars: Mapping[str, pd.DataFrame]


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("invalid decision clock")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def valid_history_symbol_map(symbols: Any) -> bool:
    """Require safe, non-aliased logical-to-broker history symbols."""
    if not isinstance(symbols, Mapping) or not symbols:
        return False
    physical: list[str] = []
    for logical, mt5_symbol in symbols.items():
        if not isinstance(logical, str) or not logical:
            return False
        if not isinstance(mt5_symbol, str) or _HISTORY_SYMBOL_RE.fullmatch(mt5_symbol) is None:
            return False
        physical.append(mt5_symbol.casefold())
    return len(physical) == len(set(physical))


def fetch_completed_m1_snapshot(
    data_manager: Any,
    symbols: Mapping[str, str],
    *,
    decision_time: Any,
    num_bars: int = 140,
    freshness_seconds: int = 120,
    broker_timezone: str = "UTC",
) -> tuple[MultiSymbolM1Snapshot | None, str]:
    """Fetch only completed M1 and fail closed on any missing/stale constituent."""
    try:
        now = _utc(decision_time)
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_multi_symbol_clock"
    if num_bars < 112 or freshness_seconds < 0 or not valid_history_symbol_map(symbols):
        return None, "invalid_multi_symbol_contract"
    frames: dict[str, pd.DataFrame] = {}
    for logical, mt5_symbol in symbols.items():
        try:
            frame = data_manager.get_historical_data(
                str(mt5_symbol), 1, int(num_bars), broker_timezone, drop_latest=True,
            )
        except Exception:
            return None, f"{logical}_m1_fetch_failed"
        if frame is None or len(frame) < 112:
            return None, f"{logical}_m1_unavailable"
        frame = frame.copy()
        frame.index = pd.DatetimeIndex([_utc(value) for value in frame.index])
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            return None, f"{logical}_m1_clock_invalid"
        # The bridge may already expose bars completed after the historical
        # signal release when the live polling cycle is late.  Trim those bars
        # before computing any feature so the snapshot is identical to an
        # as-of join at the backtest decision clock.
        frame = frame[(frame.index + pd.Timedelta(minutes=1)) <= now]
        if len(frame) < 111:
            return None, f"{logical}_m1_not_yet_available"
        frame = frame.iloc[-int(num_bars):]
        available = frame.index[-1] + pd.Timedelta(minutes=1)
        age = (now - available).total_seconds()
        if age < 0:
            return None, f"{logical}_m1_not_yet_available"
        if age > freshness_seconds:
            return None, f"{logical}_m1_stale"
        frames[str(logical)] = frame
    return MultiSymbolM1Snapshot(now, frames), "ok"


def jst1113_usd_accel_pre_session_short(
    xau_bars: pd.DataFrame,
    snapshot: MultiSymbolM1Snapshot,
) -> tuple[bool, str]:
    """Literal b4c_accel_pre_session_up using only information available now."""
    required = ("EURUSD", "GBPUSD", "AUDUSD", "USDJPY")
    if any(name not in snapshot.bars for name in required):
        return False, "constituent_missing"
    if len(xau_bars) < 62:
        return False, "xau_warmup_incomplete"
    xau_close = xau_bars["Close"].astype(float)
    # The event bar is the latest completed XAU M1. Research uses shift(1)
    # versus shift(61), so neither endpoint includes the event bar itself.
    if not float(xau_close.iloc[-2]) > float(xau_close.iloc[-62]):
        return False, "xau_pre60_not_up"
    signed15: list[float] = []
    signed110: list[float] = []
    for name in required:
        close = snapshot.bars[name]["Close"].astype(float)
        if len(close) < 111 or close.iloc[-16] <= 0 or close.iloc[-111] <= 0:
            return False, f"{name}_return_unavailable"
        r15 = float(close.iloc[-1] / close.iloc[-16] - 1.0)
        r110 = float(close.iloc[-1] / close.iloc[-111] - 1.0)
        sign = 1.0 if name == "USDJPY" else -1.0
        signed15.append(sign * r15)
        signed110.append(sign * r110)
    if sum(value > 0 for value in signed15) < 3:
        return False, "usd_breadth15_below3"
    if sum(signed15) / (4.0 * 15.0) <= sum(signed110) / (4.0 * 110.0):
        return False, "usd_acceleration_not_positive"
    return True, "signal"
