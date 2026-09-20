"""Frozen, completed-M1 research entries adopted by bot23 in 2026-09.

The caller must pass completed bars only. Prices are Bid OHLC and ``atr30`` is
the bot's completed-bar true-range ATR30.  This module deliberately contains no
order or clock access, which keeps the research formula independently testable.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Callable

import pandas as pd

SESSION_TIMEZONE = "America/New_York"
MAX_FILL_WAIT_MINUTES = 1
CALENDAR_PATH = Path(__file__).with_name("research_session_calendar.json")


@dataclass(frozen=True)
class Signal:
    side: str
    variant: str


def _close(bars: pd.DataFrame) -> pd.Series:
    return bars["Close"].astype(float)


def _continuous(bars: pd.DataFrame, lookback: int) -> bool:
    if len(bars) < lookback + 1:
        return False
    idx = pd.DatetimeIndex(bars.index[-(lookback + 1):])
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    gaps = idx.to_series().diff().dropna()
    return bool((gaps > pd.Timedelta(0)).all() and (gaps <= pd.Timedelta(minutes=5)).all())


def _finite_positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


def _eff(q: pd.Series, end: int, width: int) -> float:
    segment = q.iloc[end - width:end + 1]
    path = segment.diff().abs().sum()
    return abs(float(segment.iloc[-1] - segment.iloc[0])) / float(path) if path > 0 else 0.0


def nwave_restart(bars: pd.DataFrame) -> bool:
    if len(bars) < 68 or not _continuous(bars, 67): return False
    q = _close(bars); t = len(q)-1; atr = _finite_positive(bars["atr30"].iloc[t-20])
    if atr is None: return False
    A=q.iloc[t-20]-q.iloc[t-65]; B=q.iloc[t-8]-q.iloc[t-20]; C=q.iloc[t]-q.iloc[t-8]
    return bool(A>2*atr and B<0 and .15<=-B/A<=.65 and C>.45*A and _eff(q,t,8)>.2 and q.iloc[t]>q.iloc[t-1])


def alt_dbreak(bars: pd.DataFrame) -> bool:
    if len(bars) < 18 or not _continuous(bars, 16): return False
    q=_close(bars); d=q.diff(); t=len(q)-1; atr=_finite_positive(bars["atr30"].iloc[t])
    if atr is None: return False
    alt=all(float(d.iloc[t-j])*float(d.iloc[t-j-1])<0 for j in range(3,8))
    prior=sum(abs(float(d.iloc[t-j])) for j in range(3,9)); break2=float(d.iloc[t]+d.iloc[t-1])
    return bool(alt and d.iloc[t]>0 and d.iloc[t-1]>0 and break2>=.7*prior and break2>=.5*atr)


def path_curvature(bars: pd.DataFrame) -> bool:
    if len(bars) < 69 or not _continuous(bars, 68): return False
    q=_close(bars); t=len(q)-1; atr=_finite_positive(bars["atr30"].iloc[t])
    if atr is None: return False
    slow=-(q.iloc[t-8]-q.iloc[t-68])/60; fast=-(q.iloc[t]-q.iloc[t-8])/8
    return bool(slow>atr/60 and fast>0 and fast/slow<=.15 and _eff(q,t-8,60)>=.35)


def path_speed(bars: pd.DataFrame) -> bool:
    if len(bars) < 69 or not _continuous(bars, 68): return False
    q=_close(bars); t=len(q)-1; atr=_finite_positive(bars["atr30"].iloc[t])
    if atr is None: return False
    old=q.iloc[t-8]-q.iloc[t-68]; recent=q.iloc[t]-q.iloc[t-8]
    if old <= 0: return False
    speed=(-recent/8)/(old/60)
    return bool(old>1.5*atr and recent<0 and -recent<.75*old and speed>=1.5 and _eff(q,t-8,60)>=.4)


def nwave_centroid(bars: pd.DataFrame) -> bool:
    if len(bars) < 47 or not _continuous(bars, 45): return False
    q=_close(bars); t=len(q)-1; atr=_finite_positive(bars["atr30"].iloc[t-8])
    if atr is None: return False
    A=-(q.iloc[t-8]-q.iloc[t-38]); center=q.iloc[t-7:t+1].mean(); early=q.iloc[t-11:t-3].mean()
    return bool(A>2.5*atr and -(center-early)>.25*A and -(q.iloc[t]-q.iloc[t-3])>0 and _eff(q,t,8)<.2 and q.iloc[t]<q.iloc[t-1])


def ir_original(bars: pd.DataFrame) -> bool:
    if len(bars) < 76 or not _continuous(bars, 74): return False
    q=_close(bars); t=len(q)-1; atr=_finite_positive(bars["atr30"].iloc[t-28])
    if atr is None: return False
    p0,p1,p2,p3=q.iloc[t-73],q.iloc[t-28],q.iloc[t-13],q.iloc[t-8]
    A=p1-p0; B=p2-p1; C=p3-p2; D=q.iloc[t]-p3
    return bool(A>=1.5*atr and B<0 and .15<=-B/A<=.45 and abs(C)<=.22*A and D>0 and D/A>=.12 and 0<=p1-q.iloc[t]<=.22*A and _eff(q,t-28,45)>=.25)


def _ir_pause_base(bars: pd.DataFrame, t: int) -> tuple[bool,float]:
    q=_close(bars); atr=_finite_positive(bars["atr30"].iloc[t-31])
    if atr is None: return False, math.nan
    p0,p1,p2,p3,p4=q.iloc[t-91],q.iloc[t-31],q.iloc[t-16],q.iloc[t-8],q.iloc[t]
    A=p1-p0; B=p2-p1; C=p3-p2; D=p4-p3
    ok=A>=1.5*atr and B<0 and .15<=-B/A<=.45 and abs(C)<=.22*A and D>.12*A and 0<=p1-p4<=.22*A
    return bool(ok), float(A)


def ir_pause_reapproach(bars: pd.DataFrame) -> bool:
    if len(bars) < 102 or not _continuous(bars, 99): return False
    q=_close(bars); t=len(q)-1; ok,_base_A=_ir_pause_base(bars,t-2); atr=_finite_positive(bars["atr30"].iloc[t])
    if not ok or atr is None: return False
    A=float(q.iloc[t-31]-q.iloc[t-91])
    path3=float(q.iloc[t-4:t].diff().abs().sum())
    d=float(q.iloc[t]-q.iloc[t-1])
    return bool(path3<=.22*A and d>0 and d>=.08*atr)


EVALUATORS: dict[str, tuple[str, Callable[[pd.DataFrame], bool]]] = {
    "nwv_checkpoint_restart": ("LONG", nwave_restart),
    "alternation_double_break": ("LONG", alt_dbreak),
    "curvature_fade_short": ("LONG", path_curvature),
    "speed_reversal_long": ("SHORT", path_speed),
    "nwv_base_centroid_migration": ("SHORT", nwave_centroid),
}

LOOKBACKS = {"nwv_checkpoint_restart": 67, "alternation_double_break": 16,
             "curvature_fade_short": 68, "speed_reversal_long": 68,
             "nwv_base_centroid_migration": 45, "ir_original_priority_union": 99}


def load_session_calendar(path: Path = CALENDAR_PATH) -> dict:
    payload=json.loads(Path(path).read_text(encoding="utf-8"))
    sessions=payload.get("sessions")
    digest=hashlib.sha256(json.dumps(sessions,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    if payload.get("version") != 1 or payload.get("timezone") != SESSION_TIMEZONE or digest != payload.get("sessions_sha256"):
        raise ValueError("research session calendar identity/hash mismatch")
    if payload.get("undefined_dates_policy") != "fail_closed" or not isinstance(sessions,list) or not sessions:
        raise ValueError("research session calendar policy/schema invalid")
    prior_end=None
    for row in sessions:
        start=pd.Timestamp(row["open_local"]); end=pd.Timestamp(row["final_close_local"])
        if start.tzinfo is None or end.tzinfo is None or start>=end or (prior_end is not None and start<prior_end):
            raise ValueError("research session calendar interval invalid")
        prior_end=end
    return payload


def validate_calendar_coverage(calendar: dict, now: pd.Timestamp) -> None:
    stamp=pd.Timestamp(now); stamp=stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    start=pd.Timestamp(calendar["valid_from_utc"]); end=pd.Timestamp(calendar["expires_at_utc"])
    if not start<=stamp<=end:
        raise ValueError("research session calendar coverage expired or not started")


def declared_session_window(at: pd.Timestamp, calendar: dict | None = None) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Return only an explicitly listed session; omitted dates fail closed."""
    stamp=pd.Timestamp(at)
    stamp=stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    calendar=calendar or load_session_calendar()
    valid_start=pd.Timestamp(calendar["valid_from_utc"]); valid_end=pd.Timestamp(calendar["expires_at_utc"])
    if not valid_start<=stamp<=valid_end: return None
    for row in calendar["sessions"]:
        start=pd.Timestamp(row["open_local"]).tz_convert("UTC"); end=pd.Timestamp(row["final_close_local"]).tz_convert("UTC")
        if start<=stamp<=end: return start,end
    return None


