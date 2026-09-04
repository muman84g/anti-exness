# -*- coding: utf-8 -*-
"""S23 ZA inventory with independent JST09-11 and JST11-13 overlays."""

from __future__ import annotations

import argparse
import copy
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
from typing import Any, Callable

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
    SymbolInfo,
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
from jst1300_pre_eu30_strategy import (
    ADMISSION_BLOCK_ID as PRE_EU30_ADMISSION_BLOCK_ID,
    POLICY_ID as PRE_EU30_POLICY_ID,
    POLICY_PARAMS_HASH as PRE_EU30_POLICY_PARAMS_HASH,
    SIGNAL_IDS as PRE_EU30_SIGNAL_IDS,
    in_entry_session as in_pre_eu30_entry_session,
    signal_sides as pre_eu30_signal_sides,
)
from session_vwap_overlay import (
    POLICY_ID as SESSION_VWAP_POLICY_ID,
    PagedM1History,
    entry_history_issue as session_vwap_entry_history_issue,
    in_entry_session as in_session_vwap_entry_session,
    latest_signal as latest_session_vwap_signal,
)
from t0530_edge_overlay import (
    POLICY_ID as T0530_EDGE_POLICY_ID,
    POLICY_PARAMS_HASH as T0530_EDGE_POLICY_PARAMS_HASH,
    in_release_session as in_t0530_edge_release_session,
    latest_signal as latest_t0530_edge_signal,
)
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
EXPECTED_PRE_EU30_MAGICS = (230031, 230032, 230033)
EXPECTED_TREND_RECOVERY_MAGICS = (230034,)
EXPECTED_SESSION_VWAP_MAGICS = (230035, 230036, 230037, 230038, 230039)
EXPECTED_SESSION_VWAP_PARAMS_HASH = "b47b8d7d26094681fe559f6daf9c7e2bb1f4cd610527b0a69c5426c20a7a2a65"
EXPECTED_T0530_EDGE_MAGICS = (230040, 230041, 230042, 230043)
EXPECTED_Q01_MAGICS = (230044,)
EXPECTED_S23_MAGIC = EXPECTED_S23_MAGICS[0]
LEGACY_S23_MAGICS = (200023,)
EXPECTED_STRATEGY_ID = "bot23_za_horizontal_inventory_v001"
EXPECTED_CANDIDATE_ID = "bot23-integrated-session-vwap-on-t0530-edge-on-q01-v008"
EXPECTED_BRIDGE_NAME = "BotBridge_s23"
EXPECTED_BRIDGE_VERSION = "2026-09-04-s23-strict-ipc-q01-v31"
EXPECTED_TREND_RECOVERY_POLICY_ID = "reverse_long_stop_m1_bull_multishort_n2_tp1_sl0p5_v001"
EXPECTED_TREND_RECOVERY_PARAMS_HASH = "a29187af7e67075ef2e4eb0c39cb3cd09bbfb2a6ee7b23e4cd51bbe370c000e9"
EXPECTED_TREND_RECOVERY_ENTRY_WINDOW_MINUTES = 30
EXPECTED_TREND_RECOVERY_MAX_TOTAL_ENTRIES = 2
EXPECTED_TREND_RECOVERY_MAX_HOLD_MINUTES = 70
EXPECTED_Q01_POLICY_ID = "q01_k4_w48_t135_b12_hold30_cap1_v001"
EXPECTED_Q01_POLICY_PARAMS_HASH = "fdec1cecc71305877f280d3225fd17093f92a42708597202ba0bfad4eafacf67"
EXPECTED_Q01_VARIANCE_HORIZON_BARS = 4
EXPECTED_Q01_VARIANCE_WINDOW_BARS = 48
EXPECTED_Q01_VR_THRESHOLD = 1.35
EXPECTED_Q01_BREAKOUT_LOOKBACK_BARS = 12
EXPECTED_Q01_HOLD_MINUTES = 30
EXPECTED_Q01_MAX_POSITIONS = 1
EXPECTED_Q01_MAX_SIGNAL_DELAY_MINUTES = 7
EXPECTED_Q01_MAX_RAW_SPREAD_PRICE = 0.30
EXPECTED_Q01_M1_BARS = 600
EXPECTED_Q01_WARMUP_M5_BARS = 110
EXPECTED_Q01_ATR_PERIOD = 20
EXPECTED_Q01_FEED_GAP_SECONDS = 300
EXPECTED_Q01_LIVE_TRADING_ENABLED = False
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
EXPECTED_PRE_EU30_MAX_POSITIONS = 3
EXPECTED_PRE_EU30_M1_BARS = 420
EXPECTED_ENTRY_ADMISSION_EU_TIMEZONE = "Europe/London"
EXPECTED_ENTRY_ADMISSION_US_TIMEZONE = "America/New_York"
EXPECTED_ENTRY_ADMISSION_NOTATION = "resolved_utc_from_market_dst"
EXPECTED_ENTRY_ADMISSION_SCOPE = "new_entry_admission_only"
EXPECTED_POSITION_LIFECYCLE = "confirmed_fill_utc_independent"
EXPECTED_ENTRY_ADMISSION_BLOCKS = (
    ("jst1300_pre_eu30", "fixed_utc", "04:00", "04:00", "Europe/London", "06:30", "07:30"),
    ("eu_open_to_us_preopen", "Europe/London", "06:30", "07:30", "America/New_York", "11:30", "12:30"),
    ("us_to_eu_late", "America/New_York", "11:30", "12:30", "America/New_York", "20:30", "21:30"),
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
CONFIRMED_CLOSE_CLEAR_SYNC_REASONS = FLAT_AUTO_CLEAR_SYNC_REASONS | {
    "close_submission_result_unresolved",
    "close_deal_query_unavailable",
    "close_deal_not_confirmed",
    "close_deal_payload_invalid",
    "close_deal_timestamp_invalid",
}
OWNED_CLOSE_RETRY_SYNC_REASONS = {
    "position_query_unavailable_before_close",
    "position_missing_before_close",
    "close_trade_permission_rejected",
    "live_time_close_failed",
    "live_time_close_unconfirmed",
    "live_trend_ticket_close_failed",
}
DEFINITIVE_CLOSE_NO_FILL_RETCODES = frozenset({
    10004,  # requote
    10006,  # rejected
    10007,  # canceled
    10011,  # request processing error
    10013,  # invalid request
    10014,  # invalid volume
    10015,  # invalid price
    10016,  # invalid stops
    10017,  # trading disabled
    10018,  # market closed
    10019,  # insufficient funds
    10020,  # price changed
    10021,  # price unavailable
    10022,  # invalid expiration
    10024,  # too many requests
    10026,  # server autotrading disabled
    10027,  # client autotrading disabled
    10029,  # order or position frozen
    10030,  # invalid filling type
    10032,  # live account required
    10033,  # pending-order limit
    10034,  # volume limit
    10035,  # invalid order type
    10038,  # invalid close volume
    10040,  # position limit
    10041,  # pending activation rejected/canceled
    10042,  # long only
    10043,  # short only
    10044,  # close only
    10045,  # FIFO close required
    10046,  # hedge prohibited
})
CLOSE_TRADE_PERMISSION_RETCODES = frozenset({10026, 10027})
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
SIGNAL_EVALUATION_LOG_FILE = os.path.join(LOG_DIR, "s23_signal_evaluation.csv")
STATE_FILE = os.path.join(STATE_DIR, "s23_bot_state.json")
RUNNER_LOCK_FILE = os.path.join(STATE_DIR, "s23_runner.lock")


def acquire_runner_singleton_lock() -> Any | None:
    """Hold an OS-released process lock for the complete runner lifetime."""
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(RUNNER_LOCK_FILE, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        handle.close()
        return None
    return handle
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
SIGNAL_EVALUATION_FIELDS = [
    "timestamp_utc", "event", "strategy_group", "strategy_id", "lane_id",
    "magic", "spec_id", "configured_signal_id", "signal_variant_id",
    "signal_transform_id", "raw_side", "effective_side", "opportunity_id",
    "basket_id", "ticket", "position_identifier", "deal_id", "profit",
    "reason", "signal_bar_time", "event_time", "release_time",
    "available_time", "decision_time", "executable_at", "live",
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


_TOP_LEVEL_BOOLEAN_CONFIG_KEYS = (
    "enabled",
    "live_trading_enabled",
    "shadow_forward_enabled",
    "require_hedging_account",
    "long_target_portfolio_rearm_enabled",
    "inventory_range_fade_enabled",
    "late_short_30m_action_enabled",
    "morning_session_enabled",
    "midday_session_enabled",
    "pre_eu30_session_enabled",
    "trend_recovery_enabled",
    "session_vwap_enabled",
    "t0530_edge_enabled",
    "q01_variance_release_enabled",
    "q01_live_trading_enabled",
    "drop_latest_m1_bar",
)

_STRATEGY_CONFIG_COLLECTIONS = (
    "strategies",
    "morning_session_strategies",
    "midday_session_strategies",
    "pre_eu30_session_strategies",
    "trend_recovery_strategies",
    "session_vwap_strategies",
    "t0530_edge_strategies",
    "q01_variance_release_strategies",
)

_EXPECTED_STRATEGY_IDS_BY_COLLECTION = {
    "strategies": tuple(f"za_horizontal_lane_{index}" for index in range(1, 5)),
    "morning_session_strategies": tuple(f"jst0911_morning_lane_{index}" for index in range(1, 4)),
    "midday_session_strategies": ("jst1113_round_sweep_lane_1",),
    "pre_eu30_session_strategies": tuple(f"jst1300_pre_eu30_lane_{index}" for index in range(1, 4)),
    "trend_recovery_strategies": ("reverse_long_stop_trend_lane_1",),
    "session_vwap_strategies": tuple(f"ny0530_session_vwap_lane_{index}" for index in range(1, 6)),
    "t0530_edge_strategies": tuple(f"ny0530_edge_lane_{index}" for index in range(1, 5)),
    "q01_variance_release_strategies": ("q01_variance_release_lane_1",),
}

# A persisted policy id/hash pair is the generation marker for state introduced
# with that policy.  Once both markers exist, silently defaulting a missing lane
# or routing field could erase live lifecycle state under an apparently current
# identity.  States predating both markers retain the explicit migration path.
_STATE_GENERATION_CONTRACTS = (
    ("entry_policy_id", "entry_policy_params_hash", "strategies", ()),
    ("portfolio_rearm_policy_id", "portfolio_rearm_params_hash", None, (
        "long_target_rearm_pending_confirmation", "long_target_rearm_request_utc",
        "long_target_rearm_until_utc", "long_target_rearm_confirmed_utc",
        "long_target_rearm_trigger_lane_id", "long_target_rearm_trigger_basket_id",
        "long_target_rearm_expired_utc",
    )),
    ("inventory_range_fade_policy_id", "inventory_range_fade_params_hash", None, (
        "inventory_range_fade",
    )),
    ("morning_policy_id", "morning_policy_params_hash", "morning_session_strategies", ()),
    ("midday_policy_id", "midday_policy_params_hash", "midday_session_strategies", ()),
    ("pre_eu30_policy_id", "pre_eu30_policy_params_hash", "pre_eu30_session_strategies", ()),
    ("trend_recovery_policy_id", "trend_recovery_params_hash", "trend_recovery_strategies", (
        "trend_recovery",
    )),
    ("session_vwap_policy_id", "session_vwap_params_hash", "session_vwap_strategies", (
        "session_vwap_last_evaluated_bar", "session_vwap_last_unavailable_bar",
    )),
    ("t0530_edge_policy_id", "t0530_edge_params_hash", "t0530_edge_strategies", (
        "t0530_edge_last_evaluated_bar",
    )),
    ("q01_policy_id", "q01_params_hash", "q01_variance_release_strategies", (
        "q01_last_evaluated_m5_bar",
    )),
)

_CORE_LANE_STATE_KEYS = ("lane_id", "basket", "basket_sequence", "current_basket_id")
_REQUIRED_LANE_STATE_KEYS_BY_COLLECTION = {
    "q01_variance_release_strategies": ("q01_retry_opportunity", "q01_last_quote_msc"),
}

_STRATEGY_KEYS_BY_COLLECTION = {
    "strategies": frozenset({
        "enabled", "id", "lane_id", "spec_id", "signal_id", "magic", "comment_prefix", "lot",
        "session_start_utc", "session_end_utc", "mode", "impulse_bars", "impulse_atr",
        "add_atr", "max_positions", "add_profit_guard_ratio", "basket_target_usd",
        "basket_stop_usd", "max_hold_bars", "cooldown", "vol_min",
        "failure_to_progress_bars", "failure_to_progress_peak_usd", "entry_wait_z",
        "entry_wait_sigma", "entry_wait_minutes", "entry_require_extreme",
        "target_atr_mult", "stop_atr_mult", "failure_to_progress_peak_atr_mult",
        "entry_max_spread_atr_ratio", "adaptive_fixed_exit_atr_threshold", "reverse_on_fail",
    }),
    "morning_session_strategies": frozenset({
        "enabled", "id", "lane_id", "morning_lane_id", "spec_id", "signal_id",
        "magic", "comment_prefix", "lot", "hold_minutes", "max_positions", "cooldown",
    }),
    "midday_session_strategies": frozenset({
        "enabled", "id", "lane_id", "midday_lane_id", "spec_id", "signal_id",
        "magic", "comment_prefix", "lot", "hold_minutes", "max_positions", "cooldown",
        "level_step", "atr_period", "min_sweep_depth_atr", "reclaim_atr",
    }),
    "pre_eu30_session_strategies": frozenset({
        "enabled", "id", "lane_id", "pre_eu30_lane_id", "spec_id", "signal_id",
        "magic", "comment_prefix", "lot", "hold_minutes", "max_positions", "cooldown",
    }),
    "trend_recovery_strategies": frozenset({
        "enabled", "id", "lane_id", "spec_id", "signal_id", "magic", "comment_prefix",
        "lot", "max_positions", "cooldown", "hold_minutes", "ticket_target_usd",
        "ticket_stop_usd", "target_atr_mult", "stop_atr_mult",
        "adaptive_fixed_exit_atr_threshold", "tp_multiplier", "sl_multiplier",
    }),
    "session_vwap_strategies": frozenset({
        "enabled", "id", "lane_id", "spec_id", "signal_id", "magic", "comment_prefix",
        "lot", "hold_minutes", "max_positions", "cooldown",
    }),
    "t0530_edge_strategies": frozenset({
        "enabled", "id", "lane_id", "spec_id", "signal_id", "magic", "comment_prefix",
        "lot", "hold_minutes", "max_positions", "cooldown",
    }),
    "q01_variance_release_strategies": frozenset({
        "enabled", "id", "lane_id", "spec_id", "signal_id", "magic", "comment_prefix",
        "lot", "hold_minutes", "max_positions", "cooldown",
    }),
}


def validate_strategy_topology_config(params: dict[str, Any]) -> None:
    """Freeze all 22 lane state namespaces and executable row schemas."""
    observed_ids: list[str] = []
    for collection in _STRATEGY_CONFIG_COLLECTIONS:
        rows = params.get(collection)
        expected_ids = _EXPECTED_STRATEGY_IDS_BY_COLLECTION[collection]
        if not isinstance(rows, list) or len(rows) != len(expected_ids):
            raise ValueError(f"invalid strategy topology count: {collection}")
        ids = tuple(row.get("id") if isinstance(row, dict) else None for row in rows)
        if ids != expected_ids:
            raise ValueError(
                f"invalid strategy topology ids: {collection}={ids!r} expected={expected_ids!r}"
            )
        expected_keys = _STRATEGY_KEYS_BY_COLLECTION[collection]
        for index, row in enumerate(rows):
            if frozenset(row) != expected_keys:
                missing = sorted(expected_keys - frozenset(row))
                unknown = sorted(frozenset(row) - expected_keys)
                raise ValueError(
                    f"invalid strategy row schema: {collection}[{index}] missing={missing} unknown={unknown}"
                )
        observed_ids.extend(str(value) for value in ids)
    if len(observed_ids) != len(set(observed_ids)):
        raise ValueError("duplicate strategy state namespace")
    ordinal_contracts = (
        ("morning_session_strategies", "morning_lane_id"),
        ("midday_session_strategies", "midday_lane_id"),
        ("pre_eu30_session_strategies", "pre_eu30_lane_id"),
    )
    for collection, key in ordinal_contracts:
        observed = tuple(row[key] for row in params[collection])
        expected = tuple(range(1, len(params[collection]) + 1))
        if observed != expected:
            raise ValueError(f"invalid strategy ordinal: {collection}.{key}={observed!r}")


def validate_boolean_config(params: dict[str, Any]) -> None:
    """Reject coercible configuration booleans before they alter execution."""
    defaults = {
        "enabled": True,
        "live_trading_enabled": False,
        "shadow_forward_enabled": True,
        "require_hedging_account": True,
        "drop_latest_m1_bar": True,
    }
    for key in _TOP_LEVEL_BOOLEAN_CONFIG_KEYS:
        value = params.get(key, defaults.get(key, False))
        if not isinstance(value, bool):
            raise ValueError(f"invalid boolean config: {key}={value!r}")
    safety = params.get("safety", {})
    if not isinstance(safety, dict):
        raise ValueError("invalid safety config container")
    allowed_safety = set(LiveSafetyOptions.__dataclass_fields__)
    for key, value in safety.items():
        if key not in allowed_safety:
            raise ValueError(f"unknown safety config: {key}")
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"invalid safety boolean config: {key}={value!r}")
    for collection in _STRATEGY_CONFIG_COLLECTIONS:
        rows = params.get(collection, [])
        if not isinstance(rows, list):
            raise ValueError(f"invalid strategy config container: {collection}")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"invalid strategy config row: {collection}[{index}]")
            if "enabled" in row and not isinstance(row["enabled"], bool):
                raise ValueError(
                    f"invalid strategy boolean config: {collection}[{index}].enabled={row['enabled']!r}"
                )
            for key in ("entry_require_extreme", "reverse_on_fail"):
                if key in row and not isinstance(row[key], bool):
                    raise ValueError(
                        f"invalid strategy boolean config: {collection}[{index}].{key}={row[key]!r}"
                    )
    admission_clock = params.get("eu_entry_admission_clock")
    if not isinstance(admission_clock, dict):
        raise ValueError("invalid entry admission clock container")
    if not isinstance(admission_clock.get("routing_enabled"), bool):
        raise ValueError("invalid entry admission routing boolean")


def validate_execution_numeric_config(params: dict[str, Any]) -> None:
    """Reject coercible or unsafe execution/risk numerics before startup."""
    positive_numbers = (
        "default_lot",
        "contract_size",
        "poll_interval_seconds",
        "status_log_interval_seconds",
        "diagnostic_repeat_summary_seconds",
        "max_signal_delay_minutes",
        "daily_realized_loss_limit_usd",
        "point_size",
        "fixed_hold_close_force_after_minutes",
        "fixed_hold_market_closed_retry_seconds",
        "trade_permission_retry_seconds",
    )
    nonnegative_numbers = (
        "max_entry_spread_points",
        "fixed_hold_close_max_spread_points",
    )
    positive_integers = (
        "m1_timeframe",
        "m1_bars",
        "fixed_hold_close_stable_polls",
        "bot_log_max_bytes",
        "trade_permission_alert_threshold",
    )
    nonnegative_integers = ("price_digits", "deviation_points", "bot_log_backup_count")

    for key in positive_numbers + nonnegative_numbers:
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"invalid numeric config type: {key}={value!r}")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"invalid nonfinite numeric config: {key}={value!r}")
        if key in positive_numbers and numeric <= 0.0:
            raise ValueError(f"invalid nonpositive numeric config: {key}={value!r}")
        if key in nonnegative_numbers and numeric < 0.0:
            raise ValueError(f"invalid negative numeric config: {key}={value!r}")

    for key in positive_integers + nonnegative_integers:
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"invalid integer config type: {key}={value!r}")
        if key in positive_integers and value <= 0:
            raise ValueError(f"invalid nonpositive integer config: {key}={value!r}")
        if key in nonnegative_integers and value < 0:
            raise ValueError(f"invalid negative integer config: {key}={value!r}")
    if int(params["price_digits"]) > 12:
        raise ValueError(f"invalid price_digits config: {params['price_digits']!r}")

    blocked_hours = params.get("new_basket_blocked_hours_utc")
    if not isinstance(blocked_hours, list) or any(
        isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23
        for hour in blocked_hours
    ) or len(blocked_hours) != len(set(blocked_hours)):
        raise ValueError(f"invalid blocked-hours config: {blocked_hours!r}")

    history = params.get("session_vwap_history")
    if not isinstance(history, dict):
        raise ValueError("invalid session-VWAP history config container")
    for key, minimum in (("page_bars", 1), ("refresh_bars", 2)):
        value = history.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 5000:
            raise ValueError(f"invalid session-VWAP history integer: {key}={value!r}")
    retry_seconds = history.get("retry_seconds")
    if not isinstance(retry_seconds, list) or not retry_seconds:
        raise ValueError("invalid session-VWAP retry schedule")
    for value in retry_seconds:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ValueError(f"invalid session-VWAP retry delay: {value!r}")

    exact_top_level_integers = (
        "lane_count", "late_short_lookback_completed_m1_bars",
        "long_target_portfolio_rearm_minutes", "inventory_range_max_wait_minutes",
        "inventory_range_confirm_bars", "morning_session_start_utc",
        "morning_session_end_utc", "morning_session_max_positions",
        "midday_session_start_utc", "midday_session_end_utc",
        "midday_session_max_positions", "pre_eu30_session_max_positions",
        "trend_recovery_entry_window_minutes", "trend_recovery_max_total_entries",
        "session_vwap_lookback_calendar_days", "session_vwap_atr_period",
        "session_vwap_hold_minutes", "session_vwap_max_positions",
        "t0530_edge_lookback_bars", "t0530_edge_hold_minutes",
        "t0530_edge_max_positions", "t0530_edge_max_signal_delay_minutes",
        "q01_variance_horizon_bars", "q01_variance_window_bars",
        "q01_breakout_lookback_bars", "q01_hold_minutes",
        "q01_max_positions", "q01_max_signal_delay_minutes", "q01_m1_bars",
        "q01_warmup_m5_bars", "q01_atr_period", "q01_feed_gap_seconds",
    )
    for key in exact_top_level_integers:
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"invalid strategy integer config: {key}={value!r}")

    exact_top_level_numbers = (
        "late_short_drop_threshold", "inventory_range_return_depth_fraction",
        "session_vwap_quantile", "q01_vr_threshold", "q01_max_raw_spread_price",
    )
    for key in exact_top_level_numbers:
        value = params.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"invalid strategy numeric config: {key}={value!r}")

    expected_magic_keys = (
        "expected_magics", "expected_morning_magics", "expected_midday_magics",
        "expected_pre_eu30_magics", "expected_trend_recovery_magics",
        "expected_session_vwap_magics",
        "expected_t0530_edge_magics",
        "expected_q01_magics",
    )
    for key in expected_magic_keys:
        values = params.get(key)
        if (
            not isinstance(values, list)
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values)
            or len(values) != len(set(values))
        ):
            raise ValueError(f"invalid expected magic config: {key}={values!r}")

    strategy_integer_fields = {
        "lane_id", "magic", "max_positions", "cooldown", "hold_minutes",
        "morning_lane_id", "midday_lane_id", "pre_eu30_lane_id",
        "session_start_utc", "session_end_utc", "impulse_bars", "max_hold_bars",
        "failure_to_progress_bars", "entry_wait_minutes", "atr_period",
    }
    strategy_numeric_fields = {
        "lot", "impulse_atr", "add_atr", "add_profit_guard_ratio",
        "basket_target_usd", "basket_stop_usd", "vol_min",
        "failure_to_progress_peak_usd", "entry_wait_z", "entry_wait_sigma",
        "target_atr_mult", "stop_atr_mult", "failure_to_progress_peak_atr_mult",
        "entry_max_spread_atr_ratio", "adaptive_fixed_exit_atr_threshold",
        "level_step", "min_sweep_depth_atr", "reclaim_atr",
        "ticket_target_usd", "ticket_stop_usd", "tp_multiplier", "sl_multiplier",
    }
    for collection in _STRATEGY_CONFIG_COLLECTIONS:
        for index, row in enumerate(params.get(collection, [])):
            for key in strategy_integer_fields & row.keys():
                value = row[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(
                        f"invalid lane integer config: {collection}[{index}].{key}={value!r}"
                    )
            for key in strategy_numeric_fields & row.keys():
                value = row[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    raise ValueError(
                        f"invalid lane numeric config: {collection}[{index}].{key}={value!r}"
                    )


def _strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"nonfinite JSON constant: {value}")


def strict_json_load(handle: Any) -> Any:
    return json.load(
        handle,
        object_pairs_hook=_strict_json_pairs,
        parse_constant=_reject_json_constant,
    )


def _fsync_parent_directory(path: str) -> None:
    """Make an atomic replace durable on POSIX filesystems."""
    if os.name != "posix":
        return
    parent = os.path.dirname(os.path.abspath(path)) or "."
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    directory_fd = os.open(parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                payload, f, ensure_ascii=False, indent=2, sort_keys=True,
                allow_nan=False,
            )
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_parent_directory(path)
    except Exception:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise


def append_csv(path: str, row: dict[str, Any], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    # Recheck the header on every append.  A same-path rotation/replacement can
    # preserve the process-local cache key while changing the actual schema;
    # cached validation must never authorize writes into the replacement file.
    if exists:
        with open(path, "r", newline="", encoding="utf-8") as existing_file:
            observed_fields = next(csv.reader(existing_file), [])
        if observed_fields != fields:
            raise RuntimeError(
                f"CSV schema mismatch for {path}; archive/reset the old trades CSV before starting bot23"
            )
        with open(path, "rb") as existing_bytes:
            existing_bytes.seek(-1, os.SEEK_END)
            if existing_bytes.read(1) != b"\n":
                raise RuntimeError(
                    f"unterminated CSV tail for {path}; repair/archive the partial row before starting bot23"
                )
        _CSV_SCHEMAS_VALIDATED.add(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
            _CSV_SCHEMAS_VALIDATED.add(path)
        writer.writerow({name: row.get(name, "") for name in fields})
        f.flush()
        os.fsync(f.fileno())
    if not exists:
        _fsync_parent_directory(path)


def confirmed_close_audit_exists(
    path: str,
    row: dict[str, Any],
    fields: list[str],
) -> bool:
    """Return whether the same broker close deal is already durably audited.

    A confirmed MT5 deal is immutable and globally unique.  Scanning only on a
    confirmed close keeps the normal high-volume diagnostic path cheap while
    making restart/retry after a state-write failure idempotent.  A reused deal
    ID with different lane/position ownership is unsafe and must fail closed.
    """
    if str(row.get("event") or "") != "position_close_confirmed":
        return False
    try:
        deal_id = int(row.get("deal_id"))
        lane_id = int(row.get("lane_id"))
        position_identifier = int(row.get("position_identifier"))
    except (TypeError, ValueError, OverflowError):
        return False
    if deal_id <= 0 or lane_id <= 0 or position_identifier <= 0:
        return False
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    # A matching identity in a truncated/malformed file is not durable proof.
    # Validate the complete ledger before allowing deduplication to advance
    # close state without another append.
    close_identities = validate_csv_schema(path, fields)
    existing_ownership = close_identities.get(deal_id)
    if existing_ownership is None:
        return False
    if existing_ownership != (lane_id, position_identifier):
        raise RuntimeError(
            f"confirmed close deal identity conflict for deal {deal_id}"
        )
    # Readability does not prove durability after an earlier append/fsync
    # failure. Re-sync the existing ledger and its directory before replay
    # can consume the broker-confirmed position state.
    with open(path, "r+", newline="", encoding="utf-8") as durable_file:
        durable_file.flush()
        os.fsync(durable_file.fileno())
    _fsync_parent_directory(path)
    return True


def _csv_audit_value(value: Any) -> str:
    return "" if value is None else str(value)


def post_close_audit_key(
    row: dict[str, Any],
    fields: list[str],
) -> tuple[str, ...]:
    """Build the stable CSV identity for one derived post-close audit row."""
    return tuple(
        _csv_audit_value(row.get(name, ""))
        for name in fields
        if name != "timestamp_utc"
    )


# Keep passive evaluation writes independently patchable from the operational
# trade ledger. Existing tests and maintenance tools often replace append_csv
# to inspect only the operational rows.
append_signal_evaluation_csv = append_csv


def validate_csv_schema(
    path: str,
    fields: list[str],
    *,
    collect_post_close_keys: set[tuple[str, ...]] | None = None,
) -> dict[int, tuple[int, int]]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    with open(path, "r", newline="", encoding="utf-8") as existing_file:
        # A newline-terminated physical tail can still contain an unfinished
        # quoted field. Never let CSV's permissive recovery authorize a close.
        reader = csv.reader(existing_file, strict=True)
        observed_fields = next(reader, [])
        if observed_fields != fields:
            raise RuntimeError(
                f"CSV schema mismatch for {path}; archive/reset the old trades CSV before starting bot23"
            )
        close_identity_indexes = {
            name: observed_fields.index(name)
            for name in ("event", "deal_id", "lane_id", "position_identifier")
            if name in observed_fields
        }
        seen_close_deals: dict[int, tuple[int, int]] = {}
        for line_number, observed_row in enumerate(reader, start=2):
            if len(observed_row) != len(fields):
                raise RuntimeError(
                    f"CSV row width mismatch for {path} at line {line_number}; "
                    "archive/reset the malformed ledger before starting bot23"
                )
            if (
                len(close_identity_indexes) == 4
                and observed_row[close_identity_indexes["event"]]
                == "position_close_confirmed"
            ):
                try:
                    deal_id = int(observed_row[close_identity_indexes["deal_id"]])
                    lane_id = int(observed_row[close_identity_indexes["lane_id"]])
                    position_identifier = int(
                        observed_row[close_identity_indexes["position_identifier"]]
                    )
                except (TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError(
                        f"confirmed close audit identity malformed for {path} at line {line_number}"
                    ) from exc
                if deal_id <= 0 or lane_id <= 0 or position_identifier <= 0:
                    raise RuntimeError(
                        f"confirmed close audit identity malformed for {path} at line {line_number}"
                    )
                ownership = (lane_id, position_identifier)
                if deal_id in seen_close_deals:
                    if seen_close_deals[deal_id] != ownership:
                        raise RuntimeError(
                            f"confirmed close deal identity conflict for deal {deal_id} in {path}"
                        )
                    raise RuntimeError(
                        f"duplicate confirmed close deal {deal_id} in {path}"
                    )
                seen_close_deals[deal_id] = ownership
            elif (
                collect_post_close_keys is not None
                and "deal_id" in close_identity_indexes
            ):
                raw_deal_id = observed_row[close_identity_indexes["deal_id"]]
                try:
                    derived_deal_id = int(raw_deal_id) if raw_deal_id else 0
                except (TypeError, ValueError, OverflowError):
                    derived_deal_id = 0
                if derived_deal_id > 0:
                    observed = dict(zip(observed_fields, observed_row))
                    collect_post_close_keys.add(
                        post_close_audit_key(observed, fields)
                    )
    with open(path, "rb") as existing_bytes:
        existing_bytes.seek(-1, os.SEEK_END)
        if existing_bytes.read(1) != b"\n":
            raise RuntimeError(
                f"unterminated CSV tail for {path}; repair/archive the partial row before starting bot23"
            )
    _CSV_SCHEMAS_VALIDATED.add(path)
    return seen_close_deals


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
        validate_boolean_config(params)
        validate_strategy_topology_config(params)
        validate_execution_numeric_config(params)
        self.params = params
        self.live_enabled = params.get("live_trading_enabled", False)
        self.shadow_enabled = params.get("shadow_forward_enabled", True)
        self.safety = LiveSafetyOptions(**params.get("safety", {}))
        self.dm = MT5DataManager(self.safety)
        history = dict(params.get("session_vwap_history") or {})
        self.session_vwap_history = PagedM1History(
            self.dm,
            symbol=str(params.get("mt5_symbol", params["symbol"])),
            timeframe=int(params.get("m1_timeframe", 1)),
            broker_timezone=str(params.get("broker_timezone", "UTC")),
            page_bars=int(history.get("page_bars", 5000)),
            refresh_bars=int(history.get("refresh_bars", 10)),
            coverage_days=int(params.get("session_vwap_lookback_calendar_days", 20)),
            retry_seconds=tuple(history.get("retry_seconds", [5, 15, 30, 60])),
        )
        self._session_vwap_snapshot: Any = None
        self.executor = MT5Executor()
        self._suppress_manual_alerts = False
        self._entry_policy_state_migrated = False
        self._portfolio_rearm_state_migrated = False
        self._inventory_range_fade_state_migrated = False
        self._morning_session_state_migrated = False
        self._midday_session_state_migrated = False
        self._pre_eu30_session_state_migrated = False
        self._trend_recovery_state_migrated = False
        self._session_vwap_state_migrated = False
        self._t0530_edge_state_migrated = False
        self._q01_state_migrated = False
        self.state = self._load_state()
        self._last_status_log = 0.0
        self._diagnostic_repeats: dict[int, dict[str, Any]] = {}
        self._last_retained_block_warning: dict[int, tuple[str, str]] = {}
        self._signal_evaluation_enabled = True
        self._post_close_audit_deal_id: int | None = None
        self._post_close_state_before: dict[str, Any] | None = None
        self._post_close_commit_in_progress = False
        self._post_close_trade_keys: set[tuple[str, ...]] = set()
        self._post_close_evaluation_keys: set[tuple[str, ...]] = set()
        self.shadow_observer: Any = None
        self._shadow_observer_error_signature: str | None = None
        self.shadow_state_tagger: Any = None
        self._shadow_state_tagger_error_signature: str | None = None
        self.midday_shadow_observer: Any = None
        self._midday_shadow_observer_error_signature: str | None = None
        self.midday_shadow_state_tagger: Any = None
        self._midday_shadow_state_tagger_error_signature: str | None = None
        self.pre_eu30_shadow_observer: Any = None
        self._pre_eu30_shadow_observer_error_signature: str | None = None
        self.pre_eu30_shadow_state_tagger: Any = None
        self._pre_eu30_shadow_state_tagger_error_signature: str | None = None
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
        try:
            if ShadowOpportunityObserver is None:
                logging.error("S23 pre-EU30 shadow observer module unavailable; trading continues without passive evidence")
            else:
                self.pre_eu30_shadow_observer = ShadowOpportunityObserver(
                    params.get("pre_eu30_shadow_opportunity_observer", {}),
                    log_dir=LOG_DIR,
                    state_dir=STATE_DIR,
                    symbol=str(params.get("mt5_symbol", params["symbol"])),
                    contract_size=float(params.get("contract_size", 100.0)),
                    lot=float(params.get("default_lot", 0.01)),
                )
        except Exception as exc:
            logging.error("S23 pre-EU30 shadow observer disabled after initialization failure: %s", exc)
        try:
            if ShadowStateTagger is None:
                logging.error("S23 pre-EU30 shadow state tagger module unavailable; trading continues without passive state tags")
            else:
                self.pre_eu30_shadow_state_tagger = ShadowStateTagger(
                    params.get("pre_eu30_shadow_state_tagger", {}),
                    log_dir=LOG_DIR,
                    symbol=str(params.get("mt5_symbol", params["symbol"])),
                )
        except Exception as exc:
            logging.error("S23 pre-EU30 shadow state tagger disabled after initialization failure: %s", exc)

    def _morning_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("morning_session_strategies", []))

    def _midday_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("midday_session_strategies", []))

    def _pre_eu30_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("pre_eu30_session_strategies", []))

    def _trend_recovery_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("trend_recovery_strategies", []))

    def _session_vwap_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("session_vwap_strategies", []))

    def _t0530_edge_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("t0530_edge_strategies", []))

    def _q01_strategies(self) -> list[dict[str, Any]]:
        return list(self.params.get("q01_variance_release_strategies", []))

    def _legacy_signal_strategies(self) -> list[dict[str, Any]]:
        return (
            list(self.params.get("strategies", [])) + self._morning_strategies()
            + self._midday_strategies() + self._pre_eu30_strategies()
            + self._trend_recovery_strategies()
        )

    def _all_strategies(self) -> list[dict[str, Any]]:
        return (
            list(self.params.get("strategies", []))
            + self._morning_strategies()
            + self._midday_strategies()
            + self._pre_eu30_strategies()
            + self._trend_recovery_strategies()
            + self._session_vwap_strategies()
            + self._t0530_edge_strategies()
            + self._q01_strategies()
        )

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

    def _pre_eu30_observer_call(self, method: str, **kwargs: Any) -> Any:
        observer = self.pre_eu30_shadow_observer
        if observer is None or not observer.enabled:
            return None
        try:
            result = getattr(observer, method)(**kwargs)
            self._pre_eu30_shadow_observer_error_signature = None
            return result
        except Exception as exc:
            signature = f"{method}:{type(exc).__name__}:{exc}"
            if signature != self._pre_eu30_shadow_observer_error_signature:
                logging.error("S23 pre-EU30 shadow observer failure ignored by trading path: %s", signature)
                self._pre_eu30_shadow_observer_error_signature = signature
            return None

    def _pre_eu30_state_tagger_call(self, method: str, **kwargs: Any) -> Any:
        tagger = self.pre_eu30_shadow_state_tagger
        if tagger is None or not tagger.enabled:
            return None
        try:
            result = getattr(tagger, method)(**kwargs)
            self._pre_eu30_shadow_state_tagger_error_signature = None
            return result
        except Exception as exc:
            signature = f"{method}:{type(exc).__name__}:{exc}"
            if signature != self._pre_eu30_shadow_state_tagger_error_signature:
                logging.error("S23 pre-EU30 shadow state tagger failure ignored by trading path: %s", signature)
                self._pre_eu30_shadow_state_tagger_error_signature = signature
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
        # Preserve the existing ZA/legacy-overlay observer schema. The new
        # session-VWAP lane family has its own decision audit rows.
        context_strategies = self._legacy_signal_strategies()
        for strat in context_strategies:
            lane_id = int(strat["lane_id"])
            basket = list(self._basket_rows(strat))
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
                "pre_eu30_policy_id": str(self.params.get("pre_eu30_session_policy_id", PRE_EU30_POLICY_ID)),
                "pre_eu30_policy_params_hash": str(self.params.get("pre_eu30_session_params_hash", PRE_EU30_POLICY_PARAMS_HASH)),
                "trend_recovery_policy_id": str(self.params.get("trend_recovery_policy_id", EXPECTED_TREND_RECOVERY_POLICY_ID)),
                "trend_recovery_params_hash": str(self.params.get("trend_recovery_params_hash", EXPECTED_TREND_RECOVERY_PARAMS_HASH)),
                "session_vwap_policy_id": str(self.params.get("session_vwap_policy_id", SESSION_VWAP_POLICY_ID)),
                "session_vwap_params_hash": str(self.params.get("session_vwap_params_hash", EXPECTED_SESSION_VWAP_PARAMS_HASH)),
                "session_vwap_last_evaluated_bar": None,
                "session_vwap_last_unavailable_bar": None,
                "t0530_edge_policy_id": str(self.params.get("t0530_edge_policy_id", T0530_EDGE_POLICY_ID)),
                "t0530_edge_params_hash": str(self.params.get("t0530_edge_params_hash", T0530_EDGE_POLICY_PARAMS_HASH)),
                "t0530_edge_last_evaluated_bar": None,
                "q01_policy_id": str(self.params.get("q01_policy_id", EXPECTED_Q01_POLICY_ID)),
                "q01_params_hash": str(self.params.get("q01_params_hash", EXPECTED_Q01_POLICY_PARAMS_HASH)),
                "q01_last_evaluated_m5_bar": None,
                "trend_recovery": {
                    "active": False,
                    "episode_id": None,
                    "origin_lane_id": None,
                    "origin_basket_id": None,
                    "started_utc": None,
                    "entry_until_utc": None,
                    "frozen_atr30": None,
                    "total_entries": 0,
                    "last_processed_m1_bar": None,
                    "ended_utc": None,
                    "end_reason": None,
                },
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
                    "last_closed_side": None,
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
                    "pending_open_expires_utc": None,
                    "pending_open_side": None,
                    "pending_open_lot": None,
                    "pending_open_symbol": None,
                    "pending_open_magic": None,
                    "pending_open_comment": None,
                    "pending_open_signal_bar": None,
                    "pending_open_basket_atr30": None,
                    "pending_open_reverse_used": None,
                    "pending_open_expected_positions": None,
                    "session_vwap_retry_opportunity": None,
                    "t0530_edge_retry_opportunity": None,
                    "q01_retry_opportunity": None,
                    "q01_last_quote_msc": None,
                    "open_retry_after_utc": None,
                    "autotrading_reject_streak": 0,
                    "autotrading_reject_notified": False,
                    "close_trade_permission_reject_streak": 0,
                    "close_trade_permission_reject_notified": False,
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
            for lane_state in default["strategies"].values():
                lane_state["sync_block_reason"] = "state_file_missing"
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "state_path": str(STATE_FILE),
                    "daily_realized_pnl_preserved": False,
                    "action": "reconstruct broker inventory and current UTC-day realized PnL before provisioning a new state",
                }
            logging.critical(
                "S23 state file is missing; all new entries are blocked until explicit state reconstruction"
            )
            return default
        load_error: str | None = None
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = strict_json_load(f)
        except Exception as exc:
            logging.exception("Could not load state; refusing to start from an unverified default")
            state = None
            load_error = f"{type(exc).__name__}: {exc}"
        observed = state if isinstance(state, dict) else {}
        observed_version = observed.get("version")
        version_matches = bool(
            isinstance(observed_version, int)
            and not isinstance(observed_version, bool)
            and observed_version == default["version"]
        )
        strategies = observed.get("strategies")
        expected_strategy_ids = {str(s["id"]) for s in self._all_strategies()}
        expected_lane_ids = {str(s["id"]): int(s["lane_id"]) for s in self._all_strategies()}
        unknown_strategy_ids = (
            sorted(str(strategy_id) for strategy_id in strategies if strategy_id not in expected_strategy_ids)
            if isinstance(strategies, dict)
            else []
        )
        raw_routing = observed.get("routing")
        state_generation_errors: list[dict[str, Any]] = []
        if isinstance(raw_routing, dict) and isinstance(strategies, dict):
            for policy_key, hash_key, collection, routing_keys in _STATE_GENERATION_CONTRACTS:
                policy_value = raw_routing.get(policy_key)
                hash_value = raw_routing.get(hash_key)
                identity_absent = policy_value is None and hash_value is None
                identity_complete = policy_value is not None and hash_value is not None
                missing_strategy_ids = (
                    [
                        strategy_id
                        for strategy_id in _EXPECTED_STRATEGY_IDS_BY_COLLECTION[collection]
                        if strategy_id not in strategies
                    ]
                    if collection is not None
                    else []
                )
                missing_routing_keys = [key for key in routing_keys if key not in raw_routing]
                missing_lane_fields: dict[str, list[str]] = {}
                invalid_lane_core: dict[str, list[str]] = {}
                if collection is not None:
                    for strategy_id in _EXPECTED_STRATEGY_IDS_BY_COLLECTION[collection]:
                        lane_state = strategies.get(strategy_id)
                        if isinstance(lane_state, dict):
                            required_lane_keys = _CORE_LANE_STATE_KEYS + _REQUIRED_LANE_STATE_KEYS_BY_COLLECTION.get(collection, ())
                            missing = [key for key in required_lane_keys if key not in lane_state]
                            if missing:
                                missing_lane_fields[strategy_id] = missing
                            invalid: list[str] = []
                            raw_lane_id = lane_state.get("lane_id")
                            raw_basket = lane_state.get("basket")
                            raw_sequence = lane_state.get("basket_sequence")
                            raw_basket_id = lane_state.get("current_basket_id")
                            if (
                                isinstance(raw_lane_id, bool)
                                or not isinstance(raw_lane_id, int)
                                or raw_lane_id != expected_lane_ids[strategy_id]
                            ):
                                invalid.append("lane_id")
                            if not isinstance(raw_basket, list):
                                invalid.append("basket")
                            if (
                                isinstance(raw_sequence, bool)
                                or not isinstance(raw_sequence, int)
                                or raw_sequence < 0
                            ):
                                invalid.append("basket_sequence")
                            if raw_basket_id is not None and (
                                not isinstance(raw_basket_id, str) or not raw_basket_id.strip()
                            ):
                                invalid.append("current_basket_id")
                            lane_id_valid = (
                                isinstance(raw_lane_id, int)
                                and not isinstance(raw_lane_id, bool)
                                and raw_lane_id == expected_lane_ids[strategy_id]
                            )
                            sequence_valid = (
                                isinstance(raw_sequence, int)
                                and not isinstance(raw_sequence, bool)
                                and raw_sequence >= 0
                            )
                            if isinstance(raw_basket, list) and lane_id_valid and sequence_valid:
                                if raw_basket and raw_sequence == 0:
                                    invalid.append("basket_sequence_nonzero_for_open_basket")
                                expected_basket_id = (
                                    f"L{raw_lane_id}-B{raw_sequence:06d}"
                                    if raw_basket
                                    else None
                                )
                                if raw_basket_id != expected_basket_id:
                                    invalid.append("basket_current_id_consistency")
                            if invalid:
                                invalid_lane_core[strategy_id] = invalid
                expected_family_size = (
                    len(_EXPECTED_STRATEGY_IDS_BY_COLLECTION[collection])
                    if collection is not None
                    else 0
                )
                partial_legacy_family = bool(
                    identity_absent
                    and expected_family_size
                    and 0 < len(missing_strategy_ids) < expected_family_size
                )
                if missing_lane_fields or invalid_lane_core or partial_legacy_family or (
                    not identity_absent
                    and (
                        not identity_complete
                        or missing_strategy_ids
                        or missing_routing_keys
                    )
                ):
                    state_generation_errors.append({
                        "policy_key": policy_key,
                        "identity_complete": identity_complete,
                        "partial_legacy_family": partial_legacy_family,
                        "missing_strategy_ids": missing_strategy_ids,
                        "missing_routing_keys": missing_routing_keys,
                        "missing_lane_fields": missing_lane_fields,
                        "invalid_lane_core": invalid_lane_core,
                    })
        routing_shape_matches = (
            isinstance(raw_routing, dict)
            and all(
                key not in raw_routing
                or not isinstance(default_value, dict)
                or isinstance(raw_routing.get(key), dict)
                for key, default_value in default["routing"].items()
            )
        )
        shape_matches = (
            isinstance(strategies, dict)
            and not unknown_strategy_ids
            and not state_generation_errors
            and routing_shape_matches
            and all(
                isinstance(strategies.get(s["id"]), dict)
                for s in self.params["strategies"]
            )
            and all(
                s["id"] not in strategies
                or isinstance(strategies.get(s["id"]), dict)
                for s in self._all_strategies()
            )
        )
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
                "unknown_strategy_ids": unknown_strategy_ids,
                "state_generation_errors": state_generation_errors,
                "load_error": load_error,
            }
            logging.critical(
                "S23 state identity/shape invalid; refusing legacy, corrupt, or foreign state: bot=%s strategy_id=%s version=%s type=%s",
                observed.get("bot"), observed.get("strategy_id"), observed.get("version"), type(state).__name__,
            )
            state = default
            for strat in self._all_strategies():
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
        observed_pre_eu30_policy_id = observed_routing.get("pre_eu30_policy_id")
        observed_pre_eu30_policy_hash = observed_routing.get("pre_eu30_policy_params_hash")
        observed_trend_policy_id = observed_routing.get("trend_recovery_policy_id")
        observed_trend_policy_hash = observed_routing.get("trend_recovery_params_hash")
        observed_session_vwap_policy_id = observed_routing.get("session_vwap_policy_id")
        observed_session_vwap_policy_hash = observed_routing.get("session_vwap_params_hash")
        observed_t0530_edge_policy_id = observed_routing.get("t0530_edge_policy_id")
        observed_t0530_edge_policy_hash = observed_routing.get("t0530_edge_params_hash")
        observed_q01_policy_id = observed_routing.get("q01_policy_id")
        observed_q01_policy_hash = observed_routing.get("q01_params_hash")
        state.setdefault("routing", default["routing"])
        for key, value in default["routing"].items():
            state["routing"].setdefault(key, value)
        state.setdefault("strategies", {})
        for sid, st in default["strategies"].items():
            state["strategies"].setdefault(sid, st)
            for key, value in st.items():
                state["strategies"][sid].setdefault(key, value)
        routing = state["routing"]

        def block_za_policy_mismatch(
            reason: str,
            observed_policy_id: Any,
            observed_policy_hash: Any,
            expected_policy_id: str,
            expected_policy_hash: str,
        ) -> None:
            for strategy in self.params["strategies"]:
                lane_state = state["strategies"][strategy["id"]]
                if lane_state.get("sync_block_reason") == "state_identity_mismatch":
                    continue
                lane_state["sync_block_new_entries"] = True
                lane_state["sync_block_reason"] = reason
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "observed_policy_id": observed_policy_id,
                    "observed_policy_hash": observed_policy_hash,
                    "expected_policy_id": expected_policy_id,
                    "expected_policy_hash": expected_policy_hash,
                }

        expected_policy_id = str(self.params.get("entry_policy_id", EXPECTED_ENTRY_POLICY_ID))
        expected_policy_hash = str(self.params.get("entry_policy_params_hash", EXPECTED_ENTRY_POLICY_PARAMS_HASH))
        if observed_entry_policy_id is None and observed_entry_policy_hash is None:
            # Existing open baskets remain under the unchanged four-lane close
            # contract. Only unsubmitted local pending entries from the
            # pre-identity generation are discarded at the explicit cutover.
            pending_fields = (
                "pending_entry_side", "pending_entry_target", "pending_entry_expires_utc",
                "pending_entry_atr30", "pending_entry_signal_bar",
                "pending_entry_opportunity_id", "pending_entry_event_time",
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
        elif (
            observed_entry_policy_id != expected_policy_id
            or observed_entry_policy_hash != expected_policy_hash
        ):
            block_za_policy_mismatch(
                "entry_policy_identity_mismatch",
                observed_entry_policy_id, observed_entry_policy_hash,
                expected_policy_id, expected_policy_hash,
            )
        expected_rearm_policy_id = str(self.params.get("portfolio_rearm_policy_id", EXPECTED_PORTFOLIO_REARM_POLICY_ID))
        expected_rearm_policy_hash = str(self.params.get("portfolio_rearm_params_hash", EXPECTED_PORTFOLIO_REARM_PARAMS_HASH))
        if observed_rearm_policy_id is None and observed_rearm_policy_hash is None:
            routing["portfolio_rearm_policy_id"] = expected_rearm_policy_id
            routing["portfolio_rearm_params_hash"] = expected_rearm_policy_hash
            self._portfolio_rearm_state_migrated = True
            logging.warning(
                "S23 portfolio rearm state initialized to %s; existing baskets and pending entries were preserved",
                expected_rearm_policy_id,
            )
        elif observed_rearm_policy_id != expected_rearm_policy_id or observed_rearm_policy_hash != expected_rearm_policy_hash:
            block_za_policy_mismatch(
                "portfolio_rearm_policy_identity_mismatch",
                observed_rearm_policy_id, observed_rearm_policy_hash,
                expected_rearm_policy_id, expected_rearm_policy_hash,
            )
        expected_range_policy_id = str(self.params.get("inventory_range_fade_policy_id", EXPECTED_INVENTORY_RANGE_FADE_POLICY_ID))
        expected_range_policy_hash = str(self.params.get("inventory_range_fade_params_hash", EXPECTED_INVENTORY_RANGE_FADE_PARAMS_HASH))
        expected_range_state = default["routing"]["inventory_range_fade"]
        observed_range_state = routing.get("inventory_range_fade")
        if observed_range_policy_id is None and observed_range_policy_hash is None:
            routing["inventory_range_fade_policy_id"] = expected_range_policy_id
            routing["inventory_range_fade_params_hash"] = expected_range_policy_hash
            routing["inventory_range_fade"] = dict(expected_range_state)
            self._inventory_range_fade_state_migrated = True
            logging.warning(
                "S23 inventory range-fade state initialized to %s; existing baskets and pending ZA entries were preserved",
                expected_range_policy_id,
            )
        elif observed_range_policy_id != expected_range_policy_id or observed_range_policy_hash != expected_range_policy_hash:
            block_za_policy_mismatch(
                "inventory_range_fade_policy_identity_mismatch",
                observed_range_policy_id, observed_range_policy_hash,
                expected_range_policy_id, expected_range_policy_hash,
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
                if lane_state.get("sync_block_reason") == "state_identity_mismatch":
                    continue
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
                if lane_state.get("sync_block_reason") == "state_identity_mismatch":
                    continue
                lane_state["sync_block_new_entries"] = True
                lane_state["sync_block_reason"] = "midday_policy_identity_mismatch"
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "observed_policy_id": observed_midday_policy_id,
                    "observed_policy_hash": observed_midday_policy_hash,
                    "expected_policy_id": expected_midday_policy_id,
                    "expected_policy_hash": expected_midday_policy_hash,
                }
        expected_pre_eu30_policy_id = str(self.params.get("pre_eu30_session_policy_id", PRE_EU30_POLICY_ID))
        expected_pre_eu30_policy_hash = str(self.params.get("pre_eu30_session_params_hash", PRE_EU30_POLICY_PARAMS_HASH))
        if observed_pre_eu30_policy_id is None and observed_pre_eu30_policy_hash is None:
            routing["pre_eu30_policy_id"] = expected_pre_eu30_policy_id
            routing["pre_eu30_policy_params_hash"] = expected_pre_eu30_policy_hash
            self._pre_eu30_session_state_migrated = True
            logging.warning(
                "S23 pre-EU30 session state initialized to %s; existing strategy state was preserved",
                expected_pre_eu30_policy_id,
            )
        elif (
            observed_pre_eu30_policy_id != expected_pre_eu30_policy_id
            or observed_pre_eu30_policy_hash != expected_pre_eu30_policy_hash
        ):
            for strat in self._pre_eu30_strategies():
                lane_state = state["strategies"][strat["id"]]
                if lane_state.get("sync_block_reason") == "state_identity_mismatch":
                    continue
                lane_state["sync_block_new_entries"] = True
                lane_state["sync_block_reason"] = "pre_eu30_policy_identity_mismatch"
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "observed_policy_id": observed_pre_eu30_policy_id,
                    "observed_policy_hash": observed_pre_eu30_policy_hash,
                    "expected_policy_id": expected_pre_eu30_policy_id,
                    "expected_policy_hash": expected_pre_eu30_policy_hash,
                }
        expected_trend_policy_id = str(self.params.get("trend_recovery_policy_id", EXPECTED_TREND_RECOVERY_POLICY_ID))
        expected_trend_policy_hash = str(self.params.get("trend_recovery_params_hash", EXPECTED_TREND_RECOVERY_PARAMS_HASH))
        if observed_trend_policy_id is None and observed_trend_policy_hash is None:
            routing["trend_recovery_policy_id"] = expected_trend_policy_id
            routing["trend_recovery_params_hash"] = expected_trend_policy_hash
            routing["trend_recovery"] = default["routing"]["trend_recovery"]
            self._trend_recovery_state_migrated = True
            logging.warning(
                "S23 trend-recovery state initialized to %s; existing strategy state was preserved",
                expected_trend_policy_id,
            )
        elif observed_trend_policy_id != expected_trend_policy_id or observed_trend_policy_hash != expected_trend_policy_hash:
            for strat in self._trend_recovery_strategies():
                lane_state = state["strategies"][strat["id"]]
                if lane_state.get("sync_block_reason") == "state_identity_mismatch":
                    continue
                lane_state["sync_block_new_entries"] = True
                lane_state["sync_block_reason"] = "trend_recovery_policy_identity_mismatch"
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "observed_policy_id": observed_trend_policy_id,
                    "observed_policy_hash": observed_trend_policy_hash,
                    "expected_policy_id": expected_trend_policy_id,
                    "expected_policy_hash": expected_trend_policy_hash,
                }
        expected_session_vwap_policy_id = str(self.params.get("session_vwap_policy_id", SESSION_VWAP_POLICY_ID))
        expected_session_vwap_policy_hash = str(self.params.get("session_vwap_params_hash", EXPECTED_SESSION_VWAP_PARAMS_HASH))
        if observed_session_vwap_policy_id is None and observed_session_vwap_policy_hash is None:
            routing["session_vwap_policy_id"] = expected_session_vwap_policy_id
            routing["session_vwap_params_hash"] = expected_session_vwap_policy_hash
            self._session_vwap_state_migrated = True
            logging.warning(
                "S23 session-VWAP state initialized to %s; existing strategy state was preserved",
                expected_session_vwap_policy_id,
            )
        elif (
            observed_session_vwap_policy_id != expected_session_vwap_policy_id
            or observed_session_vwap_policy_hash != expected_session_vwap_policy_hash
        ):
            for strat in self._session_vwap_strategies():
                lane_state = state["strategies"][strat["id"]]
                if lane_state.get("sync_block_reason") == "state_identity_mismatch":
                    continue
                lane_state["sync_block_new_entries"] = True
                lane_state["sync_block_reason"] = "session_vwap_policy_identity_mismatch"
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "observed_policy_id": observed_session_vwap_policy_id,
                    "observed_policy_hash": observed_session_vwap_policy_hash,
                    "expected_policy_id": expected_session_vwap_policy_id,
                    "expected_policy_hash": expected_session_vwap_policy_hash,
                }
        expected_t0530_edge_policy_id = str(self.params.get("t0530_edge_policy_id", T0530_EDGE_POLICY_ID))
        expected_t0530_edge_policy_hash = str(self.params.get("t0530_edge_params_hash", T0530_EDGE_POLICY_PARAMS_HASH))
        if observed_t0530_edge_policy_id is None and observed_t0530_edge_policy_hash is None:
            routing["t0530_edge_policy_id"] = expected_t0530_edge_policy_id
            routing["t0530_edge_params_hash"] = expected_t0530_edge_policy_hash
            self._t0530_edge_state_migrated = True
            logging.warning(
                "S23 t0530-edge state initialized to %s; all existing strategy state was preserved",
                expected_t0530_edge_policy_id,
            )
        elif (
            observed_t0530_edge_policy_id != expected_t0530_edge_policy_id
            or observed_t0530_edge_policy_hash != expected_t0530_edge_policy_hash
        ):
            for strat in self._t0530_edge_strategies():
                lane_state = state["strategies"][strat["id"]]
                if lane_state.get("sync_block_reason") == "state_identity_mismatch":
                    continue
                lane_state["sync_block_new_entries"] = True
                lane_state["sync_block_reason"] = "t0530_edge_policy_identity_mismatch"
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "observed_policy_id": observed_t0530_edge_policy_id,
                    "observed_policy_hash": observed_t0530_edge_policy_hash,
                    "expected_policy_id": expected_t0530_edge_policy_id,
                    "expected_policy_hash": expected_t0530_edge_policy_hash,
                }
        expected_q01_policy_id = str(self.params.get("q01_policy_id", EXPECTED_Q01_POLICY_ID))
        expected_q01_policy_hash = str(self.params.get("q01_params_hash", EXPECTED_Q01_POLICY_PARAMS_HASH))
        if observed_q01_policy_id is None and observed_q01_policy_hash is None:
            routing["q01_policy_id"] = expected_q01_policy_id
            routing["q01_params_hash"] = expected_q01_policy_hash
            self._q01_state_migrated = True
            logging.warning(
                "S23 Q01 variance-release state initialized to %s; all existing strategy state was preserved",
                expected_q01_policy_id,
            )
        elif observed_q01_policy_id != expected_q01_policy_id or observed_q01_policy_hash != expected_q01_policy_hash:
            for strat in self._q01_strategies():
                lane_state = state["strategies"][strat["id"]]
                if lane_state.get("sync_block_reason") == "state_identity_mismatch":
                    continue
                lane_state["sync_block_new_entries"] = True
                lane_state["sync_block_reason"] = "q01_policy_identity_mismatch"
                lane_state["sync_block_recoverable"] = False
                lane_state["sync_block_details"] = {
                    "observed_policy_id": observed_q01_policy_id,
                    "observed_policy_hash": observed_q01_policy_hash,
                    "expected_policy_id": expected_q01_policy_id,
                    "expected_policy_hash": expected_q01_policy_hash,
                }
        return state

    def _save_state(self) -> None:
        # A broker-confirmed close can trigger several helpers that normally
        # persist their own state.  While the post-close transaction is active,
        # defer every such intermediate write: only the complete transition may
        # become durable.  This closes the process/power-loss window where a
        # basket had been consumed but rearm/recovery/cleanup was only partial.
        if (
            getattr(self, "_post_close_audit_deal_id", None) is not None
            and not getattr(self, "_post_close_commit_in_progress", False)
        ):
            return
        self.state["last_saved_utc"] = dt_text(utc_now())
        atomic_write_json(STATE_FILE, self.state)

    def _end_confirmed_close_state_transaction(self) -> None:
        self._post_close_audit_deal_id = None
        self._post_close_state_before = None
        self._post_close_commit_in_progress = False
        self._post_close_trade_keys.clear()
        self._post_close_evaluation_keys.clear()

    def _abort_confirmed_close_state_transaction(self) -> None:
        state_before = self._post_close_state_before
        if self._post_close_audit_deal_id is not None and state_before is not None:
            self.state = state_before
        self._end_confirmed_close_state_transaction()

    def _commit_confirmed_close_state_transaction(self) -> None:
        if (
            self._post_close_audit_deal_id is None
            or self._post_close_state_before is None
        ):
            raise RuntimeError("confirmed close state transaction is not active")
        self._post_close_commit_in_progress = True
        # Keep this marker set until the transaction context is cleared.  If
        # persistence raises after its atomic replace became visible, the
        # step-level exception handler can distinguish that ambiguous commit
        # from an ordinary derived-state failure and compare the exact bytes.
        self._save_state()

    def _begin_confirmed_close_state_transaction(self, deal_id: int) -> dict[str, Any]:
        if isinstance(deal_id, bool) or not isinstance(deal_id, int) or deal_id <= 0:
            raise RuntimeError("confirmed close transition deal identity is invalid")
        self._end_confirmed_close_state_transaction()
        trade_keys: set[tuple[str, ...]] = set()
        validate_csv_schema(
            TRADE_LOG_FILE,
            TRADE_FIELDS,
            collect_post_close_keys=trade_keys,
        )
        evaluation_keys: set[tuple[str, ...]] = set()
        if self._signal_evaluation_enabled:
            evaluation_path = os.path.join(
                os.path.dirname(TRADE_LOG_FILE), "s23_signal_evaluation.csv",
            )
            try:
                validate_csv_schema(
                    evaluation_path,
                    SIGNAL_EVALUATION_FIELDS,
                    collect_post_close_keys=evaluation_keys,
                )
            except (OSError, UnicodeError, csv.Error, RuntimeError) as exc:
                self._signal_evaluation_enabled = False
                evaluation_keys.clear()
                logging.error(
                    "S23 passive signal evaluation disabled during post-close replay scan: %s",
                    exc,
                )
        state_before = copy.deepcopy(self.state)
        self._post_close_state_before = state_before
        self._post_close_audit_deal_id = deal_id
        self._post_close_trade_keys.update(trade_keys)
        self._post_close_evaluation_keys.update(evaluation_keys)
        return state_before

    def _confirmed_close_commit_is_visible(self) -> bool:
        """Return whether the complete in-memory transaction is already on disk."""
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                durable_state = strict_json_load(handle)
        except (OSError, UnicodeError, ValueError, TypeError):
            return False
        return durable_state == self.state

    def _confirmed_close_state_step(
        self,
        state_before_consumption: dict[str, Any],
        action: Callable[[], Any],
        *,
        final_commit: bool = False,
    ) -> Any:
        """Run one post-close state step and restore retryable state on error."""
        try:
            result = action()
            if final_commit:
                self._end_confirmed_close_state_transaction()
            return result
        except BaseException:
            # The standalone runner normally terminates on KeyboardInterrupt or
            # SystemExit, but embedding/service wrappers may catch either one.
            # A final atomic replace can already be visible even when its
            # parent-directory fsync or immediate cleanup is interrupted.  In
            # that case keep memory converged with the complete durable state;
            # otherwise restore the retryable pre-consumption snapshot.
            # ``final_commit`` is stack-local and therefore survives a partial
            # cleanup that has already cleared process-local transaction flags.
            # Relying only on ``_post_close_commit_in_progress`` would reopen a
            # narrow split-brain window between durable replace and cleanup
            # return when an asynchronous BaseException lands there.
            commit_is_visible = bool(
                final_commit and self._confirmed_close_commit_is_visible()
            )
            if not commit_is_visible:
                self.state = state_before_consumption
            self._end_confirmed_close_state_transaction()
            raise

    def _st(self, strat: dict[str, Any]) -> dict[str, Any]:
        return self.state["strategies"][strat["id"]]

    @staticmethod
    def _basket_rows_from_state(st: dict[str, Any]) -> list[dict[str, Any]]:
        basket = st.get("basket")
        return basket if isinstance(basket, list) else []

    def _basket_rows(self, strat: dict[str, Any]) -> list[dict[str, Any]]:
        return self._basket_rows_from_state(self._st(strat))

    def _roll_daily_realized(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp | None = None) -> dict[str, Any]:
        st = self._st(strat)
        stamp = pd.Timestamp(at_utc if at_utc is not None else utc_now())
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        day = stamp.strftime("%Y-%m-%d")
        stored_day = st.get("daily_realized_date_utc")
        if stored_day is None:
            st["daily_realized_date_utc"] = day
            st["daily_realized_pnl_usd"] = 0.0
            return st
        try:
            stored_date = datetime.strptime(
                str(stored_day), "%Y-%m-%d"
            ).date()
        except (TypeError, ValueError):
            # Preserve malformed durable evidence.  Callers will block new
            # entries rather than silently replacing an unknown loss state.
            return st
        # Broker/event clocks may surface an older lifecycle after restart.
        # Daily risk state advances only; an older observation cannot erase a
        # newer day's accumulated loss.
        if stored_date < stamp.date():
            st["daily_realized_date_utc"] = day
            st["daily_realized_pnl_usd"] = 0.0
        return st

    def _record_daily_realized(self, strat: dict[str, Any], pnl: float, at_utc: datetime | pd.Timestamp | None = None) -> None:
        stamp = pd.Timestamp(at_utc if at_utc is not None else utc_now())
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        st = self._st(strat)
        stored_day = st.get("daily_realized_date_utc")
        try:
            stored_date = datetime.strptime(str(stored_day), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            stored_date = None
        # CLOSEDEAL can confirm an older lifecycle after a restart.  Its PnL
        # remains in the immutable close ledger, but it must not replace a
        # newer UTC day's loss-limit accumulator.
        if stored_date is not None and stored_date > stamp.date():
            return
        st = self._roll_daily_realized(strat, stamp)
        raw_current_pnl = st.get("daily_realized_pnl_usd", 0.0)
        if not isinstance(raw_current_pnl, (int, float)) or isinstance(raw_current_pnl, bool):
            return
        try:
            current_pnl = float(raw_current_pnl)
            realized_pnl = float(pnl)
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(current_pnl) or not math.isfinite(realized_pnl):
            return
        st["daily_realized_pnl_usd"] = current_pnl + realized_pnl

    def _new_basket_block_reason(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp) -> str | None:
        stamp = pd.Timestamp(at_utc)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        if int(stamp.hour) in {int(hour) for hour in self.params.get("new_basket_blocked_hours_utc", [])}:
            return "new_basket_blocked_hour"
        st = self._roll_daily_realized(strat, stamp)
        raw_daily_realized = st.get("daily_realized_pnl_usd", 0.0)
        if not isinstance(raw_daily_realized, (int, float)) or isinstance(raw_daily_realized, bool):
            return "daily_realized_state_invalid"
        try:
            stored_date = datetime.strptime(
                str(st.get("daily_realized_date_utc")), "%Y-%m-%d"
            ).date()
            daily_realized = float(raw_daily_realized)
        except (TypeError, ValueError, OverflowError):
            return "daily_realized_state_invalid"
        if not math.isfinite(daily_realized):
            return "daily_realized_state_invalid"
        limit = float(self.params.get("daily_realized_loss_limit_usd", 0.0))
        if stored_date > stamp.date():
            return (
                "daily_realized_loss_limit"
                if limit > 0.0 and daily_realized <= -limit
                else "daily_realized_state_invalid"
            )
        return "daily_realized_loss_limit" if limit > 0.0 and daily_realized <= -limit else None

    @staticmethod
    def _alert_signature(reason: str, details: dict[str, Any]) -> str:
        encoded = json.dumps({"reason": reason, "details": details}, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _notify_manual_action(self, strat: dict[str, Any], *, title: str, reason: str, action: str, key: str) -> bool:
        if self._suppress_manual_alerts:
            return True
        return bool(notify_manual_action_required(bot_id="bot23", symbol=str(self.params.get("mt5_symbol", self.params["symbol"])), title=title, reason=reason, action=action, key=key))

    def _notify_reconciliation_required(self, strat: dict[str, Any], reason: str, details: dict[str, Any]) -> None:
        st = self._st(strat)
        signature = self._alert_signature(reason, details)
        if st.get("manual_alert_last_signature") == signature:
            return
        delivered = self._notify_manual_action(strat, title="reconciliation_required", reason=f"{reason}; details={json.dumps(details, ensure_ascii=True, sort_keys=True, default=str)}", action="Inspect bot23-owned MT5 inventory and state before clearing the block.", key=f"bot23:reconciliation:{strat['id']}:{reason}")
        if delivered:
            st["manual_alert_last_signature"] = signature
            st["manual_alert_last_reason"] = reason
            st["manual_alert_last_at_utc"] = dt_text(utc_now())

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
        if (
            self._post_close_audit_deal_id is not None
            and event != "position_close_confirmed"
        ):
            raw_deal_id = row.get("deal_id")
            if raw_deal_id not in (None, ""):
                try:
                    supplied_deal_id = int(raw_deal_id)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError(
                        "derived post-close audit deal identity is malformed"
                    ) from exc
                if supplied_deal_id != self._post_close_audit_deal_id:
                    raise RuntimeError(
                        "derived post-close audit deal identity conflicts with transaction"
                    )
            row["deal_id"] = self._post_close_audit_deal_id
            row["_post_close_transition"] = True
        lane_id = int(strat["lane_id"])
        reason = str(row.get("reason") or "")
        coalesce = event == "entry_skip" and (
            reason in REPEATABLE_DIAGNOSTIC_REASONS or str(row.get("note") or "") == "sync_block"
        )
        active = self._diagnostic_repeats.get(lane_id)
        signature = (event, reason, str(row.get("note") or ""))
        if not coalesce:
            self._flush_diagnostic_repeat(lane_id, now)
            self._append_trade_audit_row(row)
            return
        if active is None or active["signature"] != signature:
            self._flush_diagnostic_repeat(lane_id, now)
            row["repeat_count"] = 1
            row["repeat_window_seconds"] = 0
            self._append_trade_audit_row(row)
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
            self._append_trade_audit_row(row)
        if keep_signature:
            active["first"] = at
            active["last"] = at
            active["suppressed"] = 0
        else:
            self._diagnostic_repeats.pop(int(lane_id), None)

    @staticmethod
    def _strategy_group(strat: dict[str, Any]) -> str:
        lane_id = int(strat["lane_id"])
        if 1 <= lane_id <= 4:
            return "za"
        if 5 <= lane_id <= 7:
            return "jst0911_morning"
        if lane_id == 8:
            return "jst1113_midday"
        if 9 <= lane_id <= 11:
            return "jst1300_pre_eu30"
        if lane_id == 12:
            return "trend_recovery"
        if 13 <= lane_id <= 17:
            return "session_vwap"
        if 18 <= lane_id <= 21:
            return "t0530_edge"
        if lane_id == 22:
            return "q01_variance_release"
        return "unknown"

    def _signal_attribution(
        self,
        strat: dict[str, Any],
        opportunity_id: Any,
        side: Any,
    ) -> dict[str, str]:
        group = self._strategy_group(strat)
        configured_signal_id = str(strat.get("signal_id") or "")
        variant = configured_signal_id
        transform = "none"
        raw_side = str(side or "").upper()
        effective_side = raw_side
        identity = str(opportunity_id or "")
        parts = identity.split("|") if identity else []
        if group == "za":
            configured_signal_id = configured_signal_id or "za_horizontal_impulse"
            # A legacy/adopted position can legitimately lack an opportunity
            # identity.  Never count its eventual PnL as the primary ZA signal:
            # that would manufacture variant-level performance evidence after
            # a restart or migration.  New ZA entries always carry a five-part
            # identity and therefore remain independently attributable.
            variant = "za_unattributed_legacy"
            transform = "unknown"
            if len(parts) == 5 and parts[2] == "INVENTORY_RANGE_FADE":
                variant = "za_inventory_range_false_break_fade"
                transform = "opposite_breakout"
                raw_side = ""
                effective_side = parts[3].upper()
            elif len(parts) == 5 and parts[2] in {"LONG", "SHORT"}:
                variant = "za_horizontal_primary"
                transform = "none"
                raw_side = parts[2]
                effective_side = parts[3].upper()
                if raw_side == "SHORT" and effective_side == "LONG":
                    variant = "za_late_short_reverse_long"
                    transform = "reverse_long"
        return {
            "strategy_group": group,
            "configured_signal_id": configured_signal_id,
            "signal_variant_id": variant,
            "signal_transform_id": transform,
            "raw_side": raw_side,
            "effective_side": effective_side,
        }

    def _append_signal_evaluation_row(self, trade_row: dict[str, Any]) -> None:
        if not self._signal_evaluation_enabled:
            return
        allocations = trade_row.get("_evaluation_allocations")
        if allocations is not None:
            if not isinstance(allocations, list) or not allocations:
                logging.error(
                    "S23 passive signal evaluation allocations invalid: %r",
                    allocations,
                )
                return
            for allocation in allocations:
                if not isinstance(allocation, dict):
                    logging.error(
                        "S23 passive signal evaluation allocation invalid: %r",
                        allocation,
                    )
                    continue
                allocated_row = dict(trade_row)
                allocated_row.pop("_evaluation_allocations", None)
                allocated_row.update(allocation)
                allocated_row["event"] = str(
                    allocation.get("event") or "position_close_attributed"
                )
                self._append_signal_evaluation_row(allocated_row)
            return
        strat = next(
            (
                row for row in self._all_strategies()
                if int(row["lane_id"]) == int(trade_row["lane_id"])
            ),
            None,
        )
        if strat is None:
            logging.error(
                "S23 signal evaluation row skipped: unknown lane_id=%s",
                trade_row.get("lane_id"),
            )
            return
        opportunity_id = trade_row.get("opportunity_id")
        side = trade_row.get("side")
        basket_attributions: list[dict[str, str]] = []
        if not opportunity_id:
            basket = self._basket_rows(strat)
            basket_opportunities = {
                str(position.get("opportunity_id") or "")
                for position in basket
                if str(position.get("opportunity_id") or "")
            }
            basket_sides = {
                str(position.get("side") or "").upper()
                for position in basket
                if str(position.get("side") or "").upper() in {"LONG", "SHORT"}
            }
            if len(basket_opportunities) == 1:
                opportunity_id = next(iter(basket_opportunities))
            elif len(basket_opportunities) > 1:
                basket_attributions = [
                    self._signal_attribution(strat, identity, side)
                    for identity in sorted(basket_opportunities)
                ]
            if not side and len(basket_sides) == 1:
                side = next(iter(basket_sides))
        attribution = self._signal_attribution(strat, opportunity_id, side)
        if basket_attributions:
            variants = {
                item["signal_variant_id"] for item in basket_attributions
            }
            transforms = {
                item["signal_transform_id"] for item in basket_attributions
            }
            raw_sides = {item["raw_side"] for item in basket_attributions}
            effective_sides = {
                item["effective_side"] for item in basket_attributions
            }
            attribution["signal_variant_id"] = (
                next(iter(variants)) if len(variants) == 1 else "mixed"
            )
            attribution["signal_transform_id"] = (
                next(iter(transforms)) if len(transforms) == 1 else "mixed"
            )
            attribution["raw_side"] = (
                next(iter(raw_sides)) if len(raw_sides) == 1 else "MIXED"
            )
            attribution["effective_side"] = (
                next(iter(effective_sides))
                if len(effective_sides) == 1 else "MIXED"
            )
        row = {
            "timestamp_utc": trade_row.get("timestamp_utc"),
            "event": trade_row.get("event"),
            **attribution,
            "strategy_id": strat["id"],
            "lane_id": int(strat["lane_id"]),
            "magic": int(strat["magic"]),
            "spec_id": str(strat.get("spec_id") or ""),
            "opportunity_id": opportunity_id,
            "basket_id": trade_row.get("basket_id"),
            "ticket": trade_row.get("ticket"),
            "position_identifier": trade_row.get("position_identifier"),
            "deal_id": trade_row.get("deal_id"),
            "profit": trade_row.get("profit"),
            "reason": trade_row.get("reason"),
            "signal_bar_time": trade_row.get("signal_bar_time"),
            "event_time": trade_row.get("event_time"),
            "release_time": trade_row.get("release_time"),
            "available_time": trade_row.get("available_time"),
            "decision_time": trade_row.get("decision_time"),
            "executable_at": trade_row.get("executable_at"),
            "live": trade_row.get("live"),
        }
        is_post_close_transition = (
            trade_row.get("_post_close_transition") is True
        )
        transition_key = (
            post_close_audit_key(row, SIGNAL_EVALUATION_FIELDS)
            if is_post_close_transition
            else None
        )
        try:
            evaluation_path = os.path.join(
                os.path.dirname(TRADE_LOG_FILE), "s23_signal_evaluation.csv",
            )
            if (
                transition_key is not None
                and transition_key in self._post_close_evaluation_keys
            ):
                return
            if not confirmed_close_audit_exists(
                evaluation_path, row, SIGNAL_EVALUATION_FIELDS,
            ):
                append_signal_evaluation_csv(
                    evaluation_path, row, SIGNAL_EVALUATION_FIELDS,
                )
                if transition_key is not None:
                    self._post_close_evaluation_keys.add(transition_key)
        except Exception as exc:
            # Passive evidence must be visible when broken, but may not block
            # an owned-position close or interrupt durable lifecycle handling.
            self._signal_evaluation_enabled = False
            logging.error("S23 passive signal evaluation write failed: %s", exc)

    def _append_trade_audit_row(self, row: dict[str, Any]) -> None:
        is_post_close_transition = row.get("_post_close_transition") is True
        transition_key = (
            post_close_audit_key(row, TRADE_FIELDS)
            if is_post_close_transition
            else None
        )
        if (
            transition_key is not None
            and transition_key in self._post_close_trade_keys
        ):
            pass
        elif not confirmed_close_audit_exists(TRADE_LOG_FILE, row, TRADE_FIELDS):
            append_csv(TRADE_LOG_FILE, row, TRADE_FIELDS)
            if transition_key is not None:
                self._post_close_trade_keys.add(transition_key)
        try:
            self._append_signal_evaluation_row(row)
        except Exception as exc:
            # Attribution is passive evidence.  Contain not only filesystem
            # failures inside its writer but also any unexpected construction
            # or normalization defect, so it cannot interrupt an owned close
            # after the operational trade row has already been persisted.
            self._signal_evaluation_enabled = False
            logging.error(
                "S23 passive signal evaluation processing failed: %s", exc,
            )

    def _set_sync_block(
        self,
        strat: dict[str, Any],
        reason: str | None,
        details: dict[str, Any] | None = None,
        *,
        recoverable: bool = False,
    ) -> None:
        st = self._st(strat)
        invalid_existing_contract = not self._sync_block_contract_valid(st)
        if invalid_existing_contract:
            invalid_details = {
                "previous_block": repr(st.get("sync_block_new_entries")),
                "previous_reason": repr(st.get("sync_block_reason")),
                "previous_recoverable": repr(st.get("sync_block_recoverable")),
                "previous_details_type": type(st.get("sync_block_details")).__name__,
            }
            st["sync_block_new_entries"] = True
            st["sync_block_reason"] = "sync_block_state_invalid"
            st["sync_block_recoverable"] = False
            st["sync_block_details"] = invalid_details
            st["flat_clear_confirmation_count"] = 0
            st["flat_clear_confirmation_reason"] = None
            self._trade_row(
                "position_lifecycle_recovered",
                strat,
                reason="sync_block_state_invalid",
                note=json.dumps(invalid_details, ensure_ascii=True, sort_keys=True),
            )
            if reason is None or recoverable:
                self._notify_reconciliation_required(
                    strat, "sync_block_state_invalid", invalid_details,
                )
                return
        previous = st.get("sync_block_reason")
        if reason:
            if recoverable and st.get("sync_block_new_entries") and not st.get("sync_block_recoverable"):
                # A failed broker query breaks a consecutive flat-evidence
                # streak even when the stronger non-recoverable reason is
                # retained as the visible block.
                st["flat_clear_confirmation_count"] = 0
                st["flat_clear_confirmation_reason"] = None
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
    def _sync_block_contract_valid(st: dict[str, Any]) -> bool:
        blocked = st.get("sync_block_new_entries")
        reason = st.get("sync_block_reason")
        recoverable = st.get("sync_block_recoverable")
        details = st.get("sync_block_details")
        return bool(
            isinstance(blocked, bool)
            and isinstance(recoverable, bool)
            and isinstance(details, dict)
            and (
                (blocked and isinstance(reason, str) and bool(reason.strip()))
                or (not blocked and reason is None and not recoverable)
            )
        )

    @staticmethod
    def _side_from_record(record: Any) -> str:
        return "LONG" if int(getattr(record, "type", -1)) == ORDER_TYPE_BUY else "SHORT"

    @staticmethod
    def _live_position_identity(record: Any) -> tuple[int, int] | None:
        ticket = getattr(record, "ticket", None)
        position_id = getattr(record, "identifier", None)
        if (
            isinstance(ticket, bool)
            or not isinstance(ticket, int)
            or ticket <= 0
            or isinstance(position_id, bool)
            or not isinstance(position_id, int)
            or position_id <= 0
        ):
            return None
        return ticket, position_id

    def _owned_position(self, strat: dict[str, Any], record: Any) -> bool:
        return (
            str(getattr(record, "symbol", "")) == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and int(getattr(record, "magic", -1)) == int(strat["magic"])
            and str(getattr(record, "comment", "") or "").startswith(str(strat["comment_prefix"]))
        )

    def _state_matches_live(self, strat: dict[str, Any], state_pos: dict[str, Any], live_pos: Any) -> bool:
        try:
            raw_state_ticket = state_pos.get("ticket")
            raw_position_id = state_pos.get("position_identifier")
            if (
                isinstance(raw_state_ticket, bool)
                or not isinstance(raw_state_ticket, int)
                or isinstance(raw_position_id, bool)
                or not isinstance(raw_position_id, int)
            ):
                return False
            state_ticket = raw_state_ticket
            live_identity = self._live_position_identity(live_pos)
            if live_identity is None:
                return False
            live_ticket, live_position_id = live_identity
            position_id = raw_position_id
            raw_state_lot = state_pos.get("lot")
            if (
                isinstance(raw_state_lot, bool)
                or not isinstance(raw_state_lot, (int, float))
            ):
                return False
            state_lot = float(raw_state_lot)
            live_lot = float(getattr(live_pos, "volume", 0.0))
        except (TypeError, ValueError, OverflowError):
            return False
        return (
            state_ticket > 0
            and state_ticket == live_ticket
            and position_id > 0
            and position_id == live_position_id
            and str(state_pos.get("side")) == self._side_from_record(live_pos)
            and math.isfinite(state_lot)
            and math.isfinite(live_lot)
            and math.isclose(state_lot, live_lot, rel_tol=0.0, abs_tol=1e-9)
            and self._state_ownership_proven(strat, state_pos)
            and self._owned_position(strat, live_pos)
        )

    @staticmethod
    def _position_close_intent_valid(state_pos: dict[str, Any]) -> bool:
        close_requested = state_pos.get("close_requested", False)
        pending_reason = state_pos.get("pending_close_reason")
        pending_signal_bar = state_pos.get("pending_close_signal_bar")
        submission_started = state_pos.get("close_submission_started_utc")
        return (
            isinstance(close_requested, bool)
            and (
                pending_reason is None
                or (
                    isinstance(pending_reason, str)
                    and bool(pending_reason.strip())
                )
            )
            and (
                pending_signal_bar is None
                or (
                    isinstance(pending_signal_bar, str)
                    and parse_ts(pending_signal_bar) is not None
                )
            )
            and ((pending_reason is None) == (pending_signal_bar is None))
            and (
                submission_started is None
                or (
                    isinstance(submission_started, str)
                    and parse_ts(submission_started) is not None
                )
            )
        )

    @staticmethod
    def _close_submission_unresolved(state_pos: dict[str, Any]) -> bool:
        return bool(
            state_pos.get("close_requested") is not True
            and isinstance(state_pos.get("close_submission_started_utc"), str)
            and parse_ts(state_pos.get("close_submission_started_utc")) is not None
        )

    @staticmethod
    def _close_result_definitive_no_fill(result: Any) -> bool:
        status = str(getattr(result, "status", "FAILED"))
        retcode = getattr(result, "retcode", None)
        return bool(
            status in {
                "ACCOUNT_IDENTITY_GUARD",
                "ACCOUNT_MODE_GUARD",
                "TRADE_PERMISSION_GUARD",
                "POSITION_OWNERSHIP_GUARD",
                "MARKET_CLOSED",
                "INVALID_REQUEST",
                "IPC_NOT_PUBLISHED",
            }
            or (
                status == "FAILED"
                and isinstance(retcode, int)
                and not isinstance(retcode, bool)
                and retcode in DEFINITIVE_CLOSE_NO_FILL_RETCODES
            )
        )

    def _record_close_trade_permission_reject(
        self,
        strat: dict[str, Any],
        result: Any,
        quote_time: datetime | pd.Timestamp | str | None,
    ) -> bool:
        status = str(getattr(result, "status", "FAILED"))
        retcode = getattr(result, "retcode", None)
        if not (
            status == "TRADE_PERMISSION_GUARD"
            or (
                isinstance(retcode, int)
                and not isinstance(retcode, bool)
                and retcode in CLOSE_TRADE_PERMISSION_RETCODES
            )
        ):
            return False
        st = self._st(strat)
        raw_streak = st.get("close_trade_permission_reject_streak")
        raw_notified = st.get("close_trade_permission_reject_notified")
        invalid_reset_note = None
        if (
            isinstance(raw_streak, bool)
            or not isinstance(raw_streak, int)
            or raw_streak < 0
            or not isinstance(raw_notified, bool)
        ):
            invalid_reset_note = f"streak={raw_streak!r};notified={raw_notified!r}"
            raw_streak = 0
            raw_notified = False
        st["close_trade_permission_reject_streak"] = int(raw_streak) + 1
        st["close_trade_permission_reject_notified"] = bool(raw_notified)
        stamp = parse_ts(quote_time)
        if stamp is not None:
            st["time_close_retry_after_utc"] = dt_text(
                stamp
                + pd.Timedelta(
                    seconds=float(
                        self.params.get("trade_permission_retry_seconds", 30.0)
                    )
                )
            )
        details = {
            "status": status,
            "retcode": retcode,
            "streak": st["close_trade_permission_reject_streak"],
        }
        self._set_sync_block(
            strat,
            "close_trade_permission_rejected",
            details,
            recoverable=True,
        )
        self._save_state()
        threshold = int(self.params.get("trade_permission_alert_threshold", 3))
        if (
            st["close_trade_permission_reject_streak"] >= threshold
            and not st["close_trade_permission_reject_notified"]
        ):
            delivered = self._notify_manual_action(
                strat,
                title="close trade permission rejected repeatedly",
                reason=f"status={status}; retcode={retcode}",
                action="Check MT5 AutoTrading and account trade permissions; bot23 still owns an open position awaiting CLOSE.",
                key=f"bot23:close-trade-permission:{strat['id']}",
            )
            if delivered:
                st["close_trade_permission_reject_notified"] = True
                self._save_state()
        if invalid_reset_note is not None:
            self._trade_row(
                "position_lifecycle_recovered",
                strat,
                reason="close_trade_permission_state_invalid_reset",
                note=invalid_reset_note,
            )
        self._trade_row(
            "position_close_deferred",
            strat,
            reason="close_trade_permission_rejected",
            note=(
                f"status={status};retcode={retcode};"
                f"streak={st['close_trade_permission_reject_streak']};"
                f"retry_after={st.get('time_close_retry_after_utc')}"
            ),
        )
        return True

    def _clear_trade_permission_reject_state(self, strat: dict[str, Any]) -> None:
        st = self._st(strat)
        st["close_trade_permission_reject_streak"] = 0
        st["close_trade_permission_reject_notified"] = False
        st["time_close_retry_after_utc"] = None

    @staticmethod
    def _basket_close_intent_valid(state: dict[str, Any]) -> bool:
        pending_reason = state.get("pending_close_reason")
        pending_signal_bar = state.get("pending_close_signal_bar")
        basket = state.get("basket")
        has_unbound_requested_close = bool(
            isinstance(basket, list)
            and any(
                isinstance(position, dict)
                and position.get("close_requested") is True
                and pending_reason is None
                and position.get("pending_close_reason") is None
                for position in basket
            )
        )
        has_unbound_submission = bool(
            isinstance(basket, list)
            and any(
                isinstance(position, dict)
                and isinstance(position.get("close_submission_started_utc"), str)
                and parse_ts(position.get("close_submission_started_utc")) is not None
                and pending_reason is None
                and position.get("pending_close_reason") is None
                for position in basket
            )
        )
        return (
            (
                pending_reason is None
                or (
                    isinstance(pending_reason, str)
                    and bool(pending_reason.strip())
                )
            )
            and (
                pending_signal_bar is None
                or (
                    isinstance(pending_signal_bar, str)
                    and parse_ts(pending_signal_bar) is not None
                )
            )
            and ((pending_reason is None) == (pending_signal_bar is None))
            and not has_unbound_requested_close
            and not has_unbound_submission
        )

    def _state_ownership_proven(self, strat: dict[str, Any], state_pos: dict[str, Any]) -> bool:
        raw_position_id = state_pos.get("position_identifier")
        raw_lane_id = state_pos.get("lane_id")
        raw_basket_id = state_pos.get("basket_id")
        raw_owner_symbol = state_pos.get("owner_symbol")
        raw_owner_magic = state_pos.get("owner_magic")
        raw_owner_comment = state_pos.get("owner_comment")
        raw_side = state_pos.get("side")
        current_basket_id = self._st(strat).get("current_basket_id")
        return bool(
            isinstance(raw_position_id, int)
            and not isinstance(raw_position_id, bool)
            and raw_position_id > 0
            and isinstance(raw_lane_id, int)
            and not isinstance(raw_lane_id, bool)
            and raw_lane_id == int(strat["lane_id"])
            and isinstance(raw_basket_id, str)
            and bool(raw_basket_id.strip())
            and isinstance(current_basket_id, str)
            and bool(current_basket_id.strip())
            and raw_basket_id == current_basket_id
            and isinstance(raw_owner_symbol, str)
            and raw_owner_symbol
            == str(self.params.get("mt5_symbol", self.params["symbol"]))
            and isinstance(raw_owner_magic, int)
            and not isinstance(raw_owner_magic, bool)
            and raw_owner_magic == int(strat["magic"])
            and isinstance(raw_owner_comment, str)
            and raw_owner_comment.startswith(str(strat["comment_prefix"]))
            and isinstance(raw_side, str)
            and raw_side in {"LONG", "SHORT"}
        )

    def _clear_basket_state(
        self,
        strat: dict[str, Any],
        reason: str,
        signal_bar: str | None = None,
        *,
        closed_at_utc: datetime | pd.Timestamp | str | None = None,
        closed_side: str | None = None,
    ) -> None:
        st = self._st(strat)
        closed_sides = {
            str(pos.get("side") or "").upper()
            for pos in self._basket_rows(strat)
            if str(pos.get("side") or "").upper() in {"LONG", "SHORT"}
        }
        explicit_closed_side = str(closed_side or "").upper()
        if explicit_closed_side in {"LONG", "SHORT"}:
            closed_sides.add(explicit_closed_side)
        st["basket"] = []
        st["last_add_price"] = None
        st["basket_peak_pnl_usd"] = None
        st["frozen_basket_atr30"] = None
        st["reverse_used"] = False
        st["pending_close_reason"] = None
        st["pending_close_signal_bar"] = None
        st["time_close_defer_started_utc"] = None
        st["time_close_last_quote_msc"] = None
        st["time_close_stable_count"] = 0
        st["time_close_retry_after_utc"] = None
        st["close_trade_permission_reject_streak"] = 0
        st["close_trade_permission_reject_notified"] = False
        st["time_close_wide_seen"] = False
        st["q01_last_quote_msc"] = None
        st["current_basket_id"] = None
        st["cooldown_until_bar"] = -1
        closed_bar = parse_ts(signal_bar)
        confirmed_close = parse_ts(closed_at_utc)
        cooldown_anchor = confirmed_close if confirmed_close is not None else closed_bar
        st["cooldown_until_utc"] = (
            dt_text(cooldown_anchor + pd.Timedelta(minutes=int(strat.get("cooldown", 0))))
            if cooldown_anchor is not None
            else None
        )
        st["last_closed_at_utc"] = dt_text(confirmed_close if confirmed_close is not None else utc_now())
        st["last_closed_reason"] = reason
        st["last_closed_signal_bar"] = signal_bar
        st["last_closed_side"] = next(iter(closed_sides)) if len(closed_sides) == 1 else None

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
        basket = list(self._basket_rows(strat))
        return bool(basket) and all(str(pos.get("side") or "") == "LONG" for pos in basket)

    def _validated_reverse_used(self, strat: dict[str, Any]) -> bool:
        st = self._st(strat)
        raw_reverse_used = st.get("reverse_used")
        if isinstance(raw_reverse_used, bool):
            return raw_reverse_used
        st["reverse_used"] = False
        self._trade_row(
            "position_lifecycle_recovered",
            strat,
            reason="malformed_reverse_flag_disabled",
            note=f"previous_reverse_used={raw_reverse_used!r}",
        )
        return False

    def _pending_long_target_close_requests(
        self,
        *,
        exclude_lane_id: int | None = None,
        exclude_basket_id: str | None = None,
    ) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        for candidate in self.params["strategies"]:
            lane_id = int(candidate["lane_id"])
            st = self._st(candidate)
            basket_id = st.get("current_basket_id")
            if lane_id == exclude_lane_id and basket_id == exclude_basket_id:
                continue
            if st.get("pending_close_reason") != "basket_target":
                continue
            basket = list(self._basket_rows(candidate))
            basket_sides = {str(pos.get("side") or "") for pos in basket}
            if basket and basket_sides == {"SHORT"}:
                # Portfolio rearm is a LONG-target-only contract.  A normal
                # SHORT target close must not become a malformed LONG rearm
                # request while its broker confirmation is outstanding.
                continue
            signal_bar = st.get("pending_close_signal_bar")
            request_time = parse_ts(signal_bar) if isinstance(signal_bar, str) else None
            requests.append({
                "lane_id": lane_id,
                "basket_id": basket_id,
                "request_time": request_time,
                "valid": (
                    isinstance(basket_id, str)
                    and bool(basket_id.strip())
                    and bool(basket)
                    and all(str(pos.get("side") or "") == "LONG" for pos in basket)
                    and request_time is not None
                ),
            })
        return requests

    def _refresh_long_target_rearm_pending_summary(
        self,
        *,
        exclude_lane_id: int | None = None,
        exclude_basket_id: str | None = None,
    ) -> None:
        routing = self.state["routing"]
        requests = self._pending_long_target_close_requests(
            exclude_lane_id=exclude_lane_id,
            exclude_basket_id=exclude_basket_id,
        )
        routing["long_target_rearm_pending_confirmation"] = bool(requests)
        valid_times = [row["request_time"] for row in requests if row["valid"]]
        routing["long_target_rearm_request_utc"] = (
            dt_text(min(valid_times)) if len(valid_times) == len(requests) and requests else None
        )
        if routing.get("long_target_rearm_until_utc") is not None:
            return
        if requests:
            first = min(requests, key=lambda row: (row["lane_id"], str(row["basket_id"])))
            routing["long_target_rearm_trigger_lane_id"] = first["lane_id"] if first["valid"] else None
            routing["long_target_rearm_trigger_basket_id"] = first["basket_id"] if first["valid"] else None
            return
        routing["long_target_rearm_confirmed_utc"] = None
        routing["long_target_rearm_trigger_lane_id"] = None
        routing["long_target_rearm_trigger_basket_id"] = None

    def _arm_long_target_portfolio_rearm(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp) -> None:
        routing = self.state["routing"]
        stamp = pd.Timestamp(at_utc)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        routing["long_target_rearm_pending_confirmation"] = True
        previous_request = parse_ts(routing.get("long_target_rearm_request_utc"))
        routing["long_target_rearm_request_utc"] = dt_text(
            min(stamp, previous_request) if previous_request is not None else stamp
        )
        if routing.get("long_target_rearm_until_utc") is None:
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
        raw_existing_until = routing.get("long_target_rearm_until_utc")
        existing_until = (
            parse_ts(raw_existing_until) if isinstance(raw_existing_until, str) else None
        )
        raw_existing_confirmed = routing.get("long_target_rearm_confirmed_utc")
        existing_confirmed = (
            parse_ts(raw_existing_confirmed)
            if isinstance(raw_existing_confirmed, str)
            else None
        )
        existing_lane = routing.get("long_target_rearm_trigger_lane_id")
        existing_basket = routing.get("long_target_rearm_trigger_basket_id")
        existing_active_valid = (
            raw_existing_until is not None
            and existing_until is not None
            and existing_confirmed is not None
            and existing_until == existing_confirmed + pd.Timedelta(minutes=minutes)
            and isinstance(existing_lane, int)
            and not isinstance(existing_lane, bool)
            and existing_lane in {int(row["lane_id"]) for row in self.params["strategies"]}
            and isinstance(existing_basket, str)
            and bool(existing_basket.strip())
        )
        replace_active = raw_existing_until is None or (
            existing_active_valid and existing_until < until
        )
        if replace_active:
            routing["long_target_rearm_confirmed_utc"] = dt_text(stamp)
            routing["long_target_rearm_until_utc"] = dt_text(until)
            routing["long_target_rearm_trigger_lane_id"] = int(strat["lane_id"])
            routing["long_target_rearm_trigger_basket_id"] = basket_id
        routing["long_target_rearm_expired_utc"] = None
        self._refresh_long_target_rearm_pending_summary(
            exclude_lane_id=int(strat["lane_id"]),
            exclude_basket_id=basket_id,
        )
        cancelled = self._cancel_portfolio_pending_longs(strat, "long_target_rearm_started")
        self._trade_row(
            "portfolio_rearm_started",
            strat,
            side="LONG",
            reason="basket_target_confirmed",
            executable_at=dt_text(stamp),
            note=(
                f"until={routing.get('long_target_rearm_until_utc')};"
                f"active_replaced={replace_active};pending_long_cancelled={cancelled}"
            ),
        )

    def _cancel_unconfirmed_long_target_rearm_after_other_close(
        self,
        strat: dict[str, Any],
        basket_id: str | None,
        close_reason: str,
    ) -> None:
        """Remove only the stale pending trigger owned by this closed basket."""
        if close_reason == "basket_target":
            return
        routing = self.state["routing"]
        if routing.get("long_target_rearm_pending_confirmation") is not True:
            return
        self._refresh_long_target_rearm_pending_summary(
            exclude_lane_id=int(strat["lane_id"]),
            exclude_basket_id=basket_id,
        )
        if routing.get("long_target_rearm_pending_confirmation") is True:
            return
        self._trade_row(
            "portfolio_rearm_cancelled",
            strat,
            side="LONG",
            reason="trigger_basket_closed_without_target_confirmation",
            note=f"basket_id={basket_id};close_reason={close_reason}",
        )

    def _portfolio_new_long_basket_block_reason(
        self,
        side: str,
        at_utc: datetime | pd.Timestamp | None,
    ) -> str | None:
        if side != "LONG" or not bool(self.params.get("long_target_portfolio_rearm_enabled", False)):
            return None
        routing = self.state["routing"]
        pending_confirmation = routing.get("long_target_rearm_pending_confirmation")
        if not isinstance(pending_confirmation, bool):
            return "long_target_rearm_state_invalid"
        stamp = pd.Timestamp(at_utc if at_utc is not None else utc_now())
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        pending_requests = self._pending_long_target_close_requests()
        if pending_confirmation != bool(pending_requests):
            return "long_target_rearm_state_invalid"
        trigger_lane = routing.get("long_target_rearm_trigger_lane_id")
        trigger_basket = routing.get("long_target_rearm_trigger_basket_id")
        valid_trigger = (
            isinstance(trigger_lane, int)
            and not isinstance(trigger_lane, bool)
            and trigger_lane in {int(row["lane_id"]) for row in self.params["strategies"]}
            and isinstance(trigger_basket, str)
            and bool(trigger_basket.strip())
        )
        if pending_confirmation:
            raw_request = routing.get("long_target_rearm_request_utc")
            request = parse_ts(raw_request) if isinstance(raw_request, str) else None
            if (
                request is None
                or request > stamp
                or not all(row["valid"] for row in pending_requests)
            ):
                return "long_target_rearm_state_invalid"
            return "long_target_rearm_pending_close_confirmation"
        raw_until = routing.get("long_target_rearm_until_utc")
        if raw_until is None:
            return None
        until = parse_ts(raw_until) if isinstance(raw_until, str) else None
        if until is None:
            return "long_target_rearm_state_invalid"
        raw_confirmed = routing.get("long_target_rearm_confirmed_utc")
        confirmed = parse_ts(raw_confirmed) if isinstance(raw_confirmed, str) else None
        try:
            rearm_minutes = int(self.params.get("long_target_portfolio_rearm_minutes"))
        except (TypeError, ValueError, OverflowError):
            rearm_minutes = -1
        if (
            confirmed is None
            or confirmed > stamp
            or rearm_minutes < 0
            or until != confirmed + pd.Timedelta(minutes=rearm_minutes)
            or not valid_trigger
        ):
            return "long_target_rearm_state_invalid"
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
            for position in self._basket_rows(strat):
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

    def _advance_inventory_range_fade(
        self,
        price_row: pd.Series,
        processing_time: datetime | pd.Timestamp | None = None,
    ) -> None:
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
        if processing_time is not None:
            processed_at = pd.Timestamp(processing_time)
            processed_at = processed_at.tz_localize("UTC") if processed_at.tzinfo is None else processed_at.tz_convert("UTC")
            if processed_at < bar_time + pd.Timedelta(minutes=1):
                return
        bar_text = dt_text(bar_time)
        routing = self.state["routing"]
        range_state = routing["inventory_range_fade"]
        raw_last_state_bar = range_state.get("last_state_bar")
        if raw_last_state_bar is not None and (
            not isinstance(raw_last_state_bar, str)
            or parse_ts(raw_last_state_bar) is None
        ):
            reset_state = dict(self._default_state()["routing"]["inventory_range_fade"])
            reset_state["last_state_bar"] = bar_text
            routing["inventory_range_fade"] = reset_state
            self._trade_row(
                "inventory_range_invalidated",
                self.params["strategies"][0],
                reason="malformed_persisted_range_state",
                signal_bar_time=bar_text,
                note=f"previous_last_state_bar={raw_last_state_bar!r};current_bar_consumed",
            )
            self._save_state()
            return
        previous_state_bar = (
            parse_ts(raw_last_state_bar)
            if isinstance(raw_last_state_bar, str)
            else None
        )
        if previous_state_bar is not None and previous_state_bar >= bar_time:
            if previous_state_bar > bar_time:
                self._trade_row(
                    "inventory_range_invalidated",
                    self.params["strategies"][0],
                    reason="decision_receipt_nonmonotonic",
                    signal_bar_time=bar_text,
                    note=f"high_watermark={raw_last_state_bar!r};preserved",
                )
            return
        range_state["last_state_bar"] = bar_text
        try:
            raw_phase = range_state.get("break_phase")
            raw_confirm_count = range_state.get("return_confirm_count")
            raw_active = range_state.get("active")
            raw_low = range_state.get("low")
            raw_high = range_state.get("high")
            phase = int(raw_phase)
            confirm_count = int(raw_confirm_count)
            pending_side = range_state.get("pending_side")
            low_is_number = isinstance(raw_low, (int, float)) and not isinstance(raw_low, bool)
            high_is_number = isinstance(raw_high, (int, float)) and not isinstance(raw_high, bool)
            frozen_low = float(raw_low) if low_is_number else math.nan
            frozen_high = float(raw_high) if high_is_number else math.nan
            frozen_valid = (
                low_is_number
                and high_is_number
                and math.isfinite(frozen_low)
                and math.isfinite(frozen_high)
                and frozen_high > frozen_low
            )
            pending_origin = range_state.get("pending_origin_bar")
            pending_break_side = range_state.get("pending_break_side")
            pending_origin_time = (
                parse_ts(pending_origin)
                if isinstance(pending_origin, str)
                else None
            )
            pending_valid = (
                pending_side is None
                and pending_origin is None
                and pending_break_side is None
            ) or (
                pending_side in {"LONG", "SHORT"}
                and pending_origin_time is not None
                and pending_origin_time <= bar_time
                and pending_break_side in {"LONG", "SHORT"}
                and pending_side != pending_break_side
            )
            range_state_valid = (
                not isinstance(raw_phase, bool)
                and isinstance(raw_phase, int)
                and phase in {0, 1}
                and not isinstance(raw_confirm_count, bool)
                and isinstance(raw_confirm_count, int)
                and confirm_count >= 0
                and isinstance(raw_active, bool)
                and pending_side in {None, "LONG", "SHORT"}
                and pending_valid
                and (not raw_active or frozen_valid)
                and (
                    phase == 0
                    or (
                        frozen_valid
                        and range_state.get("break_side") in {"LONG", "SHORT"}
                        and isinstance(range_state.get("break_time_utc"), str)
                        and parse_ts(range_state.get("break_time_utc")) is not None
                        and parse_ts(range_state.get("break_time_utc")) <= bar_time
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
        if side not in {"LONG", "SHORT"}:
            return None
        origin_bar = range_state.get("pending_origin_bar")
        break_side = range_state.get("pending_break_side")
        origin_time = parse_ts(origin_bar) if isinstance(origin_bar, str) else None
        raw_last_state = range_state.get("last_state_bar")
        last_state = (
            parse_ts(raw_last_state)
            if isinstance(raw_last_state, str)
            else None
        )
        raw_last_dispatch = range_state.get("last_dispatch_bar")
        last_dispatch = (
            parse_ts(raw_last_dispatch)
            if isinstance(raw_last_dispatch, str)
            else None
        )
        provenance_valid = bool(
            origin_time is not None
            and origin_time <= signal_bar
            and break_side in {"LONG", "SHORT"}
            and break_side != side
        )
        state_receipt_valid = bool(
            origin_time is not None
            and last_state is not None
            and origin_time <= last_state <= signal_bar
        )
        dispatch_receipt_valid = bool(
            raw_last_dispatch is None
            or (
                isinstance(raw_last_dispatch, str)
                and last_dispatch is not None
            )
        )
        dispatch_identity_time = origin_time if origin_time is not None else signal_bar
        dispatch_already_consumed = bool(
            last_dispatch is not None and last_dispatch >= dispatch_identity_time
        )
        if (
            not provenance_valid
            or not state_receipt_valid
            or not dispatch_receipt_valid
            or dispatch_already_consumed
        ):
            range_state["pending_side"] = None
            range_state["pending_origin_bar"] = None
            range_state["pending_break_side"] = None
            if not dispatch_already_consumed:
                # Consume the invalid pending record without lowering a valid
                # future dispatch high-water mark.
                range_state["last_dispatch_bar"] = dt_text(dispatch_identity_time)
            reason = (
                "decision_receipt_nonmonotonic"
                if (
                    (dispatch_already_consumed and last_dispatch > dispatch_identity_time)
                    or (last_state is not None and last_state > signal_bar)
                )
                else "pending_dispatch_state_invalid"
            )
            self._trade_row(
                "inventory_range_invalidated",
                self.params["strategies"][0],
                side=side,
                reason=reason,
                signal_bar_time=signal_bar_text,
                note=(
                    f"origin={origin_bar!r};break_side={break_side!r};"
                    f"last_state={raw_last_state!r};"
                    f"last_dispatch={raw_last_dispatch!r};pending_consumed"
                ),
            )
            self._save_state()
            return None
        range_state["pending_side"] = None
        range_state["pending_origin_bar"] = None
        range_state["pending_break_side"] = None
        origin_bar_text = dt_text(origin_time)
        range_state["last_dispatch_bar"] = origin_bar_text
        self._save_state()  # durable take before any lane can submit an OPEN
        release_time = origin_time + pd.Timedelta(minutes=1)
        policy = {
            "policy_id": str(self.params.get("inventory_range_fade_policy_id", EXPECTED_INVENTORY_RANGE_FADE_POLICY_ID)),
            "reason": "balanced_inventory_false_break_return",
            "action": "opposite_breakout",
            "origin_bar": origin_bar,
            "break_side": break_side,
        }
        return {
            "opportunity_id": f"{symbol}|{origin_bar_text}|INVENTORY_RANGE_FADE|{side}|{policy['policy_id']}",
            "source": "inventory_range_false_break_fade",
            "side": side,
            "raw_side": "",
            "effective_side": side,
            "entry_policy": policy,
            "event_time": origin_bar_text,
            "release_time": dt_text(release_time),
            "available_time": dt_text(release_time),
            "decision_time": dt_text(poll_time),
            "executable_at": dt_text(poll_time),
        }

    def _clear_pending_open(self, strat: dict[str, Any]) -> None:
        st = self._st(strat)
        st["pending_open_opportunity_id"] = None
        st["pending_open_started_utc"] = None
        st["pending_open_expires_utc"] = None
        st["pending_open_side"] = None
        st["pending_open_lot"] = None
        st["pending_open_symbol"] = None
        st["pending_open_magic"] = None
        st["pending_open_comment"] = None
        st["pending_open_signal_bar"] = None
        st["pending_open_basket_atr30"] = None
        st["pending_open_reverse_used"] = None
        st["pending_open_expected_positions"] = None

    def _reserve_lane_evaluation_bar(
        self,
        strat: dict[str, Any],
        signal_bar_text: str,
        decision_event: str,
    ) -> bool:
        """Reserve one completed bar or consume it when its durable receipt is malformed."""
        st = self._st(strat)
        previous = st.get("last_evaluated_bar")
        current_bar = parse_ts(signal_bar_text)
        if current_bar is None:
            return False
        if previous is not None and (
            not isinstance(previous, str) or parse_ts(previous) is None
        ):
            st["last_evaluated_bar"] = signal_bar_text
            self._trade_row(
                decision_event,
                strat,
                reason="decision_receipt_state_invalid",
                signal_bar_time=signal_bar_text,
                note=f"previous_last_evaluated_bar={previous!r};current_bar_consumed",
            )
            self._save_state()
            return False
        previous_bar = parse_ts(previous) if isinstance(previous, str) else None
        if previous_bar is not None and previous_bar >= current_bar:
            # Preserve the durable high-water mark. Rewinding it would allow
            # older completed bars to be evaluated again after data rollback.
            return False
        st["last_evaluated_bar"] = signal_bar_text
        self._save_state()
        return True

    @staticmethod
    def _low_vol_regime(strat: dict[str, Any], atr30: float | None) -> bool:
        threshold = float(strat.get("adaptive_fixed_exit_atr_threshold", 0.0))
        try:
            atr30_value = None if atr30 is None else float(atr30)
        except (TypeError, ValueError, OverflowError):
            atr30_value = None
        return threshold <= 0.0 or (
            atr30_value is not None
            and math.isfinite(atr30_value)
            and atr30_value < threshold
        )

    def _exit_thresholds(self, strat: dict[str, Any]) -> tuple[float, float, float]:
        target = float(strat["basket_target_usd"])
        stop = float(strat["basket_stop_usd"])
        peak = float(strat.get("failure_to_progress_peak_usd", 0.0))
        atr30 = self._st(strat).get("frozen_basket_atr30")
        try:
            atr30_value = None if atr30 is None else float(atr30)
        except (TypeError, ValueError, OverflowError):
            atr30_value = None
        if (
            self._low_vol_regime(strat, atr30_value)
            and atr30_value is not None
            and math.isfinite(atr30_value)
            and atr30_value > 0.0
        ):
            target = float(strat.get("target_atr_mult", 0.0)) * atr30_value or target
            stop = float(strat.get("stop_atr_mult", 0.0)) * atr30_value or stop
            peak = float(strat.get("failure_to_progress_peak_atr_mult", 0.0)) * atr30_value or peak
        return target, stop, peak

    def _trend_recovery_state(self) -> dict[str, Any]:
        return self.state["routing"]["trend_recovery"]

    def _trend_recovery_episode_valid(self, episode: dict[str, Any]) -> bool:
        raw_active = episode.get("active")
        if not isinstance(raw_active, bool):
            return False
        if not raw_active:
            return True
        raw_total_entries = episode.get("total_entries")
        raw_origin_lane = episode.get("origin_lane_id")
        raw_origin_basket = episode.get("origin_basket_id")
        raw_episode_id = episode.get("episode_id")
        raw_started = episode.get("started_utc")
        raw_entry_until = episode.get("entry_until_utc")
        raw_frozen_atr30 = episode.get("frozen_atr30")
        started = parse_ts(raw_started) if isinstance(raw_started, str) else None
        entry_until = parse_ts(raw_entry_until) if isinstance(raw_entry_until, str) else None
        last_processed = episode.get("last_processed_m1_bar")
        try:
            total_entries = int(raw_total_entries)
            origin_lane = int(raw_origin_lane)
            frozen_atr30 = float(raw_frozen_atr30)
        except (TypeError, ValueError, OverflowError):
            return False
        origin_basket_valid = bool(
            raw_origin_basket is None
            or (
                isinstance(raw_origin_basket, str)
                and bool(raw_origin_basket.strip())
            )
        )
        expected_episode_id = (
            f"TR-{origin_lane}-{raw_origin_basket or dt_text(started)}"
            if started is not None and origin_basket_valid
            else ""
        )
        expected_entry_until = (
            started
            + pd.Timedelta(
                minutes=int(self.params["trend_recovery_entry_window_minutes"])
            )
            if started is not None
            else None
        )
        return (
            not isinstance(raw_total_entries, bool)
            and isinstance(raw_total_entries, int)
            and total_entries >= 0
            and not isinstance(raw_origin_lane, bool)
            and isinstance(raw_origin_lane, int)
            and origin_lane in {1, 2, 3, 4}
            and origin_basket_valid
            and isinstance(raw_episode_id, str)
            and raw_episode_id == expected_episode_id
            and started is not None
            and entry_until is not None
            and entry_until == expected_entry_until
            and total_entries <= int(self.params["trend_recovery_max_total_entries"])
            and isinstance(raw_frozen_atr30, (int, float))
            and not isinstance(raw_frozen_atr30, bool)
            and math.isfinite(frozen_atr30)
            and frozen_atr30 > 0.0
            and (
                last_processed is None
                or (
                    isinstance(last_processed, str)
                    and parse_ts(last_processed) is not None
                )
            )
        )

    def _end_trend_recovery_episode(self, reason: str, at_utc: datetime | pd.Timestamp) -> None:
        episode = self._trend_recovery_state()
        episode["active"] = False
        episode["ended_utc"] = dt_text(at_utc)
        episode["end_reason"] = reason

    def _arm_trend_recovery_episode(
        self,
        origin_strat: dict[str, Any],
        closed_at: datetime | pd.Timestamp,
        origin_basket_id: str | None,
        frozen_atr30: Any,
    ) -> bool:
        if not bool(self.params.get("trend_recovery_enabled", False)):
            return False
        if origin_strat not in self.params.get("strategies", []):
            return False
        stamp = pd.Timestamp(closed_at)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        if not in_session(stamp, int(origin_strat["session_start_utc"]), int(origin_strat["session_end_utc"])):
            return False
        if not isinstance(frozen_atr30, (int, float)) or isinstance(frozen_atr30, bool):
            return False
        try:
            atr30 = float(frozen_atr30)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(atr30) or atr30 <= 0.0:
            return False
        if origin_basket_id is not None and (
            not isinstance(origin_basket_id, str)
            or not origin_basket_id.strip()
        ):
            return False
        recovery = self._trend_recovery_strategies()[0]
        recovery_st = self._st(recovery)
        episode = self._trend_recovery_state()
        if bool(episode.get("active")) or recovery_st.get("basket"):
            self._trade_row(
                "trend_recovery_not_armed", recovery, reason="recovery_lane_busy",
                note=f"origin_lane={origin_strat['lane_id']};origin_basket={origin_basket_id or ''}",
            )
            return False
        episode.update({
            "active": True,
            "episode_id": f"TR-{int(origin_strat['lane_id'])}-{origin_basket_id or dt_text(stamp)}",
            "origin_lane_id": int(origin_strat["lane_id"]),
            "origin_basket_id": origin_basket_id,
            "started_utc": dt_text(stamp),
            "entry_until_utc": dt_text(stamp + pd.Timedelta(minutes=int(self.params["trend_recovery_entry_window_minutes"]))),
            "frozen_atr30": atr30,
            "total_entries": 0,
            "last_processed_m1_bar": None,
            "ended_utc": None,
            "end_reason": None,
        })
        self._trade_row(
            "trend_recovery_armed", recovery, opportunity_id=str(episode["episode_id"]),
            reason="reverse_long_basket_stop_confirmed", signal_bar_time=dt_text(stamp),
            note=f"origin_lane={origin_strat['lane_id']};origin_basket={origin_basket_id or ''};frozen_atr30={atr30}",
        )
        self._save_state()
        return True

    def _trend_recovery_thresholds(self, strat: dict[str, Any]) -> tuple[float, float]:
        atr30 = float(self._trend_recovery_state().get("frozen_atr30") or 0.0)
        if atr30 < float(strat["adaptive_fixed_exit_atr_threshold"]):
            target = float(strat["target_atr_mult"]) * atr30
            stop = float(strat["stop_atr_mult"]) * atr30
        else:
            target = float(strat["ticket_target_usd"])
            stop = float(strat["ticket_stop_usd"])
        return target * float(strat["tp_multiplier"]), stop * float(strat["sl_multiplier"])

    def _position_pnl(self, pos: dict[str, Any], bid: float, ask: float) -> float:
        contract = float(self.params.get("contract_size", 100.0))
        lot = float(pos["lot"])
        if str(pos["side"]) == "LONG":
            return (bid - float(pos["entry_price"])) * contract * lot
        return (float(pos["entry_price"]) - ask) * contract * lot

    def _shadow_close_allocation(
        self,
        pos: dict[str, Any],
        *,
        basket_id: Any,
        bid: float,
        ask: float,
    ) -> dict[str, Any]:
        """Build a passive, per-position close record without changing trade state."""
        side = str(pos.get("side") or "").upper()
        exit_price = bid if side == "LONG" else ask
        return {
            "event": "position_close_attributed",
            "opportunity_id": str(pos.get("opportunity_id") or ""),
            "basket_id": basket_id or str(pos.get("basket_id") or ""),
            "ticket": pos.get("ticket"),
            "position_identifier": pos.get("position_identifier"),
            "side": side,
            "lot": pos.get("lot"),
            "entry_price": pos.get("entry_price"),
            "exit_price": exit_price,
            "price": exit_price,
            "profit": self._position_pnl(pos, bid, ask),
        }

    def _close_trend_recovery_ticket(
        self, strat: dict[str, Any], pos: dict[str, Any], reason: str, price_row: pd.Series, pnl: float
    ) -> str:
        st = self._st(strat)
        if not self.live_enabled and pos.get("shadow") is False:
            self._set_sync_block(
                strat,
                "live_origin_inventory_requires_live_close",
                {"ticket": pos.get("ticket"), "position_identifier": pos.get("position_identifier")},
                recoverable=False,
            )
            self._save_state()
            return "failed"
        if self.live_enabled:
            if self._close_submission_unresolved(pos):
                self._set_sync_block(
                    strat,
                    "close_submission_result_unresolved",
                    {"tickets": [int(pos.get("ticket") or 0)]},
                    recoverable=False,
                )
                self._save_state()
                return "failed"
            ticket = int(pos.get("ticket") or 0)
            raw_position_id = pos.get("position_identifier")
            if (
                isinstance(raw_position_id, bool)
                or not isinstance(raw_position_id, int)
                or raw_position_id <= 0
            ):
                self._set_sync_block(
                    strat,
                    "state_position_identity_invalid",
                    {"ticket": ticket, "position_identifier": repr(raw_position_id)},
                    recoverable=False,
                )
                self._save_state()
                return "failed"
            position_id = raw_position_id
            live_pos = self.executor.get_position(ticket)
            if live_pos is None:
                self._set_sync_block(
                    strat, "position_query_unavailable_before_close",
                    {"ticket": ticket}, recoverable=True,
                )
                self._save_state()
                return "failed"
            if live_pos is False:
                self._set_sync_block(
                    strat, "position_missing_before_close",
                    {"ticket": ticket, "position_identifier": position_id},
                    recoverable=True,
                )
                self._save_state()
                return "failed"
            if not self._state_matches_live(strat, pos, live_pos):
                self._set_sync_block(strat, "state_position_ownership_mismatch", {"ticket": ticket, "position_identifier": position_id}, recoverable=False)
                self._save_state()
                return "failed"
            pos["pending_close_reason"] = reason
            pos["pending_close_signal_bar"] = str(price_row.name)
            self._save_state()
            # Persisting the close intent can take long enough for the broker
            # position to change. Re-prove ownership after that durable write
            # and immediately before the single-ticket CLOSE submission.
            if not self._validate_live_position_before_close(strat, pos):
                return "failed"
            pos["close_submission_started_utc"] = dt_text(utc_now())
            self._save_state()
            result = self.executor.close_position(
                ticket,
                int(self.params.get("deviation_points", 50)),
                expected_login=int(MT5_LOGIN),
                expected_server=str(MT5_SERVER),
                expected_symbol=str(pos["owner_symbol"]),
                expected_magic=int(pos["owner_magic"]),
                expected_comment=str(pos["owner_comment"]),
                expected_identifier=position_id,
            )
            if not result:
                close_status = str(getattr(result, "status", "FAILED"))
                definitive_no_fill = self._close_result_definitive_no_fill(result)
                if definitive_no_fill:
                    pos["close_submission_started_utc"] = None
                if close_status in {"ACCOUNT_IDENTITY_GUARD", "ACCOUNT_MODE_GUARD", "POSITION_OWNERSHIP_GUARD"}:
                    block_reason = (
                        "account_identity_mismatch"
                        if close_status == "ACCOUNT_IDENTITY_GUARD"
                        else (
                            "account_margin_mode_mismatch"
                            if close_status == "ACCOUNT_MODE_GUARD"
                            else "position_ownership_guard_rejected"
                        )
                    )
                    self._set_sync_block(
                        strat,
                        block_reason,
                        {"ticket": ticket, "atomic_close_guard": close_status},
                        recoverable=False,
                    )
                    self._save_state()
                    return "failed"
                if self._record_close_trade_permission_reject(
                    strat, result, price_row.name,
                ):
                    return "trade_permission_rejected"
                if close_status == "MARKET_CLOSED":
                    pos["pending_close_reason"] = None
                    pos["pending_close_signal_bar"] = None
                    self._trade_row(
                        "time_close_deferred", strat, ticket=ticket,
                        position_identifier=position_id,
                        reason="market_closed_10018",
                        signal_bar_time=str(price_row.name),
                        note=f"retcode={getattr(result, 'retcode', None)}",
                    )
                    self._save_state()
                    return "market_closed"
                if not definitive_no_fill:
                    self._set_sync_block(
                        strat,
                        "close_submission_result_unresolved",
                        {"tickets": [ticket], "status": close_status},
                        recoverable=False,
                    )
                    self._save_state()
                    return "failed"
                self._set_sync_block(
                    strat, "live_trend_ticket_close_failed",
                    {"ticket": ticket, "status": str(getattr(result, "status", "FAILED")), "retcode": getattr(result, "retcode", None)},
                    recoverable=True,
                )
                self._save_state()
                return "failed"
            self._clear_trade_permission_reject_state(strat)
            pos["close_requested"] = True
            self._trade_row("position_close_requested", strat, ticket=ticket, position_identifier=position_id, profit=round(pnl, 2), reason=reason, signal_bar_time=str(price_row.name))
            self._save_state()
            return "requested"
        basket_id = st.get("current_basket_id")
        bid = float(price_row["Close"])
        ask = float(price_row["AskOpen"])
        allocation = self._shadow_close_allocation(
            pos, basket_id=basket_id, bid=bid, ask=ask,
        )
        st["basket"] = [row for row in st["basket"] if row is not pos]
        self._trade_row(
            "position_close", strat, profit=round(pnl, 2), reason=reason,
            signal_bar_time=str(price_row.name),
            basket_id=basket_id or "",
            _evaluation_allocations=[allocation],
        )
        self._record_daily_realized(strat, pnl, price_row.name)
        if not st["basket"]:
            self._clear_basket_state(
                strat, reason, str(price_row.name), closed_at_utc=price_row.name,
            )
        self._save_state()
        return "closed"

    def _process_trend_recovery_exits(self, info: Any, poll_time: pd.Timestamp) -> bool:
        strategies = self._trend_recovery_strategies()
        if not strategies:
            return False
        strat = strategies[0]
        entry_enabled = bool(strat.get("enabled", True))
        if not self._sync_strategy(strat):
            self._save_state()
            return False
        st = self._st(strat)
        if not st.get("basket"):
            return entry_enabled
        quote_time = self._broker_quote_time(info, poll_time)
        retry_after = self._validated_time_close_retry_after(strat, quote_time)
        if retry_after is not None:
            if quote_time is None or quote_time < retry_after:
                return False
            st["time_close_retry_after_utc"] = None
            self._save_state()
        price_row = pd.Series(
            {"Open": float(info.bid), "Close": float(info.bid), "AskOpen": float(info.ask)},
            name=quote_time if quote_time is not None else poll_time,
        )
        if st.get("pending_close_reason") or any(
            bool(pos.get("pending_close_reason")) or bool(pos.get("close_requested"))
            for pos in st["basket"]
        ):
            return False
        target, stop = self._trend_recovery_thresholds(strat)
        pnls = [(pos, self._position_pnl(pos, float(info.bid), float(info.ask))) for pos in st["basket"]]
        if any(pnl <= -stop for _pos, pnl in pnls):
            self._end_trend_recovery_episode("any_ticket_stop", poll_time)
            result = self._close_basket(
                strat, "trend_any_ticket_stop", price_row,
                sum(pnl for _pos, pnl in pnls),
            )
            if result == "market_closed" and quote_time is not None:
                self._set_market_closed_close_retry(strat, quote_time)
            return False
        for pos, pnl in pnls:
            if pnl >= target:
                result = self._close_trend_recovery_ticket(
                    strat, pos, "trend_ticket_target", price_row, pnl,
                )
                if result == "market_closed" and quote_time is not None:
                    self._set_market_closed_close_retry(strat, quote_time)
                return False
            entry_time = parse_ts(pos.get("entry_time_utc"))
            if entry_time is None:
                self._set_sync_block(strat, "state_entry_time_invalid", recoverable=False)
                self._save_state()
                return False
            lifecycle_time = quote_time
            if lifecycle_time is None and not self.live_enabled:
                lifecycle_time = poll_time
            if (
                lifecycle_time is not None
                and lifecycle_time
                >= entry_time + pd.Timedelta(minutes=int(strat["hold_minutes"]))
            ):
                result = self._close_trend_recovery_ticket(
                    strat, pos, "trend_ticket_max_hold", price_row, pnl,
                )
                if result == "market_closed" and quote_time is not None:
                    self._set_market_closed_close_retry(strat, quote_time)
                return False
        return entry_enabled

    def _process_trend_recovery_entry(self, price_row: pd.Series, info: Any, poll_time: pd.Timestamp, exit_ready: bool) -> None:
        if not exit_ready or not bool(self.params.get("trend_recovery_enabled", False)):
            return
        strategies = [row for row in self._trend_recovery_strategies() if bool(row.get("enabled", True))]
        if not strategies:
            return
        strat = strategies[0]
        episode = self._trend_recovery_state()
        if not bool(episode.get("active")):
            return
        if not self._trend_recovery_episode_valid(episode):
            self._trade_row(
                "trend_recovery_invalidated",
                strat,
                opportunity_id=str(episode.get("episode_id") or ""),
                reason="episode_state_invalid",
            )
            self._end_trend_recovery_episode("episode_state_invalid", poll_time)
            self._save_state()
            return
        bar_time = parse_ts(price_row.name)
        if bar_time is None:
            return
        started = parse_ts(episode.get("started_utc"))
        if started is None or poll_time < started or bar_time < started:
            return
        entry_until = parse_ts(episode.get("entry_until_utc"))
        if entry_until is None or poll_time > entry_until:
            self._end_trend_recovery_episode("entry_window_expired", poll_time)
            self._save_state()
            return
        release_time = bar_time + pd.Timedelta(minutes=1)
        if poll_time < release_time:
            return
        bar_text = dt_text(bar_time)
        raw_last_processed = episode.get("last_processed_m1_bar")
        last_processed = (
            parse_ts(raw_last_processed)
            if isinstance(raw_last_processed, str)
            else None
        )
        if last_processed is not None and last_processed >= bar_time:
            return
        episode["last_processed_m1_bar"] = bar_text
        max_entries = int(self.params["trend_recovery_max_total_entries"])
        if int(episode.get("total_entries") or 0) >= max_entries:
            self._end_trend_recovery_episode("max_entries_reached", poll_time)
            self._save_state()
            return
        if poll_time > release_time + pd.Timedelta(minutes=float(self.params.get("max_signal_delay_minutes", 2.0))):
            self._trade_row("entry_skip", strat, reason="stale_signal_skip", signal_bar_time=bar_text)
            self._save_state()
            return
        if not float(price_row["Close"]) > float(price_row["Open"]):
            self._save_state()
            return
        spread_points = max(0.0, float(info.ask) - float(info.bid)) / float(self.params.get("point_size", 0.001))
        if spread_points > float(self.params.get("max_entry_spread_points", 300.0)):
            self._trade_row("entry_skip", strat, reason="spread_too_wide", signal_bar_time=bar_text)
            self._save_state()
            return
        before = len(self._basket_rows(strat))
        opportunity = {
            "opportunity_id": str(episode.get("episode_id") or ""),
            "source": "reverse_long_stop_trend_recovery",
            "event_time": bar_text,
            "release_time": dt_text(release_time),
            "available_time": dt_text(release_time),
            "decision_time": dt_text(poll_time),
            "executable_at": dt_text(poll_time),
        }
        self._open_entry(
            strat, "SHORT", price_row, info, note="trend_recovery_m1_bullish",
            basket_atr30=float(episode["frozen_atr30"]), execution_time=poll_time,
            opportunity=opportunity, apply_portfolio_rearm=False, use_confirmed_fill_time=True,
        )
        after = len(self._basket_rows(strat))
        if after > before:
            episode["total_entries"] = int(episode.get("total_entries") or 0) + 1
            if int(episode["total_entries"]) >= max_entries:
                self._end_trend_recovery_episode("max_entries_reached", poll_time)
        self._save_state()

    def _entry_submission_block_reason(self, strat: dict[str, Any], at_utc: datetime | pd.Timestamp | None = None) -> str | None:
        st = self._st(strat)
        if not self._sync_block_contract_valid(st):
            return "sync_block_state_invalid"
        if st.get("sync_block_new_entries"):
            return str(st.get("sync_block_reason") or "sync_block_new_entries")
        if self.live_enabled:
            raw_basket = st.get("basket")
            if not isinstance(raw_basket, list):
                return "state_position_identity_invalid"
            state_tickets: list[int] = []
            state_position_ids: list[int] = []
            for pos in raw_basket:
                if not isinstance(pos, dict):
                    return "state_position_identity_invalid"
                raw_ticket = pos.get("ticket")
                raw_position_id = pos.get("position_identifier")
                raw_lot = pos.get("lot")
                if (
                    isinstance(raw_ticket, bool)
                    or not isinstance(raw_ticket, int)
                    or raw_ticket <= 0
                    or isinstance(raw_position_id, bool)
                    or not isinstance(raw_position_id, int)
                    or raw_position_id <= 0
                    or isinstance(raw_lot, bool)
                    or not isinstance(raw_lot, (int, float))
                    or not math.isfinite(float(raw_lot))
                    or float(raw_lot) <= 0.0
                    or not self._state_ownership_proven(strat, pos)
                ):
                    return "state_position_identity_invalid"
                state_tickets.append(raw_ticket)
                state_position_ids.append(raw_position_id)
            if (
                len(state_tickets) != len(set(state_tickets))
                or len(state_position_ids) != len(set(state_position_ids))
            ):
                return "state_position_identity_invalid"
            if len(raw_basket) >= int(strat["max_positions"]):
                return "lane_capacity_full"
        pending_open_id = st.get("pending_open_opportunity_id")
        if pending_open_id is not None and (
            not isinstance(pending_open_id, str) or not pending_open_id.strip()
        ):
            return "pending_open_state_invalid"
        if pending_open_id:
            return "unresolved_open_action"
        basket_sequence = st.get("basket_sequence")
        if (
            isinstance(basket_sequence, bool)
            or not isinstance(basket_sequence, int)
            or basket_sequence < 0
            or (bool(st.get("basket")) and basket_sequence == 0)
        ):
            return "basket_sequence_state_invalid"
        reject_streak = st.get("autotrading_reject_streak")
        reject_notified = st.get("autotrading_reject_notified")
        if (
            isinstance(reject_streak, bool)
            or not isinstance(reject_streak, int)
            or reject_streak < 0
            or not isinstance(reject_notified, bool)
        ):
            return "trade_permission_state_invalid"
        raw_retry_after = st.get("open_retry_after_utc")
        retry_after = parse_ts(raw_retry_after) if isinstance(raw_retry_after, str) else None
        if raw_retry_after is not None and retry_after is None:
            return "open_retry_state_invalid"
        if retry_after is None:
            return None
        stamp = pd.Timestamp(at_utc if at_utc is not None else utc_now())
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        retry_windows: list[float] = []
        for key in ("fixed_hold_market_closed_retry_seconds", "trade_permission_retry_seconds"):
            try:
                seconds = float(self.params.get(key))
            except (TypeError, ValueError, OverflowError):
                seconds = math.nan
            if math.isfinite(seconds) and seconds >= 0.0:
                retry_windows.append(seconds)
        if not retry_windows or retry_after > stamp + pd.Timedelta(seconds=max(retry_windows)):
            return "open_retry_state_invalid"
        return "open_retry_cooldown" if stamp < retry_after else None

    def _preflight_reject(self, reason: str) -> bool:
        """Report when a safe startup refusal leaves owned exits unmonitored."""
        changed = False
        for strat in self._all_strategies():
            st = self._st(strat)
            if not (st.get("basket") or st.get("pending_close_reason")):
                continue
            self._notify_reconciliation_required(
                strat,
                "preflight_exit_monitoring_stopped",
                {"preflight_reason": reason},
            )
            changed = True
        if changed:
            try:
                self._save_state()
            except Exception:
                logging.exception("S23 preflight-stop alert state could not be persisted")
        return False

    def connect_and_preflight(self) -> bool:
        namespace_error = self._ownership_namespace_error()
        if namespace_error:
            logging.critical("S23 ownership namespace invalid: %s", namespace_error)
            return self._preflight_reject(f"ownership_namespace_invalid:{namespace_error}")
        if any(self._st(strat).get("sync_block_reason") == "state_identity_mismatch" for strat in self._all_strategies()):
            logging.critical("S23 legacy/foreign state must be archived before this runner can start.")
            for strat in self._all_strategies():
                st = self._st(strat)
                if st.get("sync_block_reason") == "state_identity_mismatch":
                    self._notify_reconciliation_required(strat, "state_identity_mismatch", dict(st.get("sync_block_details") or {}))
            # Preserve the old on-disk state as evidence.  The operator must
            # reconcile/flat the account and archive it before first start.
            return self._preflight_reject("state_identity_mismatch")
        clock_now = utc_now()
        current_block = self._entry_admission_block(clock_now)
        eu_summer_time = is_eu_summer_time(clock_now)
        us_summer_time = is_us_summer_time(clock_now)
        logging.info(
            "S23 entry-admission clock loaded: eu_regime=%s us_regime=%s block=%s routing_enabled=true",
            "summer_time" if eu_summer_time else "standard_time",
            "summer_time" if us_summer_time else "standard_time",
            current_block.id if current_block else "off_block",
        )
        try:
            validate_csv_schema(TRADE_LOG_FILE, TRADE_FIELDS)
        except (OSError, UnicodeError, csv.Error, RuntimeError) as exc:
            logging.critical("S23 trade audit schema preflight failed: %s", exc)
            return self._preflight_reject("trade_audit_schema_invalid")
        try:
            validate_csv_schema(
                SIGNAL_EVALUATION_LOG_FILE, SIGNAL_EVALUATION_FIELDS,
            )
            self._signal_evaluation_enabled = True
        except (OSError, UnicodeError, csv.Error, RuntimeError) as exc:
            # This ledger is passive evidence.  A stale/corrupt header must be
            # visible, but refusing startup here would also stop management of
            # already-owned positions.  Disable only evaluation output for the
            # process and keep the operational ledger and lifecycle active.
            self._signal_evaluation_enabled = False
            logging.error(
                "S23 passive signal evaluation disabled by schema mismatch: %s",
                exc,
            )
        globally_enabled = bool(self.params.get("enabled", True))
        has_owned_lifecycle = any(
            self._st(strat).get("basket")
            or self._st(strat).get("pending_close_reason")
            or self._st(strat).get("pending_open_opportunity_id")
            for strat in self._all_strategies()
        )
        if not globally_enabled and not has_owned_lifecycle:
            logging.info("S23 disabled by params with no persisted owned lifecycle.")
            return False
        if not globally_enabled:
            logging.warning("S23 entries disabled by params; starting close-only inventory monitoring.")
        if not self.dm.connect():
            logging.error("S23 EA bridge connect failed.")
            return self._preflight_reject("bridge_connect_failed")
        caps = self.executor.get_bridge_capabilities()
        logging.info("S23 bridge caps: %s", caps)
        if not caps:
            logging.critical("S23 bridge capability query failed.")
            return self._preflight_reject("bridge_capability_query_failed")
        expected_bridge = str(self.params.get("expected_bridge_name") or EXPECTED_BRIDGE_NAME)
        if str(caps.get("name") or "") != expected_bridge:
            logging.critical("S23 wrong bridge attached: got=%s expected=%s", caps.get("name"), expected_bridge)
            return self._preflight_reject("bridge_name_mismatch")
        expected_bridge_version = str(
            self.params.get("expected_bridge_version") or EXPECTED_BRIDGE_VERSION
        )
        if str(caps.get("version") or "") != expected_bridge_version:
            logging.critical(
                "S23 wrong bridge version: got=%s expected=%s",
                caps.get("version"),
                expected_bridge_version,
            )
            return self._preflight_reject("bridge_version_mismatch")
        observed_commands = caps.get("commands")
        if not isinstance(observed_commands, set):
            logging.critical("S23 bridge command surface malformed: %r", observed_commands)
            return self._preflight_reject("bridge_commands_malformed")
        missing = REQUIRED_SHARED_ACCOUNT_COMMANDS - observed_commands
        unexpected = observed_commands - REQUIRED_SHARED_ACCOUNT_COMMANDS
        if missing or unexpected:
            logging.critical(
                "S23 bridge command surface mismatch: missing=%s unexpected=%s",
                sorted(missing), sorted(unexpected),
            )
            return self._preflight_reject("bridge_commands_mismatch")
        legacy_error = self._legacy_inventory_error()
        if legacy_error is not None:
            logging.critical("S23 legacy ownership preflight failed: %s", legacy_error)
            return self._preflight_reject(f"legacy_inventory:{legacy_error}")
        if self.live_enabled:
            account = self.executor.get_account_info()
            if account is None:
                logging.critical("S23 account execution metadata unavailable.")
                return self._preflight_reject("account_metadata_unavailable")
            account_identity_error = self._account_identity_error(account)
            if account_identity_error is not None:
                logging.critical("S23 account identity mismatch: %s", account_identity_error)
                return self._preflight_reject(f"account_identity:{account_identity_error}")
            if bool(self.params.get("require_hedging_account", True)) and int(account.get("margin_mode", -1)) != HEDGING_MARGIN_MODE:
                logging.critical("S23 live trading requires a hedging account: mode=%s", account.get("margin_mode_name"))
                return self._preflight_reject("account_margin_mode_mismatch")
            symbol_info = self.executor.get_symbol_info(str(self.params.get("mt5_symbol", self.params["symbol"])))
            if symbol_info is None or getattr(symbol_info, "quote_time_msc", None) is None:
                logging.critical("S23 bridge INFO response lacks broker quote timestamp; compile and attach the updated BotBridge_s23 before live use.")
                return self._preflight_reject("broker_quote_clock_unavailable")
        if self._entry_policy_state_migrated or self._portfolio_rearm_state_migrated or self._inventory_range_fade_state_migrated or self._morning_session_state_migrated or self._midday_session_state_migrated or self._pre_eu30_session_state_migrated or self._trend_recovery_state_migrated or self._session_vwap_state_migrated or self._t0530_edge_state_migrated:
            try:
                self._save_state()
            except Exception:
                logging.exception("S23 migrated state could not be persisted; refusing runtime start")
                return self._preflight_reject("migrated_state_persist_failed")
            self._entry_policy_state_migrated = False
            self._portfolio_rearm_state_migrated = False
            self._inventory_range_fade_state_migrated = False
            self._morning_session_state_migrated = False
            self._midday_session_state_migrated = False
            self._pre_eu30_session_state_migrated = False
            self._trend_recovery_state_migrated = False
            self._session_vwap_state_migrated = False
            self._t0530_edge_state_migrated = False
        return True

    def _ownership_namespace_error(self) -> str | None:
        if str(self.params.get("strategy_id") or "") != EXPECTED_STRATEGY_ID:
            return f"invalid_strategy_id={self.params.get('strategy_id')} expected={EXPECTED_STRATEGY_ID}"
        if str(self.params.get("candidate_id") or "") != EXPECTED_CANDIDATE_ID:
            return f"invalid_candidate_id={self.params.get('candidate_id')} expected={EXPECTED_CANDIDATE_ID}"
        if str(self.params.get("expected_bridge_name") or "") != EXPECTED_BRIDGE_NAME:
            return f"invalid_expected_bridge_name={self.params.get('expected_bridge_name')}"
        if str(self.params.get("expected_bridge_version") or "") != EXPECTED_BRIDGE_VERSION:
            return f"invalid_expected_bridge_version={self.params.get('expected_bridge_version')}"
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
        if not bool(self.params.get("pre_eu30_session_enabled", False)):
            return "pre_eu30_session_disabled"
        if str(self.params.get("pre_eu30_session_policy_id") or "") != PRE_EU30_POLICY_ID:
            return f"invalid_pre_eu30_policy_id={self.params.get('pre_eu30_session_policy_id')}"
        if str(self.params.get("pre_eu30_session_params_hash") or "") != PRE_EU30_POLICY_PARAMS_HASH:
            return "invalid_pre_eu30_policy_params_hash"
        if str(self.params.get("pre_eu30_session_admission_block") or "") != PRE_EU30_ADMISSION_BLOCK_ID:
            return "invalid_pre_eu30_admission_block"
        if int(self.params.get("pre_eu30_session_max_positions", 0)) != EXPECTED_PRE_EU30_MAX_POSITIONS:
            return f"invalid_pre_eu30_max_positions={self.params.get('pre_eu30_session_max_positions')}"
        if int(self.params.get("m1_bars", 0)) != EXPECTED_PRE_EU30_M1_BARS:
            return f"invalid_pre_eu30_m1_bars={self.params.get('m1_bars')} expected={EXPECTED_PRE_EU30_M1_BARS}"
        pre_eu30 = [row for row in self._pre_eu30_strategies() if bool(row.get("enabled", True))]
        pre_eu30_magics = [int(row.get("magic") or 0) for row in pre_eu30]
        configured_pre_eu30_magics = tuple(int(value) for value in self.params.get("expected_pre_eu30_magics", []))
        if tuple(pre_eu30_magics) != EXPECTED_PRE_EU30_MAGICS or configured_pre_eu30_magics != EXPECTED_PRE_EU30_MAGICS:
            return f"invalid_pre_eu30_magics={pre_eu30_magics} configured={list(configured_pre_eu30_magics)}"
        if [int(row.get("lane_id") or 0) for row in pre_eu30] != [9, 10, 11]:
            return "invalid_pre_eu30_lane_ids"
        expected_pre_eu30 = [
            (PRE_EU30_SIGNAL_IDS[0], 45, "s23_pe_l1"),
            (PRE_EU30_SIGNAL_IDS[1], 60, "s23_pe_l2"),
            (PRE_EU30_SIGNAL_IDS[2], 45, "s23_pe_l3"),
        ]
        for row, (signal_id, hold_minutes, comment) in zip(pre_eu30, expected_pre_eu30):
            if (
                str(row.get("spec_id") or "") != PRE_EU30_POLICY_ID
                or str(row.get("signal_id") or "") != signal_id
                or int(row.get("hold_minutes") or 0) != hold_minutes
                or str(row.get("comment_prefix") or "") != comment
                or not math.isclose(float(row.get("lot") or 0.0), 0.01, rel_tol=0.0, abs_tol=1e-12)
                or int(row.get("max_positions") or 0) != 1
                or int(row.get("cooldown", -1)) != 0
            ):
                return f"invalid_pre_eu30_lane_contract:{row.get('id')}"
        if not bool(self.params.get("trend_recovery_enabled", False)):
            return "trend_recovery_disabled"
        if str(self.params.get("trend_recovery_policy_id") or "") != EXPECTED_TREND_RECOVERY_POLICY_ID:
            return f"invalid_trend_recovery_policy_id={self.params.get('trend_recovery_policy_id')}"
        if str(self.params.get("trend_recovery_params_hash") or "") != EXPECTED_TREND_RECOVERY_PARAMS_HASH:
            return "invalid_trend_recovery_params_hash"
        if int(self.params.get("trend_recovery_entry_window_minutes", 0)) != EXPECTED_TREND_RECOVERY_ENTRY_WINDOW_MINUTES:
            return "invalid_trend_recovery_entry_window"
        if int(self.params.get("trend_recovery_max_total_entries", 0)) != EXPECTED_TREND_RECOVERY_MAX_TOTAL_ENTRIES:
            return "invalid_trend_recovery_max_total_entries"
        trend = [row for row in self._trend_recovery_strategies() if bool(row.get("enabled", True))]
        trend_magics = [int(row.get("magic") or 0) for row in trend]
        configured_trend_magics = tuple(int(value) for value in self.params.get("expected_trend_recovery_magics", []))
        if tuple(trend_magics) != EXPECTED_TREND_RECOVERY_MAGICS or configured_trend_magics != EXPECTED_TREND_RECOVERY_MAGICS:
            return f"invalid_trend_recovery_magics={trend_magics} configured={list(configured_trend_magics)}"
        if len(trend) != 1 or int(trend[0].get("lane_id") or 0) != 12:
            return "invalid_trend_recovery_lane_ids"
        expected_trend = {
            "spec_id": EXPECTED_TREND_RECOVERY_POLICY_ID,
            "signal_id": "completed_m1_bullish_after_reverse_long_stop",
            "comment_prefix": "s23_tr_l1", "lot": 0.01, "max_positions": 2,
            "cooldown": 0, "hold_minutes": EXPECTED_TREND_RECOVERY_MAX_HOLD_MINUTES,
            "ticket_target_usd": 10.0, "ticket_stop_usd": 18.0,
            "target_atr_mult": 3.5, "stop_atr_mult": 6.5,
            "adaptive_fixed_exit_atr_threshold": 2.0, "tp_multiplier": 1.0, "sl_multiplier": 0.5,
        }
        drift = {key: {"actual": trend[0].get(key), "expected": value} for key, value in expected_trend.items() if trend[0].get(key) != value}
        if drift:
            return f"invalid_trend_recovery_lane_contract:{json.dumps(drift, sort_keys=True)}"
        if str(self.params.get("session_vwap_policy_id") or "") != SESSION_VWAP_POLICY_ID:
            return "invalid_session_vwap_policy_id"
        if str(self.params.get("session_vwap_params_hash") or "") != EXPECTED_SESSION_VWAP_PARAMS_HASH:
            return "invalid_session_vwap_params_hash"
        if str(self.params.get("session_vwap_session_timezone") or "") != "America/New_York":
            return "invalid_session_vwap_timezone"
        if str(self.params.get("session_vwap_session_start") or "") != "05:30" or str(self.params.get("session_vwap_session_end") or "") != "08:30":
            return "invalid_session_vwap_window"
        if int(self.params.get("session_vwap_lookback_calendar_days") or 0) != 20:
            return "invalid_session_vwap_lookback"
        if int(self.params.get("session_vwap_atr_period") or 0) != 60:
            return "invalid_session_vwap_atr_period"
        if not math.isclose(float(self.params.get("session_vwap_quantile") or 0.0), 0.90, rel_tol=0.0, abs_tol=1e-12):
            return "invalid_session_vwap_quantile"
        if int(self.params.get("session_vwap_hold_minutes") or 0) != 15 or int(self.params.get("session_vwap_max_positions") or 0) != 5:
            return "invalid_session_vwap_lifecycle"
        session_vwap = [row for row in self._session_vwap_strategies() if bool(row.get("enabled", True))]
        session_vwap_magics = [int(row.get("magic") or 0) for row in session_vwap]
        configured_session_vwap_magics = tuple(int(value) for value in self.params.get("expected_session_vwap_magics", []))
        if tuple(session_vwap_magics) != EXPECTED_SESSION_VWAP_MAGICS or configured_session_vwap_magics != EXPECTED_SESSION_VWAP_MAGICS:
            return "invalid_session_vwap_magics"
        if [int(row.get("lane_id") or 0) for row in session_vwap] != [13, 14, 15, 16, 17]:
            return "invalid_session_vwap_lane_ids"
        for index, row in enumerate(session_vwap, start=1):
            expected = {
                "spec_id": SESSION_VWAP_POLICY_ID, "signal_id": "session_vwap_extension_fade",
                "comment_prefix": f"s23_sv_l{index}", "lot": 0.01, "hold_minutes": 15,
                "max_positions": 1, "cooldown": 0,
            }
            lane_drift = {key: {"actual": row.get(key), "expected": value} for key, value in expected.items() if row.get(key) != value}
            if lane_drift:
                return f"invalid_session_vwap_lane_contract:{row.get('id')}:{json.dumps(lane_drift, sort_keys=True)}"
        if str(self.params.get("t0530_edge_policy_id") or "") != T0530_EDGE_POLICY_ID:
            return "invalid_t0530_edge_policy_id"
        if str(self.params.get("t0530_edge_params_hash") or "") != T0530_EDGE_POLICY_PARAMS_HASH:
            return "invalid_t0530_edge_params_hash"
        if str(self.params.get("t0530_edge_session_timezone") or "") != "America/New_York":
            return "invalid_t0530_edge_timezone"
        if str(self.params.get("t0530_edge_session_start") or "") != "05:30" or str(self.params.get("t0530_edge_session_end") or "") != "06:00":
            return "invalid_t0530_edge_window"
        if int(self.params.get("t0530_edge_lookback_bars") or 0) != 15:
            return "invalid_t0530_edge_lookback"
        if int(self.params.get("t0530_edge_max_signal_delay_minutes") or 0) != 5:
            return "invalid_t0530_edge_signal_delay"
        if int(self.params.get("t0530_edge_hold_minutes") or 0) != 15 or int(self.params.get("t0530_edge_max_positions") or 0) != 4:
            return "invalid_t0530_edge_lifecycle"
        t0530_edge = [row for row in self._t0530_edge_strategies() if bool(row.get("enabled", True))]
        t0530_edge_magics = [int(row.get("magic") or 0) for row in t0530_edge]
        configured_t0530_edge_magics = tuple(int(value) for value in self.params.get("expected_t0530_edge_magics", []))
        if tuple(t0530_edge_magics) != EXPECTED_T0530_EDGE_MAGICS or configured_t0530_edge_magics != EXPECTED_T0530_EDGE_MAGICS:
            return "invalid_t0530_edge_magics"
        if [int(row.get("lane_id") or 0) for row in t0530_edge] != [18, 19, 20, 21]:
            return "invalid_t0530_edge_lane_ids"
        for index, row in enumerate(t0530_edge, start=1):
            expected = {
                "spec_id": T0530_EDGE_POLICY_ID, "signal_id": "t0530_edge_break_fade",
                "comment_prefix": f"s23_ed_l{index}", "lot": 0.01, "hold_minutes": 15,
                "max_positions": 1, "cooldown": 0,
            }
            lane_drift = {key: {"actual": row.get(key), "expected": value} for key, value in expected.items() if row.get(key) != value}
            if lane_drift:
                return f"invalid_t0530_edge_lane_contract:{row.get('id')}:{json.dumps(lane_drift, sort_keys=True)}"
        if str(self.params.get("q01_policy_id") or "") != EXPECTED_Q01_POLICY_ID:
            return "invalid_q01_policy_id"
        if not bool(self.params.get("q01_variance_release_enabled", False)):
            return "q01_variance_release_disabled"
        if bool(self.params.get("q01_live_trading_enabled", True)) != EXPECTED_Q01_LIVE_TRADING_ENABLED:
            return "q01_live_trading_gate_must_remain_disabled"
        if str(self.params.get("q01_params_hash") or "") != EXPECTED_Q01_POLICY_PARAMS_HASH:
            return "invalid_q01_params_hash"
        if int(self.params.get("q01_variance_horizon_bars") or 0) != EXPECTED_Q01_VARIANCE_HORIZON_BARS:
            return "invalid_q01_variance_horizon"
        if int(self.params.get("q01_variance_window_bars") or 0) != EXPECTED_Q01_VARIANCE_WINDOW_BARS:
            return "invalid_q01_variance_window"
        if not math.isclose(float(self.params.get("q01_vr_threshold") or 0.0), EXPECTED_Q01_VR_THRESHOLD, rel_tol=0.0, abs_tol=1e-12):
            return "invalid_q01_vr_threshold"
        if int(self.params.get("q01_breakout_lookback_bars") or 0) != EXPECTED_Q01_BREAKOUT_LOOKBACK_BARS:
            return "invalid_q01_breakout_lookback"
        if int(self.params.get("q01_max_signal_delay_minutes") or 0) != EXPECTED_Q01_MAX_SIGNAL_DELAY_MINUTES:
            return "invalid_q01_signal_delay"
        if int(self.params.get("q01_hold_minutes") or 0) != EXPECTED_Q01_HOLD_MINUTES or int(self.params.get("q01_max_positions") or 0) != EXPECTED_Q01_MAX_POSITIONS:
            return "invalid_q01_lifecycle"
        if int(self.params.get("q01_m1_bars") or 0) != EXPECTED_Q01_M1_BARS:
            return "invalid_q01_m1_bars"
        if int(self.params.get("q01_warmup_m5_bars") or 0) != EXPECTED_Q01_WARMUP_M5_BARS:
            return "invalid_q01_warmup_m5_bars"
        if int(self.params.get("q01_atr_period") or 0) != EXPECTED_Q01_ATR_PERIOD:
            return "invalid_q01_atr_period"
        if int(self.params.get("q01_feed_gap_seconds") or 0) != EXPECTED_Q01_FEED_GAP_SECONDS:
            return "invalid_q01_feed_gap_seconds"
        if not math.isclose(
            float(self.params.get("q01_max_raw_spread_price") or 0.0),
            EXPECTED_Q01_MAX_RAW_SPREAD_PRICE,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return "invalid_q01_max_raw_spread_price"
        q01 = [row for row in self._q01_strategies() if bool(row.get("enabled", True))]
        q01_magics = [int(row.get("magic") or 0) for row in q01]
        configured_q01_magics = tuple(int(value) for value in self.params.get("expected_q01_magics", []))
        if tuple(q01_magics) != EXPECTED_Q01_MAGICS or configured_q01_magics != EXPECTED_Q01_MAGICS:
            return "invalid_q01_magics"
        if [int(row.get("lane_id") or 0) for row in q01] != [22]:
            return "invalid_q01_lane_ids"
        for row in q01:
            expected = {
                "spec_id": EXPECTED_Q01_POLICY_ID, "signal_id": "q01_variance_ratio_release",
                "comment_prefix": "s23_q01_l1", "lot": 0.01, "hold_minutes": 30,
                "max_positions": 1, "cooldown": 5,
            }
            lane_drift = {key: {"actual": row.get(key), "expected": value} for key, value in expected.items() if row.get(key) != value}
            if lane_drift:
                return f"invalid_q01_lane_contract:{row.get('id')}:{json.dumps(lane_drift, sort_keys=True)}"
        all_magics = magics + morning_magics + midday_magics + pre_eu30_magics + trend_magics + session_vwap_magics + t0530_edge_magics + q01_magics
        all_prefixes = prefixes + [str(row.get("comment_prefix") or "") for row in morning + midday + pre_eu30 + trend + session_vwap + t0530_edge + q01]
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
        if not bool(admission_clock.get("routing_enabled", False)):
            return "entry_admission_routing_must_be_enabled_for_pre_eu30_signal"
        observed_blocks = tuple(
            (
                str(row.get("id") or ""),
                str((row.get("start") or {}).get("reference_clock") or ""),
                str((row.get("start") or {}).get("dst_utc") or ""),
                str((row.get("start") or {}).get("standard_utc") or ""),
                str((row.get("end") or {}).get("reference_clock") or ""),
                str((row.get("end") or {}).get("dst_utc") or ""),
                str((row.get("end") or {}).get("standard_utc") or ""),
            )
            for row in admission_clock.get("blocks", [])
        )
        if observed_blocks != EXPECTED_ENTRY_ADMISSION_BLOCKS:
            return f"invalid_entry_admission_blocks={observed_blocks}"
        return None

    def _get_m1(self) -> pd.DataFrame | None:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        requested_bars = max(
            int(self.params.get("m1_bars", 240)),
            int(self.params.get("q01_m1_bars", 0)) if bool(self.params.get("q01_variance_release_enabled", False)) else 0,
        )
        bars = self.dm.get_historical_data(
            symbol,
            int(self.params.get("m1_timeframe", 1)),
            requested_bars,
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

    def _session_vwap_retry_identity(self, retry: Any) -> dict[str, Any]:
        """Validate the complete persisted identity before retry or adoption."""
        opportunity = retry.get("opportunity") if isinstance(retry, dict) else None
        opportunity = opportunity if isinstance(opportunity, dict) else {}
        raw_signal_bar = retry.get("signal_bar_time") if isinstance(retry, dict) else None
        raw_event_time = opportunity.get("event_time")
        raw_release_time = opportunity.get("release_time")
        raw_available_time = opportunity.get("available_time")
        raw_expires = retry.get("expires_utc") if isinstance(retry, dict) else None
        signal_bar = parse_ts(raw_signal_bar) if isinstance(raw_signal_bar, str) else None
        event_time = parse_ts(raw_event_time) if isinstance(raw_event_time, str) else None
        release_time = parse_ts(raw_release_time) if isinstance(raw_release_time, str) else None
        available_time = parse_ts(raw_available_time) if isinstance(raw_available_time, str) else None
        expires = parse_ts(raw_expires) if isinstance(raw_expires, str) else None
        side = str(opportunity.get("side") or "").upper()
        raw_side = str(opportunity.get("raw_side") or "").upper()
        effective_side = str(opportunity.get("effective_side") or "").upper()
        opportunity_id = str(opportunity.get("opportunity_id") or "")
        source = str(opportunity.get("source") or "")
        expected_release = signal_bar + pd.Timedelta(minutes=1) if signal_bar is not None else None
        expected_expiry = (
            expected_release + pd.Timedelta(
                minutes=float(self.params.get("max_signal_delay_minutes", 2.0))
            )
            if expected_release is not None
            else None
        )
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        expected_id = (
            f"{symbol}|{dt_text(signal_bar)}|session_vwap_extension_fade|{side}"
            if signal_bar is not None and side in {"LONG", "SHORT"}
            else ""
        )
        valid = bool(
            isinstance(retry, dict)
            and isinstance(retry.get("opportunity"), dict)
            and signal_bar is not None
            and event_time == signal_bar
            and expected_release is not None
            and release_time == expected_release
            and available_time == expected_release
            and expires == expected_expiry
            and side in {"LONG", "SHORT"}
            and raw_side == side
            and effective_side == side
            and source == "session_vwap_extension_fade"
            and opportunity_id == expected_id
        )
        return {
            "valid": valid,
            "opportunity": opportunity,
            "signal_bar": signal_bar,
            "release_time": release_time,
            "expires": expires,
            "side": side,
            "opportunity_id": opportunity_id,
        }

    def _session_vwap_closed_cutoff(
        self,
        side: str,
        at_utc: datetime | pd.Timestamp | None = None,
    ) -> tuple[pd.Timestamp | None, bool]:
        cutoffs: list[pd.Timestamp] = []
        invalid = False
        reference = None
        if at_utc is not None:
            reference = pd.Timestamp(at_utc)
            reference = reference.tz_localize("UTC") if reference.tzinfo is None else reference.tz_convert("UTC")
        for row in self._session_vwap_strategies():
            state = self._st(row)
            raw_closed_side = state.get("last_closed_side")
            raw_closed_at = state.get("last_closed_at_utc")
            closed_side = str(raw_closed_side or "")
            if closed_side not in {"", "LONG", "SHORT"}:
                invalid = True
                continue
            if not closed_side:
                if raw_closed_at is not None:
                    invalid = True
                continue
            if closed_side != side:
                continue
            closed_at = (
                parse_ts(raw_closed_at)
                if isinstance(raw_closed_at, str)
                else None
            )
            if closed_at is None or (reference is not None and closed_at > reference):
                invalid = True
                continue
            cutoffs.append(closed_at)
        return (max(cutoffs) if cutoffs else None), invalid

    def _recover_session_vwap_pending_open(
        self,
        strat: dict[str, Any],
        positions: list[Any],
        *,
        orders_available: bool,
    ) -> bool:
        """Adopt one exactly identified fill after a crash-before-basket-save."""
        st = self._st(strat)
        basket_sequence = st.get("basket_sequence")
        if (
            isinstance(basket_sequence, bool)
            or not isinstance(basket_sequence, int)
            or basket_sequence < 0
        ):
            return False
        retry = st.get("session_vwap_retry_opportunity")
        identity = self._session_vwap_retry_identity(retry)
        opportunity = identity["opportunity"]
        pending_id = str(st.get("pending_open_opportunity_id") or "")
        pending_symbol = st.get("pending_open_symbol")
        pending_magic = st.get("pending_open_magic")
        pending_comment = st.get("pending_open_comment")
        pending_side = st.get("pending_open_side")
        pending_lot = st.get("pending_open_lot")
        pending_signal_bar = st.get("pending_open_signal_bar")
        pending_reverse_used = st.get("pending_open_reverse_used")
        pending_expires = parse_ts(st.get("pending_open_expires_utc"))
        pending_expected_positions = st.get("pending_open_expected_positions")
        opportunity_id = str(identity["opportunity_id"])
        side = str(identity["side"])
        release_time = identity["release_time"]
        expires = identity["expires"]
        raw_pending_started = st.get("pending_open_started_utc")
        pending_started = (
            parse_ts(raw_pending_started)
            if isinstance(raw_pending_started, str)
            else None
        )
        if (
            len(positions) != 1
            or not identity["valid"]
            or not pending_id
            or pending_id != opportunity_id
            or pending_started is None
            or release_time is None
            or expires is None
            or pending_started < release_time
            or pending_started > expires
            or strat not in self._session_vwap_strategies()
            or pending_symbol != str(self.params.get("mt5_symbol", self.params["symbol"]))
            or isinstance(pending_magic, bool) or pending_magic != int(strat["magic"])
            or pending_comment != str(strat["comment_prefix"])
            or pending_side != side
            or pending_signal_bar != str((retry or {}).get("signal_bar_time") or "")
            or pending_reverse_used is not False
            or isinstance(pending_lot, bool)
            or not isinstance(pending_lot, (int, float))
            or not math.isclose(float(pending_lot), float(strat.get("lot", self.params.get("default_lot", 0.01))), rel_tol=0.0, abs_tol=1e-9)
            or pending_expires is None or pending_expires != expires
            or isinstance(pending_expected_positions, bool)
            or pending_expected_positions != 0
        ):
            return False
        position = positions[0]
        live_identity = self._live_position_identity(position)
        if live_identity is None:
            return False
        position_ticket, position_id = live_identity
        expected_type = ORDER_TYPE_BUY if side == "LONG" else ORDER_TYPE_SELL
        expected_lot = float(strat.get("lot", self.params.get("default_lot", 0.01)))
        raw_open_time_epoch = getattr(position, "open_time", None)
        if (
            isinstance(raw_open_time_epoch, bool)
            or not isinstance(raw_open_time_epoch, int)
        ):
            return False
        open_time_epoch = raw_open_time_epoch
        open_time_msc = int(getattr(position, "open_time_msc", open_time_epoch * 1000) or 0)
        open_time = pd.Timestamp(open_time_msc, unit="ms", tz="UTC") if open_time_msc > 0 else None
        try:
            observed_lot = float(position.volume)
            observed_price = float(position.open_price)
            observed_type = int(position.type)
        except (TypeError, ValueError, OverflowError, AttributeError):
            return False
        if (
            position_id <= 0
            or open_time is None
            or open_time < pending_started.floor("s") - pd.Timedelta(seconds=2)
            or open_time > pending_started + pd.Timedelta(
                minutes=float(self.params.get("max_signal_delay_minutes", 2.0))
            )
            or open_time > expires
            or str(position.symbol) != pending_symbol
            or int(position.magic) != pending_magic
            or str(position.comment or "") != pending_comment
            or observed_type != expected_type
            or not math.isclose(observed_lot, expected_lot, rel_tol=0.0, abs_tol=1e-9)
            or not math.isfinite(observed_price)
            or observed_price <= 0.0
        ):
            return False
        st["basket_sequence"] = basket_sequence + 1
        st["current_basket_id"] = f"L{int(strat['lane_id'])}-B{int(st['basket_sequence']):06d}"
        st["basket"] = [{
            "ticket": position_ticket,
            "position_identifier": position_id,
            "side": side,
            "lot": observed_lot,
            "entry_price": observed_price,
            "entry_time_utc": dt_text(open_time),
            "open_time_epoch": open_time_epoch,
            "owner_symbol": str(position.symbol),
            "owner_magic": int(position.magic),
            "owner_comment": str(position.comment or ""),
            "lane_id": int(strat["lane_id"]),
            "basket_id": st["current_basket_id"],
            "opportunity_id": opportunity_id,
            "shadow": False,
        }]
        st["last_add_price"] = observed_price
        st["last_signal_bar"] = str(retry.get("signal_bar_time") or "")
        st["basket_peak_pnl_usd"] = None
        st["frozen_basket_atr30"] = None
        st["reverse_used"] = False
        self._clear_pending_open(strat)
        st["session_vwap_retry_opportunity"] = None
        recovery_clearable_reasons = {
            None,
            "positions_unavailable_after_open",
            "orders_unavailable",
            "open_success_position_not_confirmed",
            "ambiguous_open_result",
            "ambiguous_open_result_positions",
            "unresolved_open_action",
            "live_positions_without_state",
        }
        if orders_available and st.get("sync_block_reason") in recovery_clearable_reasons:
            self._set_sync_block(strat, None)
        self._save_state()
        self._trade_row(
            "position_lifecycle_recovered",
            strat,
            opportunity_id=opportunity_id,
            ticket=position_ticket,
            position_identifier=position_id,
            side=side,
            lot=observed_lot,
            entry_price=observed_price,
            reason="session_vwap_confirmed_fill_adopted_after_restart",
            signal_bar_time=st["last_signal_bar"],
            note="unique symbol/magic/comment/side/lot/pending-window match",
        )
        return True

    def _recover_generic_pending_open(
        self,
        strat: dict[str, Any],
        positions: list[Any],
        *,
        orders_available: bool,
    ) -> bool:
        """Adopt exactly one receipt-bound fill for any lane after a crash."""
        st = self._st(strat)
        if not orders_available:
            return False
        pending_id = st.get("pending_open_opportunity_id")
        started = parse_ts(st.get("pending_open_started_utc"))
        expires = parse_ts(st.get("pending_open_expires_utc"))
        side = st.get("pending_open_side")
        symbol = st.get("pending_open_symbol")
        comment = st.get("pending_open_comment")
        signal_bar = st.get("pending_open_signal_bar")
        raw_lot = st.get("pending_open_lot")
        raw_magic = st.get("pending_open_magic")
        raw_reverse_used = st.get("pending_open_reverse_used")
        raw_expected_positions = st.get("pending_open_expected_positions")
        try:
            lot = float(raw_lot)
            magic = int(raw_magic)
            expected_positions = int(raw_expected_positions)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            not isinstance(pending_id, str) or not pending_id.strip()
            or started is None or expires is None or expires < started
            or side not in {"LONG", "SHORT"}
            or symbol != str(self.params.get("mt5_symbol", self.params["symbol"]))
            or magic != int(strat["magic"])
            or comment != str(strat["comment_prefix"])
            or not isinstance(signal_bar, str) or parse_ts(signal_bar) is None
            or not math.isfinite(lot) or not math.isclose(
                lot, float(strat.get("lot", self.params.get("default_lot", 0.01))),
                rel_tol=0.0, abs_tol=1e-9,
            )
            or not isinstance(raw_reverse_used, bool)
            or isinstance(raw_expected_positions, bool)
            or expected_positions < 0 or expected_positions > 2
        ):
            return False
        state_basket = list(st.get("basket") or [])
        if expected_positions != len(state_basket):
            return False
        known_ids = {
            int(row.get("position_identifier") or 0)
            for row in state_basket if isinstance(row, dict)
        }
        new_positions = []
        for position in positions:
            identity = self._live_position_identity(position)
            if identity is None:
                return False
            if identity[1] not in known_ids:
                new_positions.append((position, identity))
        if len(new_positions) != 1 or len(positions) != len(state_basket) + 1:
            return False
        position, (position_ticket, position_id) = new_positions[0]
        open_epoch = getattr(position, "open_time", None)
        if isinstance(open_epoch, bool) or not isinstance(open_epoch, int):
            return False
        open_msc = int(getattr(position, "open_time_msc", open_epoch * 1000) or 0)
        open_time = pd.Timestamp(open_msc, unit="ms", tz="UTC") if open_msc > 0 else None
        try:
            observed_lot = float(position.volume)
            observed_price = float(position.open_price)
            observed_type = int(position.type)
        except (TypeError, ValueError, OverflowError, AttributeError):
            return False
        expected_type = ORDER_TYPE_BUY if side == "LONG" else ORDER_TYPE_SELL
        if (
            position_id <= 0 or open_time is None
            or open_time < started.floor("s") - pd.Timedelta(seconds=2)
            or open_time > expires
            or str(position.symbol) != symbol
            or int(position.magic) != magic
            or str(position.comment or "") != comment
            or observed_type != expected_type
            or not math.isclose(observed_lot, lot, rel_tol=0.0, abs_tol=1e-9)
            or not math.isfinite(observed_price) or observed_price <= 0.0
        ):
            return False
        if state_basket:
            if {str(row.get("side") or "") for row in state_basket} != {side}:
                return False
            basket_id = st.get("current_basket_id")
            if not isinstance(basket_id, str) or not basket_id:
                return False
        else:
            sequence = st.get("basket_sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                return False
            st["basket_sequence"] = sequence + 1
            st["current_basket_id"] = f"L{int(strat['lane_id'])}-B{int(st['basket_sequence']):06d}"
            basket_id = st["current_basket_id"]
            raw_atr = st.get("pending_open_basket_atr30")
            st["frozen_basket_atr30"] = (
                float(raw_atr)
                if isinstance(raw_atr, (int, float)) and not isinstance(raw_atr, bool)
                and math.isfinite(float(raw_atr)) and float(raw_atr) > 0.0
                else None
            )
            st["basket_peak_pnl_usd"] = None
            st["reverse_used"] = raw_reverse_used
        st["basket"].append({
            "ticket": position_ticket,
            "position_identifier": position_id,
            "side": side,
            "lot": observed_lot,
            "entry_price": observed_price,
            "entry_time_utc": dt_text(open_time),
            "open_time_epoch": open_epoch,
            "owner_symbol": symbol,
            "owner_magic": magic,
            "owner_comment": comment,
            "lane_id": int(strat["lane_id"]),
            "basket_id": basket_id,
            "opportunity_id": pending_id,
            "shadow": False,
        })
        st["last_add_price"] = observed_price
        st["last_signal_bar"] = signal_bar
        self._clear_pending_open(strat)
        if st.get("sync_block_reason") in {
            None, "positions_unavailable_after_open", "orders_unavailable",
            "open_success_position_not_confirmed", "ambiguous_open_result",
            "ambiguous_open_result_positions", "unresolved_open_action",
            "live_positions_without_state",
        }:
            self._set_sync_block(strat, None)
        self._save_state()
        self._trade_row(
            "position_lifecycle_recovered", strat,
            opportunity_id=pending_id, ticket=position_ticket,
            position_identifier=position_id, side=side, lot=observed_lot,
            entry_price=observed_price,
            reason="generic_confirmed_fill_adopted_after_restart",
            signal_bar_time=signal_bar,
            note="exact receipt symbol/magic/comment/side/lot/open-window match",
        )
        return True

    def _sync_strategy(self, strat: dict[str, Any]) -> bool:
        """Run one complete ownership sync with transaction-wide cleanup."""
        try:
            return self._sync_strategy_impl(strat)
        except BaseException:
            # Individual post-close state steps also roll back, but an
            # interrupt can occur between Python calls after the basket has
            # already been consumed.  Keep the complete sync as the outermost
            # cleanup boundary so no service wrapper can resume this runner
            # with partial memory state or a stale save-deferral marker.
            self._abort_confirmed_close_state_transaction()
            raise

    def _sync_strategy_impl(self, strat: dict[str, Any]) -> bool:
        """Reconcile ownership and report whether known inventory may be monitored.

        A True result does not imply that new entries are permitted.  Entry and
        add admission remains controlled by ``sync_block_new_entries``.  This
        distinction lets an exactly reconciled existing basket retain its
        quote-based exits while an ambiguous later OPEN remains fail-closed.
        """
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        st = self._st(strat)
        if not self._sync_block_contract_valid(st):
            self._set_sync_block(
                strat,
                "sync_block_state_invalid",
                {
                    "previous_block": repr(st.get("sync_block_new_entries")),
                    "previous_reason": repr(st.get("sync_block_reason")),
                    "previous_recoverable": repr(st.get("sync_block_recoverable")),
                },
                recoverable=False,
            )
            self._save_state()
        if not isinstance(st.get("basket"), list):
            self._set_sync_block(
                strat,
                "state_position_identity_invalid",
                {"basket_type": type(st.get("basket")).__name__},
                recoverable=False,
            )
            self._save_state()
            return False
        positions = self.executor.get_positions(symbol, int(strat["magic"]))
        if positions is None:
            self._set_sync_block(strat, "positions_unavailable", recoverable=True)
            self._save_state()
            return False
        unexpected_positions = [record for record in positions if not self._owned_position(strat, record)]
        if unexpected_positions:
            self._set_sync_block(strat, "same_magic_unexpected_position_or_order", {"tickets": [int(record.ticket) for record in unexpected_positions], "comments": [str(record.comment or "") for record in unexpected_positions]}, recoverable=False)
            return False
        if positions and st.get("basket") and not self._basket_close_intent_valid(st):
            self._set_sync_block(
                strat,
                "state_basket_close_intent_invalid",
                recoverable=False,
            )
            return False
        queried_orders = self.executor.get_orders(symbol, int(strat["magic"]))
        orders_available = queried_orders is not None
        orders = list(queried_orders or [])
        if not orders_available:
            self._set_sync_block(strat, "orders_unavailable", recoverable=True)
            self._save_state()
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
        recovered_pending_open = False
        if (
            positions
            and unresolved_open
            and (
                self._recover_session_vwap_pending_open(
                    strat, positions, orders_available=orders_available,
                )
                or self._recover_generic_pending_open(
                    strat, positions, orders_available=orders_available,
                )
            )
        ):
            state_basket = list(st.get("basket") or [])
            unresolved_open = False
            recovered_pending_open = True
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
            basket_sides = {
                str(pos.get("side") or "")
                for pos in state_basket
                if isinstance(pos, dict)
            }
            if len(basket_sides) != 1 or not basket_sides <= {"LONG", "SHORT"}:
                self._set_sync_block(
                    strat,
                    "state_basket_side_inconsistent",
                    {"sides": sorted(basket_sides), "state_rows": len(state_basket)},
                    recoverable=False,
                )
                return False
            state_identity: list[tuple[int, int]] = []
            for pos in state_basket:
                if not isinstance(pos, dict):
                    continue
                raw_ticket = pos.get("ticket")
                raw_position_id = pos.get("position_identifier")
                if (
                    isinstance(raw_ticket, bool)
                    or not isinstance(raw_ticket, int)
                    or isinstance(raw_position_id, bool)
                    or not isinstance(raw_position_id, int)
                ):
                    continue
                state_identity.append((raw_ticket, raw_position_id))
            state_tickets = [ticket for ticket, _position_id in state_identity]
            state_position_ids = [position_id for _ticket, position_id in state_identity]
            if (
                len(state_identity) != len(state_basket)
                or any(ticket <= 0 or position_id <= 0 for ticket, position_id in state_identity)
                or len(set(state_tickets)) != len(state_tickets)
                or len(set(state_position_ids)) != len(state_position_ids)
            ):
                self._set_sync_block(
                    strat,
                    "state_position_identity_invalid",
                    {
                        "state_rows": len(state_basket),
                        "valid_identity_rows": len(state_identity),
                        "state_tickets": [str(ticket) for ticket in state_tickets],
                        "state_position_ids": [str(position_id) for position_id in state_position_ids],
                    },
                    recoverable=False,
                )
                return False
            live_identity: list[tuple[int, int]] = []
            for pos in positions:
                identity = self._live_position_identity(pos)
                if identity is None:
                    live_identity = []
                    break
                live_identity.append(identity)
            live_tickets = [ticket for ticket, _position_id in live_identity]
            live_position_ids = [position_id for _ticket, position_id in live_identity]
            if (
                len(live_identity) != len(positions)
                or any(ticket <= 0 or position_id <= 0 for ticket, position_id in live_identity)
                or len(set(live_tickets)) != len(live_tickets)
                or len(set(live_position_ids)) != len(live_position_ids)
            ):
                self._set_sync_block(
                    strat,
                    "live_position_identity_invalid",
                    {
                        "live_rows": len(positions),
                        "valid_identity_rows": len(live_identity),
                        "live_tickets": [str(ticket) for ticket in live_tickets],
                        "live_position_ids": [str(position_id) for position_id in live_position_ids],
                    },
                    recoverable=False,
                )
                return False
            live_by_id = {
                position_id: pos
                for (_ticket, position_id), pos in zip(live_identity, positions)
            }
            state_ids = set(state_position_ids)
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
                position_id = int(state_pos.get("position_identifier"))
                live_pos = live_by_id.get(position_id)
                if live_pos is not None:
                    if not self._position_close_intent_valid(state_pos):
                        self._set_sync_block(
                            strat,
                            "state_position_close_intent_invalid",
                            {"ticket": position_id},
                            recoverable=False,
                        )
                        return False
                    if not self._state_matches_live(strat, state_pos, live_pos):
                        self._set_sync_block(strat, "state_position_ownership_mismatch", {"ticket": position_id}, recoverable=False)
                        return False
                    try:
                        persisted_entry_price = float(state_pos.get("entry_price"))
                    except (TypeError, ValueError, OverflowError):
                        persisted_entry_price = math.nan
                    raw_broker_entry_price = getattr(live_pos, "open_price", None)
                    try:
                        broker_entry_price = (
                            None
                            if raw_broker_entry_price is None
                            else float(raw_broker_entry_price)
                        )
                    except (TypeError, ValueError, OverflowError):
                        broker_entry_price = math.nan
                    if broker_entry_price is not None and (
                        not math.isfinite(broker_entry_price)
                        or broker_entry_price <= 0.0
                    ):
                        self._set_sync_block(
                            strat,
                            "live_position_entry_price_invalid",
                            {"ticket": position_id, "open_price": str(raw_broker_entry_price)},
                            recoverable=True,
                        )
                        return False
                    if broker_entry_price is None and (
                        not math.isfinite(persisted_entry_price)
                        or persisted_entry_price <= 0.0
                    ):
                        self._set_sync_block(
                            strat,
                            "state_position_lifecycle_invalid",
                            {"ticket": position_id, "entry_price": str(state_pos.get("entry_price"))},
                            recoverable=False,
                        )
                        return False
                    if broker_entry_price is not None and not math.isclose(
                        persisted_entry_price,
                        broker_entry_price,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    ):
                        previous_entry_price = state_pos.get("entry_price")
                        state_pos["entry_price"] = broker_entry_price
                        self._trade_row(
                            "position_lifecycle_recovered",
                            strat,
                            ticket=int(state_pos.get("ticket") or position_id),
                            position_identifier=position_id,
                            reason="confirmed_broker_entry_price_restored",
                            note=(
                                f"previous_entry_price={previous_entry_price};"
                                f"broker_entry_price={broker_entry_price}"
                            ),
                        )
                        self._save_state()
                    broker_open_epoch = int(getattr(live_pos, "open_time", 0) or 0)
                    if broker_open_epoch <= 0:
                        self._set_sync_block(
                            strat,
                            "confirmed_fill_time_unavailable",
                            {"ticket": position_id},
                            recoverable=True,
                        )
                        return False
                    else:
                        broker_entry_time = pd.Timestamp(broker_open_epoch, unit="s", tz="UTC")
                        persisted_entry_time = parse_ts(state_pos.get("entry_time_utc"))
                        try:
                            persisted_open_epoch = int(
                                state_pos.get("open_time_epoch") or 0
                            )
                        except (TypeError, ValueError, OverflowError):
                            persisted_open_epoch = 0
                        if (
                            persisted_entry_time != broker_entry_time
                            or persisted_open_epoch != broker_open_epoch
                        ):
                            previous_entry_time = state_pos.get("entry_time_utc")
                            previous_open_epoch = state_pos.get("open_time_epoch")
                            state_pos["entry_time_utc"] = dt_text(broker_entry_time)
                            state_pos["open_time_epoch"] = broker_open_epoch
                            self._trade_row(
                                "position_lifecycle_recovered",
                                strat,
                                ticket=int(state_pos.get("ticket") or position_id),
                                position_identifier=position_id,
                                reason="confirmed_broker_fill_time_restored",
                                note=(
                                    f"previous_entry_time_utc={previous_entry_time};"
                                    f"previous_open_time_epoch={previous_open_epoch};"
                                    f"broker_entry_time_utc={dt_text(broker_entry_time)};"
                                    f"broker_open_time_epoch={broker_open_epoch}"
                                ),
                            )
                            self._save_state()
                    remaining_state.append(state_pos)
                    continue
                raw_state_open_epoch = state_pos.get("open_time_epoch")
                if (
                    isinstance(raw_state_open_epoch, bool)
                    or not isinstance(raw_state_open_epoch, int)
                ):
                    self._set_sync_block(
                        strat,
                        "state_position_lifecycle_invalid",
                        {"ticket": position_id, "open_time_epoch": str(state_pos.get("open_time_epoch"))},
                        recoverable=False,
                    )
                    return False
                state_open_epoch = raw_state_open_epoch
                if state_open_epoch <= 0:
                    self._set_sync_block(
                        strat,
                        "state_position_lifecycle_invalid",
                        {
                            "ticket": position_id,
                            "open_time_epoch": str(state_pos.get("open_time_epoch")),
                        },
                        recoverable=False,
                    )
                    return False
                opened_at_epoch = state_open_epoch - 60
                state_ticket = int(state_pos.get("ticket") or 0)
                direct_position = self.executor.get_position(state_ticket)
                if direct_position is None:
                    self._set_sync_block(
                        strat,
                        "position_absence_unconfirmed",
                        {"ticket": state_ticket, "position_identifier": position_id},
                        recoverable=True,
                    )
                    return False
                if direct_position is not False:
                    if not self._state_matches_live(strat, state_pos, direct_position):
                        self._set_sync_block(
                            strat,
                            "state_position_ownership_mismatch",
                            {"ticket": state_ticket, "position_identifier": position_id},
                            recoverable=False,
                        )
                    else:
                        self._set_sync_block(
                            strat,
                            "position_inventory_inconsistent",
                            {"ticket": state_ticket, "position_identifier": position_id},
                            recoverable=True,
                        )
                    return False
                deal = self._get_confirmed_close_deal(position_id, opened_at_epoch)
                if deal is None:
                    self._set_sync_block(strat, "close_deal_query_unavailable", {"ticket": position_id}, recoverable=False)
                    return False
                if deal is False:
                    self._set_sync_block(strat, "close_deal_not_confirmed", {"ticket": position_id}, recoverable=False)
                    return False
                if (
                    int(deal.position_id) != position_id
                    or str(deal.symbol) != symbol
                    or not self._state_ownership_proven(strat, state_pos)
                ):
                    self._set_sync_block(
                        strat,
                        "close_deal_ownership_mismatch",
                        {"ticket": position_id, "deal_position_id": int(deal.position_id), "deal_magic": int(deal.magic), "deal_symbol": str(deal.symbol)},
                        recoverable=False,
                    )
                    return False
                try:
                    deal_epoch = int(getattr(deal, "deal_time", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    deal_epoch = 0
                if deal_epoch <= 0 or deal_epoch < state_open_epoch:
                    self._set_sync_block(
                        strat,
                        "close_deal_timestamp_invalid",
                        {
                            "ticket": position_id,
                            "deal_id": int(getattr(deal, "deal", 0) or 0),
                            "deal_time": getattr(deal, "deal_time", None),
                            "state_open_time": state_pos.get("open_time_epoch"),
                        },
                        recoverable=False,
                    )
                    return False
                try:
                    deal_id = int(getattr(deal, "deal", 0) or 0)
                    deal_price = float(getattr(deal, "price", 0.0) or 0.0)
                    deal_net_profit = float(deal.net_profit)
                    deal_exit_volume = float(getattr(deal, "exit_volume"))
                    state_lot = float(state_pos.get("lot"))
                except (TypeError, ValueError, OverflowError, AttributeError):
                    deal_id = 0
                    deal_price = math.nan
                    deal_net_profit = math.nan
                    deal_exit_volume = math.nan
                    state_lot = math.nan
                if (
                    deal_id <= 0
                    or not math.isfinite(deal_price)
                    or deal_price <= 0.0
                    or not math.isfinite(deal_net_profit)
                    or not math.isfinite(deal_exit_volume)
                    or deal_exit_volume <= 0.0
                    or not math.isfinite(state_lot)
                    or state_lot <= 0.0
                ):
                    self._set_sync_block(
                        strat,
                        "close_deal_payload_invalid",
                        {
                            "ticket": position_id,
                            "deal_id": deal_id,
                            "deal_price": str(getattr(deal, "price", None)),
                            "deal_net_profit": str(getattr(deal, "net_profit", None)),
                            "deal_exit_volume": str(getattr(deal, "exit_volume", None)),
                            "state_lot": str(state_pos.get("lot")),
                        },
                        recoverable=False,
                    )
                    return False
                if not math.isclose(deal_exit_volume, state_lot, rel_tol=0.0, abs_tol=1e-9):
                    self._set_sync_block(
                        strat,
                        "close_deal_volume_mismatch",
                        {
                            "ticket": position_id,
                            "deal_exit_volume": deal_exit_volume,
                            "state_lot": state_lot,
                        },
                        recoverable=False,
                    )
                    return False
                confirmed_deals.append((state_pos, deal))
            if remaining_state:
                _, latest_state_pos = max(
                    enumerate(remaining_state),
                    key=lambda item: (
                        int(item[1].get("open_time_epoch") or 0),
                        item[0],
                    ),
                )
                broker_confirmed_add_anchor = float(latest_state_pos["entry_price"])
                try:
                    persisted_add_anchor = float(st.get("last_add_price"))
                except (TypeError, ValueError, OverflowError):
                    persisted_add_anchor = math.nan
                if not math.isclose(
                    persisted_add_anchor,
                    broker_confirmed_add_anchor,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                ):
                    previous_add_anchor = st.get("last_add_price")
                    st["last_add_price"] = broker_confirmed_add_anchor
                    self._trade_row(
                        "position_lifecycle_recovered",
                        strat,
                        ticket=int(latest_state_pos.get("ticket") or 0),
                        position_identifier=int(
                            latest_state_pos.get("position_identifier")
                            or latest_state_pos.get("ticket")
                            or 0
                        ),
                        reason="confirmed_broker_last_add_price_restored",
                        note=(
                            f"previous_last_add_price={previous_add_anchor};"
                            f"broker_confirmed_last_add_price={broker_confirmed_add_anchor}"
                        ),
                    )
                    self._save_state()
            if confirmed_deals:
                # Daily realized state is a single UTC-day accumulator.  Apply
                # broker-confirmed deals chronologically so a basket whose
                # tickets disappear across midnight cannot roll the active day
                # backwards merely because persisted basket order differs.
                confirmed_deals.sort(key=lambda item: int(item[1].deal_time))
                reason = str(st.get("pending_close_reason") or "broker_or_external_close_confirmed")
                signal_bar = st.get("pending_close_signal_bar")
                closed_basket_id = st.get("current_basket_id")
                closed_basket_was_reverse = self._validated_reverse_used(strat)
                closed_basket_atr30 = st.get("frozen_basket_atr30")
                closed_basket_was_long = bool(state_basket) and all(
                    str(pos.get("side") or "") == "LONG" for pos in state_basket
                )
                confirmed_times: list[pd.Timestamp] = []
                for state_pos, deal in confirmed_deals:
                    position_reason = str(state_pos.get("pending_close_reason") or reason)
                    deal_epoch = int(deal.deal_time)
                    deal_time = pd.Timestamp(deal_epoch, unit="s", tz="UTC")
                    confirmed_times.append(pd.Timestamp(deal_time))
                    position_id = int(state_pos.get("position_identifier"))
                    self._trade_row(
                        "position_close_confirmed", strat,
                        opportunity_id=str(state_pos.get("opportunity_id") or ""),
                        ticket=int(state_pos.get("ticket") or position_id), position_identifier=position_id,
                        deal_id=int(getattr(deal, "deal", 0) or 0), side=str(state_pos.get("side") or ""),
                        lot=float(state_pos.get("lot") or 0.0), entry_price=float(state_pos.get("entry_price") or 0.0),
                        exit_price=float(getattr(deal, "price", 0.0) or 0.0), price=float(getattr(deal, "price", 0.0) or 0.0),
                        profit=float(deal.net_profit), reason=position_reason, signal_bar_time=signal_bar,
                        note=f"deal_time_utc={dt_text(deal_time)}",
                    )
                # Do not consume position state or advance the daily loss
                # accumulator until every immutable broker close deal has an
                # operational audit row.  A ledger failure must leave the old
                # basket recoverable rather than allow exception containment
                # to persist an unlogged close.  The deal/position identity
                # gate above makes a retry after a later state-write failure
                # idempotent in both operational and passive ledgers.
                state_before_close_consumption = self._begin_confirmed_close_state_transaction(
                    max(int(getattr(deal, "deal", 0) or 0) for _, deal in confirmed_deals)
                )
                st["basket"] = remaining_state
                for (_state_pos, deal), deal_time in zip(
                    confirmed_deals, confirmed_times,
                ):
                    self._confirmed_close_state_step(
                        state_before_close_consumption,
                        lambda deal=deal, deal_time=deal_time: self._record_daily_realized(
                            strat, float(deal.net_profit), deal_time,
                        ),
                    )
                if not remaining_state:
                    fully_closed_sides = {
                        str(pos.get("side") or "").upper()
                        for pos in state_basket
                        if str(pos.get("side") or "").upper() in {"LONG", "SHORT"}
                    }
                    fully_closed_side = (
                        next(iter(fully_closed_sides)) if len(fully_closed_sides) == 1 else None
                    )
                    if reason == "basket_target" and closed_basket_was_long:
                        self._confirmed_close_state_step(
                            state_before_close_consumption,
                            lambda: self._confirm_long_target_portfolio_rearm(
                                strat,
                                max(confirmed_times),
                                closed_basket_id,
                            ),
                        )
                    else:
                        self._confirmed_close_state_step(
                            state_before_close_consumption,
                            lambda: self._cancel_unconfirmed_long_target_rearm_after_other_close(
                                strat, closed_basket_id, reason,
                            ),
                        )
                    if reason == "basket_stop" and closed_basket_was_long and closed_basket_was_reverse:
                        self._confirmed_close_state_step(
                            state_before_close_consumption,
                            lambda: self._arm_trend_recovery_episode(
                                strat,
                                max(confirmed_times),
                                closed_basket_id,
                                closed_basket_atr30,
                            ),
                        )
                    self._confirmed_close_state_step(
                        state_before_close_consumption,
                        lambda: self._clear_basket_state(
                            strat,
                            reason,
                            signal_bar,
                            closed_at_utc=max(confirmed_times),
                            closed_side=fully_closed_side,
                        ),
                    )
                    clearable_after_confirmed_close = bool(
                        st.get("sync_block_new_entries") is False
                        or st.get("sync_block_recoverable") is True
                        or st.get("sync_block_reason") in CONFIRMED_CLOSE_CLEAR_SYNC_REASONS
                    )
                    if clearable_after_confirmed_close:
                        self._confirmed_close_state_step(
                            state_before_close_consumption,
                            lambda: self._set_sync_block(strat, None),
                        )
                        if not orders_available:
                            self._confirmed_close_state_step(
                                state_before_close_consumption,
                                lambda: self._set_sync_block(
                                    strat,
                                    "orders_unavailable",
                                    {"after_confirmed_close": True},
                                    recoverable=True,
                                ),
                            )
                else:
                    self._confirmed_close_state_step(
                        state_before_close_consumption,
                        lambda: self._recover_close_retry_after_owned_sync(
                            strat,
                            remaining_state,
                            orders_available=orders_available,
                            resolved_unresolved_submission=any(
                                self._close_submission_unresolved(state_pos)
                                for state_pos, _deal in confirmed_deals
                            ),
                        ),
                    )
                if unresolved_open:
                    self._confirmed_close_state_step(
                        state_before_close_consumption,
                        lambda: self._set_sync_block(
                            strat,
                            "unresolved_open_action",
                            {
                                "opportunity_id": str(st.get("pending_open_opportunity_id") or ""),
                                "started_utc": st.get("pending_open_started_utc"),
                                "live_tickets": [int(pos.ticket) for pos in positions],
                            },
                            recoverable=False,
                        ),
                    )
                # Keep the transaction snapshot active through the sole
                # durable commit.  The dedicated commit bypasses helper-save
                # deferral and clears the context only after the complete
                # payload succeeds, so an interrupt immediately before or
                # during persistence still restores the pre-consumption state.
                self._confirmed_close_state_step(
                    state_before_close_consumption,
                    self._commit_confirmed_close_state_transaction,
                    final_commit=True,
                )
                if unresolved_open:
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
                close_retry_recovered = self._recover_close_retry_after_owned_sync(
                    strat, remaining_state, orders_available=True,
                )
                if not close_retry_recovered:
                    clear_recoverable_sync_block_after_clean_sync(
                        symbol_key=strat["id"],
                        state=st,
                        save_state=self._save_state,
                        options=self.safety,
                        audit=lambda symbol_key, event, reason: self._trade_row(
                            event, strat, reason=reason, note=symbol_key,
                        ),
                    )
            if recovered_pending_open and remaining_state and len(remaining_state) == len(state_basket):
                # A separately retained non-recoverable entry block must not
                # prevent exit monitoring of the uniquely recovered position.
                return True
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

    def _recover_close_retry_after_owned_sync(
        self,
        strat: dict[str, Any],
        positions: list[dict[str, Any]],
        *,
        orders_available: bool,
        resolved_unresolved_submission: bool = False,
    ) -> bool:
        """Resume a close only after every remaining owned position re-syncs."""
        st = self._st(strat)
        reason = str(st.get("sync_block_reason") or "")
        has_unsubmitted_position = any(
            not bool(position.get("close_requested")) for position in positions
        )
        pending_close = bool(st.get("pending_close_reason")) and has_unsubmitted_position
        pending_close = pending_close or any(
            bool(position.get("pending_close_reason"))
            and not bool(position.get("close_requested"))
            for position in positions
        )
        unresolved_submissions = [
            int(position.get("ticket") or 0)
            for position in positions
            if self._close_submission_unresolved(position)
        ]
        if unresolved_submissions:
            self._set_sync_block(
                strat,
                "close_submission_result_unresolved",
                {"tickets": sorted(unresolved_submissions)},
                recoverable=False,
            )
            self._save_state()
            return False
        reason_allows_rearm = bool(
            reason in OWNED_CLOSE_RETRY_SYNC_REASONS
            or (
                reason == "close_submission_result_unresolved"
                and resolved_unresolved_submission
            )
        )
        if (
            not positions
            or not orders_available
            or (reason and not reason_allows_rearm)
            or (not reason and not pending_close)
        ):
            return False
        if reason:
            # Older local states persisted these same close-attempt reasons as
            # non-recoverable.  Exact position/order/state reconciliation is
            # the missing evidence that safely upgrades only this reason set.
            st["sync_block_recoverable"] = True
            if not clear_recoverable_sync_block_after_clean_sync(
                symbol_key=strat["id"],
                state=st,
                save_state=self._save_state,
                options=self.safety,
                audit=lambda symbol_key, event, cleared_reason: self._trade_row(
                    event, strat, reason=cleared_reason, note=symbol_key,
                ),
            ):
                return False
        has_submitted_position = any(
            bool(position.get("close_requested")) for position in positions
        )
        if not has_submitted_position:
            cleared_unconfirmed_long_target = (
                st.get("pending_close_reason") == "basket_target"
                and bool(positions)
                and all(str(position.get("side") or "") == "LONG" for position in positions)
            )
            cleared_basket_id = st.get("current_basket_id")
            st["pending_close_reason"] = None
            st["pending_close_signal_bar"] = None
            if cleared_unconfirmed_long_target:
                routing = self.state["routing"]
                had_pending_rearm = (
                    routing.get("long_target_rearm_pending_confirmation") is True
                )
                self._refresh_long_target_rearm_pending_summary()
                if (
                    had_pending_rearm
                    and routing.get("long_target_rearm_pending_confirmation") is False
                ):
                    self._trade_row(
                        "portfolio_rearm_cancelled",
                        strat,
                        side="LONG",
                        reason="partial_target_close_remaining_retry_rearmed",
                        note=f"basket_id={cleared_basket_id}",
                    )
        for position in positions:
            if bool(position.get("close_requested")):
                continue
            position["close_requested"] = False
            position["pending_close_reason"] = None
            position["pending_close_signal_bar"] = None
        self._trade_row(
            "close_retry_rearmed", strat,
            reason=reason or "durable_pending_close_reconciled",
        )
        self._save_state()
        return True

    def _validate_live_position_before_close(
        self, strat: dict[str, Any], pos: dict[str, Any],
    ) -> bool:
        ticket = int(pos.get("ticket") or 0)
        if not self._position_close_intent_valid(pos):
            self._set_sync_block(
                strat,
                "state_position_close_intent_invalid",
                {"ticket": ticket},
                recoverable=False,
            )
            self._save_state()
            return False
        raw_position_id = pos.get("position_identifier")
        if (
            isinstance(raw_position_id, bool)
            or not isinstance(raw_position_id, int)
            or raw_position_id <= 0
        ):
            self._set_sync_block(
                strat,
                "state_position_identity_invalid",
                {"ticket": ticket, "position_identifier": repr(raw_position_id)},
                recoverable=False,
            )
            self._save_state()
            return False
        position_id = raw_position_id
        live_pos = self.executor.get_position(ticket)
        if live_pos is None:
            self._set_sync_block(
                strat, "position_query_unavailable_before_close", {"ticket": ticket},
                recoverable=True,
            )
            self._save_state()
            return False
        if live_pos is False:
            self._set_sync_block(
                strat, "position_missing_before_close",
                {"ticket": ticket, "position_identifier": position_id},
                recoverable=True,
            )
            self._save_state()
            return False
        if not self._state_matches_live(strat, pos, live_pos):
            self._set_sync_block(
                strat, "state_position_ownership_mismatch",
                {"ticket": ticket, "position_identifier": position_id},
                recoverable=False,
            )
            self._save_state()
            return False
        return True

    def _pre_open_live_inventory_block(
        self, strat: dict[str, Any],
    ) -> tuple[str, dict[str, Any], bool] | None:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        positions = self.executor.get_positions(symbol, int(strat["magic"]))
        if positions is None:
            return ("positions_unavailable", {}, True)
        unexpected_positions = [
            record for record in positions if not self._owned_position(strat, record)
        ]
        if unexpected_positions:
            return (
                "same_magic_unexpected_position_or_order",
                {
                    "tickets": [repr(getattr(record, "ticket", None)) for record in unexpected_positions],
                    "comments": [str(getattr(record, "comment", "") or "") for record in unexpected_positions],
                },
                False,
            )
        orders = self.executor.get_orders(symbol, int(strat["magic"]))
        if orders is None:
            return ("orders_unavailable", {}, True)
        if orders:
            return (
                "same_magic_unexpected_order",
                {
                    "tickets": [repr(getattr(record, "ticket", None)) for record in orders],
                    "comments": [str(getattr(record, "comment", "") or "") for record in orders],
                },
                False,
            )
        raw_basket = self._st(strat).get("basket")
        if not isinstance(raw_basket, list):
            return (
                "state_position_identity_invalid",
                {"basket_type": type(raw_basket).__name__},
                False,
            )
        state_by_id: dict[int, dict[str, Any]] = {}
        for state_pos in raw_basket:
            if not isinstance(state_pos, dict):
                return ("state_position_identity_invalid", {}, False)
            raw_ticket = state_pos.get("ticket")
            raw_position_id = state_pos.get("position_identifier")
            if (
                isinstance(raw_ticket, bool)
                or not isinstance(raw_ticket, int)
                or raw_ticket <= 0
                or isinstance(raw_position_id, bool)
                or not isinstance(raw_position_id, int)
                or raw_position_id <= 0
                or raw_position_id in state_by_id
            ):
                return ("state_position_identity_invalid", {}, False)
            state_by_id[raw_position_id] = state_pos
        live_by_id: dict[int, Any] = {}
        for live_pos in positions:
            identity = self._live_position_identity(live_pos)
            if identity is None or identity[1] in live_by_id:
                return ("live_position_identity_invalid", {}, False)
            live_by_id[identity[1]] = live_pos
        if not state_by_id and live_by_id:
            return (
                "live_positions_without_state",
                {"live_ids": sorted(live_by_id)},
                False,
            )
        if set(state_by_id) != set(live_by_id):
            return (
                "position_inventory_inconsistent",
                {
                    "state_ids": sorted(state_by_id),
                    "live_ids": sorted(live_by_id),
                },
                False,
            )
        for position_id, state_pos in state_by_id.items():
            if not self._state_matches_live(strat, state_pos, live_by_id[position_id]):
                return (
                    "state_position_ownership_mismatch",
                    {"position_identifier": position_id},
                    False,
                )
        return None

    def _pre_open_account_block(
        self,
    ) -> tuple[str, dict[str, Any], bool] | None:
        account = self.executor.get_account_info()
        if account is None:
            return ("account_execution_metadata_unavailable", {}, True)
        if not isinstance(account, dict):
            return (
                "account_execution_metadata_invalid",
                {"account_type": type(account).__name__},
                False,
            )
        identity_error = self._account_identity_error(account)
        if identity_error is not None:
            return (
                "account_identity_mismatch",
                {"identity_check": identity_error},
                False,
            )
        raw_margin_mode = account.get("margin_mode")
        if (
            isinstance(raw_margin_mode, bool)
            or not isinstance(raw_margin_mode, int)
        ):
            return ("account_execution_metadata_invalid", {}, False)
        if (
            bool(self.params.get("require_hedging_account", True))
            and raw_margin_mode != HEDGING_MARGIN_MODE
        ):
            return (
                "account_margin_mode_mismatch",
                {"hedging_required": True},
                False,
            )
        permission_keys = (
            "account_trade_allowed",
            "account_trade_expert",
            "terminal_trade_allowed",
            "mql_trade_allowed",
        )
        if any(not isinstance(account.get(key), bool) for key in permission_keys):
            return ("account_execution_metadata_invalid", {}, False)
        disabled = [key for key in permission_keys if account.get(key) is not True]
        if disabled:
            return (
                "trade_permission_precheck_blocked",
                {"disabled_flags": disabled},
                True,
            )
        return None

    def _close_basket(self, strat: dict[str, Any], reason: str, price_row: pd.Series, pnl: float) -> str:
        st = self._st(strat)
        live_origin = any(pos.get("shadow") is False for pos in self._basket_rows(strat))
        if not self.live_enabled and live_origin:
            self._set_sync_block(
                strat,
                "live_origin_inventory_requires_live_close",
                {"tickets": [pos.get("ticket") for pos in self._basket_rows(strat)]},
                recoverable=False,
            )
            self._save_state()
            return "failed"
        if self.live_enabled:
            if not self._basket_close_intent_valid(st):
                self._set_sync_block(
                    strat,
                    "state_basket_close_intent_invalid",
                    {},
                    recoverable=False,
                )
                self._save_state()
                return "failed"
            positions_to_close = [
                pos for pos in list(st["basket"])
                if not bool(pos.get("close_requested"))
            ]
            if not positions_to_close:
                return "requested"
            unresolved_submissions = [
                int(pos.get("ticket") or 0)
                for pos in positions_to_close
                if self._close_submission_unresolved(pos)
            ]
            if unresolved_submissions:
                self._set_sync_block(
                    strat,
                    "close_submission_result_unresolved",
                    {"tickets": sorted(unresolved_submissions)},
                    recoverable=False,
                )
                self._save_state()
                return "failed"
            for pos in positions_to_close:
                if not self._validate_live_position_before_close(strat, pos):
                    return "failed"
            st["pending_close_reason"] = reason
            st["pending_close_signal_bar"] = str(price_row.name)
            if reason == "basket_target" and self._basket_is_long(strat):
                self._arm_long_target_portfolio_rearm(strat, utc_now())
            self._save_state()
            for pos in positions_to_close:
                # Broker state can change while an earlier ticket CLOSE is in
                # flight. Re-prove the complete ownership tuple immediately
                # before every later ticket submission.
                if not self._validate_live_position_before_close(strat, pos):
                    return "failed"
                ticket = int(pos.get("ticket") or 0)
                pos["close_submission_started_utc"] = dt_text(utc_now())
                self._save_state()
                close_result = self.executor.close_position(
                    ticket,
                    int(self.params.get("deviation_points", 50)),
                    expected_login=int(MT5_LOGIN),
                    expected_server=str(MT5_SERVER),
                    expected_symbol=str(pos["owner_symbol"]),
                    expected_magic=int(pos["owner_magic"]),
                    expected_comment=str(pos["owner_comment"]),
                    expected_identifier=int(pos["position_identifier"]),
                )
                if not close_result:
                    close_status = str(getattr(close_result, "status", "FAILED"))
                    definitive_no_fill = self._close_result_definitive_no_fill(
                        close_result
                    )
                    if definitive_no_fill:
                        pos["close_submission_started_utc"] = None
                    if close_status in {"ACCOUNT_IDENTITY_GUARD", "ACCOUNT_MODE_GUARD", "POSITION_OWNERSHIP_GUARD"}:
                        block_reason = (
                            "account_identity_mismatch"
                            if close_status == "ACCOUNT_IDENTITY_GUARD"
                            else (
                                "account_margin_mode_mismatch"
                                if close_status == "ACCOUNT_MODE_GUARD"
                                else "position_ownership_guard_rejected"
                            )
                        )
                        self._set_sync_block(
                            strat,
                            block_reason,
                            {"ticket": ticket, "atomic_close_guard": close_status},
                            recoverable=False,
                        )
                        self._save_state()
                        return "failed"
                    if self._record_close_trade_permission_reject(
                        strat, close_result, price_row.name,
                    ):
                        return "trade_permission_rejected"
                    if close_status == "MARKET_CLOSED":
                        has_submitted_close = any(
                            bool(position.get("close_requested"))
                            for position in self._basket_rows(strat)
                        )
                        if not has_submitted_close:
                            st["pending_close_reason"] = None
                            st["pending_close_signal_bar"] = None
                            if reason == "basket_target" and self._basket_is_long(strat):
                                routing = self.state["routing"]
                                had_pending_rearm = (
                                    routing.get("long_target_rearm_pending_confirmation") is True
                                )
                                self._refresh_long_target_rearm_pending_summary(
                                    exclude_lane_id=int(strat["lane_id"]),
                                    exclude_basket_id=st.get("current_basket_id"),
                                )
                                if (
                                    had_pending_rearm
                                    and routing.get("long_target_rearm_pending_confirmation") is False
                                ):
                                    self._trade_row(
                                        "portfolio_rearm_cancelled",
                                        strat,
                                        side="LONG",
                                        reason="target_close_definitive_no_fill",
                                        note=(
                                            f"basket_id={st.get('current_basket_id')};"
                                            f"ticket={ticket};retcode={getattr(close_result, 'retcode', None)}"
                                        ),
                                    )
                        self._trade_row(
                            "time_close_deferred", strat, ticket=ticket,
                            position_identifier=int(pos.get("position_identifier")),
                            reason="market_closed_10018", signal_bar_time=str(price_row.name),
                            note=f"retcode={getattr(close_result, 'retcode', None)}",
                        )
                        self._save_state()
                        return "market_closed"
                    if not definitive_no_fill:
                        self._set_sync_block(
                            strat,
                            "close_submission_result_unresolved",
                            {"tickets": [ticket], "status": close_status},
                            recoverable=False,
                        )
                        self._save_state()
                        return "failed"
                    block_reason = "live_time_close_unconfirmed" if close_status in {"MISSING_UNCONFIRMED", "MALFORMED_OK"} else "live_time_close_failed"
                    self._set_sync_block(
                        strat, block_reason,
                        {"ticket": ticket, "status": close_status, "retcode": getattr(close_result, "retcode", None)},
                        recoverable=True,
                    )
                    self._save_state()
                    return "failed"
                self._clear_trade_permission_reject_state(strat)
                pos["close_requested"] = True
                # Persist each broker-confirmed submission before the next
                # ticket can be sent.  A process stop between tickets must not
                # lose the successful ticket marker and later duplicate CLOSE.
                self._save_state()
            self._trade_row("basket_close_requested", strat, profit=round(float(pnl), 2), reason=reason, signal_bar_time=str(price_row.name))
            self._save_state()
            return "requested"
        closed_basket_id = st.get("current_basket_id")
        closed_basket_was_long = self._basket_is_long(strat)
        closed_basket_was_reverse = self._validated_reverse_used(strat)
        closed_basket_atr30 = st.get("frozen_basket_atr30")
        bid = float(price_row["Close"])
        ask = float(price_row["AskOpen"])
        allocations = [
            self._shadow_close_allocation(
                pos, basket_id=closed_basket_id, bid=bid, ask=ask,
            )
            for pos in self._basket_rows(strat)
        ]
        self._trade_row(
            "basket_close", strat, profit=round(float(pnl), 2), reason=reason,
            signal_bar_time=str(price_row.name),
            _evaluation_allocations=allocations,
        )
        self._record_daily_realized(strat, pnl, price_row.name)
        if reason == "basket_target" and closed_basket_was_long:
            self._confirm_long_target_portfolio_rearm(strat, price_row.name, closed_basket_id)
        else:
            self._cancel_unconfirmed_long_target_rearm_after_other_close(
                strat, closed_basket_id, reason,
            )
        if reason == "basket_stop" and closed_basket_was_long and closed_basket_was_reverse:
            self._arm_trend_recovery_episode(strat, price_row.name, closed_basket_id, closed_basket_atr30)
        self._clear_basket_state(
            strat, reason, str(price_row.name), closed_at_utc=price_row.name,
        )
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
        admission_time: datetime | pd.Timestamp | None = None,
        opportunity: dict[str, Any] | None = None,
        apply_portfolio_rearm: bool = True,
        use_confirmed_fill_time: bool = False,
    ) -> bool:
        st = self._st(strat)
        if not bool(self.params.get("enabled", True)):
            self._trade_row(
                "entry_skip", strat, reason="bot_entries_disabled",
                signal_bar_time=str(price_row.name), note="global_entry_guard",
            )
            return False
        if not self.live_enabled and not self.shadow_enabled:
            self._trade_row(
                "entry_skip", strat, reason="execution_modes_disabled",
                signal_bar_time=str(price_row.name), note="live_and_shadow_disabled",
            )
            return False
        if not isinstance(side, str) or side not in {"LONG", "SHORT"}:
            self._trade_row(
                "entry_skip", strat, reason="invalid_entry_side",
                signal_bar_time=str(price_row.name), note="final_open_guard",
            )
            return False
        if not st["basket"]:
            basket_block = self._new_basket_block_reason(
                strat,
                execution_time if execution_time is not None else utc_now(),
            )
            if basket_block:
                self._trade_row(
                    "entry_skip",
                    strat,
                    reason=basket_block,
                    signal_bar_time=str(price_row.name),
                    note="final_za_new_basket_guard",
                )
                return False
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
        if st["basket"] and any(
            str(pos.get("side") or "") != side for pos in self._basket_rows(strat)
        ):
            self._trade_row(
                "entry_skip", strat, reason="opposite_side_inventory",
                signal_bar_time=str(price_row.name), note="final_open_guard",
            )
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
        if self.live_enabled:
            host_clock = utc_now()
            quote_time = self._broker_quote_time(info, host_clock)
            host_submit_time = pd.Timestamp(host_clock)
            host_submit_time = (
                host_submit_time.tz_localize("UTC")
                if host_submit_time.tzinfo is None
                else host_submit_time.tz_convert("UTC")
            )
            try:
                max_clock_minutes = float(
                    self.params.get("max_signal_delay_minutes", 2.0)
                )
            except (TypeError, ValueError, OverflowError):
                max_clock_minutes = math.nan
            max_clock_delta = (
                pd.Timedelta(minutes=max_clock_minutes)
                if math.isfinite(max_clock_minutes) and max_clock_minutes > 0.0
                else None
            )
            if (
                quote_time is None
                or max_clock_delta is None
                or abs(host_submit_time - quote_time) > max_clock_delta
            ):
                max_delta_text = (
                    f"{max_clock_delta.total_seconds():.0f}"
                    if max_clock_delta is not None
                    else "invalid"
                )
                self._trade_row(
                    "entry_skip",
                    strat,
                    reason="broker_quote_clock_out_of_bounds",
                    signal_bar_time=str(price_row.name),
                    note=(
                        f"host_submit_utc={dt_text(host_submit_time)};"
                        f"broker_quote_utc={dt_text(quote_time) if quote_time is not None else None};"
                        f"max_delta_seconds={max_delta_text}"
                    ),
                )
                return False
        broker_contract_error = self._broker_entry_contract_error(info, lot=lot, digits=digits)
        if broker_contract_error is not None:
            self._trade_row(
                "entry_skip", strat, reason="broker_symbol_contract_mismatch",
                signal_bar_time=str(price_row.name), note=broker_contract_error,
            )
            return False
        ticket = None
        confirmed = None
        confirmed_open_epoch = 0
        confirmed_open_time = None
        post_open_inventory_anomaly = None
        post_open_orders_unavailable = False
        if self.live_enabled:
            opportunity_id = str((opportunity or {}).get("opportunity_id") or "")
            reservation_time = pd.Timestamp(execution_time if execution_time is not None else utc_now())
            reservation_time = (
                reservation_time.tz_localize("UTC")
                if reservation_time.tzinfo is None
                else reservation_time.tz_convert("UTC")
            )
            st["pending_open_opportunity_id"] = opportunity_id or f"lane{strat['lane_id']}:{dt_text(utc_now())}"
            st["pending_open_started_utc"] = dt_text(reservation_time)
            st["pending_open_expires_utc"] = dt_text(
                reservation_time + pd.Timedelta(
                    minutes=float(self.params.get("max_signal_delay_minutes", 2.0))
                )
            )
            st["pending_open_side"] = side
            st["pending_open_lot"] = lot
            st["pending_open_symbol"] = symbol
            st["pending_open_magic"] = int(strat["magic"])
            st["pending_open_comment"] = str(strat["comment_prefix"])
            st["pending_open_signal_bar"] = str(price_row.name)
            st["pending_open_basket_atr30"] = (
                float(basket_atr30)
                if basket_atr30 is not None and math.isfinite(float(basket_atr30))
                else None
            )
            entry_policy = dict((opportunity or {}).get("entry_policy") or {})
            st["pending_open_reverse_used"] = bool(
                side == "LONG"
                and str(entry_policy.get("policy_id") or "") == EXPECTED_ENTRY_POLICY_ID
                and str(entry_policy.get("action") or "") == "reverse_long"
            )
            st["pending_open_expected_positions"] = len(self._basket_rows(strat))
            self._save_state()
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
            inventory_block = self._pre_open_live_inventory_block(strat)
            if inventory_block is not None:
                reason, details, recoverable = inventory_block
                self._clear_pending_open(strat)
                self._set_sync_block(
                    strat, reason, details, recoverable=recoverable,
                )
                self._save_state()
                self._trade_row(
                    "entry_skip",
                    strat,
                    opportunity_id=opportunity_id,
                    side=side,
                    lot=lot,
                    reason=reason,
                    signal_bar_time=str(price_row.name),
                    note="post_reservation_inventory_guard",
                )
                return False
            account_block = self._pre_open_account_block()
            if account_block is not None:
                reason, details, recoverable = account_block
                self._clear_pending_open(strat)
                if reason == "trade_permission_precheck_blocked":
                    st["autotrading_reject_streak"] = int(
                        st.get("autotrading_reject_streak", 0)
                    ) + 1
                    st["open_retry_after_utc"] = dt_text(
                        pd.Timestamp(utc_now())
                        + pd.Timedelta(
                            seconds=float(
                                self.params.get("trade_permission_retry_seconds", 30.0)
                            )
                        )
                    )
                    threshold = int(
                        self.params.get("trade_permission_alert_threshold", 3)
                    )
                    self._save_state()
                    if (
                        st["autotrading_reject_streak"] >= threshold
                        and not st.get("autotrading_reject_notified")
                    ):
                        delivered = self._notify_manual_action(
                            strat,
                            title="trade permission disabled repeatedly",
                            reason=reason,
                            action="Check MT5 AutoTrading and account trade permissions.",
                            key=f"bot23:trade-permission:{strat['id']}",
                        )
                        if delivered:
                            st["autotrading_reject_notified"] = True
                            self._save_state()
                elif reason == "account_execution_metadata_unavailable":
                    st["open_retry_after_utc"] = dt_text(
                        pd.Timestamp(utc_now())
                        + pd.Timedelta(
                            seconds=float(
                                self.params.get("trade_permission_retry_seconds", 30.0)
                            )
                        )
                    )
                else:
                    self._set_sync_block(
                        strat, reason, details, recoverable=recoverable,
                    )
                self._save_state()
                self._trade_row(
                    "entry_skip",
                    strat,
                    opportunity_id=opportunity_id,
                    side=side,
                    lot=lot,
                    reason=reason,
                    signal_bar_time=str(price_row.name),
                    note="post_reservation_account_guard",
                )
                return False
            # The durable reservation write is part of the submission path and
            # can cross a time-policy or quote-freshness boundary. Re-evaluate
            # those live guards at the actual broker-command boundary.
            actual_submit_time = pd.Timestamp(utc_now())
            actual_submit_time = (
                actual_submit_time.tz_localize("UTC")
                if actual_submit_time.tzinfo is None
                else actual_submit_time.tz_convert("UTC")
            )
            post_reservation_block = None
            if not st["basket"]:
                post_reservation_block = self._new_basket_block_reason(
                    strat, actual_submit_time,
                )
            if (
                post_reservation_block is None
                and not st["basket"]
                and apply_portfolio_rearm
            ):
                post_reservation_block = self._portfolio_new_long_basket_block_reason(
                    side, actual_submit_time,
                )
            actual_quote_time = self._broker_quote_time(info, actual_submit_time)
            if (
                post_reservation_block is None
                and (
                    actual_quote_time is None
                    or max_clock_delta is None
                    or abs(actual_submit_time - actual_quote_time) > max_clock_delta
                )
            ):
                post_reservation_block = "broker_quote_clock_out_of_bounds"
            if post_reservation_block is not None:
                self._clear_pending_open(strat)
                self._save_state()
                self._trade_row(
                    "entry_skip",
                    strat,
                    opportunity_id=opportunity_id,
                    side=side,
                    lot=lot,
                    reason=post_reservation_block,
                    signal_bar_time=str(price_row.name),
                    executable_at=dt_text(actual_submit_time),
                    note="post_reservation_final_guard",
                )
                return False
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
                expected_login=int(MT5_LOGIN),
                expected_server=str(MT5_SERVER),
                expected_owned_positions=int(st["pending_open_expected_positions"]),
            )
            if ticket is None:
                error = str(getattr(self.executor, "last_order_error", None) or "UNKNOWN_OPEN_FAILURE")
            else:
                error = ""
            if ticket is None and error in {
                "ERR|COMMAND_BUSY",
                "ERR|CLAIM_BUSY",
                "ERR|LOCK_TIMEOUT",
                "ERR|WRITE_FAILED",
                "ERR|CLAIM_FAILED",
                "ERR|REQUEST_EXPIRED",
                "ERR|RESPONSE_BUSY",
                "ERR|INVALID_TIMEOUT",
            }:
                self._clear_pending_open(strat)
                self._set_sync_block(
                    strat,
                    "ipc_open_not_published",
                    {"error": error},
                    recoverable=False,
                )
                self._save_state()
                return True
            if ticket is None and error in {
                "ERR|ACCOUNT_IDENTITY_GUARD",
                "ERR|ACCOUNT_MODE_GUARD",
            }:
                reason = (
                    "account_identity_mismatch"
                    if error == "ERR|ACCOUNT_IDENTITY_GUARD"
                    else "account_margin_mode_mismatch"
                )
                self._clear_pending_open(strat)
                self._set_sync_block(
                    strat,
                    reason,
                    {"atomic_open_guard": error.split("|", 1)[1]},
                    recoverable=False,
                )
                self._save_state()
                return True
            if ticket is None and error in {
                "INVALID_OPEN_REQUEST",
                "OPEN_POLICY_GUARD",
                "ERR|OPEN_POLICY_GUARD",
                "ERR|SYMBOL_ADMISSION_GUARD",
                "ERR|MARGIN_ADMISSION_GUARD",
                "ERR|OPEN_INVENTORY_GUARD",
                "ERR|OPEN_INVENTORY_QUERY",
                "ERR|OPEN_ORDER_QUERY",
                "ERR|BAD_OPEN_GUARD",
                "ERR|BAD_OPEN_TYPE",
            }:
                self._clear_pending_open(strat)
                self._set_sync_block(
                    strat,
                    "open_command_contract_rejected",
                    {"error": error},
                    recoverable=False,
                )
                self._save_state()
                return True
            positions = self.executor.get_positions(symbol, int(strat["magic"]))
            if positions is None:
                self._set_sync_block(strat, "positions_unavailable_after_open", {"ticket": int(ticket or 0), "error": error}, recoverable=True)
                self._save_state()
                return True
            post_open_orders = self.executor.get_orders(symbol, int(strat["magic"]))
            post_open_orders_unavailable = post_open_orders is None
            post_open_orders = [] if post_open_orders is None else post_open_orders
            owned = [pos for pos in positions if self._owned_position(strat, pos)]
            unexpected_same_magic = [
                pos for pos in positions if not self._owned_position(strat, pos)
            ]
            owned_identity = [
                (pos, self._live_position_identity(pos))
                for pos in owned
            ]
            if any(identity is None for _pos, identity in owned_identity):
                self._set_sync_block(
                    strat,
                    "live_position_identity_invalid",
                    {"live_rows": len(owned)},
                    recoverable=False,
                )
                self._save_state()
                return True
            live_tickets = [
                int(identity[0])
                for _pos, identity in owned_identity
                if identity is not None
            ]
            live_position_ids = [
                int(identity[1])
                for _pos, identity in owned_identity
                if identity is not None
            ]
            duplicate_tickets = sorted(
                {value for value in live_tickets if live_tickets.count(value) > 1}
            )
            duplicate_position_ids = sorted(
                {
                    value
                    for value in live_position_ids
                    if live_position_ids.count(value) > 1
                }
            )
            if duplicate_tickets or duplicate_position_ids:
                self._set_sync_block(
                    strat,
                    "live_position_identity_invalid",
                    {
                        "duplicate_tickets": duplicate_tickets,
                        "duplicate_position_ids": duplicate_position_ids,
                    },
                    recoverable=False,
                )
                self._save_state()
                return True
            known_ids = {
                int(pos["position_identifier"])
                for pos in self._basket_rows(strat)
                if isinstance(pos, dict)
            }
            new_owned = [
                pos
                for pos, identity in owned_identity
                if identity is not None and identity[1] not in known_ids
            ]
            def observed_ticket_list(records: list[Any]) -> list[int]:
                tickets: set[int] = set()
                for record in records:
                    try:
                        observed_ticket = int(getattr(record, "ticket", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if observed_ticket > 0:
                        tickets.add(observed_ticket)
                return sorted(tickets)

            definitive_permission_reject = bool(
                error.startswith("ERR|10026")
                or error.startswith("ERR|10027")
                or error == "ERR|TRADE_PERMISSION_GUARD"
            )
            definitive_market_closed_reject = bool(
                error == "ERR|10018" or error.startswith("ERR|10018|")
            )
            if (
                ticket is None
                and (definitive_market_closed_reject or definitive_permission_reject)
                and (new_owned or unexpected_same_magic or post_open_orders)
            ):
                self._clear_pending_open(strat)
                self._set_sync_block(
                    strat,
                    "definitive_open_reject_with_untracked_inventory",
                    {
                        "error": error,
                        "observed_tickets": observed_ticket_list(
                            [*new_owned, *unexpected_same_magic]
                        ),
                        "observed_order_tickets": observed_ticket_list(post_open_orders),
                    },
                    recoverable=False,
                )
                self._save_state()
                return True
            if ticket is not None:
                matches = [
                    pos
                    for pos in new_owned
                    if self._live_position_identity(pos) is not None
                    and int(ticket) in self._live_position_identity(pos)
                ]
                if len(matches) != 1:
                    self._set_sync_block(strat, "open_success_position_not_confirmed", {"ticket": int(ticket)}, recoverable=False)
                    self._save_state()
                    return True
                confirmed = matches[0]
                confirmed_ticket_identity = self._live_position_identity(confirmed)
                confirmed_position_identifier = (
                    int(confirmed_ticket_identity[1])
                    if confirmed_ticket_identity is not None
                    else 0
                )
                extra_new_owned = [
                    pos
                    for pos in new_owned
                    if self._live_position_identity(pos) is not None
                    and int(self._live_position_identity(pos)[1])
                    != confirmed_position_identifier
                ]
                unexpected_tickets = observed_ticket_list(
                    [*extra_new_owned, *unexpected_same_magic]
                )
                unexpected_order_tickets = observed_ticket_list(post_open_orders)
                if unexpected_tickets or unexpected_order_tickets:
                    post_open_inventory_anomaly = {
                        "confirmed_ticket": int(ticket),
                        "unexpected_tickets": unexpected_tickets,
                        "unexpected_order_tickets": unexpected_order_tickets,
                    }
            elif not new_owned and (error == "ERR|10018" or error.startswith("ERR|10018|")):
                retry_clock = pd.Timestamp(
                    admission_time if admission_time is not None else reservation_time
                )
                retry_clock = (
                    retry_clock.tz_localize("UTC")
                    if retry_clock.tzinfo is None
                    else retry_clock.tz_convert("UTC")
                )
                st["open_retry_after_utc"] = dt_text(
                    retry_clock
                    + pd.Timedelta(
                        seconds=float(
                            self.params.get("fixed_hold_market_closed_retry_seconds", 60.0)
                        )
                    )
                )
                self._clear_pending_open(strat)
                self._save_state()
                self._trade_row(
                    "entry_skip",
                    strat,
                    reason="market_closed_open_rejected",
                    signal_bar_time=str(price_row.name),
                    note=f"retry_after={st['open_retry_after_utc']}",
                )
                return True
            elif not new_owned and (error.startswith("ERR|10026") or error.startswith("ERR|10027")):
                st["autotrading_reject_streak"] = int(st.get("autotrading_reject_streak", 0)) + 1
                st["open_retry_after_utc"] = dt_text(
                    reservation_time
                    + pd.Timedelta(seconds=float(self.params.get("trade_permission_retry_seconds", 30.0)))
                )
                self._clear_pending_open(strat)
                threshold = int(self.params.get("trade_permission_alert_threshold", 3))
                self._save_state()
                if st["autotrading_reject_streak"] >= threshold and not st.get("autotrading_reject_notified"):
                    delivered = self._notify_manual_action(strat, title="trade permission rejected repeatedly", reason=error, action="Check MT5 AutoTrading and account trade permissions.", key=f"bot23:trade-permission:{strat['id']}")
                    if delivered:
                        st["autotrading_reject_notified"] = True
                        self._save_state()
                self._trade_row("entry_skip", strat, reason="trade_permission_rejected", signal_bar_time=str(price_row.name), note=f"streak={st['autotrading_reject_streak']}")
                return True
            elif not new_owned and error == "ERR|TRADE_PERMISSION_GUARD":
                st["autotrading_reject_streak"] = int(st.get("autotrading_reject_streak", 0)) + 1
                st["open_retry_after_utc"] = dt_text(
                    reservation_time
                    + pd.Timedelta(seconds=float(self.params.get("trade_permission_retry_seconds", 30.0)))
                )
                self._clear_pending_open(strat)
                threshold = int(self.params.get("trade_permission_alert_threshold", 3))
                self._save_state()
                if st["autotrading_reject_streak"] >= threshold and not st.get("autotrading_reject_notified"):
                    delivered = self._notify_manual_action(
                        strat,
                        title="trade permission rejected repeatedly",
                        reason=error,
                        action="Check MT5 AutoTrading and account trade permissions.",
                        key=f"bot23:trade-permission:{strat['id']}",
                    )
                    if delivered:
                        st["autotrading_reject_notified"] = True
                        self._save_state()
                self._trade_row(
                    "entry_skip", strat, reason="trade_permission_rejected",
                    signal_bar_time=str(price_row.name),
                    note=f"atomic_guard_streak={st['autotrading_reject_streak']}",
                )
                return True
            elif (
                len(new_owned) == 1
                and not unexpected_same_magic
                and not post_open_orders
            ):
                confirmed = new_owned[0]
                ticket = int(confirmed.ticket)
            else:
                reason = "ambiguous_open_result_positions" if new_owned else "ambiguous_open_result"
                self._set_sync_block(strat, reason, {"tickets": [int(pos.ticket) for pos in new_owned], "error": error}, recoverable=False)
                self._save_state()
                return True
            try:
                confirmed_identity = self._live_position_identity(confirmed)
                if confirmed_identity is None:
                    raise ValueError("missing live position identity")
                confirmed_ticket, confirmed_position_id = confirmed_identity
                confirmed_type = int(confirmed.type)
                confirmed_lot = float(confirmed.volume)
                confirmed_entry_price = float(confirmed.open_price)
                confirmed_open_epoch = int(getattr(confirmed, "open_time", 0) or 0)
            except (TypeError, ValueError, OverflowError, AttributeError):
                confirmed_position_id = 0
                confirmed_type = -1
                confirmed_lot = math.nan
                confirmed_entry_price = math.nan
                confirmed_open_epoch = 0
            pending_started = parse_ts(st.get("pending_open_started_utc"))
            confirmed_open_msc = int(
                getattr(confirmed, "open_time_msc", confirmed_open_epoch * 1000) or 0
            ) if confirmed is not None else 0
            confirmed_open_time = (
                pd.Timestamp(confirmed_open_msc, unit="ms", tz="UTC")
                if confirmed_open_msc > 0
                else None
            )
            confirmed_time_outside_submission = bool(
                confirmed_open_time is not None
                and (
                    pending_started is None
                    or confirmed_open_time < pending_started.floor("s") - pd.Timedelta(seconds=2)
                    or confirmed_open_time > pending_started + pd.Timedelta(
                        minutes=float(self.params.get("max_signal_delay_minutes", 2.0))
                    )
                )
            )
            if (
                confirmed_position_id <= 0
                or confirmed_type != order_type
                or not math.isfinite(confirmed_lot)
                or not math.isclose(confirmed_lot, lot, rel_tol=0.0, abs_tol=1e-9)
                or not math.isfinite(confirmed_entry_price)
                or confirmed_entry_price <= 0.0
                or confirmed_time_outside_submission
            ):
                self._set_sync_block(
                    strat,
                    "open_confirmation_mismatch",
                    {
                        "ticket": int(getattr(confirmed, "ticket", 0) or 0),
                        "position_identifier": confirmed_position_id,
                        "expected_type": order_type,
                        "observed_type": confirmed_type,
                        "expected_lot": lot,
                        "observed_lot": str(getattr(confirmed, "volume", None)),
                        "observed_entry_price": str(getattr(confirmed, "open_price", None)),
                        "pending_open_started_utc": st.get("pending_open_started_utc"),
                        "observed_open_time": str(getattr(confirmed, "open_time", None)),
                    },
                    recoverable=False,
                )
                self._save_state()
                return True
            entry_price = confirmed_entry_price
            st["autotrading_reject_streak"] = 0
            st["autotrading_reject_notified"] = False
            st["open_retry_after_utc"] = None
        if not st["basket"]:
            st["basket_sequence"] = int(st.get("basket_sequence") or 0) + 1
            st["current_basket_id"] = f"L{int(strat['lane_id'])}-B{int(st['basket_sequence']):06d}"
        opportunity_id = str((opportunity or {}).get("opportunity_id") or st.get("pending_entry_opportunity_id") or "")
        entry_time = execution_time if execution_time is not None else (parse_ts(price_row.name) or pd.Timestamp(utc_now())) + pd.Timedelta(minutes=1)
        fill_time_unavailable = bool(self.live_enabled and confirmed_open_epoch <= 0)
        if self.live_enabled and confirmed_open_time is not None:
            entry_time = confirmed_open_time
        elif fill_time_unavailable:
            entry_time = None
        st["basket"].append(
            {
                "ticket": confirmed_ticket if confirmed is not None else ticket,
                "position_identifier": confirmed_position_id if confirmed is not None else 0,
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
            entry_policy = dict((opportunity or {}).get("entry_policy") or {})
            st["reverse_used"] = bool(
                side == "LONG"
                and str(entry_policy.get("policy_id") or "") == EXPECTED_ENTRY_POLICY_ID
                and str(entry_policy.get("action") or "") == "reverse_long"
            )
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
                {"ticket": int(ticket or 0), "position_identifier": confirmed_position_id},
                recoverable=True,
            )
            self._trade_row(
                "position_lifecycle_deferred",
                strat,
                ticket=ticket or "",
                position_identifier=confirmed_position_id,
                reason="confirmed_fill_time_unavailable",
                signal_bar_time=str(price_row.name),
                note="poll time was not substituted; next exact owned-position sync may recover broker open_time",
            )
        if post_open_inventory_anomaly is not None:
            self._set_sync_block(
                strat,
                "post_open_owned_inventory_delta_invalid",
                post_open_inventory_anomaly,
                recoverable=False,
            )
        elif post_open_orders_unavailable:
            self._set_sync_block(
                strat,
                "orders_unavailable_after_open",
                {"confirmed_ticket": int(ticket or 0)},
                recoverable=True,
            )
        # Broker-confirmed lifecycle state is authoritative. Persist it before
        # the non-authoritative CSV audit so an audit I/O failure cannot leave
        # a real position outside the durable owned basket.
        self._save_state()
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
        return True

    def _broker_entry_contract_error(self, info: Any, *, lot: float, digits: int) -> str | None:
        """Validate the real MT5 symbol contract without affecting exits."""
        if not self.live_enabled or not isinstance(info, SymbolInfo):
            return None
        try:
            volume_min = float(info.volume_min)
            volume_max = float(info.volume_max)
            volume_step = float(info.volume_step)
            broker_point = float(info.point)
            broker_contract = float(info.contract_size)
            broker_tick_value = float(info.tick_value)
            broker_tick_size = float(info.tick_size)
            margin_free = float(info.margin_free)
            configured_point = float(self.params.get("point_size", 0.0))
            configured_contract = float(self.params.get("contract_size", 0.0))
            values = (lot, volume_min, volume_max, volume_step, broker_point, configured_point, broker_contract, broker_tick_value, broker_tick_size, margin_free, configured_contract)
            if not all(math.isfinite(value) for value in values):
                return "nonfinite_symbol_contract"
            if volume_min <= 0.0 or volume_max < volume_min or volume_step <= 0.0:
                return "invalid_volume_contract"
            if lot < volume_min - 1e-12 or lot > volume_max + 1e-12:
                return f"lot_out_of_range:lot={lot};min={volume_min};max={volume_max}"
            steps = (lot - volume_min) / volume_step
            if not math.isclose(steps, round(steps), rel_tol=0.0, abs_tol=1e-9):
                return f"lot_off_step:lot={lot};min={volume_min};step={volume_step}"
            if int(info.digits) != int(digits):
                return f"digits_mismatch:configured={digits};broker={int(info.digits)}"
            if configured_point <= 0.0 or not math.isclose(
                broker_point, configured_point, rel_tol=0.0,
                abs_tol=max(1e-12, configured_point * 1e-9),
            ):
                return f"point_mismatch:configured={configured_point};broker={broker_point}"
            if configured_contract <= 0.0 or not math.isclose(
                broker_contract, configured_contract, rel_tol=0.0, abs_tol=1e-9,
            ):
                return f"contract_size_mismatch:configured={configured_contract};broker={broker_contract}"
            expected_tick_value = broker_tick_size * broker_contract
            if not math.isclose(
                broker_tick_value, expected_tick_value, rel_tol=1e-6,
                abs_tol=max(1e-9, expected_tick_value * 1e-6),
            ):
                return f"tick_value_mismatch:expected={expected_tick_value};broker={broker_tick_value}"
            if margin_free <= 0.0:
                return "free_margin_unavailable"
            if int(info.trade_mode) != 4 or (int(info.order_mode) & 1) == 0:
                return f"symbol_not_full_market:trade_mode={int(info.trade_mode)};order_mode={int(info.order_mode)}"
        except (TypeError, ValueError, OverflowError, AttributeError):
            return "symbol_contract_unavailable"
        return None

    @staticmethod
    def _account_identity_error(account: dict[str, Any]) -> str | None:
        observed_login = account.get("login")
        observed_server = str(account.get("server") or "")
        observed_currency = str(account.get("currency") or "")
        if observed_login is None or not observed_server or not observed_currency:
            return "account_identity_unavailable; recompile and attach the current BotBridge_s23"
        try:
            login_matches = int(observed_login) == int(MT5_LOGIN)
        except (TypeError, ValueError, OverflowError):
            return "account_identity_invalid"
        server_matches = observed_server.casefold() == str(MT5_SERVER).casefold()
        currency_matches = observed_currency == "USD"
        if not login_matches or not server_matches or not currency_matches:
            return (
                "account_identity_mismatch:"
                f"login_match={str(login_matches).lower()};"
                f"server_match={str(server_matches).lower()}"
                f";currency_match={str(currency_matches).lower()}"
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

    def _broker_quote_time(
        self, info: Any, poll_time: datetime | pd.Timestamp,
    ) -> pd.Timestamp | None:
        quote_time_msc = getattr(info, "quote_time_msc", None)
        if quote_time_msc is not None:
            try:
                value = int(quote_time_msc)
                return pd.Timestamp(value, unit="ms", tz="UTC") if value > 0 else None
            except (TypeError, ValueError, OverflowError):
                return None
        if self.live_enabled:
            return None
        stamp = pd.Timestamp(poll_time)
        return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")

    def _set_market_closed_close_retry(
        self, strat: dict[str, Any], quote_time: pd.Timestamp,
    ) -> None:
        self._st(strat)["time_close_retry_after_utc"] = dt_text(
            quote_time + pd.Timedelta(
                seconds=float(
                    self.params.get("fixed_hold_market_closed_retry_seconds", 60.0)
                )
            )
        )
        self._save_state()

    def _validated_time_close_retry_after(
        self,
        strat: dict[str, Any],
        quote_time: pd.Timestamp | None,
    ) -> pd.Timestamp | None:
        """Return a writer-bounded close retry or reset corrupt delay state."""
        st = self._st(strat)
        raw_retry_after = st.get("time_close_retry_after_utc")
        retry_after = (
            parse_ts(raw_retry_after)
            if isinstance(raw_retry_after, str)
            else None
        )
        retry_seconds = max(
            float(self.params.get("fixed_hold_market_closed_retry_seconds", 60.0)),
            float(self.params.get("trade_permission_retry_seconds", 30.0)),
        )
        retry_bound = (
            quote_time
            + pd.Timedelta(
                seconds=retry_seconds
            )
            if quote_time is not None
            else None
        )
        valid = bool(
            raw_retry_after is None
            or (
                isinstance(raw_retry_after, str)
                and retry_after is not None
                and (retry_bound is None or retry_after <= retry_bound)
            )
        )
        if valid:
            return retry_after
        st["time_close_retry_after_utc"] = None
        self._trade_row(
            "position_lifecycle_recovered",
            strat,
            reason="time_close_retry_state_invalid_reset",
            note=(
                f"previous_retry={raw_retry_after!r};"
                f"quote_time={dt_text(quote_time) if quote_time is not None else None};"
                f"max_future={dt_text(retry_bound) if retry_bound is not None else None}"
            ),
        )
        self._save_state()
        return None

    def _monitor_open_basket(self, strat: dict[str, Any], info: Any, price_row: pd.Series, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        st = self._st(strat)
        if st.get("pending_close_reason"):
            return True
        if not st["basket"]:
            return False
        at_utc = pd.Timestamp(poll_time if poll_time is not None else utc_now())
        at_utc = at_utc.tz_localize("UTC") if at_utc.tzinfo is None else at_utc.tz_convert("UTC")
        quote_time = self._broker_quote_time(info, at_utc)
        retry_after = self._validated_time_close_retry_after(strat, quote_time)
        if retry_after is not None:
            if quote_time is None or quote_time < retry_after:
                return True
            st["time_close_retry_after_utc"] = None
            self._save_state()
        bid = float(getattr(info, "bid", price_row["Close"]))
        ask = float(getattr(info, "ask", price_row.get("AskOpen", price_row["Open"])))
        pnl = self._basket_pnl(strat, bid, ask)
        entries = [parse_ts(pos.get("entry_time_utc")) for pos in st["basket"]]
        valid_entries = [stamp for stamp in entries if stamp is not None]
        if not valid_entries:
            self._set_sync_block(strat, "state_entry_time_invalid", recoverable=False)
            self._save_state()
            return True
        lifecycle_time = quote_time
        if lifecycle_time is None and not self.live_enabled:
            lifecycle_time = at_utc
        held = max(
            0,
            int(
                (
                    (lifecycle_time if lifecycle_time is not None else min(valid_entries))
                    - min(valid_entries)
                ).total_seconds()
                // 60
            ),
        )
        previous_peak = st.get("basket_peak_pnl_usd")
        previous_peak_is_number = (
            isinstance(previous_peak, (int, float))
            and not isinstance(previous_peak, bool)
        )
        previous_peak_value = float(previous_peak) if previous_peak_is_number else math.nan
        if previous_peak is None or not math.isfinite(previous_peak_value):
            peak = float(pnl)
            if previous_peak is not None:
                self._trade_row(
                    "position_lifecycle_recovered",
                    strat,
                    reason="basket_peak_pnl_invalid_reset",
                    note=f"previous_peak={previous_peak!r};reset_peak={peak}",
                )
        else:
            peak = max(previous_peak_value, float(pnl))
        st["basket_peak_pnl_usd"] = peak
        raw_frozen_atr30 = st.get("frozen_basket_atr30")
        frozen_atr30_is_number = (
            isinstance(raw_frozen_atr30, (int, float))
            and not isinstance(raw_frozen_atr30, bool)
        )
        frozen_atr30 = (
            None
            if raw_frozen_atr30 is None
            else float(raw_frozen_atr30) if frozen_atr30_is_number else math.nan
        )
        if raw_frozen_atr30 is not None and (
            not math.isfinite(frozen_atr30)
            or frozen_atr30 <= 0.0
        ):
            st["frozen_basket_atr30"] = None
            self._trade_row(
                "position_lifecycle_recovered",
                strat,
                reason="frozen_basket_atr30_invalid_reset",
                note=f"previous_atr30={raw_frozen_atr30!r};fallback=fixed_exit_thresholds",
            )
            self._save_state()
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
            row.name = quote_time if quote_time is not None else at_utc
            result = self._close_basket(strat, reason, row, pnl)
            if result == "market_closed" and quote_time is not None:
                self._set_market_closed_close_retry(strat, quote_time)
            return True
        return False

    def _monitor_fixed_hold_position(
        self,
        strat: dict[str, Any],
        info: Any,
        poll_time: datetime | pd.Timestamp | None,
        close_reason: str,
        *,
        defer_for_spread: bool = True,
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
        raw_retry_after = st.get("time_close_retry_after_utc")
        retry_after = self._validated_time_close_retry_after(strat, quote_time)
        raw_defer_started = st.get("time_close_defer_started_utc")
        defer_started = parse_ts(raw_defer_started) if isinstance(raw_defer_started, str) else None
        raw_last_quote_msc = st.get("time_close_last_quote_msc")
        raw_stable_count = st.get("time_close_stable_count")
        raw_wide_seen = st.get("time_close_wide_seen")
        try:
            last_quote_msc = (
                None if raw_last_quote_msc is None else int(raw_last_quote_msc)
            )
            stable_count = int(raw_stable_count)
            close_state_valid = (
                (
                    raw_last_quote_msc is None
                    or (
                        isinstance(raw_last_quote_msc, int)
                        and not isinstance(raw_last_quote_msc, bool)
                        and last_quote_msc >= 0
                        and last_quote_msc <= quote_time_msc
                    )
                )
                and not isinstance(raw_stable_count, bool)
                and isinstance(raw_stable_count, int)
                and stable_count >= 0
                and isinstance(raw_wide_seen, bool)
                and (
                    raw_defer_started is None
                    or (
                        isinstance(raw_defer_started, str)
                        and defer_started is not None
                        and defer_started <= quote_time
                    )
                )
                and (
                    (raw_wide_seen and defer_started is not None)
                    or (
                        not raw_wide_seen
                        and defer_started is None
                        and stable_count == 0
                    )
                )
            )
        except (TypeError, ValueError, OverflowError):
            last_quote_msc = None
            stable_count = 0
            close_state_valid = False
        if not close_state_valid:
            st["time_close_defer_started_utc"] = None
            st["time_close_last_quote_msc"] = None
            st["time_close_stable_count"] = 0
            st["time_close_wide_seen"] = False
            defer_started = None
            last_quote_msc = None
            stable_count = 0
            self._trade_row(
                "position_lifecycle_recovered",
                strat,
                reason="fixed_hold_close_state_invalid_reset",
                signal_bar_time=str(due),
                note=(
                    f"retry={raw_retry_after!r};defer={raw_defer_started!r};"
                    f"last_quote={raw_last_quote_msc!r};stable={raw_stable_count!r};"
                    f"wide={raw_wide_seen!r}"
                ),
            )
            self._save_state()
        if not defer_for_spread:
            # Q01's frozen lifecycle exits on the first executable quote even
            # when spread is wide. Discard any stale generic defer residue.
            st["time_close_defer_started_utc"] = None
            st["time_close_stable_count"] = 0
            st["time_close_wide_seen"] = False
            defer_started = None
            stable_count = 0
        if retry_after is not None and quote_time < retry_after:
            return True
        if last_quote_msc is not None and quote_time_msc <= last_quote_msc:
            return True
        st["time_close_last_quote_msc"] = quote_time_msc
        bid = float(getattr(info, "bid"))
        ask = float(getattr(info, "ask"))
        point = float(self.params.get("point_size", 0.001))
        spread_points = max(0.0, ask - bid) / point if point > 0 else math.inf
        spread_cap = (
            float(self.params.get("fixed_hold_close_max_spread_points", 300.0))
            if defer_for_spread
            else math.inf
        )
        wide_seen = bool(st.get("time_close_wide_seen"))
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
                    st["time_close_stable_count"] = stable_count + 1
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

    def _monitor_pre_eu30_position(self, strat: dict[str, Any], info: Any, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        return self._monitor_fixed_hold_position(strat, info, poll_time, "pre_eu30_fixed_hold")

    def _monitor_session_vwap_position(self, strat: dict[str, Any], info: Any, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        return self._monitor_fixed_hold_position(strat, info, poll_time, "session_vwap_fixed_hold")

    def _session_vwap_quote_time(
        self, info: Any, poll_time: datetime | pd.Timestamp,
    ) -> pd.Timestamp | None:
        return self._broker_quote_time(info, poll_time)

    def _refresh_session_vwap_history(self, info: Any, poll_time: pd.Timestamp) -> None:
        """Advance history acquisition without admitting an order."""
        if not bool(self.params.get("session_vwap_enabled", False)):
            self._session_vwap_snapshot = None
            return
        quote_time = self._session_vwap_quote_time(info, poll_time)
        if quote_time is None:
            self._session_vwap_snapshot = None
            return
        self._session_vwap_snapshot = self.session_vwap_history.advance(quote_time)
        if self._session_vwap_snapshot.reason == "completed_bar_revision_conflict":
            details = {
                "quote_time": dt_text(quote_time),
                "history_reason": self._session_vwap_snapshot.reason,
            }
            for strat in self._session_vwap_strategies():
                self._set_sync_block(
                    strat,
                    "session_vwap_completed_bar_revision_conflict",
                    details,
                    recoverable=False,
                )
            self._save_state()

    def _process_session_vwap_exits(self, info: Any, poll_time: pd.Timestamp) -> dict[int, bool]:
        readiness: dict[int, bool] = {}
        quote_time = self._session_vwap_quote_time(info, poll_time)
        session_active = bool(
            self.params.get("session_vwap_enabled", False)
            and quote_time is not None
            and in_session_vwap_entry_session(quote_time)
        )
        for strat in self._session_vwap_strategies():
            entry_enabled = bool(strat.get("enabled", True))
            lane_id = int(strat["lane_id"])
            st = self._st(strat)
            needs_reconciliation = bool(
                session_active or st.get("basket") or st.get("pending_open_opportunity_id")
                or st.get("pending_close_reason") or st.get("sync_block_new_entries")
                or st.get("session_vwap_retry_opportunity")
            )
            if not needs_reconciliation:
                readiness[lane_id] = False
                continue
            if not self._sync_strategy(strat):
                self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="session_vwap_sync_block")
                self._save_state()
                readiness[lane_id] = False
                continue
            exit_blocked = self._monitor_session_vwap_position(strat, info, poll_time)
            readiness[lane_id] = entry_enabled and not exit_blocked
        return readiness

    def _process_session_vwap_retries(
        self,
        info: Any,
        admission_time: pd.Timestamp,
        readiness: dict[int, bool],
        *,
        execution_time: pd.Timestamp | None = None,
    ) -> None:
        """Retry persisted, previously submitted session-VWAP opportunities.

        The original signal identity is lane-local so a newer completed M1 or
        a runner restart cannot silently replace it. Ambiguous OPEN outcomes
        remain blocked by pending_open/sync state and are never resent until a
        later owned-inventory reconciliation proves the lane safe again.
        """
        point = float(self.params.get("point_size", 0.001))
        spread_points = max(0.0, float(info.ask) - float(info.bid)) / point if point > 0 else math.inf
        spread_cap = float(self.params.get("max_entry_spread_points", 300.0))
        for strat in self._session_vwap_strategies():
            if not bool(strat.get("enabled", True)):
                continue
            st = self._st(strat)
            retry = st.get("session_vwap_retry_opportunity")
            if retry is None:
                continue
            if not isinstance(retry, dict):
                st["session_vwap_retry_opportunity"] = None
                self._trade_row(
                    "session_vwap_decision",
                    strat,
                    reason="retry_state_invalid",
                    note=f"non_object_retry={retry!r};discarded",
                )
                self._save_state()
                continue
            identity = self._session_vwap_retry_identity(retry)
            opportunity = identity["opportunity"]
            signal_bar = identity["signal_bar"]
            release_time = identity["release_time"]
            expires = identity["expires"]
            side = str(identity["side"])
            opportunity_id = str(identity["opportunity_id"])
            invalid = not bool(identity["valid"])
            if not invalid and admission_time < release_time:
                # A canonical persisted retry may be loaded before its M1 is
                # executable after a clock rollback. Preserve it untouched
                # until the original release boundary is reached.
                continue
            closed_cutoff, closed_state_invalid = self._session_vwap_closed_cutoff(side, admission_time)
            stale_after_close = bool(
                not invalid
                and not closed_state_invalid
                and closed_cutoff is not None
                and release_time <= closed_cutoff
            )
            if invalid or closed_state_invalid or admission_time > expires or stale_after_close:
                st["session_vwap_retry_opportunity"] = None
                self._trade_row(
                    "session_vwap_decision",
                    strat,
                    opportunity_id=opportunity_id,
                    side=side,
                    reason=(
                        "retry_state_invalid" if invalid
                        else "last_closed_state_invalid" if closed_state_invalid
                        else "stale_same_direction_after_close" if stale_after_close
                        else "retry_expired"
                    ),
                    signal_bar_time=dt_text(signal_bar) if signal_bar is not None else retry.get("signal_bar_time"),
                )
                self._save_state()
                continue
            if any(
                str(pos.get("opportunity_id") or "") == opportunity_id
                for pos in self._basket_rows(strat)
            ):
                st["session_vwap_retry_opportunity"] = None
                self._save_state()
                continue
            if (
                st.get("pending_open_opportunity_id")
                or st.get("sync_block_new_entries")
                or not readiness.get(int(strat["lane_id"]), False)
                or spread_points > spread_cap
            ):
                continue
            raw_retry_after = st.get("open_retry_after_utc")
            retry_after = (
                parse_ts(raw_retry_after)
                if isinstance(raw_retry_after, str)
                else None
            )
            if raw_retry_after is not None and retry_after is None:
                st["session_vwap_retry_opportunity"] = None
                self._trade_row(
                    "session_vwap_decision",
                    strat,
                    opportunity_id=opportunity_id,
                    side=side,
                    reason="open_retry_state_invalid",
                    signal_bar_time=dt_text(signal_bar),
                    note=f"previous_retry={raw_retry_after!r};retry_discarded",
                )
                self._save_state()
                continue
            if retry_after is not None and admission_time < retry_after:
                continue
            price_row = pd.Series(
                {"Open": float(info.bid), "Close": float(info.bid), "AskOpen": float(info.ask)},
                name=signal_bar,
            )
            opened = self._open_entry(
                strat,
                side,
                price_row,
                info,
                note=str(retry.get("note") or "session_vwap_retry"),
                execution_time=execution_time if execution_time is not None else admission_time,
                admission_time=admission_time,
                opportunity=opportunity,
                apply_portfolio_rearm=False,
                use_confirmed_fill_time=True,
            )
            confirmed_open = any(
                str(pos.get("opportunity_id") or "") == opportunity_id
                for pos in self._basket_rows(strat)
            )
            if confirmed_open:
                st["session_vwap_retry_opportunity"] = None
                decision_reason = "entry_opened_from_retry"
            elif st.get("pending_open_opportunity_id") or st.get("sync_block_new_entries"):
                decision_reason = "entry_action_unconfirmed"
            elif st.get("open_retry_after_utc"):
                decision_reason = "entry_retry_scheduled"
            else:
                st["session_vwap_retry_opportunity"] = None
                decision_reason = "entry_retry_not_opened" if not opened else "entry_action_unconfirmed"
            self._trade_row(
                "session_vwap_decision",
                strat,
                opportunity_id=opportunity_id,
                side=side,
                reason=decision_reason,
                signal_bar_time=dt_text(signal_bar),
            )
            self._save_state()

    def _process_session_vwap_entries(self, info: Any, poll_time: pd.Timestamp, readiness: dict[int, bool]) -> None:
        if not bool(self.params.get("session_vwap_enabled", False)):
            return
        host_time = pd.Timestamp(poll_time)
        host_time = host_time.tz_localize("UTC") if host_time.tzinfo is None else host_time.tz_convert("UTC")
        quote_time = self._session_vwap_quote_time(info, poll_time)
        if quote_time is None:
            return
        # Admission must fail closed if either the host poll clock or the
        # broker quote clock proves the signal expired.  Broker time remains
        # the execution/submission clock used to confirm the resulting fill.
        admission_time = max(host_time, quote_time)
        self._process_session_vwap_retries(
            info, admission_time, readiness, execution_time=quote_time,
        )
        snapshot = self._session_vwap_snapshot
        if snapshot is None:
            snapshot = self.session_vwap_history.advance(quote_time)
            self._session_vwap_snapshot = snapshot
        routing = self.state["routing"]
        unavailable_bar = dt_text(quote_time.floor("min") - pd.Timedelta(minutes=1))
        primary = self._session_vwap_strategies()[0]
        if not snapshot.ready or not snapshot.fresh or snapshot.bars.empty:
            if routing.get("session_vwap_last_unavailable_bar") != unavailable_bar:
                routing["session_vwap_last_unavailable_bar"] = unavailable_bar
                self._trade_row(
                    "session_vwap_decision", primary, reason="not_evaluated_data_unavailable",
                    signal_bar_time=unavailable_bar,
                    note=f"history={snapshot.reason};failures={snapshot.failures};retry_after={snapshot.retry_after_seconds:.1f}s",
                )
                self._save_state()
            return
        price_row = snapshot.bars.iloc[-1]
        signal_bar = parse_ts(price_row.name)
        if signal_bar is None:
            return
        release_time = signal_bar + pd.Timedelta(minutes=1)
        if admission_time < release_time:
            return
        if not in_session_vwap_entry_session(release_time):
            return
        history_issue = session_vwap_entry_history_issue(
            snapshot.bars,
            quote_time,
            coverage_days=int(self.params.get("session_vwap_lookback_calendar_days", 20)),
            atr_period=int(self.params.get("session_vwap_atr_period", 60)),
        )
        if history_issue is not None:
            self.session_vwap_history.request_rebackfill()
            unavailable_bar = dt_text(signal_bar)
            if routing.get("session_vwap_last_unavailable_bar") != unavailable_bar:
                routing["session_vwap_last_unavailable_bar"] = unavailable_bar
                self._trade_row(
                    "session_vwap_decision",
                    primary,
                    reason="not_evaluated_data_unavailable",
                    signal_bar_time=unavailable_bar,
                    note=f"history={history_issue}",
                )
                self._save_state()
            return
        signal_bar_text = dt_text(signal_bar)
        previous_evaluated = routing.get("session_vwap_last_evaluated_bar")
        if previous_evaluated is not None and (
            not isinstance(previous_evaluated, str)
            or parse_ts(previous_evaluated) is None
        ):
            details = {
                "previous_last_evaluated_bar": repr(previous_evaluated),
                "current_signal_bar": signal_bar_text,
            }
            for strat in self._session_vwap_strategies():
                self._set_sync_block(
                    strat,
                    "session_vwap_decision_receipt_state_invalid",
                    details,
                    recoverable=False,
                )
            self._trade_row(
                "session_vwap_decision",
                primary,
                reason="decision_receipt_state_invalid",
                signal_bar_time=signal_bar_text,
                note=f"previous_last_evaluated_bar={previous_evaluated!r};current_bar_not_consumed",
            )
            self._save_state()
            return
        previous_evaluated_bar = (
            parse_ts(previous_evaluated)
            if isinstance(previous_evaluated, str)
            else None
        )
        if (
            previous_evaluated_bar is not None
            and previous_evaluated_bar >= signal_bar
        ):
            if previous_evaluated_bar > signal_bar:
                details = {
                    "previous_last_evaluated_bar": dt_text(previous_evaluated_bar),
                    "current_signal_bar": signal_bar_text,
                    "broker_quote_time": dt_text(quote_time),
                }
                for strat in self._session_vwap_strategies():
                    self._set_sync_block(
                        strat,
                        "session_vwap_decision_receipt_future",
                        details,
                        recoverable=False,
                    )
                self._trade_row(
                    "session_vwap_decision",
                    primary,
                    reason="decision_receipt_future",
                    signal_bar_time=signal_bar_text,
                    note=json.dumps(details, ensure_ascii=True, sort_keys=True),
                )
                self._save_state()
            return
        try:
            side, signal_row = latest_session_vwap_signal(
                snapshot.bars,
                quantile=float(self.params.get("session_vwap_quantile", 0.90)),
                lookback_days=int(self.params.get("session_vwap_lookback_calendar_days", 20)),
            )
        except (TypeError, ValueError, OverflowError, FloatingPointError, pd.errors.DataError) as exc:
            self.session_vwap_history.request_rebackfill()
            if routing.get("session_vwap_last_unavailable_bar") != signal_bar_text:
                routing["session_vwap_last_unavailable_bar"] = signal_bar_text
                self._trade_row(
                    "session_vwap_decision",
                    primary,
                    reason="not_evaluated_signal_error",
                    signal_bar_time=signal_bar_text,
                    note=f"{type(exc).__name__}:{exc}",
                )
                self._save_state()
            return
        # Commit the durable receipt only after the complete signal calculation
        # has produced an outcome. A calculation failure must retry this bar.
        routing["session_vwap_last_evaluated_bar"] = signal_bar_text
        self._save_state()
        if side is None:
            self._trade_row("session_vwap_decision", primary, reason="no_signal", signal_bar_time=signal_bar_text)
            return
        opportunity_id = f"{self.params.get('mt5_symbol', self.params['symbol'])}|{signal_bar_text}|session_vwap_extension_fade|{side}"
        total_positions = sum(len(self._basket_rows(row)) for row in self._session_vwap_strategies())
        point = float(self.params.get("point_size", 0.001))
        spread_points = max(0.0, float(info.ask) - float(info.bid)) / point if point > 0 else math.inf
        stale = stale_signal_decision(
            str(price_row.name), timeframe_hours=1.0 / 60.0,
            max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)),
            now_utc=admission_time,
            options=self.safety,
        )
        common_reason = ""
        if total_positions >= int(self.params.get("session_vwap_max_positions", 5)):
            common_reason = "session_vwap_capacity_full"
        elif spread_points > float(self.params.get("max_entry_spread_points", 300.0)):
            common_reason = "spread_guard"
        elif stale.stale:
            common_reason = "stale_signal_skip"
        closed_cutoff, closed_state_invalid = self._session_vwap_closed_cutoff(side, admission_time)
        if not common_reason and closed_state_invalid:
            common_reason = "last_closed_state_invalid"
        elif not common_reason and closed_cutoff is not None and release_time <= closed_cutoff:
            common_reason = "stale_same_direction_after_close"
        for strat in self._session_vwap_strategies():
            lane_id = int(strat["lane_id"])
            st = self._st(strat)
            reason = common_reason
            if not reason and not readiness.get(lane_id, False):
                reason = "exit_or_sync_block"
            elif not reason and st.get("session_vwap_retry_opportunity"):
                reason = "session_vwap_retry_pending"
            elif not reason and (st["basket"] or len(st["basket"]) >= int(strat.get("max_positions", 1))):
                reason = "lane_capacity_full"
            if reason:
                self._trade_row(
                    "session_vwap_decision", strat, opportunity_id=opportunity_id,
                    side=side, reason=reason, signal_bar_time=signal_bar_text,
                )
                continue
            opportunity = {
                "opportunity_id": opportunity_id, "source": "session_vwap_extension_fade",
                "side": side, "raw_side": side, "effective_side": side,
                "event_time": signal_bar_text, "release_time": dt_text(release_time),
                "available_time": dt_text(release_time), "decision_time": dt_text(admission_time),
                "executable_at": dt_text(quote_time),
            }
            note = "session_vwap_q90_20d_atr60_hold_15m"
            if signal_row is not None:
                note += f";z={float(signal_row['Z']):.6f};q90={float(signal_row['Q90']):.6f}"
            st["session_vwap_retry_opportunity"] = {
                "opportunity": opportunity,
                "signal_bar_time": signal_bar_text,
                "expires_utc": dt_text(
                    release_time + pd.Timedelta(
                        minutes=float(self.params.get("max_signal_delay_minutes", 2.0))
                    )
                ),
                "note": note,
            }
            self._save_state()
            opened = self._open_entry(
                strat, side, price_row, info, note=note, execution_time=quote_time,
                admission_time=admission_time,
                opportunity=opportunity, apply_portfolio_rearm=False, use_confirmed_fill_time=True,
            )
            confirmed_open = any(
                str(pos.get("opportunity_id") or "") == opportunity_id
                for pos in self._basket_rows(strat)
            )
            retry_scheduled = bool(
                not confirmed_open
                and not self._st(strat).get("pending_open_opportunity_id")
                and self._st(strat).get("open_retry_after_utc")
            )
            if retry_scheduled:
                # The lane-local retry record retains the original bar across
                # newer M1 bars and restarts. Global evaluated identity stays
                # consumed so the first submission is never duplicated.
                pass
            elif confirmed_open:
                st["session_vwap_retry_opportunity"] = None
                self._save_state()
            elif not st.get("pending_open_opportunity_id") and not st.get("sync_block_new_entries"):
                st["session_vwap_retry_opportunity"] = None
                self._save_state()
            if confirmed_open:
                decision_reason = "entry_opened"
            elif retry_scheduled:
                decision_reason = "entry_retry_scheduled"
            elif opened:
                decision_reason = "entry_action_unconfirmed"
            else:
                decision_reason = "entry_not_opened"
            self._trade_row(
                "session_vwap_decision", strat, opportunity_id=opportunity_id, side=side,
                reason=decision_reason, signal_bar_time=signal_bar_text,
            )
            return
        self._trade_row(
            "session_vwap_decision", primary, opportunity_id=opportunity_id,
            side=side, reason="all_lanes_unavailable", signal_bar_time=signal_bar_text,
        )

    def _monitor_t0530_edge_position(self, strat: dict[str, Any], info: Any, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        return self._monitor_fixed_hold_position(strat, info, poll_time, "t0530_edge_fixed_hold")

    def _process_t0530_edge_exits(self, info: Any, poll_time: pd.Timestamp) -> dict[int, bool]:
        readiness: dict[int, bool] = {}
        group_enabled = bool(self.params.get("t0530_edge_enabled", False))
        session_active = in_t0530_edge_release_session(poll_time)
        for strat in self._t0530_edge_strategies():
            lane_id = int(strat["lane_id"])
            st = self._st(strat)
            needs_reconciliation = bool(
                (group_enabled and session_active)
                or st.get("basket")
                or st.get("pending_open_opportunity_id")
                or st.get("pending_close_reason")
                or st.get("sync_block_new_entries")
                or st.get("t0530_edge_retry_opportunity")
            )
            if not needs_reconciliation:
                readiness[lane_id] = False
                continue
            if not self._sync_strategy(strat):
                self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="t0530_edge_sync_block")
                self._save_state()
                readiness[lane_id] = False
                continue
            exit_blocked = self._monitor_t0530_edge_position(strat, info, poll_time)
            readiness[lane_id] = bool(group_enabled and strat.get("enabled", True) and not exit_blocked)
        return readiness

    def _attempt_t0530_edge_open(
        self,
        strat: dict[str, Any],
        opportunity: dict[str, Any],
        price_row: pd.Series,
        info: Any,
        poll_time: pd.Timestamp,
        note: str,
    ) -> bool:
        st = self._st(strat)
        opened = self._open_entry(
            strat, str(opportunity["side"]), price_row, info, note=note,
            execution_time=poll_time, admission_time=poll_time,
            opportunity=opportunity, apply_portfolio_rearm=False,
            use_confirmed_fill_time=True,
        )
        opportunity_id = str(opportunity["opportunity_id"])
        confirmed = any(
            str(position.get("opportunity_id") or "") == opportunity_id
            for position in self._basket_rows(strat)
        )
        if confirmed:
            st["t0530_edge_retry_opportunity"] = None
            self._save_state()
        elif not st.get("pending_open_opportunity_id") and not st.get("open_retry_after_utc"):
            st["t0530_edge_retry_opportunity"] = None
            self._save_state()
        return bool(opened or confirmed)

    def _process_t0530_edge_retries(
        self,
        info: Any,
        poll_time: pd.Timestamp,
        readiness: dict[int, bool],
        price_row: pd.Series,
    ) -> bool:
        for strat in self._t0530_edge_strategies():
            st = self._st(strat)
            retry = st.get("t0530_edge_retry_opportunity")
            if retry is None:
                continue
            if not isinstance(retry, dict) or not isinstance(retry.get("opportunity"), dict):
                st["t0530_edge_retry_opportunity"] = None
                self._set_sync_block(strat, "t0530_edge_retry_state_invalid", {"retry": repr(retry)}, recoverable=False)
                self._save_state()
                return True
            opportunity = retry["opportunity"]
            side = str(opportunity.get("side") or "")
            raw_event_time = opportunity.get("event_time")
            raw_release_time = opportunity.get("release_time")
            raw_available_time = opportunity.get("available_time")
            raw_expiry = retry.get("expires_utc")
            event_time = parse_ts(raw_event_time) if isinstance(raw_event_time, str) else None
            release_time = parse_ts(raw_release_time) if isinstance(raw_release_time, str) else None
            available_time = parse_ts(raw_available_time) if isinstance(raw_available_time, str) else None
            expiry = parse_ts(raw_expiry) if isinstance(raw_expiry, str) else None
            raw_group_receipt = self.state["routing"].get("t0530_edge_last_evaluated_bar")
            group_receipt = parse_ts(raw_group_receipt) if isinstance(raw_group_receipt, str) else None
            expected_id = (
                f"{self.params.get('mt5_symbol', self.params['symbol'])}|"
                f"{raw_event_time}|t0530_edge_break_fade|{side}"
            )
            identity_valid = (
                side in {"LONG", "SHORT"}
                and opportunity.get("source") == "t0530_edge_break_fade"
                and opportunity.get("opportunity_id") == expected_id
                and event_time is not None
                and release_time is not None
                and available_time is not None
                and expiry is not None
                and raw_event_time == dt_text(event_time)
                and raw_release_time == dt_text(release_time)
                and raw_available_time == dt_text(available_time)
                and raw_expiry == dt_text(expiry)
                and group_receipt is not None
                and raw_group_receipt == dt_text(group_receipt)
                and group_receipt == event_time
                and release_time == event_time + pd.Timedelta(minutes=1)
                and available_time == release_time
                and expiry == release_time + pd.Timedelta(
                    minutes=int(self.params.get("t0530_edge_max_signal_delay_minutes", 5))
                )
                and in_t0530_edge_release_session(release_time)
            )
            if not identity_valid:
                st["t0530_edge_retry_opportunity"] = None
                self._set_sync_block(strat, "t0530_edge_retry_identity_invalid", recoverable=False)
                self._save_state()
                return True
            if st.get("basket"):
                st["t0530_edge_retry_opportunity"] = None
                self._save_state()
                continue
            if poll_time > expiry:
                st["t0530_edge_retry_opportunity"] = None
                st["open_retry_after_utc"] = None
                self._trade_row("t0530_edge_decision", strat, opportunity_id=expected_id, side=side, reason="retry_expired")
                self._save_state()
                continue
            if poll_time < release_time:
                return True
            if st.get("pending_open_opportunity_id"):
                return True
            raw_retry_after = st.get("open_retry_after_utc")
            retry_after = parse_ts(raw_retry_after) if isinstance(raw_retry_after, str) else None
            if (
                raw_retry_after is not None
                and (
                    retry_after is None
                    or raw_retry_after != dt_text(retry_after)
                    or retry_after > expiry
                )
            ):
                st["t0530_edge_retry_opportunity"] = None
                st["open_retry_after_utc"] = None
                self._set_sync_block(strat, "t0530_edge_retry_clock_invalid", recoverable=False)
                self._save_state()
                return True
            if retry_after is not None and poll_time < retry_after:
                return True
            if not readiness.get(int(strat["lane_id"]), False):
                return True
            self._attempt_t0530_edge_open(
                strat, opportunity, price_row, info, poll_time,
                str(retry.get("note") or "t0530_edge_w15_hold_15m"),
            )
            return True
        return False

    def _process_t0530_edge_entries(
        self,
        bars: pd.DataFrame,
        price_row: pd.Series,
        info: Any,
        poll_time: pd.Timestamp,
        readiness: dict[int, bool],
    ) -> None:
        if not bool(self.params.get("t0530_edge_enabled", False)):
            return
        routing = self.state["routing"]
        raw_previous = routing.get("t0530_edge_last_evaluated_bar")
        previous = parse_ts(raw_previous) if isinstance(raw_previous, str) else None
        if raw_previous is not None and (
            previous is None or raw_previous != dt_text(previous)
        ):
            for strat in self._t0530_edge_strategies():
                self._set_sync_block(
                    strat, "t0530_edge_decision_receipt_state_invalid",
                    {"previous": repr(raw_previous)}, recoverable=False,
                )
            self._save_state()
            return
        if self._process_t0530_edge_retries(info, poll_time, readiness, price_row):
            return
        signal_bar = parse_ts(price_row.name)
        if signal_bar is None:
            return
        release_time = signal_bar + pd.Timedelta(minutes=1)
        if poll_time < release_time or not in_t0530_edge_release_session(release_time):
            return
        signal_bar_text = dt_text(signal_bar)
        if previous is not None and previous >= signal_bar:
            if previous > signal_bar:
                for strat in self._t0530_edge_strategies():
                    self._set_sync_block(
                        strat, "t0530_edge_decision_receipt_future",
                        {"previous": dt_text(previous), "current": signal_bar_text},
                        recoverable=False,
                    )
                self._save_state()
            return
        try:
            side = latest_t0530_edge_signal(bars)
        except (TypeError, ValueError, OverflowError, FloatingPointError, pd.errors.DataError) as exc:
            for strat in self._t0530_edge_strategies():
                self._set_sync_block(
                    strat, "t0530_edge_history_invalid",
                    {"type": type(exc).__name__, "message": str(exc)}, recoverable=True,
                )
            self._save_state()
            return
        if side is None:
            routing["t0530_edge_last_evaluated_bar"] = signal_bar_text
            self._save_state()
            return
        opportunity_id = f"{self.params.get('mt5_symbol', self.params['symbol'])}|{signal_bar_text}|t0530_edge_break_fade|{side}"
        total_positions = sum(len(self._basket_rows(strat)) for strat in self._t0530_edge_strategies())
        stale = stale_signal_decision(
            signal_bar_text, timeframe_hours=1.0 / 60.0,
            max_delay_minutes=float(self.params.get("t0530_edge_max_signal_delay_minutes", 5)),
            now_utc=poll_time, options=self.safety,
        )
        point = float(self.params.get("point_size", 0.001))
        spread_points = max(0.0, float(info.ask) - float(info.bid)) / point if point > 0 else math.inf
        for strat in self._t0530_edge_strategies():
            reason = ""
            st = self._st(strat)
            if total_positions >= int(self.params.get("t0530_edge_max_positions", 4)):
                reason = "t0530_edge_capacity_full"
            elif spread_points > float(self.params.get("max_entry_spread_points", 300.0)):
                reason = "spread_guard"
            elif stale.stale:
                reason = "stale_signal_skip"
            elif not readiness.get(int(strat["lane_id"]), False):
                reason = "exit_or_sync_block"
            elif st.get("basket"):
                reason = "lane_capacity_full"
            if reason:
                self._trade_row("t0530_edge_decision", strat, opportunity_id=opportunity_id, side=side, reason=reason, signal_bar_time=signal_bar_text)
                continue
            opportunity = {
                "opportunity_id": opportunity_id, "source": "t0530_edge_break_fade",
                "side": side, "raw_side": side, "effective_side": side,
                "event_time": signal_bar_text, "release_time": dt_text(release_time),
                "available_time": dt_text(release_time), "decision_time": dt_text(poll_time),
                "executable_at": dt_text(poll_time),
            }
            note = "t0530_edge_w15_onset_hold_15m"
            st["t0530_edge_retry_opportunity"] = {
                "opportunity": opportunity,
                "expires_utc": dt_text(
                    release_time + pd.Timedelta(
                        minutes=int(self.params.get("t0530_edge_max_signal_delay_minutes", 5))
                    )
                ),
                "note": note,
            }
            # One atomic state image both consumes the group-wide signal and
            # leaves a lane-local recovery record before any broker command.
            routing["t0530_edge_last_evaluated_bar"] = signal_bar_text
            self._save_state()
            opened = self._attempt_t0530_edge_open(strat, opportunity, price_row, info, poll_time, note)
            retry_scheduled = bool(
                not opened and not st.get("pending_open_opportunity_id") and st.get("open_retry_after_utc")
            )
            self._trade_row(
                "t0530_edge_decision", strat, opportunity_id=opportunity_id, side=side,
                reason="entry_opened" if opened else "entry_retry_scheduled" if retry_scheduled else "entry_not_opened",
                signal_bar_time=signal_bar_text,
            )
            return
        routing["t0530_edge_last_evaluated_bar"] = signal_bar_text
        self._save_state()

    def _attempt_q01_forced_close(
        self,
        strat: dict[str, Any],
        info: Any,
        quote_time: pd.Timestamp,
        reason: str,
    ) -> str:
        st = self._st(strat)
        bid = float(getattr(info, "bid"))
        ask = float(getattr(info, "ask"))
        pnl = self._basket_pnl(strat, bid, ask)
        row = pd.Series({"Open": bid, "Close": bid, "AskOpen": ask}, name=quote_time)
        result = self._close_basket(strat, reason, row, pnl)
        if result == "market_closed":
            # Generic close handling correctly proves 10018 as no-fill and
            # clears its transient intent. Q01 must retain the frozen forced
            # exit intent so it retries rather than reverting to normal hold.
            st["pending_close_reason"] = reason
            st["pending_close_signal_bar"] = dt_text(quote_time)
            st["time_close_retry_after_utc"] = dt_text(
                quote_time
                + pd.Timedelta(
                    seconds=float(self.params.get("fixed_hold_market_closed_retry_seconds", 60.0))
                )
            )
            self._save_state()
        return result

    def _monitor_q01_position(self, strat: dict[str, Any], info: Any, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        st = self._st(strat)
        if not st.get("basket"):
            if st.get("q01_last_quote_msc") is not None:
                st["q01_last_quote_msc"] = None
                self._save_state()
            return False
        raw_quote_msc = getattr(info, "quote_time_msc", None)
        try:
            quote_msc = int(raw_quote_msc)
        except (TypeError, ValueError, OverflowError):
            quote_msc = -1
        if quote_msc > 0:
            quote_time = pd.Timestamp(quote_msc, unit="ms", tz="UTC")
            pending_forced_reason = st.get("pending_close_reason")
            if pending_forced_reason in {"q01_feed_gap", "q01_quote_clock_invalid"}:
                retry_after = self._validated_time_close_retry_after(strat, quote_time)
                if retry_after is None or quote_time >= retry_after:
                    self._attempt_q01_forced_close(strat, info, quote_time, str(pending_forced_reason))
                return True
            raw_previous = st.get("q01_last_quote_msc")
            previous_valid = (
                raw_previous is None
                or (
                    isinstance(raw_previous, int)
                    and not isinstance(raw_previous, bool)
                    and 0 < raw_previous <= quote_msc
                )
            )
            if not previous_valid:
                self._trade_row(
                    "position_lifecycle_recovered",
                    strat,
                    reason="q01_quote_clock_state_invalid_forced_close",
                    note=f"previous={raw_previous!r};current={quote_msc}",
                )
                st["q01_last_quote_msc"] = quote_msc
                self._save_state()
                self._attempt_q01_forced_close(strat, info, quote_time, "q01_quote_clock_invalid")
                return True
            is_fresh = raw_previous is None or quote_msc > raw_previous
            if is_fresh:
                st["q01_last_quote_msc"] = quote_msc
                self._save_state()
            gap_milliseconds = int(self.params.get("q01_feed_gap_seconds", EXPECTED_Q01_FEED_GAP_SECONDS)) * 1000
            if raw_previous is not None and quote_msc - raw_previous > gap_milliseconds:
                if st.get("pending_close_reason"):
                    return True
                self._attempt_q01_forced_close(strat, info, quote_time, "q01_feed_gap")
                return True
        return self._monitor_fixed_hold_position(
            strat,
            info,
            poll_time,
            "q01_fixed_hold",
            defer_for_spread=False,
        )

    @staticmethod
    def _q01_m5_bars(bars: pd.DataFrame) -> pd.DataFrame:
        if bars.empty:
            return pd.DataFrame()
        frame = bars.copy()
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        else:
            frame.index = frame.index.tz_convert("UTC")
        required = {"Open", "High", "Low", "Close"}
        if not required.issubset(frame.columns):
            return pd.DataFrame()
        aggregate = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        if "Volume" in frame.columns:
            aggregate["Volume"] = "sum"
        return frame.resample("5min", label="left", closed="left").agg(aggregate).dropna(subset=["Open", "High", "Low", "Close"])

    @staticmethod
    def _q01_signal_side(
        m5: pd.DataFrame,
        signal_bar: pd.Timestamp,
        *,
        horizon: int,
        window: int,
        threshold: float,
        breakout: int,
        warmup: int,
        atr_period: int,
    ) -> tuple[str | None, float | None]:
        if m5.empty:
            return None, None
        if m5.index.tz is None:
            m5 = m5.copy()
            m5.index = m5.index.tz_localize("UTC")
        else:
            m5 = m5.copy()
            m5.index = m5.index.tz_convert("UTC")
        if signal_bar not in m5.index:
            return None, None
        signal_position = int(m5.index.get_loc(signal_bar))
        if signal_position < int(warmup):
            return None, None
        close = m5["Close"].astype(float)
        one = close.diff()
        horizon_ret = close.diff(int(horizon))
        # Frozen DEV formula uses only returns known before the signal M5 bar.
        one_var = one.shift(1).rolling(int(window), min_periods=int(window)).var()
        horizon_var = horizon_ret.shift(1).rolling(int(window), min_periods=int(window)).var()
        vr = horizon_var / (float(horizon) * one_var)
        try:
            vr_value = float(vr.loc[signal_bar])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None, None
        if not math.isfinite(vr_value) or vr_value < float(threshold):
            return None, vr_value if math.isfinite(vr_value) else None
        high_series = m5["High"].astype(float)
        low_series = m5["Low"].astype(float)
        previous_close = close.shift(1)
        true_range = pd.concat(
            [high_series - low_series, (high_series - previous_close).abs(), (low_series - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = true_range.rolling(int(atr_period), min_periods=int(atr_period)).mean()
        try:
            atr_value = float(atr.loc[signal_bar])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None, vr_value
        if not math.isfinite(atr_value) or atr_value <= 0.0:
            return None, vr_value
        prior_high = high_series.shift(1).rolling(int(breakout), min_periods=int(breakout)).max()
        prior_low = low_series.shift(1).rolling(int(breakout), min_periods=int(breakout)).min()
        try:
            c = float(close.loc[signal_bar])
            high = float(prior_high.loc[signal_bar])
            low = float(prior_low.loc[signal_bar])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None, vr_value
        if not all(math.isfinite(value) for value in (c, high, low)):
            return None, vr_value
        if c > high:
            return "LONG", vr_value
        if c < low:
            return "SHORT", vr_value
        return None, vr_value

    def _process_q01_exits(self, info: Any, poll_time: pd.Timestamp) -> dict[int, bool]:
        readiness: dict[int, bool] = {}
        group_enabled = bool(self.params.get("q01_variance_release_enabled", False))
        for strat in self._q01_strategies():
            lane_id = int(strat["lane_id"])
            st = self._st(strat)
            needs_reconciliation = bool(
                group_enabled
                or st.get("basket")
                or st.get("pending_open_opportunity_id")
                or st.get("pending_close_reason")
                or st.get("sync_block_new_entries")
                or st.get("q01_retry_opportunity")
            )
            if not needs_reconciliation:
                readiness[lane_id] = False
                continue
            if not self._sync_strategy(strat):
                self._trade_row("entry_skip", strat, reason=st.get("sync_block_reason"), note="q01_sync_block")
                self._save_state()
                readiness[lane_id] = False
                continue
            exit_blocked = self._monitor_q01_position(strat, info, poll_time)
            readiness[lane_id] = bool(group_enabled and strat.get("enabled", True) and not exit_blocked)
        return readiness

    def _process_q01_entries(
        self,
        bars: pd.DataFrame,
        info: Any,
        poll_time: pd.Timestamp,
        readiness: dict[int, bool],
    ) -> None:
        if not bool(self.params.get("q01_variance_release_enabled", False)):
            return
        if self._process_q01_retries(info, poll_time, readiness):
            return
        if bars.empty:
            return
        evaluation_time = pd.Timestamp(poll_time)
        evaluation_time = (
            evaluation_time.tz_localize("UTC")
            if evaluation_time.tzinfo is None
            else evaluation_time.tz_convert("UTC")
        )
        raw_quote_msc = getattr(info, "quote_time_msc", None)
        try:
            quote_msc = int(raw_quote_msc)
        except (TypeError, ValueError, OverflowError):
            quote_msc = -1
        if quote_msc > 0:
            evaluation_time = pd.Timestamp(quote_msc, unit="ms", tz="UTC")
        elif self.live_enabled:
            self._trade_row(
                "q01_decision",
                self._q01_strategies()[0],
                reason="not_evaluated_quote_timestamp_unavailable",
            )
            return
        latest_m1 = parse_ts(bars.index[-1])
        if latest_m1 is None:
            return
        latest_release_time = (latest_m1 + pd.Timedelta(minutes=1)).floor("5min")
        latest_signal_bar = latest_release_time - pd.Timedelta(minutes=5)
        m5 = self._q01_m5_bars(bars)
        if m5.empty:
            return
        available_signal_bars = m5.index[m5.index <= latest_signal_bar]
        if available_signal_bars.empty:
            return
        latest_signal_bar = pd.Timestamp(available_signal_bars[-1])
        routing = self.state["routing"]
        raw_previous = routing.get("q01_last_evaluated_m5_bar")
        previous = parse_ts(raw_previous) if isinstance(raw_previous, str) else None
        if raw_previous is not None and (previous is None or raw_previous != dt_text(previous)):
            for strat in self._q01_strategies():
                self._set_sync_block(strat, "q01_decision_receipt_state_invalid", {"previous": repr(raw_previous)}, recoverable=False)
            self._save_state()
            return
        if previous is not None:
            if previous > latest_signal_bar:
                signal_bar_text = dt_text(latest_signal_bar)
                for strat in self._q01_strategies():
                    self._set_sync_block(strat, "q01_decision_receipt_future", {"previous": dt_text(previous), "current": signal_bar_text}, recoverable=False)
                self._save_state()
                return
            unseen_signal_bars = available_signal_bars[available_signal_bars > previous]
            if unseen_signal_bars.empty:
                return
            # The frozen replay groups only bars that actually received ticks.
            # Skip nonexistent M5 intervals (weekends/feed closures) while still
            # consuming real unseen bars in chronological order.
            signal_bar = pd.Timestamp(unseen_signal_bars[0])
        else:
            # First activation starts from the latest completed M5 and never
            # replays historical signals that were not observed by this bot.
            signal_bar = latest_signal_bar
        release_time = signal_bar + pd.Timedelta(minutes=5)
        signal_bar_text = dt_text(signal_bar)
        if previous is not None and previous >= signal_bar:
            if previous > signal_bar:
                for strat in self._q01_strategies():
                    self._set_sync_block(strat, "q01_decision_receipt_future", {"previous": dt_text(previous), "current": signal_bar_text}, recoverable=False)
                self._save_state()
            return
        if evaluation_time < release_time:
            return
        try:
            side, vr_value = self._q01_signal_side(
                m5,
                signal_bar,
                horizon=int(self.params.get("q01_variance_horizon_bars", EXPECTED_Q01_VARIANCE_HORIZON_BARS)),
                window=int(self.params.get("q01_variance_window_bars", EXPECTED_Q01_VARIANCE_WINDOW_BARS)),
                threshold=float(self.params.get("q01_vr_threshold", EXPECTED_Q01_VR_THRESHOLD)),
                breakout=int(self.params.get("q01_breakout_lookback_bars", EXPECTED_Q01_BREAKOUT_LOOKBACK_BARS)),
                warmup=int(self.params.get("q01_warmup_m5_bars", EXPECTED_Q01_WARMUP_M5_BARS)),
                atr_period=int(self.params.get("q01_atr_period", EXPECTED_Q01_ATR_PERIOD)),
            )
        except (TypeError, ValueError, OverflowError, FloatingPointError, pd.errors.DataError) as exc:
            for strat in self._q01_strategies():
                self._set_sync_block(strat, "q01_history_invalid", {"type": type(exc).__name__, "message": str(exc)}, recoverable=True)
            self._save_state()
            return
        if side is None:
            routing["q01_last_evaluated_m5_bar"] = signal_bar_text
            self._trade_row("q01_decision", self._q01_strategies()[0], reason="no_signal", signal_bar_time=signal_bar_text, note=f"vr={vr_value:.6f}" if vr_value is not None else "vr=nan")
            self._save_state()
            return
        strat = self._q01_strategies()[0]
        st = self._st(strat)
        opportunity_id = f"{self.params.get('mt5_symbol', self.params['symbol'])}|{signal_bar_text}|q01_variance_ratio_release|{side}"
        point = float(self.params.get("point_size", 0.001))
        spread_points = max(0.0, float(info.ask) - float(info.bid)) / point if point > 0 else math.inf
        q01_spread_cap_points = float(self.params.get("q01_max_raw_spread_price", 0.30)) / point if point > 0 else -math.inf
        stale = stale_signal_decision(
            signal_bar_text,
            timeframe_hours=5.0 / 60.0,
            max_delay_minutes=float(self.params.get("q01_max_signal_delay_minutes", EXPECTED_Q01_MAX_SIGNAL_DELAY_MINUTES)),
            now_utc=evaluation_time,
            options=self.safety,
        )
        reason = ""
        if not readiness.get(int(strat["lane_id"]), False):
            reason = "exit_or_sync_block"
        elif st.get("basket") or len(st["basket"]) >= int(strat.get("max_positions", 1)):
            reason = "lane_capacity_full"
        elif spread_points > q01_spread_cap_points:
            reason = "spread_guard"
        elif stale.stale:
            reason = "stale_signal_skip"
        if reason:
            routing["q01_last_evaluated_m5_bar"] = signal_bar_text
            self._trade_row("q01_decision", strat, opportunity_id=opportunity_id, side=side, reason=reason, signal_bar_time=signal_bar_text, note=f"vr={vr_value:.6f}")
            self._save_state()
            return
        quote_row = pd.Series({"Open": float(info.bid), "Close": float(info.bid), "AskOpen": float(info.ask)}, name=signal_bar)
        opportunity = {
            "opportunity_id": opportunity_id,
            "source": "q01_variance_ratio_release",
            "side": side,
            "raw_side": side,
            "effective_side": side,
            "event_time": signal_bar_text,
            "release_time": dt_text(release_time),
            "available_time": dt_text(release_time),
            "decision_time": dt_text(evaluation_time),
            "executable_at": dt_text(evaluation_time),
        }
        note = f"q01_k4_w48_t135_b12_hold_30m;vr={vr_value:.6f}"
        st["q01_retry_opportunity"] = {
            "opportunity": opportunity,
            "expires_utc": dt_text(release_time + pd.Timedelta(minutes=int(self.params.get("q01_max_signal_delay_minutes", EXPECTED_Q01_MAX_SIGNAL_DELAY_MINUTES)))),
            "note": note,
        }
        routing["q01_last_evaluated_m5_bar"] = signal_bar_text
        self._save_state()
        opened = self._attempt_q01_open(strat, opportunity, quote_row, info, evaluation_time, note)
        confirmed = any(str(position.get("opportunity_id") or "") == opportunity_id for position in self._basket_rows(strat))
        self._trade_row(
            "q01_decision",
            strat,
            opportunity_id=opportunity_id,
            side=side,
            reason="entry_opened" if opened or confirmed else "entry_not_opened",
            signal_bar_time=signal_bar_text,
            note=f"vr={vr_value:.6f}",
        )

    def _attempt_q01_open(
        self,
        strat: dict[str, Any],
        opportunity: dict[str, Any],
        quote_row: pd.Series,
        info: Any,
        poll_time: pd.Timestamp,
        note: str,
    ) -> bool:
        st = self._st(strat)
        if self.live_enabled and not bool(self.params.get("q01_live_trading_enabled", False)):
            st["q01_retry_opportunity"] = None
            st["open_retry_after_utc"] = None
            self._trade_row(
                "entry_skip",
                strat,
                opportunity_id=str(opportunity.get("opportunity_id") or ""),
                side=str(opportunity.get("side") or ""),
                reason="q01_live_trading_gate_disabled",
                note=note,
            )
            self._save_state()
            return False
        opened = self._open_entry(
            strat,
            str(opportunity["side"]),
            quote_row,
            info,
            note=note,
            execution_time=poll_time,
            admission_time=poll_time,
            opportunity=opportunity,
            apply_portfolio_rearm=False,
            use_confirmed_fill_time=True,
        )
        opportunity_id = str(opportunity["opportunity_id"])
        confirmed = any(
            str(position.get("opportunity_id") or "") == opportunity_id
            for position in self._basket_rows(strat)
        )
        if confirmed:
            try:
                confirmed_quote_msc = int(getattr(info, "quote_time_msc"))
            except (TypeError, ValueError, OverflowError, AttributeError):
                confirmed_quote_msc = int(pd.Timestamp(poll_time).timestamp() * 1000)
            if confirmed_quote_msc <= 0:
                confirmed_quote_msc = int(pd.Timestamp(poll_time).timestamp() * 1000)
            # Seed the persisted feed clock at the quote that produced the
            # confirmed open. Otherwise a >300s gap before the next poll would
            # be invisible to the first-arrival gap exit.
            st["q01_last_quote_msc"] = confirmed_quote_msc
            st["q01_retry_opportunity"] = None
            self._save_state()
        elif not st.get("pending_open_opportunity_id") and not st.get("open_retry_after_utc"):
            # A definitive permanent no-fill must not replay forever merely
            # because the original signal receipt remains durable.
            st["q01_retry_opportunity"] = None
            self._save_state()
        return bool(opened or confirmed)

    def _process_q01_retries(self, info: Any, poll_time: pd.Timestamp, readiness: dict[int, bool]) -> bool:
        evaluation_time = pd.Timestamp(poll_time)
        evaluation_time = (
            evaluation_time.tz_localize("UTC")
            if evaluation_time.tzinfo is None
            else evaluation_time.tz_convert("UTC")
        )
        raw_quote_msc = getattr(info, "quote_time_msc", None)
        try:
            quote_msc = int(raw_quote_msc)
        except (TypeError, ValueError, OverflowError):
            quote_msc = -1
        if quote_msc > 0:
            evaluation_time = pd.Timestamp(quote_msc, unit="ms", tz="UTC")
        for strat in self._q01_strategies():
            st = self._st(strat)
            retry = st.get("q01_retry_opportunity")
            if retry is None:
                continue
            if not isinstance(retry, dict) or not isinstance(retry.get("opportunity"), dict):
                st["q01_retry_opportunity"] = None
                self._set_sync_block(strat, "q01_retry_state_invalid", {"retry": repr(retry)}, recoverable=False)
                self._save_state()
                return True
            opportunity = retry["opportunity"]
            side = str(opportunity.get("side") or "")
            raw_event_time = opportunity.get("event_time")
            raw_release_time = opportunity.get("release_time")
            raw_available_time = opportunity.get("available_time")
            raw_decision_time = opportunity.get("decision_time")
            raw_executable_at = opportunity.get("executable_at")
            raw_expiry = retry.get("expires_utc")
            event_time = parse_ts(raw_event_time) if isinstance(raw_event_time, str) else None
            release_time = parse_ts(raw_release_time) if isinstance(raw_release_time, str) else None
            available_time = parse_ts(raw_available_time) if isinstance(raw_available_time, str) else None
            decision_time = parse_ts(raw_decision_time) if isinstance(raw_decision_time, str) else None
            executable_at = parse_ts(raw_executable_at) if isinstance(raw_executable_at, str) else None
            expiry = parse_ts(raw_expiry) if isinstance(raw_expiry, str) else None
            raw_group_receipt = self.state["routing"].get("q01_last_evaluated_m5_bar")
            group_receipt = parse_ts(raw_group_receipt) if isinstance(raw_group_receipt, str) else None
            expected_id = (
                f"{self.params.get('mt5_symbol', self.params['symbol'])}|"
                f"{raw_event_time}|q01_variance_ratio_release|{side}"
            )
            max_delay = int(self.params.get("q01_max_signal_delay_minutes", EXPECTED_Q01_MAX_SIGNAL_DELAY_MINUTES))
            identity_valid = (
                side in {"LONG", "SHORT"}
                and opportunity.get("source") == "q01_variance_ratio_release"
                and opportunity.get("raw_side") == side
                and opportunity.get("effective_side") == side
                and opportunity.get("opportunity_id") == expected_id
                and event_time is not None
                and release_time is not None
                and available_time is not None
                and decision_time is not None
                and executable_at is not None
                and expiry is not None
                and raw_event_time == dt_text(event_time)
                and raw_release_time == dt_text(release_time)
                and raw_available_time == dt_text(available_time)
                and raw_decision_time == dt_text(decision_time)
                and raw_executable_at == dt_text(executable_at)
                and raw_expiry == dt_text(expiry)
                and group_receipt is not None
                and raw_group_receipt == dt_text(group_receipt)
                and group_receipt == event_time
                and release_time == event_time + pd.Timedelta(minutes=5)
                and available_time == release_time
                and decision_time == executable_at
                and release_time <= decision_time <= expiry
                and expiry == release_time + pd.Timedelta(minutes=max_delay)
                and int(event_time.minute) % 5 == 0
                and event_time.second == 0
                and event_time.microsecond == 0
            )
            if not identity_valid:
                st["q01_retry_opportunity"] = None
                self._set_sync_block(strat, "q01_retry_identity_invalid", recoverable=False)
                self._save_state()
                return True
            if st.get("basket"):
                st["q01_retry_opportunity"] = None
                self._save_state()
                continue
            if evaluation_time > expiry:
                st["q01_retry_opportunity"] = None
                st["open_retry_after_utc"] = None
                self._trade_row("q01_decision", strat, opportunity_id=opportunity.get("opportunity_id"), side=side, reason="retry_expired", signal_bar_time=dt_text(event_time))
                self._save_state()
                continue
            if evaluation_time < release_time or st.get("pending_open_opportunity_id"):
                return True
            raw_retry_after = st.get("open_retry_after_utc")
            retry_after = parse_ts(raw_retry_after) if isinstance(raw_retry_after, str) else None
            if raw_retry_after is not None and (retry_after is None or raw_retry_after != dt_text(retry_after) or retry_after > expiry):
                st["q01_retry_opportunity"] = None
                st["open_retry_after_utc"] = None
                self._set_sync_block(strat, "q01_retry_clock_invalid", recoverable=False)
                self._save_state()
                return True
            if retry_after is not None and evaluation_time < retry_after:
                return True
            raw_spread = max(0.0, float(info.ask) - float(info.bid))
            q01_spread_cap = float(self.params.get("q01_max_raw_spread_price", EXPECTED_Q01_MAX_RAW_SPREAD_PRICE))
            if not readiness.get(int(strat["lane_id"]), False) or raw_spread > q01_spread_cap:
                return True
            quote_row = pd.Series({"Open": float(info.bid), "Close": float(info.bid), "AskOpen": float(info.ask)}, name=event_time)
            self._attempt_q01_open(
                strat,
                opportunity,
                quote_row,
                info,
                evaluation_time,
                str(retry.get("note") or "q01_retry"),
            )
            return True
        return False

    def _process_morning_exits(self, info: Any, poll_time: pd.Timestamp) -> dict[int, bool]:
        readiness: dict[int, bool] = {}
        session_active = in_session(
            poll_time,
            EXPECTED_MORNING_SESSION_START_UTC,
            EXPECTED_MORNING_SESSION_END_UTC,
        )
        for strat in self._morning_strategies():
            entry_enabled = bool(strat.get("enabled", True))
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
            exit_blocked = self._monitor_morning_position(strat, info, poll_time)
            readiness[lane_id] = entry_enabled and not exit_blocked
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
        if poll_time < release_time:
            return
        if not in_session(release_time, EXPECTED_MORNING_SESSION_START_UTC, EXPECTED_MORNING_SESSION_END_UTC):
            return
        sides = self._morning_signal_sides(bars)
        signal_bar_text = dt_text(signal_bar)
        total_positions = sum(len(self._basket_rows(strat)) for strat in self._morning_strategies())
        for strat in self._morning_strategies():
            if not bool(strat.get("enabled", True)):
                continue
            st = self._st(strat)
            # Durable evaluation reservation prevents duplicate opens when a
            # poll is retried after an uncertain bridge response.
            if not self._reserve_lane_evaluation_bar(
                strat, signal_bar_text, "morning_decision",
            ):
                continue
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
                now_utc=poll_time,
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
            entry_enabled = bool(strat.get("enabled", True))
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
            exit_blocked = self._monitor_midday_position(strat, info, poll_time)
            readiness[lane_id] = entry_enabled and not exit_blocked
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
        if poll_time < release_time:
            return
        if not in_session(release_time, EXPECTED_MIDDAY_SESSION_START_UTC, EXPECTED_MIDDAY_SESSION_END_UTC):
            return
        signal_bar_text = dt_text(signal_bar)
        total_positions = sum(len(self._basket_rows(strat)) for strat in self._midday_strategies())
        for strat in self._midday_strategies():
            if not bool(strat.get("enabled", True)):
                continue
            st = self._st(strat)
            if not self._reserve_lane_evaluation_bar(
                strat, signal_bar_text, "midday_decision",
            ):
                continue
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
            if not bool(self.params.get("midday_session_enabled", False)):
                self._trade_row(
                    "midday_decision", strat, opportunity_id=opportunity_id, side=side,
                    reason="midday_session_disabled", signal_bar_time=signal_bar_text,
                )
                self._midday_observer_call(
                    "record_route", opportunity_id=opportunity_id, at=poll_time,
                    status="unconsumed", reason="midday_session_disabled",
                )
                continue
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
                max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)),
                now_utc=poll_time, options=self.safety,
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

    def _process_pre_eu30_exits(self, info: Any, poll_time: pd.Timestamp) -> dict[int, bool]:
        readiness: dict[int, bool] = {}
        session_active = in_pre_eu30_entry_session(poll_time)
        for strat in self._pre_eu30_strategies():
            entry_enabled = bool(strat.get("enabled", True))
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
            exit_blocked = self._monitor_pre_eu30_position(strat, info, poll_time)
            readiness[lane_id] = entry_enabled and not exit_blocked
        return readiness

    def _process_pre_eu30_entries(
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
        if poll_time < release_time:
            return
        # The frozen policy is M5-based. Evaluate only at an M5 release, and
        # use the common DST-aware clock for new-entry admission only.
        if int(release_time.minute) % 5 != 0 or not in_pre_eu30_entry_session(release_time):
            return
        sides = pre_eu30_signal_sides(bars)
        signal_bar_text = dt_text(signal_bar)
        total_positions = sum(len(self._basket_rows(strat)) for strat in self._pre_eu30_strategies())
        for strat in self._pre_eu30_strategies():
            if not bool(strat.get("enabled", True)):
                continue
            st = self._st(strat)
            if not self._reserve_lane_evaluation_bar(
                strat, signal_bar_text, "pre_eu30_decision",
            ):
                continue
            side = sides.get(str(strat["signal_id"]))
            if side is None:
                self._trade_row("pre_eu30_decision", strat, reason="no_signal", signal_bar_time=signal_bar_text)
                continue
            opportunity_id = f"{self.params.get('mt5_symbol', self.params['symbol'])}|{signal_bar_text}|{strat['signal_id']}|{side}"
            opportunity = {
                "opportunity_id": opportunity_id,
                "source": "jst1300_pre_eu30",
                "side": side,
                "raw_side": side,
                "effective_side": side,
                "event_time": signal_bar_text,
                "release_time": dt_text(release_time),
                "available_time": dt_text(release_time),
                "decision_time": dt_text(poll_time),
                "executable_at": dt_text(poll_time),
            }
            shadow_context = self._shadow_context(price_row, info, readiness)
            self._pre_eu30_observer_call(
                "register_opportunity", opportunity=opportunity, at=poll_time,
                bid=float(info.bid), ask=float(info.ask), context=shadow_context,
            )
            self._pre_eu30_state_tagger_call(
                "tag_opportunity", opportunity=opportunity, at=poll_time, bars=bars,
                bid=float(info.bid), ask=float(info.ask), context=shadow_context,
            )
            lane_id = int(strat["lane_id"])
            if not readiness.get(lane_id, False):
                reason = "exit_or_sync_block"
            elif st["basket"] or len(st["basket"]) >= int(strat.get("max_positions", 1)):
                reason = "lane_capacity_full"
            elif total_positions >= int(self.params.get("pre_eu30_session_max_positions", EXPECTED_PRE_EU30_MAX_POSITIONS)):
                reason = "pre_eu30_capacity_full"
            else:
                point = float(self.params.get("point_size", 0.001))
                spread_points = max(0.0, float(info.ask) - float(info.bid)) / point if point > 0 else math.inf
                if spread_points > float(self.params.get("max_entry_spread_points", 300.0)):
                    reason = "spread_guard"
                else:
                    stale = stale_signal_decision(
                        str(price_row.name), timeframe_hours=1.0 / 60.0,
                        max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)),
                        now_utc=poll_time, options=self.safety,
                    )
                    reason = "stale_signal_skip" if stale.stale else ""
            if reason:
                self._trade_row(
                    "pre_eu30_decision", strat, opportunity_id=opportunity_id,
                    side=side, reason=reason, signal_bar_time=signal_bar_text,
                )
                self._pre_eu30_observer_call(
                    "record_route", opportunity_id=opportunity_id, at=poll_time,
                    status="stale_rejected" if reason == "stale_signal_skip" else "unconsumed",
                    reason=reason,
                )
                continue
            opened = self._open_entry(
                strat, side, price_row, info,
                note=f"pre_eu30_{strat['signal_id']}_hold_{int(strat['hold_minutes'])}m",
                execution_time=poll_time, opportunity=opportunity,
                apply_portfolio_rearm=False, use_confirmed_fill_time=True,
            )
            if opened:
                total_positions += 1
            self._pre_eu30_observer_call(
                "record_route", opportunity_id=opportunity_id, at=poll_time,
                status="consumed" if opened else "unconsumed",
                consumed_lane_id=lane_id if opened else None,
                reason="entry_opened" if opened else "entry_not_opened",
            )

    def _monitor_pending_entry(self, strat: dict[str, Any], info: Any, poll_time: datetime | pd.Timestamp | None = None) -> bool:
        st = self._st(strat)
        raw_side = st.get("pending_entry_side")
        side = raw_side if isinstance(raw_side, str) else ""
        if not side or st["basket"]:
            return False
        at_utc = pd.Timestamp(poll_time if poll_time is not None else utc_now())
        at_utc = at_utc.tz_localize("UTC") if at_utc.tzinfo is None else at_utc.tz_convert("UTC")
        basket_block = self._new_basket_block_reason(strat, at_utc)
        if basket_block:
            signal_bar = st.get("pending_entry_signal_bar")
            opportunity_id = st.get("pending_entry_opportunity_id")
            self._clear_pending_entry(strat)
            self._trade_row(
                "entry_skip",
                strat,
                opportunity_id=opportunity_id,
                reason=basket_block,
                signal_bar_time=signal_bar,
                note="pending_za_new_basket_guard",
            )
            self._save_state()
            return True
        entry_block = self._entry_submission_block_reason(strat, at_utc)
        if entry_block:
            self._trade_row("entry_skip", strat, reason=entry_block, signal_bar_time=st.get("pending_entry_signal_bar"), note="pending_open_guard")
            return True
        raw_expires = st.get("pending_entry_expires_utc")
        raw_signal_bar = st.get("pending_entry_signal_bar")
        raw_event_time = st.get("pending_entry_event_time")
        raw_release_time = st.get("pending_entry_release_time")
        expires = parse_ts(raw_expires) if isinstance(raw_expires, str) else None
        signal_bar = parse_ts(raw_signal_bar) if isinstance(raw_signal_bar, str) else None
        event_time = parse_ts(raw_event_time) if isinstance(raw_event_time, str) else None
        release_time = parse_ts(raw_release_time) if isinstance(raw_release_time, str) else None
        raw_opportunity_id = st.get("pending_entry_opportunity_id")
        opportunity_id = raw_opportunity_id.strip() if isinstance(raw_opportunity_id, str) else ""
        raw_target = st.get("pending_entry_target")
        raw_atr30 = st.get("pending_entry_atr30")
        target = (
            float(raw_target)
            if isinstance(raw_target, (int, float)) and not isinstance(raw_target, bool)
            else math.nan
        )
        atr30 = (
            float(raw_atr30)
            if isinstance(raw_atr30, (int, float)) and not isinstance(raw_atr30, bool)
            else math.nan
        )
        canonical_release = bool(
            event_time is not None
            and release_time is not None
            and release_time == event_time + pd.Timedelta(minutes=1)
        )
        max_expiry = (
            release_time
            + pd.Timedelta(
                minutes=(
                    int(strat.get("entry_wait_minutes", 0))
                    + float(self.params.get("max_signal_delay_minutes", 2.0))
                )
            )
            if release_time is not None
            else None
        )
        opportunity_parts = opportunity_id.split("|") if opportunity_id else []
        canonical_opportunity_id = (
            len(opportunity_parts) == 5
            and opportunity_parts[0] == str(self.params.get("symbol") or "")
            and signal_bar is not None
            and opportunity_parts[1] == dt_text(signal_bar)
            and opportunity_parts[2] in {"LONG", "SHORT"}
            and opportunity_parts[3] == side
            and opportunity_parts[4] == str(self.params.get("entry_policy_id") or "")
        )
        invalid_fields = [
            field
            for field, valid in (
                ("pending_entry_side", side in {"LONG", "SHORT"}),
                ("pending_entry_target", math.isfinite(target) and target > 0.0),
                ("pending_entry_expires_utc", expires is not None),
                ("pending_entry_atr30", math.isfinite(atr30) and atr30 > 0.0),
                ("pending_entry_opportunity_id", canonical_opportunity_id),
                ("pending_entry_signal_bar", signal_bar is not None),
                ("pending_entry_event_time", event_time is not None and event_time == signal_bar),
                ("pending_entry_release_time", canonical_release),
                (
                    "pending_entry_release_not_reached",
                    release_time is not None and at_utc >= release_time,
                ),
                (
                    "pending_entry_expiry_window",
                    expires is not None
                    and release_time is not None
                    and max_expiry is not None
                    and release_time <= expires <= max_expiry,
                ),
            )
            if not valid
        ]
        if invalid_fields:
            raw_signal_bar = st.get("pending_entry_signal_bar")
            raw_opportunity_id = st.get("pending_entry_opportunity_id")
            self._clear_pending_entry(strat)
            self._trade_row("entry_skip", strat, opportunity_id=raw_opportunity_id, reason="pending_entry_state_invalid", signal_bar_time=raw_signal_bar, note=",".join(invalid_fields))
            self._save_state()
            return True
        if at_utc > expires:
            raw_signal_bar = st.get("pending_entry_signal_bar")
            raw_opportunity_id = st.get("pending_entry_opportunity_id")
            self._clear_pending_entry(strat)
            self._trade_row("entry_skip", strat, opportunity_id=raw_opportunity_id, reason="za_pullback_expired", signal_bar_time=raw_signal_bar)
            self._save_state()
            return False
        bid, ask = float(info.bid), float(info.ask)
        touched = (side == "LONG" and ask <= target) or (side == "SHORT" and bid >= target)
        if not touched:
            return False
        max_ratio = float(strat.get("entry_max_spread_atr_ratio", 0.0))
        if self._low_vol_regime(strat, atr30) and max_ratio > 0.0 and (ask - bid) / atr30 > max_ratio:
            return False
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
        raw_pending_side = st.get("pending_entry_side")
        pending_side = raw_pending_side if isinstance(raw_pending_side, str) else ""
        raw_pending_target = st.get("pending_entry_target")
        pending_target = (
            float(raw_pending_target)
            if isinstance(raw_pending_target, (int, float))
            and not isinstance(raw_pending_target, bool)
            else math.nan
        )
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
        raw_cooldown_until = st.get("cooldown_until_utc")
        cooldown_until = parse_ts(raw_cooldown_until) if isinstance(raw_cooldown_until, str) else None
        if raw_cooldown_until is not None and cooldown_until is None:
            return False, "cooldown_state_invalid"
        try:
            cooldown_minutes = int(strat.get("cooldown", 0))
        except (TypeError, ValueError, OverflowError):
            cooldown_minutes = -1
        if (
            cooldown_until is not None
            and (
                cooldown_minutes < 0
                or cooldown_until > poll_time + pd.Timedelta(minutes=cooldown_minutes)
            )
        ):
            return False, "cooldown_state_invalid"
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

    def _contain_poll_exception(self, exc: BaseException) -> None:
        """Fail new entries closed while preserving the next exit-monitoring poll."""
        self._abort_confirmed_close_state_transaction()
        details = {"type": type(exc).__name__, "message": str(exc)}
        for strat in self._all_strategies():
            try:
                self._set_sync_block(
                    strat, "poll_exception", details, recoverable=True,
                )
                if self._st(strat).get("basket"):
                    self._notify_reconciliation_required(
                        strat, "poll_exception_with_owned_inventory", details,
                    )
            except Exception:
                logging.exception(
                    "S23 could not persist poll containment for %s", strat.get("id")
                )
        try:
            self._save_state()
        except Exception:
            logging.exception("S23 poll containment state could not be persisted")

    def run_once(self) -> None:
        symbol = str(self.params.get("mt5_symbol", self.params["symbol"]))
        info = self.executor.get_symbol_info(symbol)
        if info is None:
            for strat in self._all_strategies():
                st = self._st(strat)
                if (
                    bool(strat.get("enabled", True))
                    or st.get("basket")
                    or st.get("pending_close_reason")
                    or st.get("pending_open_opportunity_id")
                ):
                    self._set_sync_block(strat, "symbol_info_failed", recoverable=True)
                    if st.get("basket"):
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
        self._pre_eu30_observer_call("observe_quote", at=quote_time, bid=float(info.bid), ask=float(info.ask))
        # Fixed-time overlay exits depend only on the executable quote and the
        # persisted actual fill time, so they remain active even if HIST/M1 is
        # temporarily unavailable.
        morning_readiness = self._process_morning_exits(info, quote_time)
        midday_readiness = self._process_midday_exits(info, quote_time)
        pre_eu30_readiness = self._process_pre_eu30_exits(info, quote_time)
        trend_recovery_readiness = self._process_trend_recovery_exits(info, quote_time)
        session_vwap_readiness = self._process_session_vwap_exits(info, quote_time)
        t0530_edge_readiness = self._process_t0530_edge_exits(info, quote_time)
        q01_readiness = self._process_q01_exits(info, quote_time)
        # History acquisition is independent of the legacy 420-bar HIST
        # consumer. This keeps backfill/retry moving even when the later legacy
        # signal path cannot evaluate a bar on this poll.
        self._refresh_session_vwap_history(info, quote_time)
        bars = self._get_m1()
        if bars is None or bars.empty:
            for strat in self.params["strategies"]:
                self._trade_row("entry_skip", strat, reason="m1_bars_unavailable")
                if not self._st(strat)["basket"]:
                    continue
                if not self._sync_strategy(strat):
                    self._save_state()
                    continue
                quote_time = utc_now()
                quote_row = pd.Series({"Open": float(info.bid), "Close": float(info.bid), "AskOpen": float(info.ask)}, name=pd.Timestamp(quote_time))
                self._monitor_open_basket(strat, info, quote_row, quote_time)
            self._process_session_vwap_entries(info, pd.Timestamp(utc_now()), session_vwap_readiness)
            self._process_q01_entries(
                bars if bars is not None else pd.DataFrame(),
                info,
                pd.Timestamp(utc_now()),
                q01_readiness,
            )
            return
        if len(bars) < 2:
            self._process_session_vwap_entries(info, pd.Timestamp(utc_now()), session_vwap_readiness)
            self._process_q01_entries(bars, info, pd.Timestamp(utc_now()), q01_readiness)
            return
        price_row = bars.iloc[-1]
        signal_bar = parse_ts(price_row.name)
        poll_time = pd.Timestamp(utc_now())
        if signal_bar is None:
            for strat in self._legacy_signal_strategies():
                if bool(strat.get("enabled", True)):
                    self._set_sync_block(strat, "signal_bar_time_invalid", {"bar_time": str(price_row.name)}, recoverable=True)
            self._save_state()
            self._process_session_vwap_entries(info, poll_time, session_vwap_readiness)
            return
        self._process_morning_entries(bars, price_row, info, poll_time, morning_readiness)
        self._process_midday_entries(bars, price_row, info, poll_time, midday_readiness)
        self._process_pre_eu30_entries(bars, price_row, info, poll_time, pre_eu30_readiness)
        self._process_trend_recovery_entry(price_row, info, poll_time, trend_recovery_readiness)
        # Match the ordered-tick replay: observe the frozen balanced-book
        # range at the first processing of each completed M1, before this
        # poll's basket exits can change the local inventory state.
        self._advance_inventory_range_fade(price_row, poll_time)
        # Process every lane's sync and exits before allowing any lane to fill a
        # pending entry or consume a new opportunity. A LONG target close in a
        # later lane therefore arms the portfolio guard before an earlier lane
        # can open a new LONG basket on the same poll.
        exit_pass_ready: dict[int, bool] = {}
        for strat in self.params["strategies"]:
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
            exit_pass_ready[lane_id] = bool(strat.get("enabled", True))
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
        previous_routed_bar = routing.get("last_routed_signal_bar")
        previous_routed_time = (
            parse_ts(previous_routed_bar)
            if isinstance(previous_routed_bar, str)
            else None
        )
        routing_receipt_valid = bool(
            previous_routed_bar is None
            or (
                isinstance(previous_routed_bar, str)
                and previous_routed_time is not None
            )
        )
        if raw_side and not routing_receipt_valid:
            routing["last_routed_signal_bar"] = signal_bar_text
            routing["last_routed_opportunity_id"] = None
            routing["last_consumed_lane_id"] = None
            routing["last_route_decision_utc"] = dt_text(poll_time)
            self._trade_row(
                "entry_skip",
                primary,
                reason="routing_decision_state_invalid",
                signal_bar_time=signal_bar_text,
                note=f"previous_last_routed_signal_bar={previous_routed_bar!r};current_bar_consumed",
            )
            self._save_state()
        if (
            raw_side
            and routing_receipt_valid
            and poll_time >= signal_bar + pd.Timedelta(minutes=1)
            and (
                previous_routed_time is None
                or previous_routed_time < signal_bar
            )
        ):
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
                    now_utc=poll_time,
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
                opportunity_time = parse_ts(opportunity.get("event_time"))
                if opportunity_time is None or not routing_receipt_valid:
                    routing["last_routed_signal_bar"] = opportunity.get("event_time")
                    routing["last_routed_opportunity_id"] = opportunity.get("opportunity_id")
                    routing["last_consumed_lane_id"] = None
                    routing["last_route_decision_utc"] = dt_text(poll_time)
                    self._trade_row(
                        "opportunity_rejected",
                        primary,
                        side=opportunity.get("side"),
                        reason="routing_decision_state_invalid",
                        note=(
                            f"previous_last_routed_signal_bar={previous_routed_bar!r};"
                            "range_opportunity_consumed"
                        ),
                        **self._opportunity_fields(opportunity),
                    )
                    self._save_state()
                    opportunity = None
                elif (
                    previous_routed_time is not None
                    and previous_routed_time >= opportunity_time
                ):
                    # Do not lower the portfolio-wide routing high-water mark.
                    self._trade_row(
                        "opportunity_rejected",
                        primary,
                        side=opportunity.get("side"),
                        reason="routing_decision_nonmonotonic",
                        note=(
                            f"high_watermark={previous_routed_bar!r};"
                            f"range_event={opportunity.get('event_time')!r};preserved"
                        ),
                        **self._opportunity_fields(opportunity),
                    )
                    opportunity = None
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
                    str(opportunity["event_time"]),
                    timeframe_hours=1.0 / 60.0,
                    max_delay_minutes=float(self.params.get("max_signal_delay_minutes", 2.0)),
                    now_utc=poll_time,
                    options=self.safety,
                )
                if stale.stale:
                    routing["last_routed_signal_bar"] = opportunity["event_time"]
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
        # Append-only overlay processing: all pre-existing entry and exit paths
        # above retain their original order and complete first.
        self._process_session_vwap_entries(info, poll_time, session_vwap_readiness)
        self._process_t0530_edge_entries(
            bars, price_row, info, poll_time, t0530_edge_readiness,
        )
        self._process_q01_entries(bars, info, poll_time, q01_readiness)
        now = time.time()
        if now - self._last_status_log >= float(self.params.get("status_log_interval_seconds", 60)):
            logging.info("S23 status: live=%s shadow=%s strategies=%s", self.live_enabled, self.shadow_enabled, {s["id"]: len(self._basket_rows(s)) for s in self._all_strategies()})
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
        return {
            "name": "BotBridge_s23",
            "version": EXPECTED_BRIDGE_VERSION,
            "commands": set(REQUIRED_SHARED_ACCOUNT_COMMANDS),
        }

    def get_account_info(self) -> dict[str, Any]:
        return {
            "margin_mode": self.margin_mode,
            "margin_mode_name": "RETAIL_HEDGING" if self.margin_mode == HEDGING_MARGIN_MODE else "RETAIL_NETTING",
            "login": MT5_LOGIN,
            "server": MT5_SERVER,
            "currency": "USD",
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
            deal_time=1767272520, exit_volume=0.01, net_profit=0.9,
        )

    def open_position(self, *_: Any, **__: Any) -> int:
        return 1

    def close_position(self, *_: Any, **__: Any) -> bool:
        return True


def load_params(path: str = PARAMS_FILE) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        params = strict_json_load(f)
    if not isinstance(params, dict):
        raise ValueError("params root must be a JSON object")
    return params


def self_test() -> None:
    params = json.loads(json.dumps(load_params()))
    params["live_trading_enabled"] = False
    params["shadow_forward_enabled"] = True
    params["shadow_opportunity_observer"]["enabled"] = False
    params["shadow_state_tagger"]["enabled"] = False
    params["midday_shadow_opportunity_observer"]["enabled"] = False
    params["midday_shadow_state_tagger"]["enabled"] = False
    params["pre_eu30_shadow_opportunity_observer"]["enabled"] = False
    params["pre_eu30_shadow_state_tagger"]["enabled"] = False
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
    assert params["pre_eu30_session_policy_id"] == PRE_EU30_POLICY_ID
    assert params["pre_eu30_session_params_hash"] == PRE_EU30_POLICY_PARAMS_HASH
    assert tuple(int(row["magic"]) for row in params["pre_eu30_session_strategies"]) == EXPECTED_PRE_EU30_MAGICS
    assert [int(row["lane_id"]) for row in params["pre_eu30_session_strategies"]] == [9, 10, 11]
    assert [int(row["hold_minutes"]) for row in params["pre_eu30_session_strategies"]] == [45, 60, 45]
    assert params["q01_policy_id"] == EXPECTED_Q01_POLICY_ID
    assert params["q01_params_hash"] == EXPECTED_Q01_POLICY_PARAMS_HASH
    assert tuple(int(row["magic"]) for row in params["q01_variance_release_strategies"]) == EXPECTED_Q01_MAGICS
    assert [int(row["lane_id"]) for row in params["q01_variance_release_strategies"]] == [22]
    assert [int(row["hold_minutes"]) for row in params["q01_variance_release_strategies"]] == [EXPECTED_Q01_HOLD_MINUTES]
    assert int(params["q01_m1_bars"]) == EXPECTED_Q01_M1_BARS
    assert int(params["q01_warmup_m5_bars"]) == EXPECTED_Q01_WARMUP_M5_BARS
    assert int(params["q01_atr_period"]) == EXPECTED_Q01_ATR_PERIOD
    assert int(params["q01_feed_gap_seconds"]) == EXPECTED_Q01_FEED_GAP_SECONDS
    assert math.isclose(float(params["q01_max_raw_spread_price"]), EXPECTED_Q01_MAX_RAW_SPREAD_PRICE)
    assert int(params["m1_bars"]) == EXPECTED_PRE_EU30_M1_BARS
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

    pending_now = pd.Timestamp("2026-08-25T13:01:02Z")
    pending_event = pending_now - pd.Timedelta(minutes=1)
    state.update({
        "pending_entry_side": "LONG",
        "pending_entry_target": 2064.05,
        "pending_entry_expires_utc": dt_text(pending_now + pd.Timedelta(minutes=5)),
        "pending_entry_atr30": 1.5,
        "pending_entry_signal_bar": dt_text(pending_event),
        "pending_entry_opportunity_id": f"XAUUSD|{dt_text(pending_event)}|LONG|LONG|{EXPECTED_ENTRY_POLICY_ID}",
        "pending_entry_event_time": dt_text(pending_event),
        "pending_entry_release_time": dt_text(pending_now),
    })
    assert runner._monitor_pending_entry(strategy, SimpleNamespace(bid=2064.02, ask=2064.05), pending_now)
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
    # Validate before logging setup or any bridge/runtime construction so
    # coercible log, timing, execution, and risk fields cannot take effect.
    validate_boolean_config(params)
    validate_strategy_topology_config(params)
    validate_execution_numeric_config(params)
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
    runner_lock = acquire_runner_singleton_lock()
    if runner_lock is None:
        logging.critical("Another bot23 runner already owns the state/order namespace; refusing to start")
        return 1
    runner = S23HorizontalInventoryRunner(params)
    if not runner.connect_and_preflight():
        return 1
    if args.once:
        try:
            runner.run_once()
            return 0
        except Exception as exc:
            logging.exception("S23 poll failed")
            runner._contain_poll_exception(exc)
            return 1
    while True:
        try:
            runner.run_once()
        except Exception as exc:
            logging.exception("S23 poll failed; entries contained and polling will continue")
            runner._contain_poll_exception(exc)
        time.sleep(float(params.get("poll_interval_seconds", 5)))


if __name__ == "__main__":
    raise SystemExit(main())
