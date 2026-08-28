# -*- coding: utf-8 -*-
"""S23 ZA inventory with independent JST09-11 and JST11-13 overlays."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pandas as pd

os.environ.setdefault("BOT_SUFFIX", "s23")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from live_data_fetcher import MT5DataManager
from live_executor import (
    HEDGING_MARGIN_MODE,
    REQUIRED_SHARED_ACCOUNT_COMMANDS,
    MT5Executor,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
)
from live_safety import (
    LiveSafetyOptions,
    clean_sync_block_if_flat,
    clear_recoverable_sync_block_after_clean_sync,
    stale_signal_decision,
)
from live_manual_alerts import notify_manual_action_required
from live_config import MT5_LOGIN, MT5_SERVER
from eu_entry_admission_clock import classify_entry_admission, is_eu_summer_time, is_us_summer_time
from position_lifecycle_clock import fixed_hold_due_at
try:
    from shadow_opportunity_observer import ShadowOpportunityObserver
except ImportError:
    ShadowOpportunityObserver = None  # type: ignore[assignment,misc]
try:
    from shadow_state_tagger import ShadowStateTagger
except ImportError:
    ShadowStateTagger = None  # type: ignore[assignment,misc]


UTC = timezone.utc
EXPECTED_S23_MAGICS = (230023, 230024, 230025, 230026)
EXPECTED_MORNING_MAGICS = (230027, 230028, 230029)
EXPECTED_MIDDAY_MAGICS = (230030,)
EXPECTED_S23_MAGIC = EXPECTED_S23_MAGICS[0]
LEGACY_S23_MAGICS = (200023,)
EXPECTED_STRATEGY_ID = "bot23_za_horizontal_inventory_v001"
EXPECTED_CANDIDATE_ID = "bot23-x-archive-plus-jst0911-plus-jst1113-round-s2p5-d0p05-r0p03-v001"
EXPECTED_MORNING_POLICY_ID = "jst0911_stable001_param_15_55_45_v001"
EXPECTED_MORNING_POLICY_PARAMS_HASH = "c36023031af830bca0c08dd441ff800868909d404813e0a89c51e4fc1f3b086e"
EXPECTED_MORNING_SESSION_START_UTC = 0
EXPECTED_MORNING_SESSION_END_UTC = 2
EXPECTED_MORNING_MAX_POSITIONS = 3
EXPECTED_MIDDAY_POLICY_ID = "jst1113_round_s2p5_d0p05_r0p03_h60_cap1_v001"
EXPECTED_MIDDAY_POLICY_PARAMS_HASH = "526d90e6dc16981ba5e60d31750f1b4862fbe3d9170382ed624fea53ef55fd83"
EXPECTED_MIDDAY_SESSION_START_UTC = 2
EXPECTED_MIDDAY_SESSION_END_UTC = 4
EXPECTED_MIDDAY_MAX_POSITIONS = 1
EXPECTED_ENTRY_ADMISSION_EU_TIMEZONE = "Europe/London"
EXPECTED_ENTRY_ADMISSION_US_TIMEZONE = "America/New_York"
EXPECTED_ENTRY_ADMISSION_NOTATION = "per_market_dst"
EXPECTED_ENTRY_ADMISSION_SCOPE = "new_entry_admission_only"
EXPECTED_POSITION_LIFECYCLE = "confirmed_fill_utc_independent"
EXPECTED_ENTRY_ADMISSION_BLOCKS = (
    ("jst1300_pre_eu30", "fixed_jst", "13:00", "13:00", "Europe/London", "15:30", "16:30"),
    ("eu_open_to_us_preopen", "Europe/London", "15:30", "16:30", "America/New_York", "20:30", "21:30"),
    ("us_to_eu_late", "America/New_York", "20:30", "21:30", "America/New_York", "05:30", "06:30"),
)
EXPECTED_ROUTING_MODE = "first_consuming_lane_preserve_primary_v1"
EXPECTED_ENTRY_POLICY_ID = "reverse_d60"
EXPECTED_ENTRY_POLICY_PARAMS_HASH = "40475d07b84eabc1b1290bee6787113903f374ca90cf2ca271c82b825b313572"
EXPECTED_PORTFOLIO_REARM_POLICY_ID = "long_target_portfolio_rearm_8m"
EXPECTED_PORTFOLIO_REARM_PARAMS_HASH = "0f8f3fc3e32c74ce00344b01fbc335d9ac6cfbf4801357e768d87c851229afb4"
EXPECTED_PORTFOLIO_REARM_MINUTES = 8
EXPECTED_INVENTORY_RANGE_FADE_POLICY_ID = "balanced_book_false_break_fade_w15_c2_both"
EXPECTED_INVENTORY_RANGE_FADE_PARAMS_HASH = "d02b82730f7f686d97317f96aab26762168c8396f40c21f4787ab8bd4296bab0"
EXPECTED_INVENTORY_RANGE_RETURN_DEPTH = 0.0
EXPECTED_INVENTORY_RANGE_MAX_WAIT_MINUTES = 15
EXPECTED_INVENTORY_RANGE_CONFIRM_BARS = 2
EXPECTED_INVENTORY_RANGE_BREAK_SIDE_FILTER = "both"
EXPECTED_LATE_SHORT_LOOKBACK = 30
EXPECTED_LATE_SHORT_DROP_THRESHOLD = 0.006
EXPECTED_LATE_SHORT_ACTION = "reverse_long"
FROZEN_LANE_FIELDS = {
    "lot": 0.01,
    "session_start_utc": 13,
    "session_end_utc": 18,
    "mode": "impulse",
    "impulse_bars": 8,
    "impulse_atr": 0.55,
    "add_atr": 0.65,
    "max_positions": 2,
    "add_profit_guard_ratio": 0.30,
    "basket_target_usd": 10.0,
    "basket_stop_usd": 18.0,
    "max_hold_bars": 70,
    "cooldown": 8,
    "vol_min": 1.05,
    "failure_to_progress_bars": 10,
    "failure_to_progress_peak_usd": 3.0,
    "entry_wait_z": 2.0,
    "entry_wait_sigma": 1.0,
    "entry_wait_minutes": 10,
    "entry_require_extreme": True,
    "target_atr_mult": 3.5,
    "stop_atr_mult": 6.5,
    "failure_to_progress_peak_atr_mult": 1.0,
    "entry_max_spread_atr_ratio": 0.10,
    "adaptive_fixed_exit_atr_threshold": 2.0,
    "reverse_on_fail": False,
}
FLAT_AUTO_CLEAR_SYNC_REASONS = {
    "open_success_position_not_confirmed",
    "unresolved_open_action",
    "live_time_close_failed",
    "live_time_close_unconfirmed",
}
REPEATABLE_DIAGNOSTIC_REASONS = {
    "symbol_info_failed",
    "positions_unavailable",
    "orders_unavailable",
    "positions_or_orders_unavailable",
    "m1_bars_unavailable",
    "close_deal_query_unavailable",
    "close_deal_not_confirmed",
}
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "state")
LOG_FILE = os.path.join(LOG_DIR, "s23_bot.log")
TRADE_LOG_FILE = os.path.join(LOG_DIR, "s23_trades.csv")
STATE_FILE = os.path.join(STATE_DIR, "s23_bot_state.json")
PARAMS_FILE = os.path.join(SCRIPT_DIR, "s23_params.json")

TRADE_FIELDS = [
    "timestamp_utc",
    "event",
    "strategy_id",
    "lane_id",
    "magic",
    "symbol",
    "mt5_symbol",
    "opportunity_id",
    "basket_id",
    "ticket",
    "position_identifier",
    "deal_id",
    "side",
    "lot",
    "entry_price",
    "exit_price",
    "price",
    "profit",
    "reason",
    "signal_bar_time",
    "event_time",
    "release_time",
    "available_time",
    "decision_time",
    "executable_at",
    "live",
    "repeat_count",
    "repeat_window_seconds",
    "note",
]
_CSV_SCHEMAS_VALIDATED: set[str] = set()


def utc_now() -> datetime:
    return datetime.now(UTC)


def dt_text(value: datetime | pd.Timestamp) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").isoformat()


def parse_ts(value: Any) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def append_csv(path: str, row: dict[str, Any], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    if exists and path not in _CSV_SCHEMAS_VALIDATED:
        with open(path, "r", newline="", encoding="utf-8") as existing_file:
            observed_fields = next(csv.reader(existing_file), [])
        if observed_fields != fields:
            raise RuntimeError(
                f"CSV schema mismatch for {path}; archive/reset the old trades CSV before starting bot23"
            )
        _CSV_SCHEMAS_VALIDATED.add(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
            _CSV_SCHEMAS_VALIDATED.add(path)
        writer.writerow({name: row.get(name, "") for name in fields})


def validate_csv_schema(path: str, fields: list[str]) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    with open(path, "r", newline="", encoding="utf-8") as existing_file:
        observed_fields = next(csv.reader(existing_file), [])
    if observed_fields != fields:
        raise RuntimeError(
            f"CSV schema mismatch for {path}; archive/reset the old trades CSV before starting bot23"
        )
    _CSV_SCHEMAS_VALIDATED.add(path)


def normalize_price(value: float, digits: int) -> float:
    return round(float(value), int(digits))


def add_features(bars: pd.DataFrame, point_size: float) -> pd.DataFrame:
    out = bars.copy()
    high = out["High"].astype(float)
    low = out["Low"].astype(float)
    close = out["Close"].astype(float)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    out["atr30"] = tr.rolling(30, min_periods=30).mean()
    volume = out.get("Volume", pd.Series(index=out.index, dtype=float)).astype(float)
    out["vol_ratio"] = volume / volume.rolling(30, min_periods=30).mean()
    out["ret5"] = close - close.shift(5)
    out["ret10"] = close - close.shift(10)
    out["roll_high30"] = high.shift(1).rolling(30, min_periods=30).max()
    out["roll_low30"] = low.shift(1).rolling(30, min_periods=30).min()
    out["bb20_mid"] = close.rolling(20, min_periods=20).mean()
    out["bb20_std"] = close.rolling(20, min_periods=20).std(ddof=0)
    out["spread_points"] = ((out.get("AskOpen", out["Open"]) - out["Open"]) / point_size).clip(lower=0.0)
    return out


def in_session(ts: pd.Timestamp, start: int, end: int) -> bool:
    hour = int(ts.hour)
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def completed_align(series: pd.Series, minutes: int, bars: pd.DataFrame) -> pd.Series:
    """Expose a resampled value only after its source bar has completed."""
    released = series.copy()
    released.index = released.index + pd.Timedelta(minutes=minutes)
    decision_clock = bars.index + pd.Timedelta(minutes=1)
    return released.reindex(decision_clock, method="ffill").set_axis(bars.index)


class S23HorizontalInventoryRunner:
    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.live_enabled = bool(params.get("live_trading_enabled", False))
        self.shadow_enabled = bool(params.get("shadow_forward_enabled", True))
        self.safety = LiveSafetyOptions(**params.get("safety", {}))
        self.dm = MT5DataManager(self.safety)
        self.executor = MT5Executor()
        self._suppress_manual_alerts = False
        self._entry_policy_state_migrated = False
        self._portfolio_rearm_state_migrated = False
        self._inventory_range_fade_state_migrated = False
        self._morning_session_state_migrated = False
        self._midday_session_state_migrated = False
        self.state = self._load_state()
        self._last_status_log = 0.0
        self._diagnostic_repeats: dict[int, dict[str, Any]] = {}
        self._last_retained_block_warning: dict[int, tuple[str, str]] = {}
        self.shadow_observer: Any = None
        self._shadow_observer_error_signature: str | None = None
        self.shadow_state_tagger: Any = None
        self._shadow_state_tagger_error_signature: str | None = None
        self.midday_shadow_observer: Any = None
        self._midday_shadow_observer_error_signature: str | None = None
        self.midday_shadow_state_tagger: Any = None
        self._midday_shadow_state_tagger_error_signature: str | None = None
        try:
            if ShadowOpportunityObserver is None:
                logging.error("S23 shadow observer module unavailable; trading continues without passive evidence")
            else:
                self.shadow_observer = ShadowOpportunityObserver(
                    params.get("shadow_opportunity_observer", {}),
                    log_dir=LOG_DIR,
                    state_dir=STATE_DIR,
                    symbol=str(params.get("mt5_symbol", params["symbol"])),
                    contract_size=float(params.get("contract_size", 100.0)),
                    lot=float(params.get("default_lot", 0.01)),
                )
        except Exception as exc:
            logging.error("S23 shadow observer disabled after initialization failure: %s", exc)
        try:
            if ShadowStateTagger is None:
                logging.error("S23 shadow state tagger module unavailable; trading continues without passive state tags")
            else:
                self.shadow_state_tagger = ShadowStateTagger(
                    params.get("shadow_state_tagger", {}),
                    log_dir=LOG_DIR,
                    symbol=str(params.get("mt5_symbol", params["symbol"])),
                )
        except Exception as exc:
            logging.error("S23 shadow state tagger disabled after initialization failure: %s", exc)
        try:
            if ShadowOpportunityObserver is None:
                logging.error("S23 midday shadow observer module unavailable; trading continues without passive evidence")
            else:
                self.midday_shadow_observer = ShadowOpportunityObserver(
                    params.get("midday_shadow_opportunity_observer", {}),
                    log_dir=LOG_DIR,
                    state_dir=STATE_DIR,
                    symbol=str(params.get("mt5_symbol", params["symbol"])),
                    contract_size=float(params.get("contract_size", 100.0)),
                    lot=float(params.get("default_lot", 0.01)),
                )
        except Exception as exc:
            logging.error("S23 midday shadow observer disabled after initialization failure: %s", exc)
        try:
            if ShadowStateTagger is None:
                logging.error("S23 midday shadow state tagger module unavailable; trading continues without passive state tags")
            else:
                self.midday_shadow_state_tagger = ShadowStateTagger(
                    params.get("midday_shadow_state_tagger", {}),
                    log_dir=LOG_DIR,
                    symbol=str(params.get("mt5_symbol", params["symbol"])),
                )
        except Exception as exc:
            logging.error("S23 midday shadow state tagger disabled after initialization failure: %s", exc)

    def _morning_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("morning_session_strategies", []))

    def _midday_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("midday_session_strategies", []))

    def _all_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("strategies", [])) + self._morning_strategies() + self._midday_strategies()

    def _entry_admission_block(self, at_utc: datetime):
        """Classify only new-entry admission; never manage an owned position."""
        return classify_entry_admission(at_utc)

    def _observer_call(self, method: str, **kwargs: Any) -> Any:
        observer = self.shadow_observer
        if observer is None or not observer.enabled:
            return None
        try:
            result = getattr(observer, method)(**kwargs)
            self._shadow_observer_error_signature = None
            return result
        except Exception as exc:
            signature = f"{method}:{type(exc).__name__}:{exc}"
            if signature != self._shadow_observer_error_signature:
                logging.error("S23 shadow observer failure ignored by trading path: %s", signature)
                self._shadow_observer_error_signature = signature
            return None

    def _state_tagger_call(self, method: str, **kwargs: Any) -> Any:
        tagger = self.shadow_state_tagger
        if tagger is None or not tagger.enabled:
            return None
        try:
            result = getattr(tagger, method)(**kwargs)
            self._shadow_state_tagger_error_signature = None
            return result
        except Exception as exc:
            signature = f"{method}:{type(exc).__name__}:{exc}"
            if signature != self._shadow_state_tagger_error_signature:
                logging.error("S23 shadow state tagger failure ignored by trading path: %s", signature)
                self._shadow_state_tagger_error_signature = signature
            return None

    def _midday_observer_call(self, method: str, **kwargs: Any) -> Any:
        observer = self.midday_shadow_observer
        if observer is None or not observer.enabled:
            return None
        try:
            result = getattr(observer, method)(**kwargs)
            self._midday_shadow_observer_error_signature = None
            return result
        except Exception as exc:
            signature = f"{method}:{type(exc).__name__}:{exc}"
            if signature != self._midday_shadow_observer_error_signature:
                logging.error("S23 midday shadow observer failure ignored by trading path: %s", signature)
                self._midday_shadow_observer_error_signature = signature
            return None

    def _midday_state_tagger_call(self, method: str, **kwargs: Any) -> Any:
        tagger = self.midday_shadow_state_tagger
        if tagger is None or not tagger.enabled:
            return None
        try:
            result = getattr(tagger, method)(**kwargs)
            self._midday_shadow_state_tagger_error_signature = None
            return result
        except Exception as exc:
            signature = f"{method}:{type(exc).__name__}:{exc}"
            if signature != self._midday_shadow_state_tagger_error_signature:
                logging.error("S23 midday shadow state tagger failure ignored by trading path: %s", signature)
                self._midday_shadow_state_tagger_error_signature = signature
            return None
    def _shadow_context(
        self,
        price_row: pd.Series,
        info: Any,
        lane_readiness: dict[int, tuple[bool, str, bool]],
    ) -> dict[str, Any]:
        lane_positions: dict[str, int] = {}
        lane_pending: dict[str, bool] = {}
        readiness: dict[str, dict[str, Any]] = {}
        long_positions = 0
        short_positions = 0
        for strat in self._all_strategies():
            lane_id = int(strat["lane_id"])
            basket = list(self._st(strat).get("basket") or [])
            lane_positions[str(lane_id)] = len(basket)
            lane_pending[str(lane_id)] = bool(self._st(strat).get("pending_entry_side"))
            readiness_value = lane_readiness.get(lane_id, (False, "lane_not_prepared", False))
            if isinstance(readiness_value, tuple):
                ready, reason, consumed = readiness_value
            else:
                ready, reason, consumed = bool(readiness_value), "ready" if readiness_value else "not_ready", False
            readiness[str(lane_id)] = {"ready": bool(ready), "reason": str(reason), "consumed": bool(consumed)}
            for position in basket:
                side = str(position.get("side") or "").upper()
                long_positions += int(side == "LONG")
                short_positions += int(side == "SHORT")
        point = float(self.params.get("point_size", 0.001))
        spread_price = max(0.0, float(info.ask) - float(info.bid))
        return {
            "spread_points": spread_price / point if point > 0 else "",
            "atr30": price_row.get("atr30", ""),
            "ret10": price_row.get("ret10", ""),
            "vol_ratio": price_row.get("vol_ratio", ""),
            "portfolio_positions": long_positions + short_positions,
            "long_positions": long_positions,
            "short_positions": short_positions,
            "lane_positions": lane_positions,
            "lane_pending": lane_pending,
            "lane_readiness": readiness,
        }

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 3,
            "bot": "bot23",
            "strategy_id": self.params["strategy_id"],
            "last_saved_utc": None,
            "routing": {
                "version": str(self.params.get("routing_mode", "first_consuming_lane_preserve_primary_v1")),
                "lane_count": int(self.params.get("lane_count", len(self.params["strategies"]))),
                "last_routed_signal_bar": None,
                "last_routed_opportunity_id": None,
                "last_consumed_lane_id": None,
                "last_route_decision_utc": None,
                "entry_policy_id": str(self.params.get("entry_policy_id", EXPECTED_ENTRY_POLICY_ID)),
                "entry_policy_params_hash": str(self.params.get("entry_policy_params_hash", EXPECTED_ENTRY_POLICY_PARAMS_HASH)),
                "portfolio_rearm_policy_id": str(self.params.get("portfolio_rearm_policy_id", EXPECTED_PORTFOLIO_REARM_POLICY_ID)),
                "portfolio_rearm_params_hash": str(self.params.get("portfolio_rearm_params_hash", EXPECTED_PORTFOLIO_REARM_PARAMS_HASH)),
                "inventory_range_fade_policy_id": str(self.params.get("inventory_range_fade_policy_id", EXPECTED_INVENTORY_RANGE_FADE_POLICY_ID)),
                "inventory_range_fade_params_hash": str(self.params.get("inventory_range_fade_params_hash", EXPECTED_INVENTORY_RANGE_FADE_PARAMS_HASH)),
                "morning_policy_id": str(self.params.get("morning_session_policy_id", EXPECTED_MORNING_POLICY_ID)),
                "morning_policy_params_hash": str(self.params.get("morning_session_params_hash", EXPECTED_MORNING_POLICY_PARAMS_HASH)),
                "midday_policy_id": str(self.params.get("midday_session_policy_id", EXPECTED_MIDDAY_POLICY_ID)),
                "midday_policy_params_hash": str(self.params.get("midday_session_params_hash", EXPECTED_MIDDAY_POLICY_PARAMS_HASH)),
                "inventory_range_fade": {
                    "active": False,
                    "low": None,
                    "high": None,
                    "break_phase": 0,
                    "break_side": None,
                    "break_time_utc": None,
                    "return_confirm_count": 0,
                    "pending_side": None,
                    "pending_origin_bar": None,
                    "pending_break_side": None,
                    "last_state_bar": None,
                    "last_dispatch_bar": None,
                },
                "long_target_rearm_pending_confirmation": False,
                "long_target_rearm_request_utc": None,
                "long_target_rearm_until_utc": None,
                "long_target_rearm_confirmed_utc": None,
                "long_target_rearm_trigger_lane_id": None,
                "long_target_rearm_trigger_basket_id": None,
                "long_target_rearm_expired_utc": None,
            },
            "strategies": {
                s["id"]: {
                    "lane_id": int(s["lane_id"]),
                    "basket": [],
                    "basket_sequence": 0,
                    "current_basket_id": None,
                    "cooldown_until_bar": -1,
                    "last_add_price": None,
                    "last_signal_bar": None,
                    "last_closed_at_utc": None,
                    "last_closed_reason": None,
                    "last_closed_signal_bar": None,
                    "reverse_used": False,
                    "sync_block_new_entries": False,
                    "sync_block_reason": None,
                    "sync_block_recoverable": False,
                    "sync_block_details": {},
                    "flat_clear_confirmation_count": 0,
                    "flat_clear_confirmation_reason": None,
                    "basket_peak_pnl_usd": None,
                    "pending_close_reason": None,
                    "pending_close_signal_bar": None,
                    "time_close_defer_started_utc": None,
                    "time_close_last_quote_msc": None,
                    "time_close_stable_count": 0,
                    "time_close_retry_after_utc": None,
                    "time_close_wide_seen": False,
                    "cooldown_until_utc": None,
                    "last_evaluated_bar": None,
                    "daily_realized_date_utc": None,
                    "daily_realized_pnl_usd": 0.0,
                    "frozen_basket_atr30": None,
                    "pending_entry_side": None,
                    "pending_entry_target": None,
                    "pending_entry_expires_utc": None,
                    "pending_entry_atr30": None,
                    "pending_entry_signal_bar": None,
                    "pending_entry_opportunity_id": None,
                    "pending_entry_event_time": None,
                    "pending_entry_release_time": None,
                    "pending_open_opportunity_id": None,
                    "pending_open_started_utc": None,
                    "open_retry_after_utc": None,
                    "autotrading_reject_streak": 0,
                    "autotrading_reject_notified": False,
                    "manual_alert_last_signature": None,
                    "manual_alert_last_reason": None,
                    "manual_alert_last_at_utc": None,
                }
                for s in self._all_strategies()
            },
        }

    def _load_state(self) -> dict[str, Any]:
        default = self._default_state()
        if not os.path.exists(STATE_FILE):
            return default
        load_error: str | None = None
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as exc:
            logging.exception("Could not load state; refusing to start from an unverified default")
            state = None
            load_error = f"{type(exc).__name__}: {exc}"
        observed = state if isinstance(state, dict) else {}
        try:
            version_matches = int(observed.get("version", 0)) == int(default["version"])
        except (TypeError, ValueError, OverflowError):
            version_matches = False
        strategies = observed.get("strategies")
        shape_matches = isinstance(strategies, dict) and all(isinstance(strategies.get(s["id"]), dict) for s in self.params["strategies"])
        identity_matches = (
            observed.get("bot") == default["bot"]
            and observed.get("strategy_id") == default["strategy_id"]
            and version_matches
        )
        if not identity_matches or not shape_matches:
            observed_identity = {
                "bot": observed.get("bot"),
                "strategy_id": observed.get("strategy_id"),
                "version": observed.get("version"),
                "state_type": type(state).__name__,
                "shape_valid": shape_matches,
                "load_error": load_error,
            }
            logging.critical(
                "S23 state identity/shape invalid; refusing legacy, corrupt, or foreign state: bot=%s strategy_id=%s version=%s type=%s",
                observed.get("bot"), observed.get("strategy_id"), observed.get("version"), type(state).__name__,
            )
            state = default
            for strat in self.params["strategies"]:
                st = state["strategies"][strat["id"]]
                st["sync_block_new_entries"] = True
                st["sync_block_reason"] = "state_identity_mismatch"
                st["sync_block_recoverable"] = False
                st["sync_block_details"] = {"observed": observed_identity, "expected": {"bot": default["bot"], "strategy_id": default["strategy_id"], "version": default["version"]}}
        observed_routing = state.get("routing") if isinstance(state.get("routing"), dict) else {}
        observed_entry_policy_id = observed_routing.get("entry_policy_id")
        observed_entry_policy_hash = observed_routing.get("entry_policy_params_hash")
        observed_rearm_policy_id = observed_routing.get("portfolio_rearm_policy_id")
        observed_rearm_policy_hash = observed_routing.get("portfolio_rearm_params_hash")
        observed_range_policy_id = observed_routing.get("inventory_range_fade_policy_id")
        observed_range_policy_hash = observed_routing.get("inventory_range_fade_params_hash")
        observed_morning_policy_id = observed_routing.get("morning_policy_id")
        observed_morning_policy_hash = observed_routing.get("morning_policy_params_hash")
        observed_midday_policy_id = observed_routing.get("midday_policy_id")
        observed_midday_policy_hash = observed_routing.get("midday_policy_params_hash")
        state.setdefault("routing", default["routing"])
        for key, value in default["routing"].items():
            state["routing"].setdefault(key, value)
        state.setdefault("strategies", {})
        for sid, st in default["strategies"].items():
            state["strategies"].setdefault(sid, st)
            for key, value in st.items():
                state["strategies"][sid].setdefault(key, value)
        routing = state["routing"]
        expected_policy_id = str(self.params.get("entry_policy_id", EXPECTED_ENTRY_POLICY_ID))
        expected_policy_hash = str(self.params.get("entry_policy_params_hash", EXPECTED_ENTRY_POLICY_PARAMS_HASH))
        if (
            observed_entry_policy_id != expected_policy_id
            or observed_entry_policy_hash != expected_policy_hash
        ):
            # Existing open baskets remain under the unchanged four-lane close
            # contract.  Only unsubmitted local pending entries from the prior
            # entry policy are discarded so they cannot fill after cutover.
            pending_fields = (
                "pending_entry_side",
                "pending_entry_target",
                "pending_entry_expires_utc",
                "pending_entry_atr30",
                "pending_entry_signal_bar",
                "pending_entry_opportunity_id",
                "pending_entry_event_time",
                "pending_entry_release_time",
            )
            for strat in self.params["strategies"]:
                lane_state = state["strategies"][strat["id"]]
                for key in pending_fields:
                    lane_state[key] = None
            routing["entry_policy_id"] = expected_policy_id
            routing["entry_policy_params_hash"] = expected_policy_hash
            self._entry_policy_state_migrated = True
            logging.warning(
                "S23 entry policy state migrated to %s; prior unsubmitted pending entries were cleared",
                expected_policy_id,
            )
        expected_rearm_policy_id = str(self.params.get("portfolio_rearm_policy_id", EXPECTED_PORTFOLIO_REARM_POLICY_ID))
        expected_rearm_policy_hash = str(self.params.get("portfolio_rearm_params_hash", EXPECTED_PORTFOLIO_REARM_PARAMS_HASH))
        if observed_rearm_policy_id != expected_rearm_policy_id or observed_rearm_policy_hash != expected_rearm_policy_hash:
            routing["portfolio_rearm_policy_id"] = expected_rearm_policy_id
            routing["portfolio_rearm_params_hash"] = expected_rearm_policy_hash
            self._portfolio_rearm_state_migrated = True
            logging.warning(
                "S23 portfolio rearm state initialized to %s; existing baskets and pending entries were preserved",
                expected_rearm_policy_id,
            )
        expected_range_policy_id = str(self.params.get("inventory_range_fade_policy_id", EXPECTED_INVENTORY_RANGE_FADE_POLICY_ID))
        expected_range_policy_hash = str(self.params.get("inventory_range_fade_params_hash", EXPECTED_INVENTORY_RANGE_FADE_PARAMS_HASH))
        expected_range_state = default["routing"]["inventory_range_fade"]
        observed_range_state = routing.get("inventory_range_fade")
        if (
            observed_range_policy_id != expected_range_policy_id
            or observed_range_policy_hash != expected_range_policy_hash
            or not isinstance(observed_range_state, dict)
        ):
            routing["inventory_range_fade_policy_id"] = expected_range_policy_id
            routing["inventory_range_fade_params_hash"] = expected_range_policy_hash
            routing["inventory_range_fade"] = dict(expected_range_state)
            self._inventory_range_fade_state_migrated = True
            logging.warning(
                "S23 inventory range-fade state initialized to %s; existing baskets and pending ZA entries were preserved",
                expected_range_policy_id,
            )
        else:
            for key, value in expected_range_state.items():
                observed_range_state.setdefault(key, value)
        expected_morning_policy_id = str(self.params.get("morning_session_policy_id", EXPECTED_MORNING_POLICY_ID))
        expected_morning_policy_hash = str(self.params.get("morning_session_params_hash", EXPECTED_MORNING_POLICY_PARAMS_HASH))
        if observed_morning_policy_id is None and observed_morning_policy_hash is None:
            routing["morning_policy_id"] = expected_morning_policy_id
            routing["morning_policy_params_hash"] = expected_morning_policy_hash
            self._morning_session_state_migrated = True
            logging.warning(
                "S23 morning-session state initialized to %s; existing ZA baskets and pending entries were preserved",
                expected_morning_policy_id,
            )
        elif observed_morning_policy_id != expected_morning_policy_id or observed_morning_policy_hash != expected_morning_policy_hash:
            for strat in self._morning_strategies():
                lane_state = state["strategies"][strat["id"]]
                lane_state["sync_block_new_entries"] = True
                lane_state["sync_block_reason"] = "morning_policy_identity_mismatch"
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "observed_policy_id": observed_morning_policy_id,
                    "observed_policy_hash": observed_morning_policy_hash,
                    "expected_policy_id": expected_morning_policy_id,
                    "expected_policy_hash": expected_morning_policy_hash,
                }
        expected_midday_policy_id = str(self.params.get("midday_session_policy_id", EXPECTED_MIDDAY_POLICY_ID))
        expected_midday_policy_hash = str(self.params.get("midday_session_params_hash", EXPECTED_MIDDAY_POLICY_PARAMS_HASH))
        if observed_midday_policy_id is None and observed_midday_policy_hash is None:
            routing["midday_policy_id"] = expected_midday_policy_id
            routing["midday_policy_params_hash"] = expected_midday_policy_hash
            self._midday_session_state_migrated = True
            logging.warning(
                "S23 midday-session state initialized to %s; existing ZA and JST09-11 state was preserved",
                expected_midday_policy_id,
            )
        elif observed_midday_policy_id != expected_midday_policy_id or observed_midday_policy_hash != expected_midday_policy_hash:
            for strat in self._midday_strategies():
                lane_state = state["strategies"][strat["id"]]
                lane_state["sync_block_new_entries"] = True
                lane_state["sync_block_reason"] = "midday_policy_identity_mismatch"
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "observed_policy_id": observed_midday_policy_id,
                    "observed_policy_hash": observed_midday_policy_hash,
                    "expected_policy_id": expected_midday_policy_id,
                    "expected_policy_hash": expected_midday_policy_hash,
                }
        return state

    def _save_state(self) -> None:
        self.state["last_saved_utc"] = dt_text(utc_now())
        atomic_write_json(STATE_FILE, self.state)

    def _st(self, strat: dict[str, Any]) -> dict[str, Any]:
        return self.state["strategies"][strat["id"]]

    def _roll_daily_realized(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp | None = None) -> dict[str, Any]:
        st = self._st(strat)
        stamp = pd.Timestamp(at_utc if at_utc is not None else utc_now())
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        day = stamp.strftime("%Y-%m-%d")
        if st.get("daily_realized_date_utc") != day:
            st["daily_realized_date_utc"] = day
            st["daily_realized_pnl_usd"] = 0.0
        return st

    def _record_daily_realized(self, strat: dict[str, Any], pnl: float, at_utc: datetime | pd.Timestamp | None = None) -> None:
        st = self._roll_daily_realized(strat, at_utc)
        st["daily_realized_pnl_usd"] = float(st.get("daily_realized_pnl_usd", 0.0)) + float(pnl)

    def _new_basket_block_reason(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp) -> str | None:
        stamp = pd.Timestamp(at_utc)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        if int(stamp.hour) in {int(hour) for hour in self.params.get("new_basket_blocked_hours_utc", [])}:
            return "new_basket_blocked_hour"
        st = self._roll_daily_realized(strat, stamp)
        limit = float(self.params.get("daily_realized_loss_limit_usd", 0.0))
        return "daily_realized_loss_limit" if limit > 0.0 and float(st.get("daily_realized_pnl_usd", 0.0)) <= -limit else None

    @staticmethod
    def _alert_signature(reason: str, details: dict[str, Any]) -> str:
        encoded = json.dumps({"reason": reason, "details": details}, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _notify_manual_action(self, strat: dict[str, Any], *, title: str, reason: str, action: str, key: str) -> None:
        if self._suppress_manual_alerts:
            return
        notify_manual_action_required(bot_id="bot23", symbol=str(self.params.get("mt5_symbol", self.params["symbol"])), title=title, reason=reason, action=action, key=key)

    def _notify_reconciliation_required(self, strat: dict[str, Any], reason: str, details: dict[str, Any]) -> None:
        st = self._st(strat)
        signature = self._alert_signature(reason, details)
        if st.get("manual_alert_last_signature") == signature:
            return
        st["manual_alert_last_signature"] = signature
        st["manual_alert_last_reason"] = reason
        st["manual_alert_last_at_utc"] = dt_text(utc_now())
        self._notify_manual_action(strat, title="reconciliation_required", reason=f"{reason}; details={json.dumps(details, ensure_ascii=True, sort_keys=True, default=str)}", action="Inspect bot23-owned MT5 inventory and state before clearing the block.", key=f"bot23:reconciliation:{strat['id']}:{reason}")

    def _trade_row(self, event: str, strat: dict[str, Any], **kwargs: Any) -> None:
        now = utc_now()
        row = {
            "timestamp_utc": dt_text(now),
            "event": event,
            "strategy_id": strat["id"],
            "lane_id": int(strat["lane_id"]),
            "magic": int(strat["magic"]),
            "symbol": self.params["symbol"],
            "mt5_symbol": self.params.get("mt5_symbol", self.params["symbol"]),
            "basket_id": self._st(strat).get("current_basket_id") or "",
            "live": self.live_enabled,
        }
        row.update(kwargs)
        lane_id = int(strat["lane_id"])
        reason = str(row.get("reason") or "")
        coalesce = event == "entry_skip" and (
            reason in REPEATABLE_DIAGNOSTIC_REASONS or str(row.get("note") or "") == "sync_block"
        )
        active = self._diagnostic_repeats.get(lane_id)
        signature = (event, reason, str(row.get("note") or ""))
        if not coalesce:
            self._flush_diagnostic_repeat(lane_id, now)
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
            return
        if active is None or active["signature"] != signature:
            self._flush_diagnostic_repeat(lane_id, now)
            row["repeat_count"] = 1
            row["repeat_window_seconds"] = 0
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
            self._diagnostic_repeats[lane_id] = {
                "signature": signature,
                "first": now,
                "last": now,
                "suppressed": 0,
                "row": dict(row),
            }
            return
        active["last"] = now
        active["suppressed"] = int(active.get("suppressed", 0)) + 1
        interval = float(self.params.get("diagnostic_repeat_summary_seconds", 300.0))
        if (now - active["first"]).total_seconds() >= interval:
            self._flush_diagnostic_repeat(lane_id, now, keep_signature=True)

    def _flush_diagnostic_repeat(
        self,
        lane_id: int,
        now: datetime | None = None,
        *,
        keep_signature: bool = False,
    ) -> None:
        active = self._diagnostic_repeats.get(int(lane_id))
        if active is None:
            return
        at = now or utc_now()
        suppressed = int(active.get("suppressed", 0))
        if suppressed > 0:
            row = dict(active["row"])
            original_note = str(row.get("note") or "")
            row.update(
                {
                    "timestamp_utc": dt_text(at),
                    "event": "diagnostic_repeat_summary",
                    "repeat_count": suppressed,
                    "repeat_window_seconds": round(max(0.0, (active["last"] - active["first"]).total_seconds()), 3),
                    "note": f"source_event=entry_skip;source_note={original_note}",
                }
            )
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
        if keep_signature:
            active["first"] = at
            active["last"] = at
            active["suppressed"] = 0
        else:
            self._diagnostic_repeats.pop(int(lane_id), None)

    def _set_sync_block(
        self,
        strat: dict[str, Any],
        reason: str | None,
        details: dict[str, Any] | None = None,
        *,
        recoverable: bool = False,
    ) -> None:
        st = self._st(strat)
        previous = st.get("sync_block_reason")
        if reason:
            if recoverable and st.get("sync_block_new_entries") and not st.get("sync_block_recoverable"):
                lane_id = int(strat["lane_id"])
                signature = (str(previous or ""), str(reason))
                if self._last_retained_block_warning.get(lane_id) != signature:
                    logging.warning(
                        "S23 retained non-recoverable block for %s: existing=%s ignored_transient=%s",
                        strat["id"],
                        previous,
                        reason,
                    )
                    self._last_retained_block_warning[lane_id] = signature
                return
            self._last_retained_block_warning.pop(int(strat["lane_id"]), None)
            if previous != reason:
                st["flat_clear_confirmation_count"] = 0
                st["flat_clear_confirmation_reason"] = None
                logging.error("S23 new entries blocked for %s: %s", strat["id"], reason)
            st["sync_block_new_entries"] = True
            st["sync_block_reason"] = reason
            st["sync_block_recoverable"] = bool(recoverable)
            st["sync_block_details"] = details or {}
            if not recoverable:
                self._notify_reconciliation_required(strat, reason, st["sync_block_details"])
            return
        if st.get("sync_block_new_entries"):
            logging.warning("S23 new-entry block cleared for %s after clean sync: %s", strat["id"], previous)
        self._last_retained_block_warning.pop(int(strat["lane_id"]), None)
        st["sync_block_new_entries"] = False
        st["sync_block_reason"] = None
        st["sync_block_recoverable"] = False
        st["sync_block_details"] = {}
        st["flat_clear_confirmation_count"] = 0
        st["flat_clear_confirmation_reason"] = None
        st["manual_alert_last_signature"] = None
        st["manual_alert_last_reason"] = None

    @staticmethod
    def _side_from_record(record: Any) -> str:
        return "LONG" if int(getattr(record, "type", -1)) == ORDER_TYPE_BUY else "SHORT"

    def _owned_position(self, strat: dict[str, Any], record: Any) -> bool:
        return (
            str(getattr(record, "symbol", "")) == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and int(getattr(record, "magic", -1)) == int(strat["magic"])
            and str(getattr(record, "comment", "") or "").startswith(str(strat["comment_prefix"]))
        )

    def _state_matches_live(self, strat: dict[str, Any], state_pos: dict[str, Any], live_pos: Any) -> bool:
        position_id = int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0)
        live_position_id = int(getattr(live_pos, "identifier", 0) or getattr(live_pos, "ticket", 0))
        return (
            position_id > 0
            and position_id == live_position_id
            and str(state_pos.get("side")) == self._side_from_record(live_pos)
            and math.isclose(float(state_pos.get("lot") or 0.0), float(getattr(live_pos, "volume", 0.0)), rel_tol=0.0, abs_tol=1e-9)
            and self._owned_position(strat, live_pos)
        )

    def _state_ownership_proven(self, strat: dict[str, Any], state_pos: dict[str, Any]) -> bool:
        return (
            int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0) > 0
            and str(state_pos.get("owner_symbol") or "") == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and int(state_pos.get("owner_magic") or -1) == int(strat["magic"])
            and str(state_pos.get("owner_comment") or "").startswith(str(strat["comment_prefix"]))
            and str(state_pos.get("side") or "") in {"LONG", "SHORT"}
        )

    def _clear_basket_state(self, strat: dict[str, Any], reason: str, signal_bar: str | None = None) -> None:
        st = self._st(strat)
        st["basket"] = []
        st["last_add_price"] = None
        st["basket_peak_pnl_usd"] = None
        st["frozen_basket_atr30"] = None
        st["pending_close_reason"] = None
        st["pending_close_signal_bar"] = None
        st["time_close_defer_started_utc"] = None
        st["time_close_last_quote_msc"] = None
        st["time_close_stable_count"] = 0
        st["time_close_retry_after_utc"] = None
        st["time_close_wide_seen"] = False
        st["current_basket_id"] = None
        st["cooldown_until_bar"] = -1
        closed_bar = parse_ts(signal_bar)
        st["cooldown_until_utc"] = dt_text(closed_bar + pd.Timedelta(minutes=int(strat.get("cooldown", 0)))) if closed_bar is not None else None
        st["last_closed_at_utc"] = dt_text(utc_now())
        st["last_closed_reason"] = reason
        st["last_closed_signal_bar"] = signal_bar

    def _clear_pending_entry(self, strat: dict[str, Any]) -> None:
        st = self._st(strat)
        for key in (
            "pending_entry_side",
            "pending_entry_target",
            "pending_entry_expires_utc",
            "pending_entry_atr30",
            "pending_entry_signal_bar",
            "pending_entry_opportunity_id",
            "pending_entry_event_time",
            "pending_entry_release_time",
        ):
            st[key] = None

    def _cancel_portfolio_pending_longs(self, trigger_strat: dict[str, Any], reason: str) -> int:
        cancelled = 0
        trigger_lane = int(trigger_strat["lane_id"])
        for strat in self.params["strategies"]:
            st = self._st(strat)
            if str(st.get("pending_entry_side") or "") != "LONG":
                continue
            opportunity_id = st.get("pending_entry_opportunity_id")
            signal_bar = st.get("pending_entry_signal_bar")
            self._clear_pending_entry(strat)
            self._trade_row(
                "pending_cancelled",
                strat,
                opportunity_id=opportunity_id,
                reason=reason,
                signal_bar_time=signal_bar,
                note=f"trigger_lane={trigger_lane}",
            )
            cancelled += 1
        return cancelled

    def _basket_is_long(self, strat: dict[str, Any]) -> bool:
        basket = list(self._st(strat).get("basket") or [])
        return bool(basket) and all(str(pos.get("side") or "") == "LONG" for pos in basket)

    def _arm_long_target_portfolio_rearm(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp) -> None:
        routing = self.state["routing"]
        stamp = pd.Timestamp(at_utc)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        routing["long_target_rearm_pending_confirmation"] = True
        routing["long_target_rearm_request_utc"] = dt_text(stamp)
        routing["long_target_rearm_trigger_lane_id"] = int(strat["lane_id"])
        routing["long_target_rearm_trigger_basket_id"] = self._st(strat).get("current_basket_id")
        routing["long_target_rearm_expired_utc"] = None
        cancelled = self._cancel_portfolio_pending_longs(strat, "long_target_rearm_armed")
        self._trade_row(
            "portfolio_rearm_armed",
            strat,
            side="LONG",
            reason="basket_target",
            executable_at=dt_text(stamp),
            note=f"pending_long_cancelled={cancelled}",
        )

    def _confirm_long_target_portfolio_rearm(
        self,
        strat: dict[str, Any],
        confirmed_at_utc: datetime | pd.Timestamp,
        basket_id: str | None,
    ) -> None:
        routing = self.state["routing"]
        stamp = pd.Timestamp(confirmed_at_utc)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        minutes = int(self.params.get("long_target_portfolio_rearm_minutes", EXPECTED_PORTFOLIO_REARM_MINUTES))
        until = stamp + pd.Timedelta(minutes=minutes)
        routing["long_target_rearm_pending_confirmation"] = False
        routing["long_target_rearm_confirmed_utc"] = dt_text(stamp)
        routing["long_target_rearm_until_utc"] = dt_text(until)
        routing["long_target_rearm_trigger_lane_id"] = int(strat["lane_id"])
        routing["long_target_rearm_trigger_basket_id"] = basket_id
        routing["long_target_rearm_expired_utc"] = None
        cancelled = self._cancel_portfolio_pending_longs(strat, "long_target_rearm_started")
        self._trade_row(
            "portfolio_rearm_started",
            strat,
            side="LONG",
            reason="basket_target_confirmed",
            executable_at=dt_text(stamp),
            note=f"until={dt_text(until)};pending_long_cancelled={cancelled}",
        )

    def _portfolio_new_long_basket_block_reason(
        self,
        side: str,
        at_utc: datetime | pd.Timestamp | None,
    ) -> str | None:
        if side != "LONG" or not bool(self.params.get("long_target_portfolio_rearm_enabled", False)):
            return None
        routing = self.state["routing"]
        if bool(routing.get("long_target_rearm_pending_confirmation")):
            return "long_target_rearm_pending_close_confirmation"
        raw_until = routing.get("long_target_rearm_until_utc")
        if not raw_until:
            return None
        until = parse_ts(raw_until)
        if until is None:
            return "long_target_rearm_state_invalid"
        stamp = pd.Timestamp(at_utc if at_utc is not None else utc_now())
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        if stamp < until:
            return "long_target_portfolio_rearm"
        routing["long_target_rearm_until_utc"] = None
        routing["long_target_rearm_expired_utc"] = dt_text(stamp)
        logging.info("S23 LONG-target portfolio rearm expired at %s", dt_text(stamp))
        self._save_state()
        return None

    def _inventory_range_snapshot(self) -> tuple[bool, float | None, float | None, int, int]:
        long_positions = 0
        short_positions = 0
        entry_prices: list[float] = []
        for strat in self.params["strategies"]:
            for position in self._st(strat).get("basket") or []:
                side = str(position.get("side") or "").upper()
                long_positions += int(side == "LONG")
                short_positions += int(side == "SHORT")
                try:
                    entry_price = float(position.get("entry_price"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(entry_price):
                    entry_prices.append(entry_price)
        balanced = long_positions > 0 and long_positions == short_positions
        if not entry_prices:
            return balanced, None, None, long_positions, short_positions
        return balanced, min(entry_prices), max(entry_prices), long_positions, short_positions

    @staticmethod
    def _clear_inventory_range_break(range_state: dict[str, Any]) -> None:
        range_state["break_phase"] = 0
        range_state["break_side"] = None
        range_state["break_time_utc"] = None
        range_state["return_confirm_count"] = 0

    def _advance_inventory_range_fade(self, price_row: pd.Series) -> None:
        """Advance the fixed completed-M1 range-fade state exactly once per bar."""
        if not bool(self.params.get("inventory_range_fade_enabled", False)):
            return
        bar_time = parse_ts(price_row.name)
        try:
            completed_close = float(price_row["Close"])
        except (KeyError, TypeError, ValueError):
            return
        if bar_time is None or not math.isfinite(completed_close):
            return
        bar_text = dt_text(bar_time)
        routing = self.state["routing"]
        range_state = routing["inventory_range_fade"]
        if range_state.get("last_state_bar") == bar_text:
            return
        range_state["last_state_bar"] = bar_text
        try:
            phase = int(range_state.get("break_phase") or 0)
            confirm_count = int(range_state.get("return_confirm_count") or 0)
            pending_side = range_state.get("pending_side")
            frozen_low = float(range_state.get("low")) if range_state.get("low") is not None else math.nan
            frozen_high = float(range_state.get("high")) if range_state.get("high") is not None else math.nan
            frozen_valid = math.isfinite(frozen_low) and math.isfinite(frozen_high) and frozen_high > frozen_low
            range_state_valid = (
                phase in {0, 1}
                and confirm_count >= 0
                and pending_side in {None, "LONG", "SHORT"}
                and (not bool(range_state.get("active")) or frozen_valid)
                and (
                    phase == 0
                    or (
                        frozen_valid
                        and range_state.get("break_side") in {"LONG", "SHORT"}
                        and parse_ts(range_state.get("break_time_utc")) is not None
                    )
                )
            )
        except (TypeError, ValueError, OverflowError):
            range_state_valid = False
        if not range_state_valid:
            reset_state = dict(self._default_state()["routing"]["inventory_range_fade"])
            reset_state["last_state_bar"] = bar_text
            routing["inventory_range_fade"] = reset_state
            self._trade_row(
                "inventory_range_invalidated",
                self.params["strategies"][0],
                reason="malformed_persisted_range_state",
                signal_bar_time=bar_text,
            )
            self._save_state()
            return
        balanced, range_low_now, range_high_now, long_count, short_count = self._inventory_range_snapshot()
        primary = self.params["strategies"][0]
        broke_range = False

        if int(range_state.get("break_phase") or 0) != 0 and not balanced:
            self._trade_row(
                "inventory_range_invalidated",
                primary,
                reason="inventory_unbalanced",
                signal_bar_time=bar_text,
                note=f"long={long_count};short={short_count}",
            )
            self._clear_inventory_range_break(range_state)

        break_time = parse_ts(range_state.get("break_time_utc"))
        max_wait = int(self.params.get("inventory_range_max_wait_minutes", EXPECTED_INVENTORY_RANGE_MAX_WAIT_MINUTES))
        if (
            int(range_state.get("break_phase") or 0) != 0
            and max_wait > 0
            and break_time is not None
            and bar_time > break_time + pd.Timedelta(minutes=max_wait)
        ):
            self._trade_row(
                "inventory_range_invalidated",
                primary,
                reason="return_timeout",
                signal_bar_time=bar_text,
                note=f"break_time={dt_text(break_time)};max_wait_minutes={max_wait}",
            )
            self._clear_inventory_range_break(range_state)

        if int(range_state.get("break_phase") or 0) == 1:
            try:
                frozen_low = float(range_state.get("low"))
                frozen_high = float(range_state.get("high"))
            except (TypeError, ValueError):
                frozen_low = math.nan
                frozen_high = math.nan
            break_side = str(range_state.get("break_side") or "")
            if not math.isfinite(frozen_low) or not math.isfinite(frozen_high) or frozen_high <= frozen_low or break_side not in {"LONG", "SHORT"}:
                self._trade_row("inventory_range_invalidated", primary, reason="invalid_frozen_range", signal_bar_time=bar_text)
                self._clear_inventory_range_break(range_state)
            else:
                depth = float(self.params.get("inventory_range_return_depth_fraction", EXPECTED_INVENTORY_RANGE_RETURN_DEPTH))
                width = frozen_high - frozen_low
                upper_return_level = frozen_high - depth * width
                lower_return_level = frozen_low + depth * width
                returned_inside = (
                    break_side == "LONG" and completed_close <= upper_return_level
                ) or (
                    break_side == "SHORT" and completed_close >= lower_return_level
                )
                range_state["return_confirm_count"] = int(range_state.get("return_confirm_count") or 0) + 1 if returned_inside else 0
                confirm_bars = int(self.params.get("inventory_range_confirm_bars", EXPECTED_INVENTORY_RANGE_CONFIRM_BARS))
                if int(range_state["return_confirm_count"]) >= confirm_bars:
                    synthetic_side = "SHORT" if break_side == "LONG" else "LONG"
                    if not range_state.get("pending_side"):
                        range_state["pending_side"] = synthetic_side
                        range_state["pending_origin_bar"] = bar_text
                        range_state["pending_break_side"] = break_side
                        self._trade_row(
                            "inventory_range_opportunity_created",
                            primary,
                            side=synthetic_side,
                            reason="false_break_return_confirmed",
                            signal_bar_time=bar_text,
                            note=f"break_side={break_side};low={frozen_low};high={frozen_high};confirm_bars={confirm_bars}",
                        )
                    else:
                        self._trade_row(
                            "inventory_range_opportunity_dropped",
                            primary,
                            side=synthetic_side,
                            reason="pending_already_exists",
                            signal_bar_time=bar_text,
                        )
                    self._clear_inventory_range_break(range_state)

        if bool(range_state.get("active")) and int(range_state.get("break_phase") or 0) == 0:
            try:
                frozen_low = float(range_state.get("low"))
                frozen_high = float(range_state.get("high"))
            except (TypeError, ValueError):
                frozen_low = math.nan
                frozen_high = math.nan
            breakout_side = "LONG" if completed_close > frozen_high else "SHORT" if completed_close < frozen_low else None
            if breakout_side is not None:
                side_filter = str(self.params.get("inventory_range_break_side_filter", EXPECTED_INVENTORY_RANGE_BREAK_SIDE_FILTER)).lower()
                if side_filter == "both" or side_filter == breakout_side.lower():
                    range_state["break_phase"] = 1
                    range_state["break_side"] = breakout_side
                    range_state["break_time_utc"] = bar_text
                    range_state["return_confirm_count"] = 0
                    self._trade_row(
                        "inventory_range_break",
                        primary,
                        side=breakout_side,
                        reason="completed_m1_close_outside",
                        signal_bar_time=bar_text,
                        note=f"close={completed_close};low={frozen_low};high={frozen_high}",
                    )
                range_state["active"] = False
                broke_range = True
            elif not balanced:
                range_state["active"] = False
                self._trade_row(
                    "inventory_range_invalidated",
                    primary,
                    reason="inventory_unbalanced_before_break",
                    signal_bar_time=bar_text,
                    note=f"long={long_count};short={short_count}",
                )

        if (
            not broke_range
            and int(range_state.get("break_phase") or 0) == 0
            and not bool(range_state.get("active"))
            and balanced
            and range_low_now is not None
            and range_high_now is not None
            and range_high_now > range_low_now
            and range_low_now <= completed_close <= range_high_now
        ):
            range_state["active"] = True
            range_state["low"] = float(range_low_now)
            range_state["high"] = float(range_high_now)
            self._trade_row(
                "inventory_range_armed",
                primary,
                reason="balanced_book_close_inside",
                signal_bar_time=bar_text,
                note=f"long={long_count};short={short_count};low={range_low_now};high={range_high_now}",
            )
        self._save_state()

    def _take_inventory_range_fade_opportunity(
        self,
        *,
        raw_side: str | None,
        signal_bar: pd.Timestamp,
        poll_time: pd.Timestamp,
        symbol: str,
    ) -> dict[str, Any] | None:
        """Return one pending synthetic opportunity only when ZA is absent."""
        if raw_side or not bool(self.params.get("inventory_range_fade_enabled", False)):
            return None
        routing = self.state["routing"]
        range_state = routing["inventory_range_fade"]
        side = str(range_state.get("pending_side") or "")
        signal_bar_text = dt_text(signal_bar)
        if side not in {"LONG", "SHORT"} or range_state.get("last_dispatch_bar") == signal_bar_text:
            return None
        origin_bar = range_state.get("pending_origin_bar")
        break_side = range_state.get("pending_break_side")
        range_state["pending_side"] = None
        range_state["pending_origin_bar"] = None
        range_state["pending_break_side"] = None
        range_state["last_dispatch_bar"] = signal_bar_text
        self._save_state()  # durable take before any lane can submit an OPEN
        release_time = signal_bar + pd.Timedelta(minutes=1)
        policy = {
            "policy_id": str(self.params.get("inventory_range_fade_policy_id", EXPECTED_INVENTORY_RANGE_FADE_POLICY_ID)),
            "reason": "balanced_inventory_false_break_return",
            "action": "opposite_breakout",
            "origin_bar": origin_bar,
            "break_side": break_side,
        }
        return {
            "opportunity_id": f"{symbol}|{signal_bar_text}|INVENTORY_RANGE_FADE|{side}|{policy['policy_id']}",
            "source": "inventory_range_false_break_fade",
            "side": side,
            "raw_side": "",
            "effective_side": side,
            "entry_policy": policy,
            "event_time": signal_bar_text,
            "release_time": dt_text(release_time),
            "available_time": dt_text(release_time),
            "decision_time": dt_text(poll_time),
            "executable_at": dt_text(poll_time),
        }

    def _clear_pending_open(self, strat: dict[str, Any]) -> None:
        st = self._st(strat)
        st["pending_open_opportunity_id"] = None
        st["pending_open_started_utc"] = None

    @staticmethod
    def _low_vol_regime(strat: dict[str, Any], atr30: float | None) -> bool:
        threshold = float(strat.get("adaptive_fixed_exit_atr_threshold", 0.0))
        return threshold <= 0.0 or (atr30 is not None and math.isfinite(float(atr30)) and float(atr30) < threshold)

    def _exit_thresholds(self, strat: dict[str, Any]) -> tuple[float, float, float]:
        target = float(strat["basket_target_usd"])
        stop = float(strat["basket_stop_usd"])
        peak = float(strat.get("failure_to_progress_peak_usd", 0.0))
        atr30 = self._st(strat).get("frozen_basket_atr30")
        if self._low_vol_regime(strat, atr30) and atr30 is not None and math.isfinite(float(atr30)) and float(atr30) > 0.0:
            target = float(strat.get("target_atr_mult", 0.0)) * float(atr30) or target
            stop = float(strat.get("stop_atr_mult", 0.0)) * float(atr30) or stop
            peak = float(strat.get("failure_to_progress_peak_atr_mult", 0.0)) * float(atr30) or peak
        return target, stop, peak

    def _entry_submission_block_reason(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp | None = None) -> str | None:
        st = self._st(strat)
        if st.get("sync_block_new_entries"):
            return str(st.get("sync_block_reason") or "sync_block_new_entries")
        if st.get("pending_open_opportunity_id"):
            return "unresolved_open_action"
        retry_after = parse_ts(st.get("open_retry_after_utc"))
        if retry_after is None:
            return None
        stamp = pd.Timestamp(at_utc if at_utc is not None else utc_now())
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        return "open_retry_cooldown" if stamp < retry_after else None

    def connect_and_preflight(self) -> bool:
        namespace_error = self._ownership_namespace_error()
        if namespace_error:
            logging.critical("S23 ownership namespace invalid: %s", namespace_error)
            return False
        if any(self._st(strat).get("sync_block_reason") == "state_identity_mismatch" for strat in self._all_strategies()):
            logging.critical("S23 legacy/foreign state must be archived before this runner can start.")
            for strat in self._all_strategies():
                st = self._st(strat)
                if st.get("sync_block_reason") == "state_identity_mismatch":
                    self._notify_reconciliation_required(strat, "state_identity_mismatch", dict(st.get("sync_block_details") or {}))
            # Preserve the old on-disk state as evidence.  The operator must
            # reconcile/flat the account and archive it before first start.
            return False
        clock_now = utc_now()
        current_block = self._entry_admission_block(clock_now)
        eu_summer_time = is_eu_summer_time(clock_now)
        us_summer_time = is_us_summer_time(clock_now)
        logging.info(
            "S23 entry-admission clock loaded: eu_regime=%s us_regime=%s block=%s routing_enabled=false",
            "summer_time" if eu_summer_time else "standard_time",
            "summer_time" if us_summer_time else "standard_time",
            current_block.id if current_block else "off_block",
        )
        try:
            validate_csv_schema(TRADE_LOG_FILE, TRADE_FIELDS)
        except RuntimeError as exc:
            logging.critical("S23 trade audit schema preflight failed: %s", exc)
            return False
        if not bool(self.params.get("enabled", True)):
            logging.info("S23 disabled by params.")
            return False
        if not self.dm.connect():
            logging.error("S23 EA bridge connect failed.")
            return False
        caps = self.executor.get_bridge_capabilities()
        logging.info("S23 bridge caps: %s", caps)
        if not caps:
            logging.critical("S23 bridge capability query failed.")
            return False
        expected_bridge = str(self.params.get("expected_bridge_name") or "BotBridge_s23")
        if str(caps.get("name") or "") != expected_bridge:
            logging.critical("S23 wrong bridge attached: got=%s expected=%s", caps.get("name"), expected_bridge)
            return False
        missing = REQUIRED_SHARED_ACCOUNT_COMMANDS - {str(x).upper() for x in caps.get("commands", set())}
        if missing:
            logging.critical("S23 bridge missing required commands: %s", sorted(missing))
            return False
        legacy_error = self._legacy_inventory_error()
        if legacy_error is not None:
            logging.critical("S23 legacy ownership preflight failed: %s", legacy_error)
            return False
        if self.live_enabled:
            account = self.executor.get_account_info()
            if account is None:
                logging.critical("S23 account execution metadata unavailable.")
                return False
            account_identity_error = self._account_identity_error(account)
            if account_identity_error is not None:
                logging.critical("S23 account identity mismatch: %s", account_identity_error)
                return False
            if bool(self.params.get("require_hedging_account", True)) and int(account.get("margin_mode", -1)) != HEDGING_MARGIN_MODE:
                logging.critical("S23 live trading requires a hedging account: mode=%s", account.get("margin_mode_name"))
                return False
            symbol_info = self.executor.get_symbol_info(str(self.params.get("mt5_symbol", self.params["symbol"])))
            if symbol_info is None or getattr(symbol_info, "quote_time_msc", None) is None:
                logging.critical("S23 bridge INFO response lacks broker quote timestamp; compile and attach the updated BotBridge_s23 before live use.")
                return False
        if self._entry_policy_state_migrated or self._portfolio_rearm_state_migrated or self._inventory_range_fade_state_migrated or self._morning_session_state_migrated or self._midday_session_state_migrated:
            self._save_state()
            self._entry_policy_state_migrated = False
            self._portfolio_rearm_state_migrated = False
            self._inventory_range_fade_state_migrated = False
            self._morning_session_state_migrated = False
            self._midday_session_state_migrated = False
        return True

    def _ownership_namespace_error(self) -> str | None:
        if str(self.params.get("strategy_id") or "") != EXPECTED_STRATEGY_ID:
            return f"invalid_strategy_id={self.params.get('strategy_id')} expected={EXPECTED_STRATEGY_ID}"
        if str(self.params.get("candidate_id") or "") != EXPECTED_CANDIDATE_ID:
            return f"invalid_candidate_id={self.params.get('candidate_id')} expected={EXPECTED_CANDIDATE_ID}"
        if str(self.params.get("routing_mode") or "") != EXPECTED_ROUTING_MODE:
            return f"invalid_routing_mode={self.params.get('routing_mode')} expected={EXPECTED_ROUTING_MODE}"
        if str(self.params.get("entry_policy_id") or "") != EXPECTED_ENTRY_POLICY_ID:
            return f"invalid_entry_policy_id={self.params.get('entry_policy_id')} expected={EXPECTED_ENTRY_POLICY_ID}"
        if str(self.params.get("entry_policy_params_hash") or "") != EXPECTED_ENTRY_POLICY_PARAMS_HASH:
            return "invalid_entry_policy_params_hash"
        if not bool(self.params.get("long_target_portfolio_rearm_enabled", False)):
            return "long_target_portfolio_rearm_disabled"
        if str(self.params.get("portfolio_rearm_policy_id") or "") != EXPECTED_PORTFOLIO_REARM_POLICY_ID:
            return f"invalid_portfolio_rearm_policy_id={self.params.get('portfolio_rearm_policy_id')}"
        if str(self.params.get("portfolio_rearm_params_hash") or "") != EXPECTED_PORTFOLIO_REARM_PARAMS_HASH:
            return "invalid_portfolio_rearm_params_hash"
        if int(self.params.get("long_target_portfolio_rearm_minutes") or 0) != EXPECTED_PORTFOLIO_REARM_MINUTES:
            return f"invalid_long_target_portfolio_rearm_minutes={self.params.get('long_target_portfolio_rearm_minutes')}"
        if not bool(self.params.get("inventory_range_fade_enabled", False)):
            return "inventory_range_fade_disabled"
        if str(self.params.get("inventory_range_fade_policy_id") or "") != EXPECTED_INVENTORY_RANGE_FADE_POLICY_ID:
            return f"invalid_inventory_range_fade_policy_id={self.params.get('inventory_range_fade_policy_id')}"
        if str(self.params.get("inventory_range_fade_params_hash") or "") != EXPECTED_INVENTORY_RANGE_FADE_PARAMS_HASH:
            return "invalid_inventory_range_fade_params_hash"
        if not math.isclose(
            float(self.params.get("inventory_range_return_depth_fraction") or 0.0),
            EXPECTED_INVENTORY_RANGE_RETURN_DEPTH,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return f"invalid_inventory_range_return_depth={self.params.get('inventory_range_return_depth_fraction')}"
        if int(self.params.get("inventory_range_max_wait_minutes") or 0) != EXPECTED_INVENTORY_RANGE_MAX_WAIT_MINUTES:
            return f"invalid_inventory_range_max_wait_minutes={self.params.get('inventory_range_max_wait_minutes')}"
        if int(self.params.get("inventory_range_confirm_bars") or 0) != EXPECTED_INVENTORY_RANGE_CONFIRM_BARS:
            return f"invalid_inventory_range_confirm_bars={self.params.get('inventory_range_confirm_bars')}"
        if str(self.params.get("inventory_range_break_side_filter") or "").lower() != EXPECTED_INVENTORY_RANGE_BREAK_SIDE_FILTER:
            return f"invalid_inventory_range_break_side_filter={self.params.get('inventory_range_break_side_filter')}"
        if not bool(self.params.get("late_short_30m_action_enabled", False)):
            return "late_short_30m_action_disabled"
        if int(self.params.get("late_short_lookback_completed_m1_bars") or 0) != EXPECTED_LATE_SHORT_LOOKBACK:
            return f"invalid_late_short_lookback={self.params.get('late_short_lookback_completed_m1_bars')}"
        if not math.isclose(
            float(self.params.get("late_short_drop_threshold") or 0.0),
            EXPECTED_LATE_SHORT_DROP_THRESHOLD,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return f"invalid_late_short_drop_threshold={self.params.get('late_short_drop_threshold')}"
        if str(self.params.get("late_short_action") or "") != EXPECTED_LATE_SHORT_ACTION:
            return f"invalid_late_short_action={self.params.get('late_short_action')}"
        if int(self.params.get("lane_count") or 0) != 4:
            return f"invalid_lane_count={self.params.get('lane_count')} expected=4"
        strategies = [row for row in self.params.get("strategies", []) if bool(row.get("enabled", True))]
        magics = [int(row.get("magic") or 0) for row in strategies]
        configured_magics = tuple(int(value) for value in self.params.get("expected_magics", []))
        prefixes = [str(row.get("comment_prefix") or "") for row in strategies]
        lane_ids = [int(row.get("lane_id") or 0) for row in strategies]
        if tuple(magics) != EXPECTED_S23_MAGICS:
            return f"invalid_magics={magics} expected={list(EXPECTED_S23_MAGICS)}"
        if configured_magics != EXPECTED_S23_MAGICS:
            return f"invalid_expected_magics={list(configured_magics)} expected={list(EXPECTED_S23_MAGICS)}"
        if lane_ids != [1, 2, 3, 4]:
            return f"invalid_lane_ids={lane_ids} expected={[1, 2, 3, 4]}"
        if len(magics) != len(set(magics)):
            return f"duplicate_magics={magics}"
        if any(not prefix.startswith("s23_") for prefix in prefixes) or len(prefixes) != len(set(prefixes)):
            return f"invalid_or_duplicate_comment_prefixes={prefixes}"
        for row in strategies:
            expected_spec = f"bot23_late_short_30m_action_matrix_v001_reverse_d60_lane_{int(row['lane_id'])}"
            if str(row.get("spec_id") or "") != expected_spec:
                return f"invalid_lane_spec_id:{row.get('id')}:{row.get('spec_id')} expected={expected_spec}"
            drift = {key: {"actual": row.get(key), "expected": expected} for key, expected in FROZEN_LANE_FIELDS.items() if row.get(key) != expected}
            if drift:
                return f"frozen_lane_contract_drift:{row.get('id')}:{json.dumps(drift, sort_keys=True)}"
        if not bool(self.params.get("morning_session_enabled", False)):
            return "morning_session_disabled"
        if str(self.params.get("morning_session_policy_id") or "") != EXPECTED_MORNING_POLICY_ID:
            return f"invalid_morning_policy_id={self.params.get('morning_session_policy_id')}"
        if str(self.params.get("morning_session_params_hash") or "") != EXPECTED_MORNING_POLICY_PARAMS_HASH:
            return "invalid_morning_policy_params_hash"
        if int(self.params.get("morning_session_start_utc", -1)) != EXPECTED_MORNING_SESSION_START_UTC:
            return f"invalid_morning_session_start_utc={self.params.get('morning_session_start_utc')}"
        if int(self.params.get("morning_session_end_utc", -1)) != EXPECTED_MORNING_SESSION_END_UTC:
            return f"invalid_morning_session_end_utc={self.params.get('morning_session_end_utc')}"
        if int(self.params.get("morning_session_max_positions", 0)) != EXPECTED_MORNING_MAX_POSITIONS:
            return f"invalid_morning_session_max_positions={self.params.get('morning_session_max_positions')}"
        morning = [row for row in self._morning_strategies() if bool(row.get("enabled", True))]
        morning_magics = [int(row.get("magic") or 0) for row in morning]
        configured_morning_magics = tuple(int(value) for value in self.params.get("expected_morning_magics", []))
        if tuple(morning_magics) != EXPECTED_MORNING_MAGICS or configured_morning_magics != EXPECTED_MORNING_MAGICS:
            return f"invalid_morning_magics={morning_magics} configured={list(configured_morning_magics)}"
        if [int(row.get("lane_id") or 0) for row in morning] != [5, 6, 7]:
            return "invalid_morning_lane_ids"
        expected_morning = [
            ("jst09_range_false_break_confirm_direction_control", 15, "s23_am_l1"),
            ("price_effort_divergence_edge_direction_control", 55, "s23_am_l2"),
            ("m15_compression_m5_edge_release_primary", 45, "s23_am_l3"),
        ]
        for row, (signal_id, hold_minutes, comment) in zip(morning, expected_morning):
            if (
                str(row.get("signal_id") or "") != signal_id
                or int(row.get("hold_minutes") or 0) != hold_minutes
                or str(row.get("comment_prefix") or "") != comment
                or not math.isclose(float(row.get("lot") or 0.0), 0.01, rel_tol=0.0, abs_tol=1e-12)
                or int(row.get("max_positions") or 0) != 1
                or int(row.get("cooldown", -1)) != 0
            ):
                return f"invalid_morning_lane_contract:{row.get('id')}"
        if not bool(self.params.get("midday_session_enabled", False)):
            return "midday_session_disabled"
        if str(self.params.get("midday_session_policy_id") or "") != EXPECTED_MIDDAY_POLICY_ID:
            return f"invalid_midday_policy_id={self.params.get('midday_session_policy_id')}"
        if str(self.params.get("midday_session_params_hash") or "") != EXPECTED_MIDDAY_POLICY_PARAMS_HASH:
            return "invalid_midday_policy_params_hash"
        if int(self.params.get("midday_session_start_utc", -1)) != EXPECTED_MIDDAY_SESSION_START_UTC:
            return f"invalid_midday_session_start_utc={self.params.get('midday_session_start_utc')}"
        if int(self.params.get("midday_session_end_utc", -1)) != EXPECTED_MIDDAY_SESSION_END_UTC:
            return f"invalid_midday_session_end_utc={self.params.get('midday_session_end_utc')}"
        if int(self.params.get("midday_session_max_positions", 0)) != EXPECTED_MIDDAY_MAX_POSITIONS:
            return f"invalid_midday_session_max_positions={self.params.get('midday_session_max_positions')}"
        midday = [row for row in self._midday_strategies() if bool(row.get("enabled", True))]
        midday_magics = [int(row.get("magic") or 0) for row in midday]
        configured_midday_magics = tuple(int(value) for value in self.params.get("expected_midday_magics", []))
        if tuple(midday_magics) != EXPECTED_MIDDAY_MAGICS or configured_midday_magics != EXPECTED_MIDDAY_MAGICS:
            return f"invalid_midday_magics={midday_magics} configured={list(configured_midday_magics)}"
        if [int(row.get("lane_id") or 0) for row in midday] != [8]:
            return "invalid_midday_lane_ids"
        expected_midday = {
            "signal_id": "round_s2p5_d0p05_r0p03", "hold_minutes": 60, "comment_prefix": "s23_md_l1",
            "lot": 0.01, "max_positions": 1, "cooldown": 0, "level_step": 2.5, "atr_period": 60,
            "min_sweep_depth_atr": 0.05, "reclaim_atr": 0.03,
        }
        for row in midday:
            drift = {key: {"actual": row.get(key), "expected": value} for key, value in expected_midday.items() if row.get(key) != value}
            if str(row.get("spec_id") or "") != EXPECTED_MIDDAY_POLICY_ID or drift:
                return f"invalid_midday_lane_contract:{row.get('id')}:{json.dumps(drift, sort_keys=True)}"
        all_magics = magics + morning_magics + midday_magics
        all_prefixes = prefixes + [str(row.get("comment_prefix") or "") for row in morning + midday]
        if len(all_magics) != len(set(all_magics)) or len(all_prefixes) != len(set(all_prefixes)):
            return "duplicate_combined_ownership_namespace"
        admission_clock = self.params.get("eu_entry_admission_clock")
        if not isinstance(admission_clock, dict):
            return "missing_eu_entry_admission_clock"
        if str(admission_clock.get("eu_timezone") or "") != EXPECTED_ENTRY_ADMISSION_EU_TIMEZONE:
            return f"invalid_entry_admission_eu_timezone={admission_clock.get('eu_timezone')}"
        if str(admission_clock.get("us_timezone") or "") != EXPECTED_ENTRY_ADMISSION_US_TIMEZONE:
            return f"invalid_entry_admission_us_timezone={admission_clock.get('us_timezone')}"
        if str(admission_clock.get("notation") or "") != EXPECTED_ENTRY_ADMISSION_NOTATION:
            return f"invalid_entry_admission_notation={admission_clock.get('notation')}"
        if str(admission_clock.get("scope") or "") != EXPECTED_ENTRY_ADMISSION_SCOPE:
            return f"invalid_entry_admission_scope={admission_clock.get('scope')}"
        if str(admission_clock.get("position_lifecycle") or "") != EXPECTED_POSITION_LIFECYCLE:
            return f"invalid_position_lifecycle={admission_clock.get('position_lifecycle')}"
        if bool(admission_clock.get("routing_enabled", True)):
            return "entry_admission_routing_must_remain_disabled_until_signal_adoption"
        observed_blocks = tuple(
            (
                str(row.get("id") or ""),
                str((row.get("start") or {}).get("reference_clock") or ""),
                str((row.get("start") or {}).get("dst_jst") or ""),
                str((row.get("start") or {}).get("standard_jst") or ""),
                str((row.get("end") or {}).get("reference_clock") or ""),
                str((row.get("end") or {}).get("dst_jst") or ""),
                str((row.get("end") or {}).get("standard_jst") or ""),
            )
            for row in admission_clock.get("blocks", [])
        )
        if observed_blocks != EXPECTED_ENTRY_ADMISSION_BLOCKS:
            return f"invalid_entry_admission_blocks={observed_blocks}"
        return None

    def _get_m1(self) -> pd.DataFrame | None:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        bars = self.dm.get_historical_data(
            symbol,
            int(self.params.get("m1_timeframe", 1)),
            int(self.params.get("m1_bars", 240)),
            str(self.params.get("broker_timezone", "UTC")),
            drop_latest=bool(self.params.get("drop_latest_m1_bar", True)),
        )
        if bars is None or len(bars) < 100:
            return None
        point = float(self.params.get("point_size", 0.01))
        return add_features(bars, point)

    def _signal(self, row: pd.Series, strat: dict[str, Any]) -> str | None:
        ts = pd.Timestamp(row.name)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts = ts.tz_convert("UTC")
        if not in_session(ts, int(strat["session_start_utc"]), int(strat["session_end_utc"])):
            return None
        if float(row.get("spread_points", 0.0)) > float(self.params.get("max_entry_spread_points", 300.0)):
            return None
        atr30 = float(row.get("atr30", math.nan))
        vol_ratio = float(row.get("vol_ratio", math.nan))
        if not math.isfinite(atr30) or not math.isfinite(vol_ratio) or vol_ratio < float(strat.get("vol_min", 1.0)):
            return None
        imp = float(row["ret5"] if int(strat["impulse_bars"]) <= 6 else row["ret10"])
        long_imp = imp >= float(strat["impulse_atr"]) * atr30
        short_imp = -imp >= float(strat["impulse_atr"]) * atr30
        long_break = float(row["Close"]) >= float(row["roll_high30"])
        short_break = float(row["Close"]) <= float(row["roll_low30"])
        mode = str(strat["mode"])
        if mode == "impulse":
            long_ok, short_ok = long_imp, short_imp
        elif mode == "breakout_impulse":
            long_ok, short_ok = long_imp and long_break, short_imp and short_break
        else:
            return None
        if long_ok:
            return "LONG"
        if short_ok:
            return "SHORT"
        return None

    @staticmethod
    def _morning_signal_sides(bars: pd.DataFrame) -> dict[str, str | None]:
        """Return stable_001 sides for the latest completed M1 bar."""
        signal_ids = (
            "jst09_range_false_break_confirm_direction_control",
            "price_effort_divergence_edge_direction_control",
            "m15_compression_m5_edge_release_primary",
        )
        result = {signal_id: None for signal_id in signal_ids}
        if bars.empty:
            return result
        frame = bars.copy()
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        else:
            frame.index = frame.index.tz_convert("UTC")
        required = {"Open", "High", "Low", "Close"}
        if not required.issubset(frame.columns):
            return result
        o = frame["Open"].astype(float)
        h = frame["High"].astype(float)
        low = frame["Low"].astype(float)
        c = frame["Close"].astype(float)
        activity = frame.get("Volume", pd.Series(0.0, index=frame.index)).astype(float)
        decision_time = frame.index[-1] + pd.Timedelta(minutes=1)
        if not in_session(decision_time, EXPECTED_MORNING_SESSION_START_UTC, EXPECTED_MORNING_SESSION_END_UTC):
            return result

        day = pd.Series(frame.index.date, index=frame.index)
        minute = frame.index.hour * 60 + frame.index.minute
        hour0 = minute < 60
        hour1 = (minute >= 60) & (minute < 120)
        h0_high = h.where(hour0).groupby(day).transform("max")
        h0_low = low.where(hour0).groupby(day).transform("min")
        swept_high = hour1 & (h > h0_high) & (c < h0_high)
        swept_low = hour1 & (low < h0_low) & (c > h0_low)
        primary_short = swept_high.shift(1, fill_value=False) & (c < o) & (c < c.shift(1))
        primary_long = swept_low.shift(1, fill_value=False) & (c > o) & (c > c.shift(1))
        if bool(primary_long.iloc[-1]):
            result[signal_ids[0]] = "SHORT"
        elif bool(primary_short.iloc[-1]):
            result[signal_ids[0]] = "LONG"

        delta = c.diff()
        pressure = (delta.apply(lambda value: 1.0 if value > 0 else (-1.0 if value < 0 else 0.0)) * activity).rolling(15, min_periods=15).sum()
        prior_high = h.shift(1).rolling(15, min_periods=15).max()
        prior_low = low.shift(1).rolling(15, min_periods=15).min()
        primary_div_short = (h > prior_high) & (c < prior_high) & (pressure < 0)
        primary_div_long = (low < prior_low) & (c > prior_low) & (pressure > 0)
        if bool(primary_div_long.iloc[-1]):
            result[signal_ids[1]] = "SHORT"
        elif bool(primary_div_short.iloc[-1]):
            result[signal_ids[1]] = "LONG"

        aggregate = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        m5 = frame.assign(Volume=activity).resample("5min", label="left", closed="left").agg(aggregate).dropna(subset=["Open", "High", "Low", "Close"])
        m15 = frame.assign(Volume=activity).resample("15min", label="left", closed="left").agg(aggregate).dropna(subset=["Open", "High", "Low", "Close"])
        if not m5.empty and not m15.empty:
            m15_range = (m15["High"] - m15["Low"]).clip(lower=0.001)
            reference = m15_range.shift(1).rolling(4, min_periods=4).median()
            compressed = m15_range <= 0.60 * reference
            comp_high = completed_align(m15["High"].where(compressed), 15, m5).ffill(limit=3)
            comp_low = completed_align(m15["Low"].where(compressed), 15, m5).ffill(limit=3)
            m5_side = pd.Series(0, index=m5.index, dtype=int)
            m5_side = m5_side.mask(m5["Close"] > comp_high, 1).mask(m5["Close"] < comp_low, -1)
            aligned = completed_align(m5_side, 5, frame).fillna(0).astype(int)
            m1_pulse = aligned.where(aligned.ne(aligned.shift(1)), 0)
            if int(m1_pulse.iloc[-1]) > 0:
                result[signal_ids[2]] = "LONG"
            elif int(m1_pulse.iloc[-1]) < 0:
                result[signal_ids[2]] = "SHORT"
        return result

    @staticmethod
    def _midday_signal_side(bars: pd.DataFrame, strat: dict[str, Any]) -> str | None:
        """Return the fixed round-level sweep onset for the latest completed M1."""
        if bars.empty:
            return None
        frame = bars.copy()
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        else:
            frame.index = frame.index.tz_convert("UTC")
        required = {"High", "Low", "Close"}
        if not required.issubset(frame.columns):
            return None
        decision_time = frame.index[-1] + pd.Timedelta(minutes=1)
        if not in_session(decision_time, EXPECTED_MIDDAY_SESSION_START_UTC, EXPECTED_MIDDAY_SESSION_END_UTC):
            return None
        atr_period = int(strat.get("atr_period", 60))
        if atr_period <= 0 or len(frame) < atr_period + 2:
            return None
        high = frame["High"].astype(float)
        low = frame["Low"].astype(float)
        close = frame["Close"].astype(float)
        previous_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = true_range.rolling(atr_period, min_periods=atr_period).mean()
        step = float(strat.get("level_step", 2.5))
        min_depth = float(strat.get("min_sweep_depth_atr", 0.05))
        reclaim = float(strat.get("reclaim_atr", 0.03))
        if step <= 0.0:
            return None
        scaled_previous = previous_close / step
        upper_level = scaled_previous.apply(lambda value: math.ceil(value) if pd.notna(value) else math.nan) * step
        lower_level = scaled_previous.apply(lambda value: math.floor(value) if pd.notna(value) else math.nan) * step
        valid_atr = atr.where(atr > 0.0)
        long_depth = (lower_level - low) / valid_atr
        short_depth = (high - upper_level) / valid_atr
        long_raw = (low < lower_level) & (long_depth >= min_depth) & (close > lower_level + reclaim * valid_atr)
        short_raw = (high > upper_level) & (short_depth >= min_depth) & (close < upper_level - reclaim * valid_atr)
        raw = pd.Series(
            [1 if bool(long_ok) else (-1 if bool(short_ok) else 0) for long_ok, short_ok in zip(long_raw.fillna(False), short_raw.fillna(False))],
            index=frame.index,
            dtype=int,
        )
        onset = raw.where(raw.ne(raw.shift(1).fillna(0)), 0)
        latest = int(onset.iloc[-1])
        return "LONG" if latest > 0 else ("SHORT" if latest < 0 else None)

    def _apply_entry_policy(
        self,
        raw_side: str,
        bars: pd.DataFrame,
        info: Any,
    ) -> tuple[str | None, dict[str, Any]]:
        policy = {
            "policy_id": str(self.params.get("entry_policy_id", "")),
            "raw_side": raw_side,
            "effective_side": raw_side,
            "action": "unchanged",
            "reason": "not_short",
            "lookback_completed_m1_bars": int(self.params.get("late_short_lookback_completed_m1_bars", 0)),
            "drop_threshold": float(self.params.get("late_short_drop_threshold", 0.0)),
            "prior30_close": None,
            "signal_bid": None,
            "decline_ratio": None,
        }
        if raw_side != "SHORT" or not bool(self.params.get("late_short_30m_action_enabled", False)):
            return raw_side, policy
        lookback = int(policy["lookback_completed_m1_bars"])
        if lookback <= 0 or len(bars) < lookback + 1:
            policy.update({"effective_side": None, "action": "blocked", "reason": "insufficient_completed_m1_history"})
            return None, policy
        try:
            # At the first executable poll after signal-bar completion, -31 is
            # the completed-M1 close exactly 30 minutes before the current
            # executable quote.  The quote itself, not the signal-bar close,
            # matches the frozen tick replay definition.
            prior_close = float(bars["Close"].iloc[-(lookback + 1)])
            signal_bid = float(info.bid)
        except (KeyError, TypeError, ValueError, OverflowError):
            policy.update({"effective_side": None, "action": "blocked", "reason": "late_short_price_unavailable"})
            return None, policy
        if not math.isfinite(prior_close) or prior_close <= 0.0 or not math.isfinite(signal_bid):
            policy.update({"effective_side": None, "action": "blocked", "reason": "late_short_price_invalid"})
            return None, policy
        decline = (signal_bid - prior_close) / prior_close
        policy.update({"prior30_close": prior_close, "signal_bid": signal_bid, "decline_ratio": decline})
        if decline <= -float(policy["drop_threshold"]):
            if str(self.params.get("late_short_action")) != "reverse_long":
                policy.update({"effective_side": None, "action": "blocked", "reason": "unsupported_late_short_action"})
                return None, policy
            policy.update({"effective_side": "LONG", "action": "reverse_long", "reason": "late_short_drop_threshold_met"})
            return "LONG", policy
        policy["reason"] = "late_short_drop_threshold_not_met"
        return "SHORT", policy

    @staticmethod
    def _entry_policy_note(policy: dict[str, Any]) -> str:
        values = {
            "policy": policy.get("policy_id"),
            "raw_side": policy.get("raw_side"),
            "effective_side": policy.get("effective_side"),
            "action": policy.get("action"),
            "reason": policy.get("reason"),
            "lookback": policy.get("lookback_completed_m1_bars"),
            "threshold": policy.get("drop_threshold"),
            "prior30_close": policy.get("prior30_close"),
            "signal_bid": policy.get("signal_bid"),
            "decline_ratio": policy.get("decline_ratio"),
        }
        return ";".join(f"{key}={value}" for key, value in values.items())

    def _basket_pnl(self, strat: dict[str, Any], bid: float, ask: float) -> float:
        pnl = 0.0
        contract = float(self.params.get("contract_size", 100.0))
        for pos in self._st(strat)["basket"]:
            lot = float(pos["lot"])
            if pos["side"] == "LONG":
                pnl += (bid - float(pos["entry_price"])) * contract * lot
            else:
                pnl += (float(pos["entry_price"]) - ask) * contract * lot
        return pnl

    def _get_confirmed_close_deal(self, position_id: int, opened_at_epoch: int) -> Any:
        deal = self.executor.get_position_close_deal(position_id, opened_at_epoch)
        if deal is False and opened_at_epoch > 0:
            deal = self.executor.get_position_close_deal(position_id, 0)
        return deal

    def _sync_strategy(self, strat: dict[str, Any]) -> bool:
        """Reconcile ownership and report whether known inventory may be monitored.

        A True result does not imply that new entries are permitted.  Entry and
        add admission remains controlled by ``sync_block_new_entries``.  This
        distinction lets an exactly reconciled existing basket retain its
        quote-based exits while an ambiguous later OPEN remains fail-closed.
        """
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        st = self._st(strat)
        positions = self.executor.get_positions(symbol, int(strat["magic"]))
        if positions is None:
            self._set_sync_block(strat, "positions_unavailable", recoverable=True)
            return False
        unexpected_positions = [record for record in positions if not self._owned_position(strat, record)]
        if unexpected_positions:
            self._set_sync_block(strat, "same_magic_unexpected_position_or_order", {"tickets": [int(record.ticket) for record in unexpected_positions], "comments": [str(record.comment or "") for record in unexpected_positions]}, recoverable=False)
            return False
        queried_orders = self.executor.get_orders(symbol, int(strat["magic"]))
        orders_available = queried_orders is not None
        orders = list(queried_orders or [])
        if not orders_available:
            self._set_sync_block(strat, "orders_unavailable", recoverable=True)
        if orders:
            self._set_sync_block(
                strat,
                "same_magic_unexpected_order",
                {"tickets": [int(record.ticket) for record in orders], "comments": [str(record.comment or "") for record in orders]},
                recoverable=False,
            )
            return False
        if clean_sync_block_if_flat(
            symbol_key=strat["id"],
            state=st,
            positions=positions,
            orders=orders if orders_available else None,
            save_state=self._save_state,
            options=self.safety,
            audit=lambda _symbol, event, reason: self._trade_row(event, strat, reason=reason),
            flat_auto_clear_reasons=FLAT_AUTO_CLEAR_SYNC_REASONS,
            confirm_position_absent=self.executor.confirm_position_absent,
            required_flat_confirmations=(
                3 if st.get("sync_block_reason") == "unresolved_open_action" else 2
            ),
        ):
            logging.info("S23 clean sync cleared: %s", strat["id"])
            self._clear_pending_open(strat)
            self._save_state()
        if not self.live_enabled:
            return not bool(st.get("sync_block_new_entries"))
        state_basket = list(st.get("basket") or [])
        unresolved_open = bool(st.get("pending_open_opportunity_id"))
        if not state_basket and positions:
            self._set_sync_block(
                strat,
                "live_positions_without_state",
                {"tickets": [int(pos.ticket) for pos in positions]},
                recoverable=False,
            )
            return False
        if not state_basket and unresolved_open:
            self._set_sync_block(
                strat,
                "unresolved_open_action",
                {
                    "opportunity_id": str(st.get("pending_open_opportunity_id") or ""),
                    "started_utc": st.get("pending_open_started_utc"),
                    "live_tickets": [int(pos.ticket) for pos in positions],
                },
                recoverable=False,
            )
            return False
        if not state_basket:
            return not bool(st.get("sync_block_new_entries"))
        if state_basket:
            live_by_id = {int(getattr(pos, "identifier", 0) or pos.ticket): pos for pos in positions}
            state_ids = {int(pos.get("position_identifier") or pos.get("ticket") or 0) for pos in state_basket}
            unexpected_live_ids = set(live_by_id) - state_ids
            if unexpected_live_ids:
                self._set_sync_block(
                    strat,
                    "state_ticket_unowned_or_foreign",
                    {"state_ids": sorted(state_ids), "live_ids": sorted(live_by_id)},
                    recoverable=False,
                )
                return False
            remaining_state: list[dict[str, Any]] = []
            confirmed_deals = []
            for state_pos in state_basket:
                position_id = int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0)
                live_pos = live_by_id.get(position_id)
                if live_pos is not None:
                    if not self._state_matches_live(strat, state_pos, live_pos):
                        self._set_sync_block(strat, "state_position_ownership_mismatch", {"ticket": position_id}, recoverable=False)
                        return False
                    if "hold_minutes" in strat:
                        broker_open_epoch = int(getattr(live_pos, "open_time", 0) or 0)
                        if broker_open_epoch <= 0:
                            self._set_sync_block(
                                strat,
                                "confirmed_fill_time_unavailable",
                                {"ticket": position_id},
                                recoverable=True,
                            )
                            return False
                        broker_entry_time = pd.Timestamp(broker_open_epoch, unit="s", tz="UTC")
                        persisted_entry_time = parse_ts(state_pos.get("entry_time_utc"))
                        if persisted_entry_time != broker_entry_time:
                            previous_entry_time = state_pos.get("entry_time_utc")
                            state_pos["entry_time_utc"] = dt_text(broker_entry_time)
                            state_pos["open_time_epoch"] = broker_open_epoch
                            self._trade_row(
                                "position_lifecycle_recovered",
                                strat,
                                ticket=int(state_pos.get("ticket") or position_id),
                                position_identifier=position_id,
                                reason="confirmed_broker_fill_time_restored",
                                note=f"previous_entry_time_utc={previous_entry_time};broker_entry_time_utc={dt_text(broker_entry_time)}",
                            )
                            self._save_state()
                    remaining_state.append(state_pos)
                    continue
                opened_at_epoch = max(0, int(state_pos.get("open_time_epoch") or 0) - 60)
                deal = self._get_confirmed_close_deal(position_id, opened_at_epoch)
                if deal is None:
                    self._set_sync_block(strat, "close_deal_query_unavailable", {"ticket": position_id}, recoverable=False)
                    return False
                if deal is False:
                    self._set_sync_block(strat, "close_deal_not_confirmed", {"ticket": position_id}, recoverable=False)
                    return False
                if int(deal.position_id) != position_id or str(deal.symbol) != symbol or not self._state_ownership_proven(strat, state_pos):
                    self._set_sync_block(
                        strat,
                        "close_deal_ownership_mismatch",
                        {"ticket": position_id, "deal_position_id": int(deal.position_id), "deal_magic": int(deal.magic), "deal_symbol": str(deal.symbol)},
                        recoverable=False,
                    )
                    return False
                confirmed_deals.append((state_pos, deal))
            if confirmed_deals:
                reason = str(st.get("pending_close_reason") or "broker_or_external_close_confirmed")
                signal_bar = st.get("pending_close_signal_bar")
                closed_basket_id = st.get("current_basket_id")
                closed_basket_was_long = bool(state_basket) and all(
                    str(pos.get("side") or "") == "LONG" for pos in state_basket
                )
                confirmed_net = sum(float(deal.net_profit) for _state_pos, deal in confirmed_deals)
                st["basket"] = remaining_state
                confirmed_times: list[pd.Timestamp] = []
                for state_pos, deal in confirmed_deals:
                    deal_epoch = int(getattr(deal, "deal_time", 0) or 0)
                    deal_time = pd.Timestamp(deal_epoch, unit="s", tz="UTC") if deal_epoch > 0 else utc_now()
                    confirmed_times.append(pd.Timestamp(deal_time))
                    self._record_daily_realized(strat, float(deal.net_profit), deal_time)
                    position_id = int(state_pos.get("position_identifier") or state_pos.get("ticket") or 0)
                    self._trade_row(
                        "position_close_confirmed", strat,
                        ticket=int(state_pos.get("ticket") or position_id), position_identifier=position_id,
                        deal_id=int(getattr(deal, "deal", 0) or 0), side=str(state_pos.get("side") or ""),
                        lot=float(state_pos.get("lot") or 0.0), entry_price=float(state_pos.get("entry_price") or 0.0),
                        exit_price=float(getattr(deal, "price", 0.0) or 0.0), price=float(getattr(deal, "price", 0.0) or 0.0),
                        profit=float(deal.net_profit), reason=reason, signal_bar_time=signal_bar,
                        note=f"deal_time_utc={dt_text(deal_time)}",
                    )
                if not remaining_state:
                    if reason == "basket_target" and closed_basket_was_long:
                        self._confirm_long_target_portfolio_rearm(
                            strat,
                            max(confirmed_times),
                            closed_basket_id,
                        )
                    self._clear_basket_state(strat, reason, signal_bar)
                    if orders_available:
                        self._set_sync_block(strat, None)
                self._save_state()
                if unresolved_open:
                    self._set_sync_block(
                        strat,
                        "unresolved_open_action",
                        {
                            "opportunity_id": str(st.get("pending_open_opportunity_id") or ""),
                            "started_utc": st.get("pending_open_started_utc"),
                            "live_tickets": [int(pos.ticket) for pos in positions],
                        },
                        recoverable=False,
                    )
                    self._save_state()
                    # Confirmed remaining state is safe to close, but a flat
                    # or fully reconciled-away basket has nothing to monitor.
                    return bool(remaining_state)
                return not bool(st.get("sync_block_new_entries"))
            if unresolved_open:
                self._set_sync_block(
                    strat,
                    "unresolved_open_action",
                    {
                        "opportunity_id": str(st.get("pending_open_opportunity_id") or ""),
                        "started_utc": st.get("pending_open_started_utc"),
                        "live_tickets": [int(pos.ticket) for pos in positions],
                    },
                    recoverable=False,
                )
                self._save_state()
                # Every live position has already matched a persisted ticket,
                # side, lot, symbol, magic, and comment.  Keep the unresolved
                # OPEN as a hard entry/add block while allowing only those
                # proven positions to reach target/stop/FTP/max-hold exits.
                return bool(remaining_state) and len(remaining_state) == len(state_basket)
            if remaining_state and len(remaining_state) == len(state_basket) and orders_available and not orders:
                clear_recoverable_sync_block_after_clean_sync(
                    symbol_key=strat["id"],
                    state=st,
                    save_state=self._save_state,
                    options=self.safety,
                    audit=lambda symbol_key, event, reason: self._trade_row(event, strat, reason=reason, note=symbol_key),
                )
            if (
                remaining_state
                and len(remaining_state) == len(state_basket)
                and not orders_available
                and st.get("sync_block_reason") == "orders_unavailable"
                and bool(st.get("sync_block_recoverable"))
            ):
                # Pending-order visibility is required before any new entry,
                # but it is not required to monitor or close already-proven
                # market positions owned by this lane.
                return True
        return not bool(st.get("sync_block_new_entries"))

    def _close_basket(self, strat: dict[str, Any], reason: str, price_row: pd.Series, pnl: float) -> str:
        st = self._st(strat)
        if self.live_enabled:
            for pos in list(st["basket"]):
                ticket = int(pos.get("ticket") or 0)
                position_id = int(pos.get("position_identifier") or ticket)
                live_pos = self.executor.get_position(ticket)
                if live_pos is None:
                    self._set_sync_block(strat, "position_query_unavailable_before_close", {"ticket": ticket}, recoverable=False)
                    self._save_state()
                    return "failed"
                if live_pos is False or not self._owned_position(strat, live_pos) or int(getattr(live_pos, "identifier", 0) or live_pos.ticket) != position_id:
                    self._set_sync_block(strat, "state_ticket_unowned_or_foreign", {"ticket": ticket, "position_identifier": position_id}, recoverable=False)
                    self._save_state()
                    return "failed"
            st["pending_close_reason"] = reason
            st["pending_close_signal_bar"] = str(price_row.name)
            if reason == "basket_target" and self._basket_is_long(strat):
                self._arm_long_target_portfolio_rearm(strat, utc_now())
            self._save_state()
            for pos in list(st["basket"]):
                ticket = int(pos.get("ticket") or 0)
                close_result = self.executor.close_position(ticket, int(self.params.get("deviation_points", 50)))
                if not close_result:
                    close_status = str(getattr(close_result, "status", "FAILED"))
                    if close_status == "MARKET_CLOSED" and reason in {"morning_fixed_hold", "midday_fixed_hold"}:
                        st["pending_close_reason"] = None
                        st["pending_close_signal_bar"] = None
                        self._trade_row(
                            "time_close_deferred", strat, ticket=ticket,
                            position_identifier=int(pos.get("position_identifier") or ticket),
                            reason="market_closed_10018", signal_bar_time=str(price_row.name),
                            note=f"retcode={getattr(close_result, 'retcode', None)}",
                        )
                        self._save_state()
                        return "market_closed"
                    block_reason = "live_time_close_unconfirmed" if close_status in {"MISSING_UNCONFIRMED", "MALFORMED_OK"} else "live_time_close_failed"
                    self._set_sync_block(
                        strat, block_reason,
                        {"ticket": ticket, "status": close_status, "retcode": getattr(close_result, "retcode", None)},
                        recoverable=False,
                    )
                    self._save_state()
                    return "failed"
                pos["close_requested"] = True
            self._trade_row("basket_close_requested", strat, profit=round(float(pnl), 2), reason=reason, signal_bar_time=str(price_row.name))
            self._save_state()
            return
        closed_basket_id = st.get("current_basket_id")
        closed_basket_was_long = self._basket_is_long(strat)
        self._trade_row("basket_close", strat, profit=round(float(pnl), 2), reason=reason, signal_bar_time=str(price_row.name))
        self._record_daily_realized(strat, pnl, price_row.name)
        if reason == "basket_target" and closed_basket_was_long:
            self._confirm_long_target_portfolio_rearm(strat, price_row.name, closed_basket_id)
        self._clear_basket_state(strat, reason, str(price_row.name))
        self._save_state()
        return "closed"

    def _open_entry(
        self,
        strat: dict[str, Any],
        side: str,
        price_row: pd.Series,
        info: Any,
        note: str = "",
        *,
        basket_atr30: float | None = None,
        execution_time: datetime | pd.Timestamp | None = None,
        opportunity: dict[str, Any] | None = None,
        apply_portfolio_rearm: bool = True,
        use_confirmed_fill_time: bool = False,
    ) -> bool:
        st = self._st(strat)
        if not st["basket"] and apply_portfolio_rearm:
            portfolio_block = self._portfolio_new_long_basket_block_reason(side, execution_time)
            if portfolio_block:
                self._trade_row(
                    "entry_skip",
                    strat,
                    reason=portfolio_block,
                    signal_bar_time=str(price_row.name),
                    note="final_portfolio_rearm_guard",
                )
                return False
        entry_block = self._entry_submission_block_reason(strat, execution_time)
        if entry_block:
            self._trade_row("entry_skip", strat, reason=entry_block, signal_bar_time=str(price_row.name), note="final_open_guard")
            return False
        if note != "reverse_after_stop" and st.get("last_closed_signal_bar") == str(price_row.name):
            self._trade_row("entry_skip", strat, reason="same_bar_reentry_after_close", signal_bar_time=str(price_row.name))
            return False
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        digits = int(self.params.get("price_digits", 2))
        lot = float(strat.get("lot", self.params.get("default_lot", 0.01)))
        ask = float(getattr(info, "ask", price_row.get("AskOpen", price_row["Open"])))
        bid = float(getattr(info, "bid", price_row["Open"]))
        entry_price = normalize_price(ask if side == "LONG" else bid, digits)
        ticket = None
        confirmed = None
        if self.live_enabled:
            opportunity_id = str((opportunity or {}).get("opportunity_id") or "")
            st["pending_open_opportunity_id"] = opportunity_id or f"lane{strat['lane_id']}:{dt_text(utc_now())}"
            st["pending_open_started_utc"] = dt_text(utc_now())
            self._trade_row(
                "open_reserved",
                strat,
                opportunity_id=opportunity_id,
                side=side,
                lot=lot,
                signal_bar_time=str(price_row.name),
                decision_time=(opportunity or {}).get("decision_time"),
                executable_at=dt_text(execution_time if execution_time is not None else utc_now()),
            )
            self._save_state()
            order_type = ORDER_TYPE_BUY if side == "LONG" else ORDER_TYPE_SELL
            ticket = self.executor.open_position(
                symbol,
                order_type,
                lot,
                0.0,
                0.0,
                deviation=int(self.params.get("deviation_points", 50)),
                magic=int(strat["magic"]),
                comment=str(strat["comment_prefix"]),
                digits=digits,
            )
            if ticket is None:
                error = str(getattr(self.executor, "last_order_error", None) or "UNKNOWN_OPEN_FAILURE")
            else:
                error = ""
            positions = self.executor.get_positions(symbol, int(strat["magic"]))
            if positions is None:
                self._set_sync_block(strat, "positions_unavailable_after_open", {"ticket": int(ticket or 0), "error": error}, recoverable=True)
                self._save_state()
                return True
            owned = [pos for pos in positions if self._owned_position(strat, pos)]
            known_ids = {int(pos.get("position_identifier") or pos.get("ticket") or 0) for pos in st.get("basket", [])}
            new_owned = [pos for pos in owned if int(getattr(pos, "identifier", 0) or pos.ticket) not in known_ids]
            if ticket is not None:
                matches = [pos for pos in new_owned if int(pos.ticket) == int(ticket) or int(getattr(pos, "identifier", 0) or 0) == int(ticket)]
                if len(matches) != 1:
                    self._set_sync_block(strat, "open_success_position_not_confirmed", {"ticket": int(ticket)}, recoverable=False)
                    self._save_state()
                    return True
                confirmed = matches[0]
            elif not new_owned and (error.startswith("ERR|10026") or error.startswith("ERR|10027")):
                st["autotrading_reject_streak"] = int(st.get("autotrading_reject_streak", 0)) + 1
                st["open_retry_after_utc"] = dt_text(utc_now() + pd.Timedelta(seconds=float(self.params.get("trade_permission_retry_seconds", 30.0))))
                self._clear_pending_open(strat)
                threshold = int(self.params.get("trade_permission_alert_threshold", 3))
                self._trade_row("entry_skip", strat, reason="trade_permission_rejected", signal_bar_time=str(price_row.name), note=f"streak={st['autotrading_reject_streak']}")
                if st["autotrading_reject_streak"] >= threshold and not st.get("autotrading_reject_notified"):
                    st["autotrading_reject_notified"] = True
                    self._notify_manual_action(strat, title="trade permission rejected repeatedly", reason=error, action="Check MT5 AutoTrading and account trade permissions.", key=f"bot23:trade-permission:{strat['id']}")
                self._save_state()
                return True
            elif len(new_owned) == 1:
                confirmed = new_owned[0]
                ticket = int(confirmed.ticket)
            else:
                reason = "ambiguous_open_result_positions" if new_owned else "ambiguous_open_result"
                self._set_sync_block(strat, reason, {"tickets": [int(pos.ticket) for pos in new_owned], "error": error}, recoverable=False)
                self._save_state()
                return True
            entry_price = float(confirmed.open_price)
            st["autotrading_reject_streak"] = 0
            st["autotrading_reject_notified"] = False
            st["open_retry_after_utc"] = None
        if not st["basket"]:
            st["basket_sequence"] = int(st.get("basket_sequence") or 0) + 1
            st["current_basket_id"] = f"L{int(strat['lane_id'])}-B{int(st['basket_sequence']):06d}"
        opportunity_id = str((opportunity or {}).get("opportunity_id") or st.get("pending_entry_opportunity_id") or "")
        entry_time = execution_time if execution_time is not None else (parse_ts(price_row.name) or pd.Timestamp(utc_now())) + pd.Timedelta(minutes=1)
        confirmed_open_epoch = int(getattr(confirmed, "open_time", 0) or 0) if confirmed is not None else 0
        fill_time_unavailable = bool(self.live_enabled and use_confirmed_fill_time and confirmed_open_epoch <= 0)
        if use_confirmed_fill_time and confirmed_open_epoch > 0:
            entry_time = pd.Timestamp(confirmed_open_epoch, unit="s", tz="UTC")
        elif fill_time_unavailable:
            entry_time = None
        st["basket"].append(
            {
                "ticket": ticket,
                "position_identifier": int(getattr(confirmed, "identifier", 0) or ticket or 0) if confirmed is not None else 0,
                "side": side,
                "lot": lot,
                "entry_price": entry_price,
                "entry_time_utc": dt_text(entry_time) if entry_time is not None else None,
                "open_time_epoch": confirmed_open_epoch,
                "owner_symbol": symbol,
                "owner_magic": int(strat["magic"]),
                "owner_comment": str(getattr(confirmed, "comment", "") or strat["comment_prefix"]) if confirmed is not None else str(strat["comment_prefix"]),
                "lane_id": int(strat["lane_id"]),
                "basket_id": st["current_basket_id"],
                "opportunity_id": opportunity_id,
                "shadow": not self.live_enabled,
            }
        )
        if len(st["basket"]) == 1:
            st["basket_peak_pnl_usd"] = None
            st["frozen_basket_atr30"] = float(basket_atr30) if basket_atr30 is not None and math.isfinite(float(basket_atr30)) else None
            self._clear_pending_entry(strat)
        st["last_add_price"] = entry_price
        st["last_signal_bar"] = str(price_row.name)
        self._clear_pending_open(strat)
        if fill_time_unavailable:
            self._set_sync_block(
                strat,
                "confirmed_fill_time_unavailable",
                {"ticket": int(ticket or 0), "position_identifier": int(getattr(confirmed, "identifier", 0) or ticket or 0)},
                recoverable=True,
            )
            self._trade_row(
                "position_lifecycle_deferred",
                strat,
                ticket=ticket or "",
                position_identifier=int(getattr(confirmed, "identifier", 0) or ticket or 0),
                reason="confirmed_fill_time_unavailable",
                signal_bar_time=str(price_row.name),
                note="poll time was not substituted; next exact owned-position sync may recover broker open_time",
            )
        self._trade_row(
            "entry",
            strat,
            opportunity_id=opportunity_id,
            ticket=ticket or "",
            side=side,
            lot=lot,
            price=entry_price,
            signal_bar_time=str(price_row.name),
            event_time=(opportunity or {}).get("event_time"),
            release_time=(opportunity or {}).get("release_time"),
            available_time=(opportunity or {}).get("available_time"),
            decision_time=(opportunity or {}).get("decision_time"),
            executable_at=dt_text(execution_time if execution_time is not None else utc_now()),
            note=note,
        )
        self._save_state()
        return True

    @staticmethod
    def _account_identity_error(account: dict[str, Any]) -> str | None:
        observed_login = account.get("login")
        observed_server = str(account.get("server") or "")
        if observed_login is None or not observed_server:
            return "account_identity_unavailable; recompile and attach the current BotBridge_s23"
        if int(observed_login) != int(MT5_LOGIN) or observed_server.casefold() != str(MT5_SERVER).casefold():
            return (
                f"observed_login={int(observed_login)} observed_server={observed_server} "
                f"expected_login={int(MT5_LOGIN)} expected_server={MT5_SERVER}"
            )
        return None

    def _legacy_inventory_error(self) -> str | None:
        """Refuse cutover while the retired single-lane namespace is not flat."""
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        for magic in LEGACY_S23_MAGICS:
            positions = self.executor.get_positions(symbol, magic)
            if positions is None:
                return f"legacy_positions_unavailable:magic={magic}"
            orders = self.executor.get_orders(symbol, magic)
            if orders is None:
                return f"legacy_orders_unavailable:magic={magic}"
            if positions or orders:
                return (
                    f"legacy_inventory_not_flat:magic={magic}:"
                    f"positions={[int(row.ticket) for row in positions]}:"
                    f"orders={[int(row.ticket) for row in orders]}"
                )
        return None

    def _monitor_open_basket(self, strat: dict[str, Any], info: Any, price_row: pd.Series, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        st = self._st(strat)
        if st.get("pending_close_reason"):
            return True
        if not st["basket"]:
            return False
        at_utc = pd.Timestamp(poll_time if poll_time is not None else utc_now())
        at_utc = at_utc.tz_localize("UTC") if at_utc.tzinfo is None else at_utc.tz_convert("UTC")
        bid = float(getattr(info, "bid", price_row["Close"]))
        ask = float(getattr(info, "ask", price_row.get("AskOpen", price_row["Open"])))
        pnl = self._basket_pnl(strat, bid, ask)
        entries = [parse_ts(pos.get("entry_time_utc")) for pos in st["basket"]]
        valid_entries = [stamp for stamp in entries if stamp is not None]
        if not valid_entries:
            self._set_sync_block(strat, "state_entry_time_invalid", recoverable=False)
            self._save_state()
            return True
        held = max(0, int((at_utc - min(valid_entries)).total_seconds() // 60))
        previous_peak = st.get("basket_peak_pnl_usd")
        peak = float(pnl) if previous_peak is None else max(float(previous_peak), float(pnl))
        st["basket_peak_pnl_usd"] = peak
        target, stop, ftp_peak = self._exit_thresholds(strat)
        reason = None
        if pnl >= target:
            reason = "basket_target"
        elif pnl <= -stop:
            reason = "basket_stop"
        elif int(strat.get("failure_to_progress_bars", 0)) > 0 and held >= int(strat["failure_to_progress_bars"]) and peak < ftp_peak:
            reason = "failure_to_progress"
        elif held >= int(strat["max_hold_bars"]):
            reason = "max_hold"
        if reason:
            row = price_row.copy()
            row.name = at_utc
            self._close_basket(strat, reason, row, pnl)
            return True
        return False

    def _monitor_fixed_hold_position(
        self,
        strat: dict[str, Any],
        info: Any,
        poll_time: datetime | pd.Timestamp | None,
        close_reason: str,
    ) -> bool:
        st = self._st(strat)
        if st.get("pending_close_reason"):
            return True
        if not st["basket"]:
            return False
        at_utc = pd.Timestamp(poll_time if poll_time is not None else utc_now())
        at_utc = at_utc.tz_localize("UTC") if at_utc.tzinfo is None else at_utc.tz_convert("UTC")
        entries = [parse_ts(pos.get("entry_time_utc")) for pos in st["basket"]]
        valid_entries = [stamp for stamp in entries if stamp is not None]
        if not valid_entries:
            raw_entries = [pos.get("entry_time_utc") for pos in st["basket"]]
            missing_confirmed_fill = all(value is None or value == "" for value in raw_entries)
            self._set_sync_block(
                strat,
                "confirmed_fill_time_unavailable" if missing_confirmed_fill else "state_entry_time_invalid",
                recoverable=missing_confirmed_fill,
            )
            self._save_state()
            return True
        # Once filled, this lane owns an absolute UTC lifecycle. Session/DST
        # classification is intentionally absent from the close deadline.
        due = fixed_hold_due_at(valid_entries, int(strat["hold_minutes"]))
        quote_time_msc = getattr(info, "quote_time_msc", None)
        if quote_time_msc is None:
            if self.live_enabled:
                self._trade_row(
                    "time_close_deferred", strat, reason="quote_timestamp_unavailable",
                    signal_bar_time=str(due), note="updated BotBridge_s23 INFO response is required",
                )
                return True
            quote_time_msc = int(at_utc.timestamp() * 1000)
        try:
            quote_time_msc = int(quote_time_msc)
        except (TypeError, ValueError, OverflowError):
            return True
        quote_time = pd.Timestamp(quote_time_msc, unit="ms", tz="UTC")
        # The broker quote clock is authoritative for a live fixed-hold exit.
        # Host/poll clock drift must neither advance nor delay the deadline.
        if quote_time < due:
            return False if at_utc < due else True
        retry_after = parse_ts(st.get("time_close_retry_after_utc"))
        if retry_after is not None and quote_time < retry_after:
            return True
        last_quote_msc = st.get("time_close_last_quote_msc")
        if last_quote_msc is not None and quote_time_msc <= int(last_quote_msc):
            return True
        st["time_close_last_quote_msc"] = quote_time_msc
        bid = float(getattr(info, "bid"))
        ask = float(getattr(info, "ask"))
        point = float(self.params.get("point_size", 0.001))
        spread_points = max(0.0, ask - bid) / point if point > 0 else math.inf
        spread_cap = float(self.params.get("fixed_hold_close_max_spread_points", 300.0))
        wide_seen = bool(st.get("time_close_wide_seen"))
        defer_started = parse_ts(st.get("time_close_defer_started_utc"))
        if not wide_seen and spread_points > spread_cap:
            st["time_close_wide_seen"] = True
            st["time_close_defer_started_utc"] = dt_text(quote_time)
            st["time_close_stable_count"] = 0
            self._trade_row(
                "time_close_deferred", strat, reason="spread_wide",
                signal_bar_time=str(due),
                note=f"spread_points={spread_points:.3f};cap={spread_cap:.3f}",
            )
            self._save_state()
            return True
        if wide_seen:
            if defer_started is None:
                defer_started = quote_time
                st["time_close_defer_started_utc"] = dt_text(quote_time)
            force_after = defer_started + pd.Timedelta(
                minutes=float(self.params.get("fixed_hold_close_force_after_minutes", 30.0))
            )
            if quote_time < force_after:
                if spread_points <= spread_cap:
                    st["time_close_stable_count"] = int(st.get("time_close_stable_count") or 0) + 1
                else:
                    st["time_close_stable_count"] = 0
                stable_required = int(self.params.get("fixed_hold_close_stable_polls", 3))
                self._save_state()
                if int(st["time_close_stable_count"]) < stable_required:
                    return True
        pnl = self._basket_pnl(strat, bid, ask)
        row = pd.Series({"Open": bid, "Close": bid, "AskOpen": ask}, name=quote_time)
        result = self._close_basket(strat, close_reason, row, pnl)
        if result == "market_closed":
            st["time_close_retry_after_utc"] = dt_text(
                quote_time + pd.Timedelta(seconds=float(self.params.get("fixed_hold_market_closed_retry_seconds", 60.0)))
            )
            st["time_close_defer_started_utc"] = None
            st["time_close_stable_count"] = 0
            st["time_close_wide_seen"] = False
            self._save_state()
        return True

    def _monitor_morning_position(self, strat: dict[str, Any], info: Any, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        return self._monitor_fixed_hold_position(strat, info, poll_time, "morning_fixed_hold")

    def _monitor_midday_position(self, strat: dict[str, Any], info: Any, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        return self._monitor_fixed_hold_position(strat, info, poll_time, "midday_fixed_hold")

    def _process_morning_exits(self, info: Any, poll_time: pd.Timestamp) -> dict[int, bool]:
        readiness: dict[int, bool] = {}
        session_active = in_session(
            poll_time,
            EXPECTED_MORNING_SESSION_START_UTC,
            EXPECTED_MORNING_SESSION_END_UTC,
        )
        for strat in self._morning_strategies():
            if not bool(strat.get("enabled", True)):
                continue
            lane_id = int(strat["lane_id"])
            st = self._st(strat)
            needs_reconciliation = bool(
                session_active
                or st.get("basket")
                or st.get("pending_open_opportunity_id")
                or st.get("pending_close_reason")
                or st.get("sync_block_new_entries")
            )
            if not needs_reconciliation:
                readiness[lane_id] = False
                continue
            if not self._sync_strategy(strat):
                self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="sync_block")
                self._save_state()
                readiness[lane_id] = False
                continue
            readiness[lane_id] = not self._monitor_morning_position(strat, info, poll_time)
        return readiness

    def _process_morning_entries(
        self,
        bars: pd.DataFrame,
        price_row: pd.Series,
        info: Any,
        poll_time: pd.Timestamp,
        readiness: dict[int, bool],
    ) -> None:
        signal_bar = parse_ts(price_row.name)
        if signal_bar is None:
            return "requested"
        release_time = signal_bar + pd.Timedelta(minutes=1)
        if not in_session(release_time, EXPECTED_MORNING_SESSION_START_UTC, EXPECTED_MORNING_SESSION_END_UTC):
            return
        sides = self._morning_signal_sides(bars)
        signal_bar_text = dt_text(signal_bar)
        total_positions = sum(len(self._st(strat).get("basket") or []) for strat in self._morning_strategies())
        for strat in self._morning_strategies():
            if not bool(strat.get("enabled", True)):
                continue
            st = self._st(strat)
            if st.get("last_evaluated_bar") == signal_bar_text:
                continue
            # Durable evaluation reservation prevents duplicate opens when a
            # poll is retried after an uncertain bridge response.
            st["last_evaluated_bar"] = signal_bar_text
            self._save_state()
            side = sides.get(str(strat["signal_id"]))
            if side is None:
                self._trade_row("morning_decision", strat, reason="no_signal", signal_bar_time=signal_bar_text)
                continue
            opportunity_id = f"{self.params.get('mt5_symbol', self.params['symbol'])}|{signal_bar_text}|{strat['signal_id']}|{side}"
            if not readiness.get(int(strat["lane_id"]), False):
                self._trade_row("morning_decision", strat, opportunity_id=opportunity_id, side=side, reason="exit_or_sync_block", signal_bar_time=signal_bar_text)
                continue
            if st["basket"] or len(st["basket"]) >= int(strat.get("max_positions", 1)):
                self._trade_row("morning_decision", strat, opportunity_id=opportunity_id, side=side, reason="lane_capacity_full", signal_bar_time=signal_bar_text)
                continue
            if total_positions >= int(self.params.get("morning_session_max_positions", EXPECTED_MORNING_MAX_POSITIONS)):
                self._trade_row("morning_decision", strat, opportunity_id=opportunity_id, side=side, reason="morning_capacity_full", signal_bar_time=signal_bar_text)
                continue
            point = float(self.params.get("point_size", 0.001))
            spread_points = max(0.0, float(info.ask) - float(info.bid)) / point if point > 0 else math.inf
            if spread_points > float(self.params.get("max_entry_spread_points", 300.0)):
                self._trade_row("morning_decision", strat, opportunity_id=opportunity_id, side=side, reason="spread_guard", signal_bar_time=signal_bar_text)
                continue
            stale = stale_signal_decision(
                str(price_row.name),
                timeframe_hours=1.0 / 60.0,
                max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)),
                options=self.safety,
            )
            if stale.stale:
                self._trade_row("morning_decision", strat, opportunity_id=opportunity_id, side=side, reason="stale_signal_skip", signal_bar_time=signal_bar_text)
                continue
            opportunity = {
                "opportunity_id": opportunity_id,
                "event_time": signal_bar_text,
                "release_time": dt_text(release_time),
                "available_time": dt_text(release_time),
                "decision_time": dt_text(poll_time),
            }
            opened = self._open_entry(
                strat,
                side,
                price_row,
                info,
                note=f"morning_stable001_hold_{int(strat['hold_minutes'])}m",
                execution_time=poll_time,
                opportunity=opportunity,
                apply_portfolio_rearm=False,
                use_confirmed_fill_time=True,
            )
            if opened:
                total_positions += 1

    def _process_midday_exits(self, info: Any, poll_time: pd.Timestamp) -> dict[int, bool]:
        readiness: dict[int, bool] = {}
        session_active = in_session(poll_time, EXPECTED_MIDDAY_SESSION_START_UTC, EXPECTED_MIDDAY_SESSION_END_UTC)
        for strat in self._midday_strategies():
            if not bool(strat.get("enabled", True)):
                continue
            lane_id = int(strat["lane_id"])
            st = self._st(strat)
            needs_reconciliation = bool(
                session_active or st.get("basket") or st.get("pending_open_opportunity_id")
                or st.get("pending_close_reason") or st.get("sync_block_new_entries")
            )
            if not needs_reconciliation:
                readiness[lane_id] = False
                continue
            if not self._sync_strategy(strat):
                self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="sync_block")
                self._save_state()
                readiness[lane_id] = False
                continue
            readiness[lane_id] = not self._monitor_midday_position(strat, info, poll_time)
        return readiness

    def _process_midday_entries(
        self,
        bars: pd.DataFrame,
        price_row: pd.Series,
        info: Any,
        poll_time: pd.Timestamp,
        readiness: dict[int, bool],
    ) -> None:
        signal_bar = parse_ts(price_row.name)
        if signal_bar is None:
            return
        release_time = signal_bar + pd.Timedelta(minutes=1)
        if not in_session(release_time, EXPECTED_MIDDAY_SESSION_START_UTC, EXPECTED_MIDDAY_SESSION_END_UTC):
            return
        signal_bar_text = dt_text(signal_bar)
        total_positions = sum(len(self._st(strat).get("basket") or []) for strat in self._midday_strategies())
        for strat in self._midday_strategies():
            if not bool(strat.get("enabled", True)):
                continue
            st = self._st(strat)
            if st.get("last_evaluated_bar") == signal_bar_text:
                continue
            st["last_evaluated_bar"] = signal_bar_text
            self._save_state()
            side = self._midday_signal_side(bars, strat)
            if side is None:
                self._trade_row("midday_decision", strat, reason="no_signal", signal_bar_time=signal_bar_text)
                continue
            opportunity_id = f"{self.params.get('mt5_symbol', self.params['symbol'])}|{signal_bar_text}|{strat['signal_id']}|{side}"
            opportunity = {
                "opportunity_id": opportunity_id, "source": "jst1113_round_sweep",
                "side": side, "raw_side": side, "effective_side": side,
                "event_time": signal_bar_text, "release_time": dt_text(release_time),
                "available_time": dt_text(release_time), "decision_time": dt_text(poll_time),
                "executable_at": dt_text(poll_time),
            }
            shadow_context = self._shadow_context(price_row, info, readiness)
            self._midday_observer_call(
                "register_opportunity", opportunity=opportunity, at=poll_time,
                bid=float(info.bid), ask=float(info.ask), context=shadow_context,
            )
            self._midday_state_tagger_call(
                "tag_opportunity", opportunity=opportunity, at=poll_time, bars=bars,
                bid=float(info.bid), ask=float(info.ask), context=shadow_context,
            )
            if not readiness.get(int(strat["lane_id"]), False):
                self._trade_row("midday_decision", strat, opportunity_id=opportunity_id, side=side, reason="exit_or_sync_block", signal_bar_time=signal_bar_text)
                self._midday_observer_call("record_route", opportunity_id=opportunity_id, at=poll_time, status="unconsumed", reason="exit_or_sync_block")
                continue
            if st["basket"] or len(st["basket"]) >= int(strat.get("max_positions", 1)):
                self._trade_row("midday_decision", strat, opportunity_id=opportunity_id, side=side, reason="lane_capacity_full", signal_bar_time=signal_bar_text)
                self._midday_observer_call("record_route", opportunity_id=opportunity_id, at=poll_time, status="unconsumed", reason="lane_capacity_full")
                continue
            if total_positions >= int(self.params.get("midday_session_max_positions", EXPECTED_MIDDAY_MAX_POSITIONS)):
                self._trade_row("midday_decision", strat, opportunity_id=opportunity_id, side=side, reason="midday_capacity_full", signal_bar_time=signal_bar_text)
                self._midday_observer_call("record_route", opportunity_id=opportunity_id, at=poll_time, status="unconsumed", reason="midday_capacity_full")
                continue
            point = float(self.params.get("point_size", 0.001))
            spread_points = max(0.0, float(info.ask) - float(info.bid)) / point if point > 0 else math.inf
            if spread_points > float(self.params.get("max_entry_spread_points", 300.0)):
                self._trade_row("midday_decision", strat, opportunity_id=opportunity_id, side=side, reason="spread_guard", signal_bar_time=signal_bar_text)
                self._midday_observer_call("record_route", opportunity_id=opportunity_id, at=poll_time, status="unconsumed", reason="spread_guard")
                continue
            stale = stale_signal_decision(
                str(price_row.name), timeframe_hours=1.0 / 60.0,
                max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)), options=self.safety,
            )
            if stale.stale:
                self._trade_row("midday_decision", strat, opportunity_id=opportunity_id, side=side, reason="stale_signal_skip", signal_bar_time=signal_bar_text)
                self._midday_observer_call("record_route", opportunity_id=opportunity_id, at=poll_time, status="stale_rejected", reason="stale_signal_skip")
                continue
            opened = self._open_entry(
                strat, side, price_row, info,
                note="midday_round_s2p5_d0p05_r0p03_hold_60m",
                execution_time=poll_time, opportunity=opportunity,
                apply_portfolio_rearm=False, use_confirmed_fill_time=True,
            )
            if opened:
                total_positions += 1
            self._midday_observer_call(
                "record_route", opportunity_id=opportunity_id, at=poll_time,
                status="consumed" if opened else "unconsumed",
                consumed_lane_id=int(strat["lane_id"]) if opened else None,
                reason="entry_opened" if opened else "entry_not_opened",
            )

    def _monitor_pending_entry(self, strat: dict[str, Any], info: Any, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        st = self._st(strat)
        side = str(st.get("pending_entry_side") or "")
        if not side or st["basket"]:
            return False
        at_utc = pd.Timestamp(poll_time if poll_time is not None else utc_now())
        at_utc = at_utc.tz_localize("UTC") if at_utc.tzinfo is None else at_utc.tz_convert("UTC")
        entry_block = self._entry_submission_block_reason(strat, at_utc)
        if entry_block:
            self._trade_row("entry_skip", strat, reason=entry_block, signal_bar_time=st.get("pending_entry_signal_bar"), note="pending_open_guard")
            return True
        expires = parse_ts(st.get("pending_entry_expires_utc"))
        try:
            target = float(st["pending_entry_target"])
            atr30 = float(st["pending_entry_atr30"])
        except (KeyError, TypeError, ValueError):
            target, atr30 = math.nan, math.nan
        if side not in {"LONG", "SHORT"} or expires is None or not math.isfinite(target) or not math.isfinite(atr30) or atr30 <= 0.0:
            invalid_fields = [
                field
                for field, valid in (
                    ("pending_entry_side", side in {"LONG", "SHORT"}),
                    ("pending_entry_target", math.isfinite(target)),
                    ("pending_entry_expires_utc", expires is not None),
                    ("pending_entry_atr30", math.isfinite(atr30) and atr30 > 0.0),
                )
                if not valid
            ]
            signal_bar = st.get("pending_entry_signal_bar")
            opportunity_id = st.get("pending_entry_opportunity_id")
            self._clear_pending_entry(strat)
            self._trade_row("entry_skip", strat, opportunity_id=opportunity_id, reason="pending_entry_state_invalid", signal_bar_time=signal_bar, note=",".join(invalid_fields))
            self._save_state()
            return True
        if at_utc > expires:
            signal_bar = st.get("pending_entry_signal_bar")
            opportunity_id = st.get("pending_entry_opportunity_id")
            self._clear_pending_entry(strat)
            self._trade_row("entry_skip", strat, opportunity_id=opportunity_id, reason="za_pullback_expired", signal_bar_time=signal_bar)
            self._save_state()
            return False
        bid, ask = float(info.bid), float(info.ask)
        touched = (side == "LONG" and ask <= target) or (side == "SHORT" and bid >= target)
        if not touched:
            return False
        max_ratio = float(strat.get("entry_max_spread_atr_ratio", 0.0))
        if self._low_vol_regime(strat, atr30) and max_ratio > 0.0 and (ask - bid) / atr30 > max_ratio:
            return False
        signal_bar = parse_ts(st.get("pending_entry_signal_bar")) or at_utc
        row = pd.Series({"Open": bid, "Close": bid, "AskOpen": ask}, name=signal_bar)
        opportunity = {
            "opportunity_id": st.get("pending_entry_opportunity_id"),
            "event_time": st.get("pending_entry_event_time"),
            "release_time": st.get("pending_entry_release_time"),
            "available_time": st.get("pending_entry_release_time"),
            "decision_time": dt_text(at_utc),
        }
        return self._open_entry(
            strat,
            side,
            row,
            info,
            note="za_pullback_fill",
            basket_atr30=atr30,
            execution_time=at_utc,
            opportunity=opportunity,
        )

    def _prepare_lane_admission(self, strat: dict[str, Any], price_row: pd.Series, info: Any, poll_time: pd.Timestamp) -> tuple[bool, str, bool]:
        st = self._st(strat)
        entry_block = self._entry_submission_block_reason(strat, poll_time)
        if entry_block:
            return False, entry_block, False
        pending_side = str(st.get("pending_entry_side") or "")
        try:
            pending_target = float(st.get("pending_entry_target"))
        except (TypeError, ValueError):
            pending_target = math.nan
        pending_expiry = parse_ts(st.get("pending_entry_expires_utc"))
        pending_touch = (
            pending_side in {"LONG", "SHORT"}
            and math.isfinite(pending_target)
            and pending_expiry is not None
            and poll_time <= pending_expiry
            and (
                (pending_side == "LONG" and float(info.ask) <= pending_target)
                or (pending_side == "SHORT" and float(info.bid) >= pending_target)
            )
        )
        if self._monitor_pending_entry(strat, info, poll_time):
            # The frozen engine handles a pending fill before evaluating the
            # raw signal on the same tick, and marks that tick consumed.
            return False, "pending_entry_fill", pending_touch
        return True, "ready", False

    def _prepare_lane(self, strat: dict[str, Any], price_row: pd.Series, info: Any, poll_time: pd.Timestamp) -> tuple[bool, str, bool]:
        st = self._st(strat)
        if not self._sync_strategy(strat):
            self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="sync_block")
            self._save_state()
            return False, str(st.get("sync_block_reason") or "sync_block"), False
        if self._monitor_open_basket(strat, info, price_row, poll_time):
            return False, "open_basket_exit_or_pending_close", False
        return self._prepare_lane_admission(strat, price_row, info, poll_time)

    @staticmethod
    def _opportunity_fields(opportunity: dict[str, Any]) -> dict[str, Any]:
        return {
            "opportunity_id": opportunity["opportunity_id"],
            "signal_bar_time": opportunity["event_time"],
            "event_time": opportunity["event_time"],
            "release_time": opportunity["release_time"],
            "available_time": opportunity["available_time"],
            "decision_time": opportunity["decision_time"],
            "executable_at": opportunity["executable_at"],
        }

    def _set_pending_from_opportunity(
        self,
        strat: dict[str, Any],
        opportunity: dict[str, Any],
        side: str,
        target: float,
        atr30: float,
        poll_time: pd.Timestamp,
    ) -> None:
        st = self._st(strat)
        st["pending_entry_side"] = side
        st["pending_entry_target"] = target
        st["pending_entry_expires_utc"] = dt_text(poll_time + pd.Timedelta(minutes=int(strat.get("entry_wait_minutes", 0))))
        st["pending_entry_atr30"] = atr30
        st["pending_entry_signal_bar"] = opportunity["event_time"]
        st["pending_entry_opportunity_id"] = opportunity["opportunity_id"]
        st["pending_entry_event_time"] = opportunity["event_time"]
        st["pending_entry_release_time"] = opportunity["release_time"]

    def _consume_opportunity(
        self,
        strat: dict[str, Any],
        opportunity: dict[str, Any],
        price_row: pd.Series,
        info: Any,
        poll_time: pd.Timestamp,
    ) -> tuple[bool, str]:
        st = self._st(strat)
        side = str(opportunity["side"])
        source = str(opportunity.get("source") or "za")
        is_inventory_range_fade = source == "inventory_range_false_break_fade"
        fields = self._opportunity_fields(opportunity)
        st["last_evaluated_bar"] = opportunity["event_time"]
        entry_block = self._entry_submission_block_reason(strat, poll_time)
        if entry_block:
            return False, entry_block
        cooldown_until = parse_ts(st.get("cooldown_until_utc"))
        if cooldown_until is not None and poll_time < cooldown_until:
            return False, "cooldown"
        if len(st["basket"]) >= int(strat["max_positions"]):
            return False, "capacity_full"
        bid = float(getattr(info, "bid", price_row["Close"]))
        ask = float(getattr(info, "ask", price_row.get("AskOpen", price_row["Open"])))
        atr30 = float(price_row.get("atr30", math.nan))
        if st["basket"]:
            if any(pos["side"] != side for pos in st["basket"]):
                return False, "opposite_side_inventory"
            if not math.isfinite(atr30):
                return False, "atr_unavailable_for_add"
            target, _, _ = self._exit_thresholds(strat)
            if self._basket_pnl(strat, bid, ask) >= target * float(strat.get("add_profit_guard_ratio", 99.0)):
                return False, "add_profit_guard"
            last_add = st.get("last_add_price")
            if last_add is None:
                return False, "last_add_price_missing"
            favorable = (
                side == "LONG" and float(price_row["Close"]) >= float(last_add) + float(strat["add_atr"]) * atr30
            ) or (
                side == "SHORT" and float(price_row["Close"]) <= float(last_add) - float(strat["add_atr"]) * atr30
            )
            if not favorable:
                return False, "add_distance_not_reached"
            attempted = self._open_entry(
                strat,
                side,
                price_row,
                info,
                note="horizontal_lane_add",
                basket_atr30=atr30,
                execution_time=poll_time,
                opportunity=opportunity,
            )
            return attempted, "add_attempted" if attempted else "add_final_guard"

        portfolio_block = self._portfolio_new_long_basket_block_reason(side, poll_time)
        if portfolio_block:
            return False, portfolio_block
        block_reason = self._new_basket_block_reason(strat, poll_time)
        if block_reason:
            return False, block_reason
        low_vol = self._low_vol_regime(strat, atr30)
        max_ratio = float(strat.get("entry_max_spread_atr_ratio", 0.0))
        if low_vol and max_ratio > 0.0 and (
            not math.isfinite(atr30) or atr30 <= 0.0 or (ask - bid) / atr30 > max_ratio
        ):
            return False, "za_spread_atr_gate"
        pending_side = str(st.get("pending_entry_side") or "")
        mid = float(price_row.get("bb20_mid", math.nan))
        std = float(price_row.get("bb20_std", math.nan))
        if pending_side == side:
            if not math.isfinite(mid) or not math.isfinite(std) or std <= 0.0:
                return False, "bollinger_state_unavailable"
            target = mid + (std if side == "LONG" else -std) * float(strat.get("entry_wait_sigma", 1.0))
            self._set_pending_from_opportunity(strat, opportunity, side, target, atr30, poll_time)
            self._trade_row("entry_wait", strat, reason="za_pullback_refreshed", price=target, **fields)
            self._save_state()
            self._monitor_pending_entry(strat, info, poll_time)
            return True, "pending_refreshed"
        if pending_side:
            previous_id = st.get("pending_entry_opportunity_id")
            self._clear_pending_entry(strat)
            self._trade_row("pending_cancelled", strat, opportunity_id=previous_id, reason="opposite_signal")
        if low_vol and not is_inventory_range_fade:
            if not math.isfinite(mid) or not math.isfinite(std) or std <= 0.0:
                return False, "bollinger_state_unavailable"
            z = (float(price_row["Close"]) - mid) / std
            extreme = (side == "LONG" and z > float(strat.get("entry_wait_z", 99.0))) or (
                side == "SHORT" and z < -float(strat.get("entry_wait_z", 99.0))
            )
            if bool(strat.get("entry_require_extreme", False)) and not extreme:
                return False, "za_not_extreme"
            target = mid + (std if side == "LONG" else -std) * float(strat.get("entry_wait_sigma", 1.0))
            self._set_pending_from_opportunity(strat, opportunity, side, target, atr30, poll_time)
            self._trade_row("entry_wait", strat, reason="za_pullback_armed", price=target, **fields)
            self._save_state()
            self._monitor_pending_entry(strat, info, poll_time)
            return True, "pending_armed"
        attempted = self._open_entry(
            strat,
            side,
            price_row,
            info,
            note="inventory_range_false_break_fade_entry" if is_inventory_range_fade else "horizontal_lane_entry",
            basket_atr30=atr30,
            execution_time=poll_time,
            opportunity=opportunity,
        )
        return attempted, "entry_attempted" if attempted else "entry_final_guard"

    def _route_opportunity(
        self,
        opportunity: dict[str, Any],
        price_row: pd.Series,
        info: Any,
        poll_time: pd.Timestamp,
        lane_readiness: dict[int, tuple[bool, str, bool]],
    ) -> tuple[int | None, str]:
        routing = self.state["routing"]
        routing["last_routed_signal_bar"] = opportunity["event_time"]
        routing["last_routed_opportunity_id"] = opportunity["opportunity_id"]
        routing["last_consumed_lane_id"] = None
        routing["last_route_decision_utc"] = dt_text(poll_time)
        self._save_state()  # durable reservation before any possible OPEN
        primary = self.params["strategies"][0]
        fields = self._opportunity_fields(opportunity)
        policy = dict(opportunity.get("entry_policy") or {})
        self._trade_row(
            "synthetic_opportunity" if opportunity.get("source") == "inventory_range_false_break_fade" else "raw_opportunity",
            primary,
            side=opportunity["side"],
            reason=str(policy.get("reason") or "legacy_za_confirmed_m1_impulse"),
            note=self._entry_policy_note(policy) if policy else "",
            **fields,
        )
        for strat in self.params["strategies"]:
            lane_id = int(strat["lane_id"])
            ready, prep_reason, prep_consumed = lane_readiness.get(lane_id, (False, "lane_not_prepared", False))
            if prep_consumed:
                self._trade_row("opportunity_consumed", strat, reason=prep_reason, **fields)
                routing["last_consumed_lane_id"] = lane_id
                self._save_state()
                return lane_id, prep_reason
            if not ready:
                self._trade_row("opportunity_noop", strat, reason=prep_reason, **fields)
                continue
            consumed, reason = self._consume_opportunity(strat, opportunity, price_row, info, poll_time)
            self._trade_row("opportunity_consumed" if consumed else "opportunity_noop", strat, reason=reason, **fields)
            if consumed:
                routing["last_consumed_lane_id"] = lane_id
                self._save_state()
                return lane_id, reason
        self._trade_row("opportunity_unconsumed", primary, reason="all_lanes_noop", **fields)
        self._save_state()
        return None, "all_lanes_noop"

    def run_once(self) -> None:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        info = self.executor.get_symbol_info(symbol)
        if info is None:
            for strat in self._all_strategies():
                if bool(strat.get("enabled", True)):
                    self._set_sync_block(strat, "symbol_info_failed", recoverable=True)
                    if self._st(strat).get("basket"):
                        self._notify_manual_action(
                            strat,
                            title="market data unavailable while bot23 inventory is open",
                            reason="symbol_info_failed",
                            action="Inspect BotBridge_s23 and the bot23-owned MT5 positions; automated basket exits cannot run without an executable Bid/Ask quote.",
                            key=f"bot23:open-inventory-symbol-info:{strat['id']}",
                        )
            self._save_state()
            return
        quote_time = pd.Timestamp(utc_now())
        self._observer_call("observe_quote", at=quote_time, bid=float(info.bid), ask=float(info.ask))
        self._midday_observer_call("observe_quote", at=quote_time, bid=float(info.bid), ask=float(info.ask))
        # Fixed-time overlay exits depend only on the executable quote and the
        # persisted actual fill time, so they remain active even if HIST/M1 is
        # temporarily unavailable.
        morning_readiness = self._process_morning_exits(info, quote_time)
        midday_readiness = self._process_midday_exits(info, quote_time)
        bars = self._get_m1()
        if bars is None or bars.empty:
            for strat in self.params["strategies"]:
                self._trade_row("entry_skip", strat, reason="m1_bars_unavailable")
                if not bool(strat.get("enabled", True)) or not self._st(strat)["basket"]:
                    continue
                if not self._sync_strategy(strat):
                    self._save_state()
                    continue
                quote_time = utc_now()
                quote_row = pd.Series({"Open": float(info.bid), "Close": float(info.bid), "AskOpen": float(info.ask)}, name=pd.Timestamp(quote_time))
                self._monitor_open_basket(strat, info, quote_row, quote_time)
            return
        if len(bars) < 2:
            return
        price_row = bars.iloc[-1]
        signal_bar = parse_ts(price_row.name)
        poll_time = pd.Timestamp(utc_now())
        if signal_bar is None:
            for strat in self._all_strategies():
                if bool(strat.get("enabled", True)):
                    self._set_sync_block(strat, "signal_bar_time_invalid", {"bar_time": str(price_row.name)}, recoverable=True)
            self._save_state()
            return
        self._process_morning_entries(bars, price_row, info, poll_time, morning_readiness)
        self._process_midday_entries(bars, price_row, info, poll_time, midday_readiness)
        # Match the ordered-tick replay: observe the frozen balanced-book
        # range at the first processing of each completed M1, before this
        # poll's basket exits can change the local inventory state.
        self._advance_inventory_range_fade(price_row)
        # Process every lane's sync and exits before allowing any lane to fill a
        # pending entry or consume a new opportunity. A LONG target close in a
        # later lane therefore arms the portfolio guard before an earlier lane
        # can open a new LONG basket on the same poll.
        exit_pass_ready: dict[int, bool] = {}
        for strat in self.params["strategies"]:
            if not bool(strat.get("enabled", True)):
                continue
            lane_id = int(strat["lane_id"])
            st = self._st(strat)
            if not self._sync_strategy(strat):
                self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="sync_block")
                self._save_state()
                exit_pass_ready[lane_id] = False
                continue
            if self._monitor_open_basket(strat, info, price_row, poll_time):
                exit_pass_ready[lane_id] = False
                continue
            exit_pass_ready[lane_id] = True
        lane_readiness: dict[int, tuple[bool, str, bool]] = {}
        for strat in self.params["strategies"]:
            if not bool(strat.get("enabled", True)):
                continue
            lane_id = int(strat["lane_id"])
            if exit_pass_ready.get(lane_id):
                lane_readiness[lane_id] = self._prepare_lane_admission(strat, price_row, info, poll_time)
            else:
                lane_readiness[lane_id] = (False, "open_basket_exit_or_sync_block", False)
        primary = self.params["strategies"][0]
        raw_side = self._signal(price_row, primary)
        signal_bar_text = dt_text(signal_bar)
        routing = self.state["routing"]
        if raw_side and routing.get("last_routed_signal_bar") != signal_bar_text:
            side, entry_policy = self._apply_entry_policy(raw_side, bars, info)
            release_time = signal_bar + pd.Timedelta(minutes=1)
            opportunity = {
                "opportunity_id": f"{symbol}|{signal_bar_text}|{raw_side}|{side or 'BLOCKED'}|{entry_policy['policy_id']}",
                "source": "za",
                "side": side or raw_side,
                "raw_side": raw_side,
                "effective_side": side or "",
                "entry_policy": entry_policy,
                "event_time": signal_bar_text,
                "release_time": dt_text(release_time),
                "available_time": dt_text(release_time),
                "decision_time": dt_text(poll_time),
                "executable_at": dt_text(poll_time),
            }
            shadow_context = self._shadow_context(price_row, info, lane_readiness)
            self._observer_call(
                "register_opportunity",
                opportunity=opportunity,
                at=poll_time,
                bid=float(info.bid),
                ask=float(info.ask),
                context=shadow_context,
            )
            self._state_tagger_call(
                "tag_opportunity",
                opportunity=opportunity,
                at=poll_time,
                bars=bars,
                bid=float(info.bid),
                ask=float(info.ask),
                context=shadow_context,
            )
            if side is None:
                routing["last_routed_signal_bar"] = signal_bar_text
                routing["last_routed_opportunity_id"] = opportunity["opportunity_id"]
                routing["last_consumed_lane_id"] = None
                routing["last_route_decision_utc"] = dt_text(poll_time)
                self._trade_row(
                    "opportunity_rejected",
                    primary,
                    side=raw_side,
                    reason=str(entry_policy["reason"]),
                    note=self._entry_policy_note(entry_policy),
                    **self._opportunity_fields(opportunity),
                )
                self._observer_call(
                    "record_route",
                    opportunity_id=opportunity["opportunity_id"],
                    at=poll_time,
                    status="policy_rejected",
                    consumed_lane_id=None,
                    reason=str(entry_policy["reason"]),
                )
                self._save_state()
            else:
                stale = stale_signal_decision(
                    str(price_row.name),
                    timeframe_hours=1.0 / 60.0,
                    max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)),
                    options=self.safety,
                )
                if stale.stale:
                    routing["last_routed_signal_bar"] = signal_bar_text
                    routing["last_routed_opportunity_id"] = opportunity["opportunity_id"]
                    routing["last_consumed_lane_id"] = None
                    routing["last_route_decision_utc"] = dt_text(poll_time)
                    self._trade_row(
                        "opportunity_rejected",
                        primary,
                        side=side,
                        reason="stale_signal_skip",
                        note=f"entry_due={stale.entry_due_utc} latest={stale.latest_allowed_utc};{self._entry_policy_note(entry_policy)}",
                        **self._opportunity_fields(opportunity),
                    )
                    self._observer_call(
                        "record_route",
                        opportunity_id=opportunity["opportunity_id"],
                        at=poll_time,
                        status="stale_rejected",
                        consumed_lane_id=None,
                        reason="stale_signal_skip",
                    )
                    self._save_state()
                else:
                    consumed_lane_id, route_reason = self._route_opportunity(
                        opportunity, price_row, info, poll_time, lane_readiness
                    )
                    self._observer_call(
                        "record_route",
                        opportunity_id=opportunity["opportunity_id"],
                        at=poll_time,
                        status="consumed" if consumed_lane_id is not None else "unconsumed",
                        consumed_lane_id=consumed_lane_id,
                        reason=route_reason,
                    )
        if not raw_side:
            opportunity = self._take_inventory_range_fade_opportunity(
                raw_side=None,
                signal_bar=signal_bar,
                poll_time=poll_time,
                symbol=symbol,
            )
            if opportunity is not None:
                shadow_context = self._shadow_context(price_row, info, lane_readiness)
                self._observer_call(
                    "register_opportunity",
                    opportunity=opportunity,
                    at=poll_time,
                    bid=float(info.bid),
                    ask=float(info.ask),
                    context=shadow_context,
                )
                self._state_tagger_call(
                    "tag_opportunity",
                    opportunity=opportunity,
                    at=poll_time,
                    bars=bars,
                    bid=float(info.bid),
                    ask=float(info.ask),
                    context=shadow_context,
                )
                stale = stale_signal_decision(
                    str(price_row.name),
                    timeframe_hours=1.0 / 60.0,
                    max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)),
                    options=self.safety,
                )
                if stale.stale:
                    routing["last_routed_signal_bar"] = signal_bar_text
                    routing["last_routed_opportunity_id"] = opportunity["opportunity_id"]
                    routing["last_consumed_lane_id"] = None
                    routing["last_route_decision_utc"] = dt_text(poll_time)
                    self._trade_row(
                        "opportunity_rejected",
                        primary,
                        side=opportunity["side"],
                        reason="stale_signal_skip",
                        note=f"entry_due={stale.entry_due_utc} latest={stale.latest_allowed_utc};source=inventory_range_false_break_fade",
                        **self._opportunity_fields(opportunity),
                    )
                    self._observer_call(
                        "record_route",
                        opportunity_id=opportunity["opportunity_id"],
                        at=poll_time,
                        status="stale_rejected",
                        consumed_lane_id=None,
                        reason="stale_signal_skip",
                    )
                    self._save_state()
                else:
                    consumed_lane_id, route_reason = self._route_opportunity(
                        opportunity, price_row, info, poll_time, lane_readiness
                    )
                    self._observer_call(
                        "record_route",
                        opportunity_id=opportunity["opportunity_id"],
                        at=poll_time,
                        status="consumed" if consumed_lane_id is not None else "unconsumed",
                        consumed_lane_id=consumed_lane_id,
                        reason=route_reason,
                    )
        now = time.time()
        if now - self._last_status_log >= float(self.params.get("status_log_interval_seconds", 60)):
            logging.info("S23 status: live=%s shadow=%s strategies=%s", self.live_enabled, self.shadow_enabled, {s["id"]: len(self._st(s)["basket"]) for s in self._all_strategies()})
            self._last_status_log = now


S23LossAbortRunner = S23HorizontalInventoryRunner  # compatibility for frozen audit harnesses


class FakeDM:
    def __init__(self, *_: Any):
        pass

    def connect(self) -> bool:
        return True

    def get_historical_data(self, *_: Any, **__: Any) -> pd.DataFrame:
        idx = pd.date_range("2026-01-01 12:00:00", periods=160, freq="1min", tz="UTC")
        close = pd.Series([2000.0 + i * 0.4 for i in range(160)], index=idx)
        return pd.DataFrame({"Open": close, "High": close + 0.2, "Low": close - 0.2, "Close": close, "AskOpen": close + 0.03, "Volume": 10}, index=idx)


class FakeExecutor:
    def __init__(self, *, positions: list[Any] | None = None, orders: list[Any] | None = None, margin_mode: int = HEDGING_MARGIN_MODE):
        self.positions = [] if positions is None else positions
        self.orders = [] if orders is None else orders
        self.margin_mode = margin_mode
        self.last_order_error = None

    def get_bridge_capabilities(self) -> dict[str, Any]:
        return {"name": "BotBridge_s23", "commands": set(REQUIRED_SHARED_ACCOUNT_COMMANDS)}

    def get_account_info(self) -> dict[str, Any]:
        return {
            "margin_mode": self.margin_mode,
            "margin_mode_name": "RETAIL_HEDGING" if self.margin_mode == HEDGING_MARGIN_MODE else "RETAIL_NETTING",
            "login": MT5_LOGIN,
            "server": MT5_SERVER,
        }

    def get_symbol_info(self, *_: Any) -> Any:
        return type("Info", (), {"bid": 2064.0, "ask": 2064.03})()

    def get_positions(self, *_: Any) -> list[Any]:
        return list(self.positions)

    def get_orders(self, *_: Any) -> list[Any]:
        return list(self.orders)

    def get_position(self, ticket: int) -> Any:
        for pos in self.positions:
            if int(pos.ticket) == int(ticket):
                return pos
        return False

    def confirm_position_absent(self, ticket: int) -> bool:
        return self.get_position(ticket) is False

    def get_position_close_deal(self, position_id: int, *_: Any) -> Any:
        return SimpleNamespace(
            deal=8000 + position_id, position_id=position_id, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC,
            reason="DEAL_REASON_EXPERT", price=2066.0, profit=1.0, commission=-0.1, swap=0.0, fee=0.0,
            deal_time=1767272520, net_profit=0.9,
        )

    def open_position(self, *_: Any, **__: Any) -> int:
        return 1

    def close_position(self, *_: Any, **__: Any) -> bool:
        return True


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def self_test() -> None:
    params = json.loads(json.dumps(load_params()))
    params["live_trading_enabled"] = False
    params["shadow_forward_enabled"] = True
    params["shadow_opportunity_observer"]["enabled"] = False
    params["shadow_state_tagger"]["enabled"] = False
    params["midday_shadow_opportunity_observer"]["enabled"] = False
    params["midday_shadow_state_tagger"]["enabled"] = False
    params["safety"]["stale_signal_guard"] = False
    strategy = params["strategies"][0]
    assert params["candidate_id"] == EXPECTED_CANDIDATE_ID
    assert params["routing_mode"] == "first_consuming_lane_preserve_primary_v1"
    assert params["entry_policy_id"] == EXPECTED_ENTRY_POLICY_ID
    assert params["entry_policy_params_hash"] == EXPECTED_ENTRY_POLICY_PARAMS_HASH
    assert int(params["late_short_lookback_completed_m1_bars"]) == EXPECTED_LATE_SHORT_LOOKBACK
    assert math.isclose(float(params["late_short_drop_threshold"]), EXPECTED_LATE_SHORT_DROP_THRESHOLD)
    assert params["late_short_action"] == EXPECTED_LATE_SHORT_ACTION
    assert params["inventory_range_fade_policy_id"] == EXPECTED_INVENTORY_RANGE_FADE_POLICY_ID
    assert params["inventory_range_fade_params_hash"] == EXPECTED_INVENTORY_RANGE_FADE_PARAMS_HASH
    assert math.isclose(float(params["inventory_range_return_depth_fraction"]), EXPECTED_INVENTORY_RANGE_RETURN_DEPTH)
    assert int(params["inventory_range_max_wait_minutes"]) == EXPECTED_INVENTORY_RANGE_MAX_WAIT_MINUTES
    assert int(params["inventory_range_confirm_bars"]) == EXPECTED_INVENTORY_RANGE_CONFIRM_BARS
    assert params["inventory_range_break_side_filter"] == EXPECTED_INVENTORY_RANGE_BREAK_SIDE_FILTER
    assert int(params["lane_count"]) == 4
    assert tuple(int(row["magic"]) for row in params["strategies"]) == EXPECTED_S23_MAGICS
    assert [int(row["lane_id"]) for row in params["strategies"]] == [1, 2, 3, 4]
    assert params["morning_session_policy_id"] == EXPECTED_MORNING_POLICY_ID
    assert params["morning_session_params_hash"] == EXPECTED_MORNING_POLICY_PARAMS_HASH
    assert tuple(int(row["magic"]) for row in params["morning_session_strategies"]) == EXPECTED_MORNING_MAGICS
    assert [int(row["lane_id"]) for row in params["morning_session_strategies"]] == [5, 6, 7]
    assert [int(row["hold_minutes"]) for row in params["morning_session_strategies"]] == [15, 55, 45]
    assert params["midday_session_policy_id"] == EXPECTED_MIDDAY_POLICY_ID
    assert params["midday_session_params_hash"] == EXPECTED_MIDDAY_POLICY_PARAMS_HASH
    assert tuple(int(row["magic"]) for row in params["midday_session_strategies"]) == EXPECTED_MIDDAY_MAGICS
    assert [int(row["lane_id"]) for row in params["midday_session_strategies"]] == [8]
    assert [int(row["hold_minutes"]) for row in params["midday_session_strategies"]] == [60]
    assert int(strategy["max_positions"]) == 2 and float(strategy["add_atr"]) == 0.65
    assert (float(strategy["entry_wait_z"]), float(strategy["entry_wait_sigma"]), int(strategy["entry_wait_minutes"])) == (2.0, 1.0, 10)
    assert (float(strategy["target_atr_mult"]), float(strategy["stop_atr_mult"]), float(strategy["failure_to_progress_peak_atr_mult"])) == (3.5, 6.5, 1.0)

    runner = S23HorizontalInventoryRunner(params)
    runner.state = runner._default_state()
    runner._save_state = lambda: None
    runner._trade_row = lambda *_args, **_kwargs: None
    assert runner._ownership_namespace_error() is None
    range_state = runner.state["routing"]["inventory_range_fade"]
    assert not range_state["active"] and range_state["pending_side"] is None
    policy_bars = pd.DataFrame(
        {"Close": [4640.0] + [4615.0] * 30},
        index=pd.date_range("2026-08-25 12:30:00", periods=31, freq="1min", tz="UTC"),
    )
    effective, policy = runner._apply_entry_policy("SHORT", policy_bars, SimpleNamespace(bid=4610.0, ask=4610.2))
    assert effective == "LONG" and policy["action"] == "reverse_long"
    effective, policy = runner._apply_entry_policy("SHORT", policy_bars, SimpleNamespace(bid=4615.0, ask=4615.2))
    assert effective == "SHORT" and policy["action"] == "unchanged"
    effective, policy = runner._apply_entry_policy("LONG", policy_bars, SimpleNamespace(bid=4610.0, ask=4610.2))
    assert effective == "LONG" and policy["reason"] == "not_short"
    state = runner._st(strategy)
    state["frozen_basket_atr30"] = 1.5
    assert runner._exit_thresholds(strategy) == (5.25, 9.75, 1.5)
    state["frozen_basket_atr30"] = 2.0
    assert runner._exit_thresholds(strategy) == (10.0, 18.0, 3.0)

    state.update({"pending_entry_side": "LONG", "pending_entry_target": 2064.05, "pending_entry_expires_utc": dt_text(utc_now() + pd.Timedelta(minutes=5)), "pending_entry_atr30": 1.5, "pending_entry_signal_bar": dt_text(utc_now() - pd.Timedelta(minutes=1))})
    assert runner._monitor_pending_entry(strategy, SimpleNamespace(bid=2064.02, ask=2064.05), utc_now())
    assert len(state["basket"]) == 1 and state["frozen_basket_atr30"] == 1.5

    foreign = SimpleNamespace(ticket=9100, identifier=9100, symbol="XAUUSD", magic=EXPECTED_S23_MAGIC, comment="s22_foreign", type=ORDER_TYPE_BUY)
    runner.state = runner._default_state()
    runner.executor = FakeExecutor(positions=[foreign])
    assert not runner._sync_strategy(strategy)
    assert runner._st(strategy)["sync_block_reason"] == "same_magic_unexpected_position_or_order"

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)
    params = load_params()
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=int(params.get("bot_log_max_bytes", 10 * 1024 * 1024)),
        backupCount=int(params.get("bot_log_backup_count", 5)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    if args.self_test:
        self_test()
        print("s23 self-test ok")
        return 0
    runner = S23HorizontalInventoryRunner(params)
    if not runner.connect_and_preflight():
        return 1
    if args.once:
        runner.run_once()
        return 0
    while True:
        runner.run_once()
        time.sleep(float(params.get("poll_interval_seconds", 5)))


if __name__ == "__main__":
    raise SystemExit(main())