def entry_session_guard(signal_id: str, bars: pd.DataFrame, quote_time: pd.Timestamp, hold_minutes: int,
                        calendar: dict | None = None) -> bool:
    """Reject undefined sessions, history crossings, maintenance and weekend carries."""
    lookback=LOOKBACKS.get(signal_id)
    if lookback is None or len(bars)<lookback+1: return False
    quote_window=declared_session_window(quote_time,calendar)
    if quote_window is None: return False
    start,end=quote_window
    oldest=pd.Timestamp(bars.index[-(lookback+1)]); newest=pd.Timestamp(bars.index[-1])
    oldest=oldest.tz_localize("UTC") if oldest.tzinfo is None else oldest.tz_convert("UTC")
    newest=newest.tz_localize("UTC") if newest.tzinfo is None else newest.tz_convert("UTC")
    quote=pd.Timestamp(quote_time); quote=quote.tz_localize("UTC") if quote.tzinfo is None else quote.tz_convert("UTC")
    planned_exit=quote+pd.Timedelta(minutes=hold_minutes+MAX_FILL_WAIT_MINUTES)
    return bool(start<=oldest<=newest<quote<=end and planned_exit<=end and _continuous(bars,lookback))


def abnormal_gap_exit_reason(previous_quote_msc: int, current_quote_msc: int,
                             due: pd.Timestamp | None) -> str | None:
    """Classify the first fresh quote after a >5m outage, separate from normal exits."""
    if previous_quote_msc <= 0 or current_quote_msc <= previous_quote_msc or current_quote_msc-previous_quote_msc <= 300_000:
        return None
    current=pd.Timestamp(current_quote_msc,unit="ms",tz="UTC")
    if due is not None and current > pd.Timestamp(due)+pd.Timedelta(minutes=1):
        return "overdue_exit_after_gap"
    return "session_gap_forced_close"


def evaluate(signal_id: str, bars: pd.DataFrame) -> Signal | None:
    """Evaluate a lane. IR is an original-priority, single-capacity union."""
    if signal_id == "ir_original_priority_union":
        if ir_original(bars): return Signal("LONG", "interrupted_reapproach")
        if ir_pause_reapproach(bars): return Signal("LONG", "IR_pause_reapproach")
        return None
    side, fn = EVALUATORS[signal_id]
    return Signal(side, signal_id) if fn(bars) else None
