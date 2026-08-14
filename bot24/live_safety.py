# -*- coding: utf-8 -*-
"""Reusable live-bot safety helpers.

Copy this file into a new botNN folder first, then wire the callbacks from the
target runner. The helpers are deliberately small and side-effect-light: they
never place, close, modify, or cancel orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Callable

import pandas as pd


UTC = timezone.utc


@dataclass(frozen=True)
class LiveSafetyOptions:
    """Feature switches for generated live bots.

    Use True to enable a safety element, False to keep it disabled, and None for
    "not applicable to this bot". This makes the generated botNN params explicit.
    """

    hist_timestamps_are_utc: bool | None = True
    stale_signal_guard: bool | None = True
    preflight_clean_sync: bool | None = True
    periodic_clean_sync: bool | None = True
    clear_recoverable_sync_block: bool | None = True
    save_state_after_clear: bool | None = True
    broker_sl_residual_clear: bool | None = False
    audit_log: bool | None = True


@dataclass(frozen=True)
class StaleSignalDecision:
    stale: bool
    signal_time_utc: pd.Timestamp | None
    entry_due_utc: pd.Timestamp | None
    latest_allowed_utc: pd.Timestamp | None
    now_utc: pd.Timestamp
    reason: str


def utc_now_timestamp() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(UTC))


def normalize_hist_bars(
    raw: pd.DataFrame | None,
    *,
    drop_latest: bool,
    configured_timezone: str = "UTC",
    options: LiveSafetyOptions | None = None,
) -> pd.DataFrame | None:
    """Normalize EA HIST bars before signal/stale logic.

    Default is UTC because BotBridge HIST timestamps for current CentOS EAs were
    verified as UTC. If a future broker is proven otherwise, set
    hist_timestamps_are_utc=False and provide configured_timezone.
    """

    if raw is None or raw.empty:
        return None
    opts = options or LiveSafetyOptions()
    bars = raw.copy().sort_index()
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        if opts.hist_timestamps_are_utc is False:
            idx = idx.tz_localize(str(configured_timezone), ambiguous="infer", nonexistent="shift_forward")
        else:
            idx = idx.tz_localize("UTC", ambiguous="infer", nonexistent="shift_forward")
    idx = idx.tz_convert("UTC")
    bars.index = idx
    if drop_latest and len(bars) > 1:
        bars = bars.iloc[:-1]
    return bars if not bars.empty else None


def stale_signal_decision(
    signal_bar_time: Any,
    *,
    timeframe_hours: float = 1.0,
    max_delay_minutes: float = 0.0,
    now_utc: pd.Timestamp | None = None,
    options: LiveSafetyOptions | None = None,
) -> StaleSignalDecision:
    opts = options or LiveSafetyOptions()
    now = (now_utc or utc_now_timestamp()).tz_convert("UTC")
    if opts.stale_signal_guard is not True or max_delay_minutes <= 0:
        return StaleSignalDecision(False, None, None, None, now, "disabled")
    try:
        signal_time = pd.Timestamp(signal_bar_time)
        if signal_time.tzinfo is None:
            signal_time = signal_time.tz_localize("UTC")
        signal_time = signal_time.tz_convert("UTC")
    except Exception:
        return StaleSignalDecision(True, None, None, None, now, "invalid_signal_time")
    entry_due = signal_time + pd.Timedelta(hours=float(timeframe_hours))
    latest_allowed = entry_due + pd.Timedelta(minutes=float(max_delay_minutes))
    stale = now > latest_allowed
    return StaleSignalDecision(
        stale,
        signal_time,
        entry_due,
        latest_allowed,
        now,
        "stale" if stale else "fresh",
    )


def is_broker_sl_residual_block(state: dict[str, Any]) -> bool:
    if state.get("sync_block_reason") != "same_magic_unexpected_order":
        return False
    comments = (state.get("sync_block_details") or {}).get("comments") or []
    if not comments:
        return False
    return all(re.fullmatch(r"\[sl [0-9]+(?:\.[0-9]+)?\]", str(comment or "")) for comment in comments)


def clean_sync_block_if_flat(
    *,
    symbol_key: str,
    state: dict[str, Any],
    positions: list[Any] | None,
    orders: list[Any] | None,
    save_state: Callable[[], None],
    audit: Callable[[str, str, str], None] | None = None,
    options: LiveSafetyOptions | None = None,
    flat_auto_clear_reasons: set[str] | None = None,
    confirm_position_absent: Callable[[int], bool | None] | None = None,
    required_flat_confirmations: int = 2,
) -> bool:
    """Clear only stale blocks that are proven flat for the target symbol/magic.

    `positions is None` or `orders is None` means the bridge query failed, so the
    helper keeps fail-closed behavior and does nothing.
    """

    opts = options or LiveSafetyOptions()
    if opts.periodic_clean_sync is not True and opts.preflight_clean_sync is not True:
        return False
    if positions is None or orders is None:
        return False
    if positions or orders:
        return False
    if not state.get("sync_block_new_entries"):
        return False

    reason = str(state.get("sync_block_reason") or "")
    can_clear = bool(state.get("sync_block_recoverable")) and opts.clear_recoverable_sync_block is True
    if not can_clear and opts.broker_sl_residual_clear is True:
        can_clear = is_broker_sl_residual_block(state)
        if can_clear:
            reason = "broker_sl_residual_flat"
    auto_clear = reason in (flat_auto_clear_reasons or set())
    if not can_clear and not auto_clear:
        return False
    if auto_clear:
        details = state.get("sync_block_details") or {}
        related_ticket = int(details.get("ticket") or details.get("order_ticket") or 0)
        if related_ticket > 0:
            if confirm_position_absent is None or confirm_position_absent(related_ticket) is not True:
                state["flat_clear_confirmation_count"] = 0
                state["flat_clear_confirmation_reason"] = None
                save_state()
                return False
        previous_count = int(state.get("flat_clear_confirmation_count") or 0)
        same_reason = state.get("flat_clear_confirmation_reason") == reason
        state["flat_clear_confirmation_count"] = previous_count + 1 if same_reason else 1
        state["flat_clear_confirmation_reason"] = reason
        save_state()
        if state["flat_clear_confirmation_count"] < max(2, int(required_flat_confirmations)):
            return False

    state["sync_block_new_entries"] = False
    state["sync_block_reason"] = None
    state["sync_block_recoverable"] = False
    state["sync_block_details"] = {}
    state["flat_clear_confirmation_count"] = 0
    state["flat_clear_confirmation_reason"] = None
    if audit and opts.audit_log is True:
        audit(symbol_key, "sync_block_cleared_flat", reason)
    if opts.save_state_after_clear is True:
        save_state()
    return True


def audit_sync_snapshot(
    *,
    symbol_key: str,
    state: dict[str, Any],
    positions: list[Any] | None,
    orders: list[Any] | None,
) -> dict[str, Any]:
    return {
        "symbol": symbol_key,
        "active": bool(state.get("active")),
        "last_signal_bar": state.get("last_signal_bar"),
        "sync_block": bool(state.get("sync_block_new_entries")),
        "reason": state.get("sync_block_reason"),
        "positions_query_ok": positions is not None,
        "orders_query_ok": orders is not None,
        "positions_count": None if positions is None else len(positions),
        "orders_count": None if orders is None else len(orders),
    }


def self_test() -> None:
    save_calls: list[bool] = []
    audit_rows: list[tuple[str, str, str]] = []
    state = {
        "sync_block_new_entries": True,
        "sync_block_reason": "positions_unavailable",
        "sync_block_recoverable": True,
        "sync_block_details": {},
    }
    assert clean_sync_block_if_flat(
        symbol_key="EURUSD",
        state=state,
        positions=[],
        orders=[],
        save_state=lambda: save_calls.append(True),
        audit=lambda *row: audit_rows.append(row),
    )
    assert not state["sync_block_new_entries"]
    assert save_calls and audit_rows

    state = {
        "sync_block_new_entries": True,
        "sync_block_reason": "same_magic_unexpected_order",
        "sync_block_recoverable": False,
        "sync_block_details": {"comments": ["[sl 7427.42]"], "tickets": [1]},
    }
    assert clean_sync_block_if_flat(
        symbol_key="US500",
        state=state,
        positions=[],
        orders=[],
        save_state=lambda: None,
        options=LiveSafetyOptions(broker_sl_residual_clear=True),
    )

    state = {
        "sync_block_new_entries": True,
        "sync_block_reason": "same_magic_unexpected_order",
        "sync_block_recoverable": False,
        "sync_block_details": {"comments": ["manual"]},
    }
    assert not clean_sync_block_if_flat(
        symbol_key="US500",
        state=state,
        positions=[],
        orders=[],
        save_state=lambda: None,
        options=LiveSafetyOptions(broker_sl_residual_clear=True),
    )

    fresh = stale_signal_decision("2026-01-01 10:00:00+00:00", now_utc=pd.Timestamp("2026-01-01 11:05:00+00:00"), max_delay_minutes=10)
    stale = stale_signal_decision("2026-01-01 10:00:00+00:00", now_utc=pd.Timestamp("2026-01-01 11:11:00+00:00"), max_delay_minutes=10)
    assert not fresh.stale and stale.stale


if __name__ == "__main__":
    self_test()
    print("live_safety self-test ok")
