# -*- coding: utf-8 -*-
"""Isolated NY-session VWAP extension-fade signal and paged M1 cache."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import numpy as np
import pandas as pd


POLICY_ID = "ny0530_0830_session_vwap_extension_fade_q90_20d_atr60_h15_cap5_v001"
SESSION_TIMEZONE = "America/New_York"


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def in_entry_session(value: Any) -> bool:
    local = _utc(value).tz_convert(SESSION_TIMEZONE)
    minute = int(local.hour) * 60 + int(local.minute)
    return 5 * 60 + 30 <= minute < 8 * 60 + 30


def entry_history_issue(
    bars: pd.DataFrame,
    quote_time: Any,
    *,
    coverage_days: int = 20,
    atr_period: int = 60,
) -> str | None:
    """Return the first entry-blocking history defect for an active session bar.

    CopyRates can return a syntactically successful but stale or sparse page
    while terminal history is still loading.  Calendar span alone therefore
    cannot establish that the latest completed signal, ATR tail, and current
    session VWAP are reproducible.
    """
    if bars is None or bars.empty:
        return "history_empty"
    required = ["Open", "High", "Low", "Close", "Volume"]
    if any(column not in bars.columns for column in required):
        return "history_columns_invalid"
    numeric = bars[required].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        return "history_numeric_invalid"
    index = pd.DatetimeIndex(bars.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    if not index.is_monotonic_increasing or index.has_duplicates:
        return "history_index_invalid"
    quote = _utc(quote_time)
    expected_latest = quote.floor("min") - pd.Timedelta(minutes=1)
    if index[-1] != expected_latest:
        return "latest_completed_m1_missing"
    if index[0] > index[-1] - pd.Timedelta(days=int(coverage_days)):
        return "history_coverage_short"

    tail_count = max(30, int(atr_period))
    tail = index[-tail_count:]
    if len(tail) < tail_count or not tail.to_series().diff().iloc[1:].eq(pd.Timedelta(minutes=1)).all():
        return "atr_tail_not_contiguous"

    available = index + pd.Timedelta(minutes=1)
    local = available.tz_convert(SESSION_TIMEZONE)
    latest_local = local[-1]
    latest_minute = int(latest_local.hour) * 60 + int(latest_local.minute)
    if not 5 * 60 + 30 <= latest_minute < 8 * 60 + 30:
        return None
    session_start_local = pd.Timestamp(latest_local.date()).tz_localize(SESSION_TIMEZONE) + pd.Timedelta(
        hours=5, minutes=30
    )
    first_event = session_start_local.tz_convert("UTC") - pd.Timedelta(minutes=1)
    expected_session = pd.date_range(first_event, index[-1], freq="1min")
    present = index[(index >= first_event) & (index <= index[-1])]
    if not present.equals(expected_session):
        return "current_session_not_contiguous"
    session_available = index + pd.Timedelta(minutes=1)
    session_local = session_available.tz_convert(SESSION_TIMEZONE)
    relevant_session = np.asarray(
        [
            (5 * 60 + 30) <= (stamp.hour * 60 + stamp.minute) < (8 * 60 + 30)
            for stamp in session_local
        ],
        dtype=bool,
    )
    if (numeric.loc[relevant_session, "Volume"] <= 0.0).any():
        return "session_volume_nonpositive"
    return None


def signal_frame(bars: pd.DataFrame, *, quantile: float = 0.90, lookback_days: int = 20) -> pd.DataFrame:
    """Build the frozen HIST-reproducible signal using completed Bid M1 bars."""
    required = ["Open", "High", "Low", "Close", "Volume"]
    frame = bars[required].copy().sort_index()
    frame[required] = frame[required].apply(pd.to_numeric, errors="coerce").astype("float64")
    if not np.isfinite(frame[required].to_numpy(dtype=float)).all():
        raise ValueError("history_numeric_invalid")
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3.0
    volume = frame["Volume"].astype(float).clip(lower=0.0)
    # The frozen backtest assigns session date/minute from availability, not
    # from the M1 event/open timestamp. A 05:29 bar is therefore the first
    # usable 05:30 New-York observation.
    available_index = frame.index + pd.Timedelta(minutes=1)
    local = available_index.tz_convert(SESSION_TIMEZONE)
    session_key = pd.Series(local.date, index=frame.index)
    in_session = pd.Series(
        [(5 * 60 + 30) <= (x.hour * 60 + x.minute) < (8 * 60 + 30) for x in local],
        index=frame.index,
    )
    if (volume.loc[in_session] <= 0.0).any():
        raise ValueError("session_volume_nonpositive")
    weighted = (typical * volume).where(in_session)
    session_volume = volume.where(in_session)
    cumulative_weighted = weighted.groupby(session_key).cumsum()
    cumulative_volume = session_volume.groupby(session_key).cumsum()
    vwap = cumulative_weighted / cumulative_volume.replace(0.0, np.nan)

    previous_close = frame["Close"].shift(1)
    true_range = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - previous_close).abs(),
            (frame["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr60 = true_range.rolling(60, min_periods=30).mean()
    z = (frame["Close"] - vwap) / atr60.replace(0.0, np.nan)
    threshold = z.abs().shift(1).rolling(f"{int(lookback_days)}D", min_periods=1).quantile(float(quantile))
    side = pd.Series(0, index=frame.index, dtype="int8")
    side.loc[in_session & (z > threshold)] = -1
    side.loc[in_session & (z < -threshold)] = 1
    condition = side.ne(0)
    onset = condition & (~condition.shift(1, fill_value=False) | side.ne(side.shift(1, fill_value=0)))
    frame["SessionVWAP"] = vwap
    frame["ATR60"] = atr60
    frame["Z"] = z
    frame["Q90"] = threshold
    frame["Side"] = side
    frame["Onset"] = onset
    return frame


def latest_signal(bars: pd.DataFrame, *, quantile: float = 0.90, lookback_days: int = 20) -> tuple[str | None, pd.Series | None]:
    if bars.empty:
        return None, None
    frame = signal_frame(bars, quantile=quantile, lookback_days=lookback_days)
    row = frame.iloc[-1]
    if not bool(row["Onset"]):
        return None, row
    return ("LONG" if int(row["Side"]) > 0 else "SHORT"), row


@dataclass(frozen=True)
class HistorySnapshot:
    bars: pd.DataFrame
    ready: bool
    fresh: bool
    reason: str
    failures: int
    retry_after_seconds: float


class PagedM1History:
    """Retain successful pages and retry transient MT5 history gaps autonomously."""

    def __init__(
        self,
        data_manager: Any,
        *,
        symbol: str,
        timeframe: int = 1,
        broker_timezone: str = "UTC",
        page_bars: int = 5000,
        refresh_bars: int = 10,
        coverage_days: int = 20,
        retry_seconds: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0),
    ):
        self.data_manager = data_manager
        self.symbol = symbol
        self.timeframe = int(timeframe)
        self.broker_timezone = broker_timezone
        self.page_bars = min(5000, max(1, int(page_bars)))
        self.refresh_bars = min(5000, max(2, int(refresh_bars)))
        self.coverage_days = int(coverage_days)
        self.retry_seconds = tuple(float(x) for x in retry_seconds) or (5.0,)
        self.bars = pd.DataFrame(
            {
                "Open": pd.Series(dtype="float64"),
                "High": pd.Series(dtype="float64"),
                "Low": pd.Series(dtype="float64"),
                "Close": pd.Series(dtype="float64"),
                "Volume": pd.Series(dtype="float64"),
            }
        )
        # A backfill generation is built separately from the last admitted
        # cache.  This preserves the last successful cache for diagnostics and
        # transient failures without allowing its coverage to make a new
        # generation look complete prematurely.
        self._backfill_bars = self.bars.copy()
        self.next_start_pos = 0
        self.failures = 0
        self.retry_at = 0.0
        self.ready = False

    def _snapshot(self, *, fresh: bool, reason: str, now: float) -> HistorySnapshot:
        return HistorySnapshot(
            self.bars.copy(), self.ready, fresh, reason, self.failures, max(0.0, self.retry_at - now)
        )

    def request_rebackfill(self) -> None:
        """Start an isolated generation while retaining the admitted cache."""
        self.ready = False
        self.next_start_pos = 0
        self.retry_at = 0.0
        self._backfill_bars = self.bars.iloc[0:0].copy()

    @staticmethod
    def _page_is_valid(page: pd.DataFrame) -> bool:
        try:
            required = ["Open", "High", "Low", "Close", "Volume"]
            if any(column not in page.columns for column in required):
                return False
            index = pd.DatetimeIndex(page.index)
            if not index.is_monotonic_increasing or index.has_duplicates:
                return False
            numeric = page[required].apply(pd.to_numeric, errors="coerce")
            if not np.isfinite(numeric.to_numpy(dtype=float)).all():
                return False
            prices = numeric[["Open", "High", "Low", "Close"]]
            if (prices <= 0.0).any().any() or (numeric["Volume"] < 0.0).any():
                return False
            if (numeric["High"] < prices[["Open", "Low", "Close"]].max(axis=1)).any():
                return False
            if (numeric["Low"] > prices[["Open", "High", "Close"]].min(axis=1)).any():
                return False
            return True
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _merge_page(base: pd.DataFrame, page: pd.DataFrame) -> pd.DataFrame | None:
        """Merge only when every overlapping completed bar is byte-value equivalent."""
        required = ["Open", "High", "Low", "Close", "Volume"]
        overlap = base.index.intersection(page.index)
        if len(overlap) and not base.loc[overlap, required].equals(page.loc[overlap, required]):
            return None
        return pd.concat([base, page]).sort_index().loc[
            lambda value: ~value.index.duplicated(keep="last")
        ]

    def advance(self, quote_time: Any, *, monotonic_now: float | None = None) -> HistorySnapshot:
        """Fetch at most one page; failures retain cache and schedule a retry."""
        now = time.monotonic() if monotonic_now is None else float(monotonic_now)
        if now < self.retry_at:
            return self._snapshot(fresh=False, reason="retry_backoff", now=now)
        start_pos = 0 if self.ready else self.next_start_pos
        count = self.refresh_bars if self.ready else self.page_bars
        try:
            page = self.data_manager.get_historical_page(
                self.symbol, self.timeframe, start_pos, count, self.broker_timezone
            )
        except Exception:
            page = None
        if page is None or page.empty:
            self.failures += 1
            delay = self.retry_seconds[min(self.failures - 1, len(self.retry_seconds) - 1)]
            self.retry_at = now + delay
            return self._snapshot(fresh=False, reason="history_fetch_failed", now=now)

        if not self._page_is_valid(page):
            self.failures += 1
            delay = self.retry_seconds[min(self.failures - 1, len(self.retry_seconds) - 1)]
            self.retry_at = now + delay
            return self._snapshot(fresh=False, reason="history_page_invalid", now=now)

        page = page.copy()
        required = ["Open", "High", "Low", "Close", "Volume"]
        page[required] = page[required].apply(pd.to_numeric, errors="coerce").astype("float64")
        if page.index.tz is None:
            page.index = page.index.tz_localize("UTC")
        else:
            page.index = page.index.tz_convert("UTC")
        quote = _utc(quote_time)
        page = page.loc[(page.index + pd.Timedelta(minutes=1)) <= quote]
        if page.empty:
            self.failures += 1
            delay = self.retry_seconds[min(self.failures - 1, len(self.retry_seconds) - 1)]
            self.retry_at = now + delay
            return self._snapshot(fresh=False, reason="no_completed_m1_in_page", now=now)

        self.failures = 0
        self.retry_at = 0.0
        if not self.ready:
            merged = self._merge_page(self._backfill_bars, page)
            if merged is None:
                self.failures += 1
                delay = self.retry_seconds[min(self.failures - 1, len(self.retry_seconds) - 1)]
                self.retry_at = now + delay
                return self._snapshot(fresh=False, reason="completed_bar_revision_conflict", now=now)
            self._backfill_bars = merged
            self.next_start_pos += len(page)
            newest = self._backfill_bars.index.max()
            oldest = self._backfill_bars.index.min()
            self.ready = bool(oldest <= newest - pd.Timedelta(days=self.coverage_days))
            if self.ready:
                self.bars = self._backfill_bars
                self._backfill_bars = self.bars.iloc[0:0].copy()
            return self._snapshot(
                fresh=self.ready,
                reason="ready" if self.ready else "backfill_in_progress",
                now=now,
            )
        merged = self._merge_page(self.bars, page)
        if merged is None:
            self.failures += 1
            delay = self.retry_seconds[min(self.failures - 1, len(self.retry_seconds) - 1)]
            self.retry_at = now + delay
            return self._snapshot(fresh=False, reason="completed_bar_revision_conflict", now=now)
        self.bars = merged
        cutoff = self.bars.index.max() - pd.Timedelta(days=self.coverage_days + 2)
        self.bars = self.bars.loc[self.bars.index >= cutoff]
        return self._snapshot(fresh=True, reason="refreshed", now=now)
