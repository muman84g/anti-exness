# -*- coding: utf-8 -*-
#プロ口座専用
# ==============================================================================
# STRATEGY s14 CONCEPT: GBPUSD Move-Catcher Paired Reverse-EA Live Trading Bot
# 【戦略s14コンセプト: GBPUSD 二系統反転EA＋等間隔配置＋二数列分解管理】
# ------------------------------------------------------------------------------
# - Instrument: GBPUSD (GBP/USD Pro Account)
# - Logic: Bot A/B both reverse on TP and continue in the same direction on SL.
# - Initial placement: Bot B starts in the same direction at 0.5W from Bot A's first entry.
# - Pair geometry: new entries are restricted to 0.5W from the other entry.
# - Weekend/News intervention: disabled in article-faithful configuration
# - Money Management: article-exact independent [0, 1] decomposed Monte Carlo
# ==============================================================================
import os
import sys
import time
import json
import logging
import traceback
import csv
import uuid
import shutil
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import warnings

warnings.filterwarnings('ignore')

JST = timezone(timedelta(hours=9), "JST")

# Absolute path of the current script
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# Logging Setup
LOG_DIR = os.path.join(script_dir, "logs")
LOG_FILE = os.path.join(LOG_DIR, "s14_bot.log")


def configure_logging():
    """実行時だけログ出力先を作成し、import時のファイル副作用を避ける。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )


if __name__ == "__main__":
    configure_logging()

# Import dependencies
from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_BUY, ORDER_TYPE_SELL
from s14_pair_alignment import (
    classify_pair_mode,
    evaluate_pair_alignment,
    is_spread_allowed,
    next_direction_after_outcome,
    protection_levels_match,
)
from s14_money_management import (
    ARTICLE_DMC_STATE_VERSION,
    MonteCarloManager,
    calculate_article_lot,
)

# ============================================================
# Bot Configuration & Path Variables
# ============================================================
POLL_INTERVAL_SECONDS = 1  # TP/SL後の再エントリー遅延を抑える
STATE_DIR = os.path.join(script_dir, "state")
STATE_FILE = os.path.join(STATE_DIR, "s14_bot_state.json")
PARAMS_FILE = os.path.join(script_dir, "s14_params.json")

DEFAULT_PARAMS = {
    'symbol': 'GBPUSD',
    'live_trading_enabled': False,
    'allow_unbounded_bet_units': False,
    'W_pips': 43.0,
    'initial_offset_ratio': 0.50,
    'lot_multiplier': 0.01,
    'max_bet_units': 0,
    'initial_sequence': [0, 1],
    'weekend_filter': False,
    'weekend_stop_hour_jst': 2,
    'weekend_entry_stop_weekday_jst': 5,
    'weekend_entry_stop_hour_jst': 2,
    'weekend_entry_stop_minute_jst': 0,
    'weekend_close_weekday_jst': 5,
    'weekend_close_hour_jst': 2,
    'weekend_close_minute_jst': 30,
    'monday_start_hour_jst': 8,
    'monday_start_minute_jst': 0,
    'news_filter': False,
    'avoidance_hours': 0.0,
    'news_file': 'macro_events_2026.json',
    'max_spread_pips': 0.9,
    'enabled': True,
    'pair_alignment_target_ratio': 0.50,
    'pair_alignment_tolerance_pips': 0.6,
    'pair_alignment_spread_multiplier': 2.0,
    'enforce_pair_alignment_on_reentry': True,
    'repair_missing_sl_tp_on_sync': True,
    'warn_sl_tp_mismatch_pips': 0.2
}

def load_params():
    if os.path.exists(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, "r") as f:
                params = json.load(f)
            logging.info(f"Successfully loaded parameters from {PARAMS_FILE}")
            # Fill missing keys
            for k, v in DEFAULT_PARAMS.items():
                if k not in params:
                    params[k] = v
            return params
        except Exception as e:
            logging.error(f"Error loading {PARAMS_FILE}, using default parameters: {e}")
            return DEFAULT_PARAMS.copy()
    else:
        try:
            with open(PARAMS_FILE, "w") as f:
                json.dump(DEFAULT_PARAMS, f, indent=4)
            logging.info(f"Created default parameters file at {PARAMS_FILE}")
        except Exception as e:
            logging.error(f"Failed to create default parameters file: {e}")
        return DEFAULT_PARAMS.copy()

def normalize_params(raw_params, overrides=None):
    params = DEFAULT_PARAMS.copy()
    if raw_params:
        params.update(raw_params)
    if overrides:
        params.update(overrides)
    return params

def build_param_profiles(raw_params):
    if not isinstance(raw_params, dict):
        return [DEFAULT_PARAMS.copy()]

    symbols_config = raw_params.get("symbols")
    if not symbols_config:
        return [normalize_params(raw_params)]

    shared = {k: v for k, v in raw_params.items() if k != "symbols"}
    profiles = []
    if isinstance(symbols_config, dict):
        iterable = []
        for symbol, symbol_params in symbols_config.items():
            item = dict(symbol_params or {})
            item.setdefault("symbol", symbol)
            iterable.append(item)
    else:
        iterable = list(symbols_config)

    seen = set()
    for item in iterable:
        if isinstance(item, str):
            item = {"symbol": item}
        if not isinstance(item, dict):
            logging.warning(f"Skipping invalid symbol profile: {item}")
            continue
        if not bool(item.get("enabled", True)):
            logging.info(f"Skipping disabled symbol profile: {item.get('symbol', '<unknown>')}")
            continue
        profile = normalize_params(shared, item)
        symbol = str(profile.get("symbol", "")).strip()
        if not symbol:
            logging.warning(f"Skipping symbol profile without symbol: {item}")
            continue
        if symbol in seen:
            logging.warning(f"Skipping duplicate symbol profile: {symbol}")
            continue
        profile["symbol"] = symbol
        seen.add(symbol)
        profiles.append(profile)

    if not profiles:
        raise ValueError("No enabled symbol profiles are configured.")
    return profiles


def apply_confirmed_pending_open_reconciliations(state_by_symbol, directives):
    """Apply explicit operator-confirmed pending-open reconciliations."""
    if not isinstance(directives, list):
        return []

    applied = []
    for directive in directives:
        if not isinstance(directive, dict):
            continue
        if directive.get("confirmed_no_position_order_or_deal") is not True:
            continue

        symbol = str(directive.get("symbol", "")).strip()
        request_id = str(directive.get("request_id", "")).strip()
        state = state_by_symbol.get(symbol)
        if not symbol or not request_id or not isinstance(state, dict):
            continue

        pending_open = state.get("pending_open")
        if not isinstance(pending_open, dict):
            continue
        if (
            pending_open.get("symbol") != symbol
            or pending_open.get("request_id") != request_id
        ):
            continue

        state.pop("pending_open", None)
        reconciliation = state.get("reconciliation_required")
        if (
            isinstance(reconciliation, dict)
            and reconciliation.get("type") == "pending_open"
            and reconciliation.get("request_id") == request_id
        ):
            state.pop("reconciliation_required", None)

        expected_reason = f"Unresolved pending_open request: {request_id}"
        if state.get("sync_block_reason") == expected_reason:
            state.pop("sync_block_reason", None)
            state.pop("sync_block_new_entries", None)

        audit_entry = {
            "request_id": request_id,
            "confirmed_at_jst": str(directive.get("confirmed_at_jst", "")),
            "reason": str(directive.get("reason", "")),
        }
        audit_log = state.setdefault("manual_pending_open_reconciliations", [])
        if not any(
            isinstance(item, dict) and item.get("request_id") == request_id
            for item in audit_log
        ):
            audit_log.append(audit_entry)
        applied.append({"symbol": symbol, "request_id": request_id})

    return applied


def apply_confirmed_missing_position_reconciliations(
    state_by_symbol,
    manager_by_symbol,
    directives,
):
    """Apply exact operator-confirmed exits to legacy blocked positions."""
    if not isinstance(directives, list):
        return []

    applied = []
    for directive in directives:
        if not isinstance(directive, dict):
            continue
        if directive.get("confirmed_by_operator") is not True:
            continue

        symbol = str(directive.get("symbol", "")).strip()
        bot_type = str(directive.get("bot_type", "")).strip().upper()
        direction = str(directive.get("direction", "")).strip().upper()
        outcome = str(directive.get("outcome", "")).strip().upper()
        try:
            ticket = int(directive.get("ticket", 0))
        except (TypeError, ValueError):
            continue
        if (
            not symbol
            or bot_type not in {"A", "B"}
            or direction not in {"LONG", "SHORT"}
            or outcome not in {"WIN", "LOSE"}
            or ticket <= 0
        ):
            continue

        state = state_by_symbol.get(symbol)
        manager = manager_by_symbol.get(symbol)
        if not isinstance(state, dict) or manager is None:
            continue
        pos_key = "pos_A" if bot_type == "A" else "pos_B"
        next_key = "next_direction_A" if bot_type == "A" else "next_direction_B"
        position = state.get(pos_key)
        if not isinstance(position, dict):
            continue
        if (
            int(position.get("ticket", 0)) != ticket
            or position.get("direction") != direction
            or position.get("missing_on_mt5") is not True
        ):
            continue

        bet_units = int(position.get("bet_units", 0))
        if bot_type == "A":
            manager.update_mc(outcome, None, bet_units, 0)
        else:
            manager.update_mc(None, outcome, 0, bet_units)
        state[pos_key] = None
        state[next_key] = next_direction_after_outcome(direction, outcome)
        if bot_type == "A" and not state.get("pair_initialized"):
            state["initial_anchor_A"] = None

        pending_close = state.get("pending_close")
        if isinstance(pending_close, dict) and pending_close.get("ticket") == ticket:
            state.pop("pending_close", None)
        reconciliation = state.get("reconciliation_required")
        if isinstance(reconciliation, dict) and reconciliation.get("ticket") == ticket:
            state.pop("reconciliation_required", None)
        state.pop("sync_block_new_entries", None)
        state.pop("sync_block_reason", None)
        state["pair_mode"] = classify_pair_mode(
            state.get("pos_A"),
            state.get("pos_B"),
            state.get("pair_initialized", False),
        )
        state["mc_manager"] = manager.to_dict()

        audit_entry = {
            "ticket": ticket,
            "bot_type": bot_type,
            "direction": direction,
            "outcome": outcome,
            "confirmed_at_jst": str(directive.get("confirmed_at_jst", "")),
            "reason": str(directive.get("reason", "")),
        }
        audit_log = state.setdefault("manual_missing_position_reconciliations", [])
        if not any(
            isinstance(item, dict) and int(item.get("ticket", 0)) == ticket
            for item in audit_log
        ):
            audit_log.append(audit_entry)
        applied.append(
            {
                "symbol": symbol,
                "ticket": ticket,
                "bot_type": bot_type,
                "outcome": outcome,
            }
        )

    return applied


def backup_state_before_pending_open_reconciliation(state_file, applied):
    """Preserve the pre-reconciliation state without overwriting an old backup."""
    if not applied:
        return None

    def safe_token(value):
        return "".join(
            char if char.isalnum() or char in "-_" else "_"
            for char in str(value)
        )

    suffix = "__".join(
        f"{safe_token(item['symbol'])}_{safe_token(item['request_id'])}"
        for item in applied
    )
    backup_file = f"{state_file}.before_pending_reconcile_{suffix}.bak"
    if not os.path.exists(backup_file):
        shutil.copy2(state_file, backup_file)
    return backup_file


def backup_state_before_missing_position_reconciliation(state_file, applied):
    """Preserve state before applying confirmed legacy exit outcomes."""
    if not applied:
        return None

    suffix = "__".join(
        f"{item['symbol']}_{item['bot_type']}_{item['ticket']}_{item['outcome']}"
        for item in applied
    )
    backup_file = f"{state_file}.before_exit_reconcile_{suffix}.bak"
    if not os.path.exists(backup_file):
        shutil.copy2(state_file, backup_file)
    return backup_file

RAW_PARAMS = load_params()
PARAM_PROFILES = build_param_profiles(RAW_PARAMS)
PARAMS = PARAM_PROFILES[0]

S14_MAGIC_A = 140034
S14_MAGIC_B = 140035
S14_COMMENT_A = "s14_article_A"
S14_COMMENT_B = "s14_article_B"

# ============================================================
# Decomposed Monte Carlo Logic Classes (from Backtest)
# ============================================================
# Helpers
# ============================================================
def weekly_minute_jst(t_jst):
    return int(t_jst.weekday()) * 24 * 60 + int(t_jst.hour) * 60 + int(t_jst.minute)

def is_in_weekly_window_jst(t_jst, start_weekday, start_hour, start_minute, end_weekday, end_hour, end_minute):
    current = weekly_minute_jst(t_jst)
    start = int(start_weekday) * 24 * 60 + int(start_hour) * 60 + int(start_minute)
    end = int(end_weekday) * 24 * 60 + int(end_hour) * 60 + int(end_minute)
    if start <= end:
        return start <= current < end
    return current >= start or current < end

def is_weekend_entry_blocked_jst(
    t_jst,
    entry_stop_weekday=5,
    entry_stop_hour=4,
    entry_stop_minute=0,
    monday_start_hour=8,
    monday_start_minute=0,
):
    return is_in_weekly_window_jst(
        t_jst,
        entry_stop_weekday,
        entry_stop_hour,
        entry_stop_minute,
        0,
        monday_start_hour,
        monday_start_minute,
    )

def is_weekend_close_window_jst(
    t_jst,
    close_weekday=5,
    close_hour=4,
    close_minute=30,
    monday_start_hour=8,
    monday_start_minute=0,
):
    return is_in_weekly_window_jst(
        t_jst,
        close_weekday,
        close_hour,
        close_minute,
        0,
        monday_start_hour,
        monday_start_minute,
    )

class S14TradingBot:
    def __init__(self):
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        self.param_profiles = PARAM_PROFILES
        self.params = None
        self.active_symbol = None
        self.state_by_symbol = {}
        self.mc_manager_by_symbol = {}
        self.uses_multi_symbol_state = len(self.param_profiles) > 1
        self.state = {}
        self.mc_manager = None
        self.persistence_healthy = True
        self.state_load_error = None
        self.macro_times = []
        self.load_news_events()
        self.load_state()
        self.apply_configured_pending_open_reconciliations()
        self.apply_configured_missing_position_reconciliations()
        for params in self.param_profiles:
            self.activate_profile(params)
            logging.info(
                "Effective s14 params: "
                f"symbol={PARAMS.get('symbol')} "
                f"W_pips={PARAMS.get('W_pips')} "
                f"initial_offset_ratio={PARAMS.get('initial_offset_ratio')} "
                f"pair_alignment_target_ratio={PARAMS.get('pair_alignment_target_ratio')} "
                f"lot_multiplier={PARAMS.get('lot_multiplier')} "
                f"max_bet_units={PARAMS.get('max_bet_units')} "
                f"max_spread_pips={PARAMS.get('max_spread_pips')} "
                f"money_management={ARTICLE_DMC_STATE_VERSION} "
                f"avoidance_hours={PARAMS.get('avoidance_hours')} "
                f"repair_missing_sl_tp_on_sync={PARAMS.get('repair_missing_sl_tp_on_sync')}"
            )
            logging.info(
                "Restored s14 state: "
                f"symbol={PARAMS.get('symbol')} "
                f"pos_A={self.state.get('pos_A')} "
                f"pos_B={self.state.get('pos_B')} "
                f"next_direction_A={self.state.get('next_direction_A')} "
                f"next_direction_B={self.state.get('next_direction_B')} "
                f"pair_initialized={self.state.get('pair_initialized')} "
                f"initial_anchor_A={self.state.get('initial_anchor_A')} "
                f"pair_mode={self.state.get('pair_mode')} "
                f"mc_manager={json.dumps(self.mc_manager.to_dict(), ensure_ascii=True)}"
            )

    def activate_profile(self, params):
        global PARAMS
        symbol = params["symbol"]
        PARAMS = params
        self.params = params
        self.active_symbol = symbol
        self.state = self.state_by_symbol[symbol]
        self.mc_manager = self.mc_manager_by_symbol[symbol]

    def apply_configured_pending_open_reconciliations(self):
        directives = RAW_PARAMS.get("confirmed_pending_open_reconciliations", [])
        applied = apply_confirmed_pending_open_reconciliations(
            self.state_by_symbol,
            directives,
        )
        if not applied:
            return
        try:
            backup_file = backup_state_before_pending_open_reconciliation(
                STATE_FILE,
                applied,
            )
        except Exception as exc:
            self.state_load_error = (
                "Confirmed pending_open reconciliation backup failed: " + str(exc)
            )
            logging.critical(self.state_load_error)
            return
        if not self.save_state():
            self.state_load_error = (
                "Confirmed pending_open reconciliation could not be persisted."
            )
            return
        logging.warning(f"Pre-reconciliation state backup: {backup_file}")
        for item in applied:
            logging.warning(
                f"[{item['symbol']}] Cleared operator-confirmed pending_open "
                f"request {item['request_id']}; no live position, order, or deal "
                "was found during manual reconciliation."
            )

    def apply_configured_missing_position_reconciliations(self):
        directives = RAW_PARAMS.get(
            "confirmed_missing_position_reconciliations",
            [],
        )
        applied = apply_confirmed_missing_position_reconciliations(
            self.state_by_symbol,
            self.mc_manager_by_symbol,
            directives,
        )
        if not applied:
            return
        try:
            backup_file = backup_state_before_missing_position_reconciliation(
                STATE_FILE,
                applied,
            )
        except Exception as exc:
            self.state_load_error = (
                "Confirmed missing-position reconciliation backup failed: "
                + str(exc)
            )
            logging.critical(self.state_load_error)
            return
        if not self.save_state():
            self.state_load_error = (
                "Confirmed missing-position reconciliation could not be persisted."
            )
            return
        logging.warning(f"Pre-exit-reconciliation state backup: {backup_file}")
        for item in applied:
            logging.warning(
                f"[{item['symbol']}][Bot {item['bot_type']}] Applied operator-confirmed "
                f"{item['outcome']} for missing ticket {item['ticket']}."
            )

    def load_news_events(self):
        news_file = RAW_PARAMS.get("news_file", PARAMS.get("news_file", "macro_events_2026.json"))
        
        # Paths to search
        paths_to_try = [
            os.path.join(script_dir, news_file),
            os.path.join(os.path.dirname(script_dir), "data", news_file),
        ]
        
        events_loaded = False
        for p in paths_to_try:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        events = json.load(f)
                    self.macro_times = [pd.Timestamp(ev['release_time_jst'], tz='Asia/Tokyo') for ev in events]
                    logging.info(f"Successfully loaded {len(self.macro_times)} news events from {p}")
                    events_loaded = True
                    break
                except Exception as e:
                    logging.error(f"Error reading news file {p}: {e}")
        
        if not events_loaded:
            logging.warning("No news events file found or loaded. News avoidance will be disabled.")
            self.macro_times = []

    def is_in_news_window(self, t_jst):
        if not PARAMS.get("news_filter", False) or not self.macro_times:
            return False
        avoidance_hours = PARAMS.get("avoidance_hours", 2.0)
        if avoidance_hours <= 0:
            return False
        dt = pd.Timedelta(hours=avoidance_hours)
        for mt in self.macro_times:
            if mt - dt <= t_jst <= mt + dt:
                return True
        return False

    def load_state(self):
        loaded_state = None
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    loaded_state = json.load(f)
                logging.info("Successfully loaded state file.")
            except Exception as e:
                self.state_load_error = str(e)
                logging.critical(f"Error loading state file: {e}")
                loaded_state = None

        symbol_states = {}
        if isinstance(loaded_state, dict) and isinstance(loaded_state.get("symbols"), dict):
            if loaded_state.get("version") != 3:
                self.state_load_error = (
                    "Unsupported multi-symbol state version. "
                    "Archive the old state and initialize a new version 3 state."
                )
                logging.critical(self.state_load_error)
            else:
                self.uses_multi_symbol_state = True
                symbol_states = loaded_state["symbols"]
        elif isinstance(loaded_state, dict) and "pos_A" in loaded_state:
            self.state_load_error = (
                "Incompatible legacy single-symbol state detected. "
                "Archive the old state and initialize a new version 3 state."
            )
            logging.critical(self.state_load_error)

        for params in self.param_profiles:
            symbol = params["symbol"]
            state, manager = self.build_runtime_state(params, symbol_states.get(symbol))
            self.state_by_symbol[symbol] = state
            self.mc_manager_by_symbol[symbol] = manager

    def build_runtime_state(self, params, state_data=None):
        state = {
            "pos_A": None,
            "pos_B": None,
            "next_direction_A": "LONG",
            "next_direction_B": None,
            "pair_initialized": False,
            "initial_anchor_A": None,
            "pair_mode": "INITIALIZING",
            "mc_manager": {}
        }
        if isinstance(state_data, dict):
            state.update(state_data)

        state.pop("waiting_B", None)
        state.pop("S", None)
        state["pair_initialized"] = bool(
            state.get("pair_initialized") or state.get("pos_B")
        )
        if state.get("pos_A") and state.get("initial_anchor_A") is None:
            state["initial_anchor_A"] = state["pos_A"].get("entry_price")
        state["pair_mode"] = classify_pair_mode(
            state.get("pos_A"), state.get("pos_B"), state["pair_initialized"]
        )

        manager = MonteCarloManager(initial_sequence=params.get("initial_sequence", [0, 1]))
        if isinstance(state.get("mc_manager"), dict):
            manager.from_dict(state["mc_manager"])
        state["mc_manager"] = manager.to_dict()
        return state, manager

    def init_empty_state(self):
        self.state_by_symbol = {}
        self.mc_manager_by_symbol = {}
        for params in self.param_profiles:
            state, manager = self.build_runtime_state(params)
            self.state_by_symbol[params["symbol"]] = state
            self.mc_manager_by_symbol[params["symbol"]] = manager
        if self.param_profiles:
            self.activate_profile(self.param_profiles[0])
        self.save_state()

    def save_state(self):
        temp_state_file = STATE_FILE + ".tmp"
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            if self.active_symbol:
                self.state["mc_manager"] = self.mc_manager.to_dict()
                self.state_by_symbol[self.active_symbol] = self.state
            if self.uses_multi_symbol_state:
                state_to_save = {
                    "version": 3,
                    "symbols": self.state_by_symbol,
                }
            else:
                state_to_save = self.state
            with open(temp_state_file, "w", encoding="utf-8", newline="\n") as f:
                json.dump(state_to_save, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_state_file, STATE_FILE)
            self.persistence_healthy = True
            return True
        except Exception as e:
            self.persistence_healthy = False
            logging.critical(f"Failed to atomically save state: {e}")
            return False

    def update_pair_mode(self):
        new_mode = classify_pair_mode(
            self.state.get("pos_A"),
            self.state.get("pos_B"),
            self.state.get("pair_initialized", False),
        )
        old_mode = self.state.get("pair_mode")
        self.state["pair_mode"] = new_mode
        if old_mode != new_mode:
            logging.info(f"Pair mode changed: {old_mode} -> {new_mode}")
        return new_mode

    def open_bot_position(
        self,
        bot_type,
        direction,
        symbol,
        info,
        now_jst,
        W,
        W_pips,
        pip_val,
        current_spread,
    ):
        pos_key = "pos_A" if bot_type == "A" else "pos_B"
        other_key = "pos_B" if bot_type == "A" else "pos_A"
        next_key = "next_direction_A" if bot_type == "A" else "next_direction_B"
        magic = S14_MAGIC_A if bot_type == "A" else S14_MAGIC_B
        comment = S14_COMMENT_A if bot_type == "A" else S14_COMMENT_B
        mc = self.mc_manager.mc_A if bot_type == "A" else self.mc_manager.mc_B

        expected_px = info.ask if direction == "LONG" else info.bid
        aligned, distance_pips, target_pips, tolerance_pips = evaluate_pair_alignment(
            expected_px,
            self.state.get(other_key),
            pip_value=pip_val,
            w_pips=W_pips,
            target_ratio=PARAMS.get("pair_alignment_target_ratio", 0.5),
            tolerance_pips=PARAMS.get("pair_alignment_tolerance_pips", 0.6),
            current_spread_price=current_spread,
            spread_multiplier=PARAMS.get("pair_alignment_spread_multiplier", 2.0),
        )
        initial_pair_entry = bot_type == "B" and not self.state.get("pair_initialized")
        enforce_alignment = initial_pair_entry or bool(
            PARAMS.get("enforce_pair_alignment_on_reentry", True)
        )
        if not aligned and enforce_alignment:
            distance_text = "unknown" if distance_pips is None else f"{distance_pips:.1f}"
            logging.info(
                f"[Bot {bot_type}] Entry postponed: pair distance {distance_text} pips; "
                f"target {target_pips:.1f} +/- {tolerance_pips:.1f} pips."
            )
            return False
        if not aligned:
            logging.warning(
                f"[Bot {bot_type}] Re-entering immediately outside ideal pair distance: "
                f"distance={distance_pips}, target={target_pips:.1f}, "
                f"tolerance={tolerance_pips:.1f} pips. Maintenance is required."
            )

        bet_units = mc.get_bet_units()
        try:
            lot = calculate_article_lot(
                bet_units, PARAMS["lot_multiplier"], PARAMS["max_bet_units"], info
            )
        except ValueError as exc:
            logging.critical(f"[Bot {bot_type}] Entry blocked: {exc}")
            return False
        order_type = ORDER_TYPE_BUY if direction == "LONG" else ORDER_TYPE_SELL
        price_digits = int(getattr(info, "digits", 5))
        if direction == "LONG":
            initial_tp = round(expected_px + W, price_digits)
            initial_sl = round(expected_px - W, price_digits)
        else:
            initial_tp = round(expected_px - W, price_digits)
            initial_sl = round(expected_px + W, price_digits)

        logging.info(
            f"[Bot {bot_type}] Preparing to open {direction} | "
            f"Bet Units: {bet_units} | Lot: {lot}"
        )
        if self.state.get("pending_open"):
            logging.critical(
                f"[Bot {bot_type}] Entry blocked because an unresolved pending_open exists."
            )
            return False

        request_id = uuid.uuid4().hex[:8]
        request_comment = f"{comment}:{bet_units}:{request_id}"[:31]
        self.state["pending_open"] = {
            "request_id": request_id,
            "comment": request_comment,
            "bot_type": bot_type,
            "symbol": symbol,
            "direction": direction,
            "order_type": int(order_type),
            "lot": float(lot),
            "bet_units": int(bet_units),
            "requested_sl": float(initial_sl),
            "requested_tp": float(initial_tp),
            "requested_at_jst": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "INTENT_PERSISTED",
        }
        if not self.save_state():
            logging.critical(
                f"[Bot {bot_type}] Entry blocked because pending_open could not be persisted."
            )
            return False

        ticket = self.executor.open_position(
            symbol,
            order_type,
            lot,
            sl=initial_sl,
            tp=initial_tp,
            magic=magic,
            comment=request_comment,
        )
        if not ticket:
            pending_open = self.state.get("pending_open")
            if isinstance(pending_open, dict):
                pending_open["status"] = "OPEN_RESPONSE_UNCONFIRMED"
                pending_open["last_checked_at_jst"] = datetime.now(JST).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                self.save_state()
            logging.critical(
                f"[Bot {bot_type}] OPEN did not return a confirmed ticket. "
                "The durable intent is retained and automatic retry is blocked."
            )
            return False

        exec_px = self.resolve_entry_price(ticket, expected_px, bot_type)
        if direction == "LONG":
            tp = round(exec_px + W, price_digits)
            sl = round(exec_px - W, price_digits)
        else:
            tp = round(exec_px - W, price_digits)
            sl = round(exec_px + W, price_digits)
        protection_sync_pending = not self.executor.modify_position_sl_tp(ticket, sl, tp)
        server_sl = sl
        server_tp = tp
        if protection_sync_pending:
            live_pos = self.executor.get_position(ticket)
            if live_pos is not None:
                server_sl = self.positive_float(getattr(live_pos, "sl", 0.0)) or initial_sl
                server_tp = self.positive_float(getattr(live_pos, "tp", 0.0)) or initial_tp
            else:
                server_sl = initial_sl
                server_tp = initial_tp
            logging.critical(
                f"[Bot {bot_type}] Position {ticket} opened, but SL/TP could not be "
                "synchronized to the execution price. Further entries are blocked until repair."
            )

        self.state[pos_key] = {
            "ticket": int(ticket),
            "direction": direction,
            "entry_time": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price": exec_px,
            "tp": float(server_tp),
            "sl": float(server_sl),
            "bet_units": int(bet_units),
            "lot_size": float(lot),
        }
        if protection_sync_pending:
            self.state[pos_key].update(
                {
                    "protection_sync_pending": True,
                    "desired_sl": float(sl),
                    "desired_tp": float(tp),
                }
            )
        actual_aligned, actual_distance, target_pips, actual_tolerance = evaluate_pair_alignment(
            exec_px,
            self.state.get(other_key),
            pip_value=pip_val,
            w_pips=W_pips,
            target_ratio=PARAMS.get("pair_alignment_target_ratio", 0.5),
            tolerance_pips=PARAMS.get("pair_alignment_tolerance_pips", 0.6),
            current_spread_price=current_spread,
            spread_multiplier=PARAMS.get("pair_alignment_spread_multiplier", 2.0),
        )
        if not actual_aligned:
            self.state["pair_alignment_warning"] = {
                "bot_type": bot_type,
                "distance_pips": actual_distance,
                "target_pips": target_pips,
                "tolerance_pips": actual_tolerance,
                "detected_at_jst": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
            }
            logging.error(
                f"[Bot {bot_type}] Executed outside pair tolerance: "
                f"distance={actual_distance}, target={target_pips:.1f}, "
                f"tolerance={actual_tolerance:.1f} pips."
            )
        else:
            self.state.pop("pair_alignment_warning", None)
        self.state[next_key] = None
        if bot_type == "A" and not self.state.get("pair_initialized"):
            self.state["initial_anchor_A"] = exec_px
        elif bot_type == "B":
            self.state["pair_initialized"] = True
        self.update_pair_mode()
        self.state.pop("pending_open", None)
        if not self.save_state():
            logging.critical(
                f"[Bot {bot_type}] Position {int(ticket)} opened but the confirmed "
                "position state could not be persisted. Further cycles are blocked."
            )
            return False
        self.log_trade_csv(
            f"ENTRY_{bot_type}", int(ticket), symbol, direction, lot, exec_px
        )
        return True

    def write_trade_log_row(self, csv_file, header, row):
        file_exists = os.path.isfile(csv_file) and os.path.getsize(csv_file) > 0
        active_header = header
        if file_exists:
            try:
                with open(csv_file, mode="r", newline="", encoding="utf-8-sig") as f:
                    active_header = next(csv.reader(f), header)
            except Exception as e:
                logging.warning(f"Failed to read existing trade CSV header: {e}")
                active_header = header

        row_map = dict(zip(header, row))
        if active_header != header:
            legacy_header = header[:-1]
            if active_header == legacy_header:
                row = [row_map.get(col, "") for col in active_header]
            else:
                logging.warning(
                    f"Unexpected trade CSV header in {csv_file}. Writing to a v2 CSV instead."
                )
                csv_file = csv_file.replace(".csv", "_v2.csv")
                file_exists = os.path.isfile(csv_file) and os.path.getsize(csv_file) > 0
                active_header = header
                row = [row_map.get(col, "") for col in active_header]
        else:
            row = [row_map.get(col, "") for col in active_header]

        with open(csv_file, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(active_header)
            writer.writerow(row)

    def log_trade_csv(self, action, ticket, symbol, direction="", lot_size=0.0, price=0.0, pnl=0.0, reason=""):
        csv_file = os.path.join(
            LOG_DIR,
            "s14_trade_errors.csv" if action.startswith("EXIT_FAIL_") else "s14_trades.csv",
        )
        now_jst = datetime.now(JST)
        header = ["Timestamp_JST", "Action", "Ticket", "Symbol", "Direction", "LotSize", "Price", "PnL", "Reason"]
        row = [
            now_jst.strftime("%Y-%m-%d %H:%M:%S"),
            action,
            ticket,
            symbol,
            direction,
            lot_size,
            "" if price is None else price,
            "" if pnl is None else pnl,
            reason,
        ]
        try:
            self.write_trade_log_row(csv_file, header, row)
        except Exception as e:
            logging.error(f"Failed to write trade log to CSV: {e}")

    @staticmethod
    def positive_float(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        return value if value > 0.0 else 0.0

    def resolve_entry_price(self, ticket, fallback_price, bot_type):
        exec_price = self.positive_float(getattr(ticket, "price", 0.0))
        if exec_price > 0.0:
            return exec_price

        fallback_price = self.positive_float(fallback_price)
        if fallback_price > 0.0:
            logging.warning(
                f"[Bot {bot_type}] EA did not return entry price for ticket {int(ticket)}. "
                f"Using current market price {fallback_price:.5f} for state and CSV."
            )
            return fallback_price

        return 0.0

    def get_market_exit_price(self, symbol, direction, current_bid=None, current_ask=None):
        bid = self.positive_float(current_bid)
        ask = self.positive_float(current_ask)

        if direction == "LONG" and bid > 0.0:
            return bid
        if direction == "SHORT" and ask > 0.0:
            return ask

        try:
            info = self.executor.get_symbol_info(symbol)
        except Exception as e:
            logging.warning(f"Failed to fetch market price for exit log: {e}")
            return 0.0

        if not info:
            return 0.0
        if direction == "LONG":
            return self.positive_float(getattr(info, "bid", 0.0))
        if direction == "SHORT":
            return self.positive_float(getattr(info, "ask", 0.0))
        return 0.0

    def estimate_gross_pnl(self, symbol, direction, entry_price, exit_price, lot):
        entry_price = self.positive_float(entry_price)
        exit_price = self.positive_float(exit_price)
        lot = self.positive_float(lot)
        if entry_price <= 0.0 or exit_price <= 0.0 or lot <= 0.0:
            return 0.0

        contract_size = 100000.0
        if direction == "LONG":
            pnl_quote = (exit_price - entry_price) * lot * contract_size
        elif direction == "SHORT":
            pnl_quote = (entry_price - exit_price) * lot * contract_size
        else:
            return 0.0

        if "JPY" in symbol:
            return pnl_quote / exit_price
        return pnl_quote

    def get_exit_log_values(self, symbol, pos, close_result=None, current_bid=None, current_ask=None, live_pos=None):
        direction = pos.get("direction", "")
        lot = self.positive_float(pos.get("lot_size", 0.0))
        entry_price = self.positive_float(pos.get("entry_price", 0.0))

        if live_pos is not None:
            lot = self.positive_float(getattr(live_pos, "volume", lot)) or lot
            entry_price = self.positive_float(getattr(live_pos, "open_price", entry_price)) or entry_price

        if close_result is not None:
            lot = self.positive_float(getattr(close_result, "lot", lot)) or lot
            entry_price = self.positive_float(getattr(close_result, "open_price", entry_price)) or entry_price
            close_price = self.positive_float(getattr(close_result, "close_price", 0.0))
            pnl = float(getattr(close_result, "profit", 0.0) or 0.0)
        else:
            close_price = 0.0
            pnl = 0.0

        if close_price <= 0.0:
            close_price = self.get_market_exit_price(symbol, direction, current_bid, current_ask)

        if abs(pnl) <= 0.0000001:
            live_profit = 0.0
            if live_pos is not None:
                live_profit = float(getattr(live_pos, "profit", 0.0) or 0.0)
            if abs(live_profit) > 0.0000001:
                pnl = live_profit
            else:
                pnl = self.estimate_gross_pnl(symbol, direction, entry_price, close_price, lot)

        return lot, close_price, pnl

    def classify_live_position(self, live_pos):
        comment = live_pos.comment or ""
        if live_pos.magic == S14_MAGIC_A or comment.startswith(S14_COMMENT_A):
            return "A"
        if live_pos.magic == S14_MAGIC_B or comment.startswith(S14_COMMENT_B):
            return "B"
        return None

    def get_bet_units_from_live_position(self, live_pos):
        comment = live_pos.comment or ""
        if ":" in comment:
            raw = comment.split(":", 1)[1]
            digits = []
            for ch in raw:
                if ch.isdigit():
                    digits.append(ch)
                else:
                    break
            if digits:
                return max(1, int("".join(digits)))

        lot_multiplier = PARAMS.get("lot_multiplier", 0.01)
        if lot_multiplier > 0:
            return max(1, int(round(float(live_pos.volume) / lot_multiplier)))
        return 1

    def expected_sl_tp(self, direction, entry_price, W):
        if direction == "LONG":
            return entry_price - W, entry_price + W
        if direction == "SHORT":
            return entry_price + W, entry_price - W
        return 0.0, 0.0

    def repair_missing_sl_tp_on_sync(self, bot_type, live_pos, expected_sl, expected_tp):
        ticket = int(live_pos.ticket)
        current_sl = self.positive_float(getattr(live_pos, "sl", 0.0))
        current_tp = self.positive_float(getattr(live_pos, "tp", 0.0))

        if current_sl > 0.0 and current_tp > 0.0:
            return True, False

        if not PARAMS.get("repair_missing_sl_tp_on_sync", True):
            logging.warning(
                f"[Bot {bot_type}] MT5 position ticket {ticket} is missing SL/TP "
                "but auto repair is disabled."
            )
            return False, False

        repaired_sl = current_sl if current_sl > 0.0 else expected_sl
        repaired_tp = current_tp if current_tp > 0.0 else expected_tp
        if repaired_sl <= 0.0 or repaired_tp <= 0.0:
            logging.warning(
                f"[Bot {bot_type}] Cannot repair missing SL/TP for ticket {ticket}: "
                f"expected_sl={expected_sl}, expected_tp={expected_tp}"
            )
            return False, False

        logging.warning(
            f"[Bot {bot_type}] MT5 position ticket {ticket} is missing SL/TP. "
            f"Repairing to SL={repaired_sl:.5f}, TP={repaired_tp:.5f}."
        )
        if self.executor.modify_position_sl_tp(ticket, repaired_sl, repaired_tp):
            live_pos.sl = float(repaired_sl)
            live_pos.tp = float(repaired_tp)
            return True, True

        logging.error(f"[Bot {bot_type}] Failed to repair missing SL/TP for ticket {ticket}.")
        return False, False

    def warn_sl_tp_mismatch(self, bot_type, live_pos, expected_sl, expected_tp, pip_val):
        current_sl = self.positive_float(getattr(live_pos, "sl", 0.0))
        current_tp = self.positive_float(getattr(live_pos, "tp", 0.0))
        if current_sl <= 0.0 or current_tp <= 0.0:
            return

        tolerance = float(PARAMS.get("warn_sl_tp_mismatch_pips", 0.2)) * pip_val
        sl_diff = abs(current_sl - expected_sl)
        tp_diff = abs(current_tp - expected_tp)
        if sl_diff > tolerance or tp_diff > tolerance:
            logging.warning(
                f"[Bot {bot_type}] MT5 SL/TP differs from current W-based expectation for ticket {int(live_pos.ticket)}. "
                f"MT5 SL={current_sl:.5f}, TP={current_tp:.5f}; "
                f"expected SL={expected_sl:.5f}, TP={expected_tp:.5f}. Keeping MT5 values."
            )

    def live_position_to_state(self, live_pos, W, now_jst):
        direction = live_pos.direction
        entry_price = float(live_pos.open_price)
        fallback_sl, fallback_tp = self.expected_sl_tp(direction, entry_price, W)

        tp = float(live_pos.tp) if live_pos.tp > 0 else fallback_tp
        sl = float(live_pos.sl) if live_pos.sl > 0 else fallback_sl

        return {
            "ticket": int(live_pos.ticket),
            "direction": direction,
            "entry_time": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price": entry_price,
            "tp": float(tp),
            "sl": float(sl),
            "bet_units": int(self.get_bet_units_from_live_position(live_pos)),
            "lot_size": float(live_pos.volume),
            "mt5_magic": int(live_pos.magic),
            "mt5_comment": live_pos.comment,
            "restored_from_mt5": True,
        }

    def refresh_state_position_from_live(self, pos_key, live_pos, W, pip_val, bot_type):
        pos = self.state.get(pos_key)
        if not pos:
            return False, False

        changed = False
        entry_price = self.positive_float(pos.get("entry_price", 0.0)) or self.positive_float(live_pos.open_price)

        if pos.get("protection_sync_pending"):
            desired_sl = self.positive_float(pos.get("desired_sl", 0.0))
            desired_tp = self.positive_float(pos.get("desired_tp", 0.0))
            if desired_sl <= 0.0 or desired_tp <= 0.0:
                desired_sl, desired_tp = self.expected_sl_tp(live_pos.direction, entry_price, W)

            current_sl = self.positive_float(getattr(live_pos, "sl", 0.0))
            current_tp = self.positive_float(getattr(live_pos, "tp", 0.0))
            if not protection_levels_match(current_sl, current_tp, desired_sl, desired_tp):
                logging.warning(
                    f"[Bot {bot_type}] Retrying pending SL/TP synchronization for "
                    f"ticket {int(live_pos.ticket)}."
                )
                if not self.executor.modify_position_sl_tp(live_pos.ticket, desired_sl, desired_tp):
                    if current_sl > 0.0:
                        pos["sl"] = current_sl
                    if current_tp > 0.0:
                        pos["tp"] = current_tp
                    return True, True
                live_pos.sl = float(desired_sl)
                live_pos.tp = float(desired_tp)

            pos["sl"] = float(desired_sl)
            pos["tp"] = float(desired_tp)
            pos.pop("protection_sync_pending", None)
            pos.pop("desired_sl", None)
            pos.pop("desired_tp", None)
            changed = True
            logging.info(
                f"[Bot {bot_type}] Pending SL/TP synchronization completed for "
                f"ticket {int(live_pos.ticket)}."
            )

        expected_sl = self.positive_float(pos.get("sl", 0.0))
        expected_tp = self.positive_float(pos.get("tp", 0.0))
        if expected_sl <= 0.0 or expected_tp <= 0.0:
            expected_sl, expected_tp = self.expected_sl_tp(live_pos.direction, entry_price, W)

        repaired_ok, repaired = self.repair_missing_sl_tp_on_sync(bot_type, live_pos, expected_sl, expected_tp)
        if not repaired_ok:
            return changed, True
        if repaired:
            changed = True

        current_expected_sl, current_expected_tp = self.expected_sl_tp(live_pos.direction, float(live_pos.open_price), W)
        self.warn_sl_tp_mismatch(bot_type, live_pos, current_expected_sl, current_expected_tp, pip_val)

        updates = {
            "entry_price": float(live_pos.open_price),
            "lot_size": float(live_pos.volume),
            "mt5_magic": int(live_pos.magic),
            "mt5_comment": live_pos.comment,
        }
        if live_pos.sl > 0:
            updates["sl"] = float(live_pos.sl)
        if live_pos.tp > 0:
            updates["tp"] = float(live_pos.tp)

        for key, value in updates.items():
            old_value = pos.get(key)
            if isinstance(value, float):
                if old_value is None or abs(float(old_value) - value) > 0.000001:
                    pos[key] = value
                    changed = True
            elif old_value != value:
                pos[key] = value
                changed = True

        return changed, False

    def infer_missing_position_outcome(self, pos, current_bid, current_ask, pip_val):
        direction = pos.get("direction")
        tp = float(pos.get("tp", 0.0))
        sl = float(pos.get("sl", 0.0))
        tol = pip_val * 0.5

        if direction == "LONG":
            if tp > 0 and current_bid >= tp - tol:
                return "WIN"
            if sl > 0 and current_bid <= sl + tol:
                return "LOSE"
        elif direction == "SHORT":
            if tp > 0 and current_ask <= tp + tol:
                return "WIN"
            if sl > 0 and current_ask >= sl - tol:
                return "LOSE"
        return "MANUAL"

    def handle_missing_state_position(self, bot_type, pos, current_bid, current_ask, pip_val):
        ticket = pos.get("ticket")
        direction = pos.get("direction", "")
        price_hint = self.infer_missing_position_outcome(
            pos, current_bid, current_ask, pip_val
        )

        # A server-side TP/SL can remove the position before the one-second
        # Python loop observes the target. Corroborate the bulk-list absence
        # with a dedicated ticket lookup. Only a protected price boundary and
        # an explicit POSITION_NOT_FOUND response may advance DMC state.
        absence_confirmed = self.executor.confirm_position_absent(ticket)
        if absence_confirmed is True and price_hint in {"WIN", "LOSE"}:
            bet_units = int(pos.get("bet_units", 0))
            next_direction = next_direction_after_outcome(direction, price_hint)
            logging.warning(
                f"[Bot {bot_type}] Confirmed missing ticket {ticket} as {price_hint}: "
                "bulk position list and dedicated ticket lookup both report it "
                f"absent while the protected price boundary is reached. "
                f"Applying DMC and scheduling {next_direction}."
            )
            if not self.commit_confirmed_exit(
                bot_type,
                direction,
                price_hint,
                bet_units,
                ticket,
            ):
                logging.critical(
                    f"[Bot {bot_type}] Failed to persist the corroborated "
                    f"{price_hint} exit for ticket {ticket}."
                )
                return False, True
            return True, False

        existing = self.state.get("reconciliation_required")
        if isinstance(existing, dict) and existing.get("ticket") == ticket:
            return False, True

        # 現在価格は決済価格・決済理由の証拠にならない。deal履歴で照合できるまで
        # positionとDMC stateを保持し、新規発注をfail-closedで停止する。
        self.state["reconciliation_required"] = {
            "bot_type": bot_type,
            "ticket": ticket,
            "direction": direction,
            "price_only_hint": price_hint,
            "detected_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "MT5 position missing; deal-history reconciliation required",
        }
        pos["missing_on_mt5"] = True
        logging.critical(
            f"[Bot {bot_type}] State ticket {ticket} is missing from MT5 positions. "
            f"Dedicated absence confirmation={absence_confirmed}; "
            f"current-price hint={price_hint} is not applied to DMC. "
            "Further entries are blocked until operator reconciliation."
        )
        return True, True

    def sync_positions_with_mt5(self, symbol, W, current_bid, current_ask, pip_val, now_jst):
        live_positions = self.executor.get_positions(symbol)
        if live_positions is None:
            reason = "MT5 position list unavailable"
            if self.state.get("sync_block_reason") != reason:
                self.state["sync_block_new_entries"] = True
                self.state["sync_block_reason"] = reason
                self.save_state()
                logging.warning("MT5 position sync failed. Blocking new entries for this cycle.")
            return False

        live_by_ticket = {int(pos.ticket): pos for pos in live_positions}
        managed_tickets = set()
        changed = False
        block_entries_this_cycle = False
        pending_open = self.state.get("pending_open")

        for bot_type, pos_key in (("A", "pos_A"), ("B", "pos_B")):
            pos = self.state.get(pos_key)
            if not pos:
                continue

            ticket = int(pos.get("ticket", 0))
            live_pos = live_by_ticket.get(ticket)
            if live_pos:
                managed_tickets.add(ticket)
                reconciliation = self.state.get("reconciliation_required")
                if (
                    isinstance(reconciliation, dict)
                    and reconciliation.get("ticket") == ticket
                ):
                    self.state.pop("reconciliation_required", None)
                    pos.pop("missing_on_mt5", None)
                    logging.warning(
                        f"[Bot {bot_type}] MT5 position ticket {ticket} reappeared; "
                        "clearing the reconciliation block."
                    )
                    changed = True
                did_refresh, should_block = self.refresh_state_position_from_live(pos_key, live_pos, W, pip_val, bot_type)
                block_entries_this_cycle = block_entries_this_cycle or should_block
                if did_refresh:
                    logging.info(f"[Bot {bot_type}] Refreshed state from MT5 position ticket {ticket}.")
                    changed = True
            else:
                did_change, should_block = self.handle_missing_state_position(
                    bot_type, pos, current_bid, current_ask, pip_val
                )
                changed = changed or did_change
                block_entries_this_cycle = block_entries_this_cycle or should_block

        for live_pos in live_positions:
            ticket = int(live_pos.ticket)
            if ticket in managed_tickets:
                continue

            bot_type = self.classify_live_position(live_pos)
            if not bot_type:
                continue

            pending_match = False
            if isinstance(pending_open, dict) and pending_open.get("bot_type") == bot_type:
                expected_comment = str(pending_open.get("comment", ""))
                actual_comment = str(getattr(live_pos, "comment", "") or "")
                if not expected_comment or actual_comment != expected_comment:
                    logging.critical(
                        f"[Bot {bot_type}] Live position ticket {ticket} has comment "
                        f"'{actual_comment}', which does not match pending_open "
                        f"'{expected_comment}'. Refusing automatic adoption."
                    )
                    block_entries_this_cycle = True
                    continue
                pending_match = True

            pos_key = "pos_A" if bot_type == "A" else "pos_B"
            if self.state.get(pos_key) is not None:
                continue

            entry_price = float(live_pos.open_price)
            expected_sl, expected_tp = self.expected_sl_tp(live_pos.direction, entry_price, W)
            repaired_ok, repaired = self.repair_missing_sl_tp_on_sync(bot_type, live_pos, expected_sl, expected_tp)
            if not repaired_ok:
                block_entries_this_cycle = True
            if repaired:
                changed = True
            self.warn_sl_tp_mismatch(bot_type, live_pos, expected_sl, expected_tp, pip_val)

            self.state[pos_key] = self.live_position_to_state(live_pos, W, now_jst)
            managed_tickets.add(ticket)
            changed = True
            logging.warning(f"[Bot {bot_type}] Adopted live MT5 position ticket {ticket} into local state.")

            if bot_type == "A":
                self.state["next_direction_A"] = None
                if not self.state.get("pair_initialized"):
                    self.state["initial_anchor_A"] = float(live_pos.open_price)
            else:
                self.state["next_direction_B"] = None
                self.state["pair_initialized"] = True

            self.update_pair_mode()
            if pending_match:
                request_id = pending_open.get("request_id")
                self.state.pop("pending_open", None)
                reconciliation = self.state.get("reconciliation_required")
                if (
                    isinstance(reconciliation, dict)
                    and reconciliation.get("type") == "pending_open"
                    and reconciliation.get("request_id") == request_id
                ):
                    self.state.pop("reconciliation_required", None)
                pending_open = None
                changed = True
                logging.warning(
                    f"[Bot {bot_type}] Reconciled pending_open request {request_id} "
                    f"to live ticket {ticket}."
                )

        unmanaged = [pos for pos in live_positions if int(pos.ticket) not in managed_tickets]
        if unmanaged:
            tickets = ",".join(str(pos.ticket) for pos in unmanaged)
            reason = f"Unmanaged live positions: {tickets}"
            if self.state.get("sync_block_reason") != reason:
                self.state["sync_block_new_entries"] = True
                self.state["sync_block_reason"] = reason
                logging.warning(
                    f"Found unmanaged {symbol} positions ({tickets}). "
                    "Blocking new entries until they are closed or adopted by state."
                )
                changed = True
            block_entries_this_cycle = True
        else:
            if self.state.pop("sync_block_new_entries", None) is not None:
                changed = True
            if self.state.pop("sync_block_reason", None) is not None:
                changed = True

        if isinstance(pending_open, dict):
            request_id = pending_open.get("request_id")
            reason = f"Unresolved pending_open request: {request_id}"
            self.state["sync_block_new_entries"] = True
            self.state["sync_block_reason"] = reason
            existing = self.state.get("reconciliation_required")
            if not (
                isinstance(existing, dict)
                and existing.get("type") == "pending_open"
                and existing.get("request_id") == request_id
            ):
                self.state["reconciliation_required"] = {
                    "type": "pending_open",
                    "request_id": request_id,
                    "bot_type": pending_open.get("bot_type"),
                    "detected_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": "OPEN outcome is unconfirmed; order/deal-history reconciliation required",
                }
                changed = True
            block_entries_this_cycle = True
            logging.critical(
                f"[{symbol}] pending_open request {request_id} could not be matched "
                "to a live position. "
                "Automatic retry is blocked."
            )

        if changed and not self.save_state():
            return False

        return not block_entries_this_cycle

    def start(self):
        logging.info("Starting s14 Move-Catcher Live Bot execution loop...")
        if getattr(self, "state_load_error", None):
            logging.critical(
                "State file could not be loaded. Refusing to start with a reset DMC state: "
                + self.state_load_error
            )
            return

        disabled_symbols = [
            params["symbol"]
            for params in self.param_profiles
            if not bool(params.get("live_trading_enabled", False))
        ]
        if disabled_symbols:
            logging.critical(
                "Live trading is disabled by configuration for: "
                + ", ".join(disabled_symbols)
            )
            return

        unsafe_unbounded = [
            params["symbol"]
            for params in self.param_profiles
            if int(params.get("max_bet_units", 0)) <= 0
            and not bool(params.get("allow_unbounded_bet_units", False))
        ]
        if unsafe_unbounded:
            logging.critical(
                "Unbounded DMC requires explicit allow_unbounded_bet_units=true for: "
                + ", ".join(unsafe_unbounded)
            )
            return

        # Always prove that the current state is durably writable before bridge access.
        # A missing state file must not make the first live OPEN the writeability test.
        if not self.save_state():
            logging.critical("State persistence is unavailable. Refusing to connect to bridge.")
            return

        if not self.dm.connect():
            logging.error("Failed to connect via EA Bridge. Exit.")
            return

        try:
            while True:
                self.run_cycle()
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def run_cycle(self):
        for params in self.param_profiles:
            self.activate_profile(params)
            try:
                self._run_cycle_core()
            except Exception as e:
                logging.error(f"Error in execution cycle for {params.get('symbol')}: {e}")
                logging.error(traceback.format_exc())

    def _run_cycle_core(self):
        if not getattr(self, "persistence_healthy", True):
            logging.critical("State persistence is unhealthy. Retrying state save before trading.")
            if not self.save_state():
                return
            logging.warning("State persistence recovered; trading cycle may resume.")

        now_utc = datetime.now(timezone.utc)
        now_jst = pd.Timestamp(now_utc).tz_convert('Asia/Tokyo')
        
        symbol = PARAMS['symbol']
        W_pips = PARAMS['W_pips']
        initial_offset_ratio = float(PARAMS.get('initial_offset_ratio', 0.50))
        
        if "JPY" in symbol:
            pip_val = 0.01
        else:
            pip_val = 0.0001
            
        W = W_pips * pip_val

        weekend_entry_blocked = False
        weekend_close_window = False
        if PARAMS.get("weekend_filter", False):
            monday_start_hour = PARAMS.get("monday_start_hour_jst", 8)
            monday_start_minute = PARAMS.get("monday_start_minute_jst", 0)
            weekend_entry_blocked = is_weekend_entry_blocked_jst(
                now_jst,
                entry_stop_weekday=PARAMS.get("weekend_entry_stop_weekday_jst", 5),
                entry_stop_hour=PARAMS.get("weekend_entry_stop_hour_jst", PARAMS.get("weekend_stop_hour_jst", 20)),
                entry_stop_minute=PARAMS.get("weekend_entry_stop_minute_jst", 0),
                monday_start_hour=monday_start_hour,
                monday_start_minute=monday_start_minute,
            )
            weekend_close_window = is_weekend_close_window_jst(
                now_jst,
                close_weekday=PARAMS.get("weekend_close_weekday_jst", 5),
                close_hour=PARAMS.get("weekend_close_hour_jst", PARAMS.get("weekend_stop_hour_jst", 20)),
                close_minute=PARAMS.get("weekend_close_minute_jst", 0),
                monday_start_hour=monday_start_hour,
                monday_start_minute=monday_start_minute,
            )

        if weekend_close_window:
            closed_any = False
            if self.state["pos_A"]:
                logging.info(f"[Bot A] Weekend forced close triggered. Closing ticket {self.state['pos_A']['ticket']}")
                ticket_A = self.state['pos_A']['ticket']
                if not self.close_and_cleanup('A', ticket_A, "WEEKEND"):
                    return
                if not self.commit_confirmed_forced_exit("A", ticket_A):
                    return
                closed_any = True
            if self.state["pos_B"]:
                logging.info(f"[Bot B] Weekend forced close triggered. Closing ticket {self.state['pos_B']['ticket']}")
                ticket_B = self.state['pos_B']['ticket']
                if not self.close_and_cleanup('B', ticket_B, "WEEKEND"):
                    return
                if not self.commit_confirmed_forced_exit("B", ticket_B):
                    return
                closed_any = True

            if (
                closed_any
                or self.state.get("pair_initialized")
                or self.state.get("initial_anchor_A") is not None
                or self.state.get("next_direction_A") != "LONG"
            ):
                self.state["pos_A"] = None
                self.state["pos_B"] = None
                self.state["next_direction_A"] = "LONG"
                self.state["next_direction_B"] = None
                self.state["pair_initialized"] = False
                self.state["initial_anchor_A"] = None
                self.state["pair_mode"] = "INITIALIZING"
                self.state.pop("pending_close", None)
                if not self.save_state():
                    return
            return

        # Get latest market info (Bid / Ask / min_vol etc.)
        info = self.executor.get_symbol_info(symbol)
        if not info:
            logging.warning(f"Failed to fetch symbol info for {symbol}. Skipping cycle.")
            return

        current_ask = info.ask
        current_bid = info.bid
        current_spread = current_ask - current_bid
        max_spread = PARAMS.get("max_spread_pips", 0.3) * pip_val
        position_sync_ok = self.sync_positions_with_mt5(
            symbol, W, current_bid, current_ask, pip_val, now_jst
        )

        if not position_sync_ok:
            # Missing or unsynchronized MT5 state must never fall through to
            # current-price exit inference. A confirmed close result is required.
            return

        # B. News Filter Check
        in_news = self.is_in_news_window(now_jst)

        # C. Active Position Price Target Monitoring (Bot A & Bot B)
        # --- Bot A Position Exit Check ---
        pos_A = self.state["pos_A"]
        if pos_A:
            ticket_A = pos_A["ticket"]
            direction_A = pos_A["direction"]
            tp_A = pos_A["tp"]
            sl_A = pos_A["sl"]
            
            close_A = False
            outcome_A = None
            
            if direction_A == "LONG":
                if current_bid >= tp_A:
                    close_A = True
                    outcome_A = "WIN"
                elif current_bid <= sl_A:
                    close_A = True
                    outcome_A = "LOSE"
            else: # SHORT
                if current_ask <= tp_A:
                    close_A = True
                    outcome_A = "WIN"
                elif current_ask >= sl_A:
                    close_A = True
                    outcome_A = "LOSE"

            if close_A:
                logging.info(f"[Bot A] Exit Target Triggered ({outcome_A}). Ticket: {ticket_A}.")
                close_res = self.close_and_cleanup('A', ticket_A, outcome_A, current_bid, current_ask)
                if not close_res:
                    return

                if not self.commit_confirmed_exit(
                    "A", direction_A, outcome_A, pos_A["bet_units"], ticket_A
                ):
                    return

        # --- Bot B Position Exit Check ---
        pos_B = self.state["pos_B"]
        if pos_B:
            ticket_B = pos_B["ticket"]
            direction_B = pos_B["direction"]
            tp_B = pos_B["tp"]
            sl_B = pos_B["sl"]
            
            close_B = False
            outcome_B = None
            
            if direction_B == "LONG":
                if current_bid >= tp_B:
                    close_B = True
                    outcome_B = "WIN"
                elif current_bid <= sl_B:
                    close_B = True
                    outcome_B = "LOSE"
            else: # SHORT
                if current_ask <= tp_B:
                    close_B = True
                    outcome_B = "WIN"
                elif current_ask >= sl_B:
                    close_B = True
                    outcome_B = "LOSE"

            if close_B:
                logging.info(f"[Bot B] Exit Target Triggered ({outcome_B}). Ticket: {ticket_B}.")
                close_res = self.close_and_cleanup('B', ticket_B, outcome_B, current_bid, current_ask)
                if not close_res:
                    return

                if not self.commit_confirmed_exit(
                    "B", direction_B, outcome_B, pos_B["bet_units"], ticket_B
                ):
                    return

        # D. New Position / Activation Triggers (If not suspended by spread or news)
        # Check spread safety
        # 境界値は許可しない。例: 上限0.7pipsならspreadが0.7pips未満のときだけ許可する。
        spread_ok = is_spread_allowed(current_spread, max_spread)
        if not spread_ok:
            # We don't skip entire cycle if monitoring exits, but we block entries
            pass

        # --- Bot A Entry Trigger ---
        if self.state["pos_A"] is None and self.state["next_direction_A"] is not None:
            if position_sync_ok and not in_news and spread_ok and not weekend_entry_blocked:
                next_dir = self.state["next_direction_A"]
                self.open_bot_position(
                    "A",
                    next_dir,
                    symbol,
                    info,
                    now_jst,
                    W,
                    W_pips,
                    pip_val,
                    current_spread,
                )
                return
            else:
                if not position_sync_ok:
                    logging.info("[Bot A] Entry postponed: MT5 position sync is not clean.")
                elif weekend_entry_blocked:
                    logging.info("[Bot A] Entry postponed: weekend entry stop window.")
                elif in_news:
                    logging.info("[Bot A] Entry postponed: currently in NEWS window.")
                elif not spread_ok:
                    logging.info(
                        f"[{symbol}][Bot A] Entry postponed: Spread too wide "
                        f"({current_spread/pip_val:.1f} pips >= "
                        f"{max_spread/pip_val:.1f} pips limit)."
                    )

        # --- Bot B Activation Trigger ---
        if self.state["pos_B"] is None:
            trigger_direction = None
            if self.state.get("pair_initialized"):
                trigger_direction = self.state.get("next_direction_B")
            else:
                pos_a = self.state.get("pos_A")
                if not pos_a:
                    return
                anchor = self.state.get("initial_anchor_A")
                if anchor is None:
                    anchor = float(pos_a["entry_price"])
                    self.state["initial_anchor_A"] = anchor
                    if not self.save_state():
                        return
                offset = W * initial_offset_ratio
                price_digits = int(getattr(info, "digits", 5))
                upper_trigger = round(anchor + offset, price_digits)
                lower_trigger = round(anchor - offset, price_digits)
                if current_bid >= upper_trigger or current_ask <= lower_trigger:
                    trigger_direction = pos_a["direction"]

            if not trigger_direction:
                return

            if position_sync_ok and not in_news and spread_ok and not weekend_entry_blocked:
                self.open_bot_position(
                    "B",
                    trigger_direction,
                    symbol,
                    info,
                    now_jst,
                    W,
                    W_pips,
                    pip_val,
                    current_spread,
                )
            elif not position_sync_ok:
                logging.info("[Bot B] Entry postponed: MT5 position sync is not clean.")
            elif weekend_entry_blocked:
                logging.info("[Bot B] Entry postponed: weekend entry stop window.")
            elif in_news:
                logging.info("[Bot B] Entry postponed: currently in NEWS window.")
            elif not spread_ok:
                logging.info(
                    f"[{symbol}][Bot B] Entry postponed: Spread too wide "
                    f"({current_spread/pip_val:.1f} pips >= "
                    f"{max_spread/pip_val:.1f} pips limit)."
                )
            return

    def commit_confirmed_exit(self, bot_type, direction, outcome, bet_units, ticket):
        """Persist DMC, direction, and position removal as one local state transition."""
        pos_key = "pos_A" if bot_type == "A" else "pos_B"
        next_key = "next_direction_A" if bot_type == "A" else "next_direction_B"
        if bot_type == "A":
            self.mc_manager.update_mc(outcome, None, bet_units, 0)
        else:
            self.mc_manager.update_mc(None, outcome, 0, bet_units)
        self.state[pos_key] = None
        self.state[next_key] = next_direction_after_outcome(direction, outcome)
        if bot_type == "A" and not self.state.get("pair_initialized"):
            self.state["initial_anchor_A"] = None
        pending_close = self.state.get("pending_close")
        if isinstance(pending_close, dict) and pending_close.get("ticket") == ticket:
            self.state.pop("pending_close", None)
        reconciliation = self.state.get("reconciliation_required")
        if isinstance(reconciliation, dict) and reconciliation.get("ticket") == ticket:
            self.state.pop("reconciliation_required", None)
        self.update_pair_mode()
        return self.save_state()

    def commit_confirmed_forced_exit(self, bot_type, ticket):
        """Persist one confirmed forced close without applying a TP/SL DMC outcome."""
        pos_key = "pos_A" if bot_type == "A" else "pos_B"
        self.state[pos_key] = None
        pending_close = self.state.get("pending_close")
        if isinstance(pending_close, dict) and pending_close.get("ticket") == ticket:
            self.state.pop("pending_close", None)
        self.update_pair_mode()
        return self.save_state()

    def close_and_cleanup(self, bot_type, ticket, reason, current_bid=None, current_ask=None):
        pos_key = "pos_A" if bot_type == 'A' else "pos_B"
        pos = self.state[pos_key]
        if not pos:
            return None
            
        lot = pos.get("lot_size", 0.0)
        direction = pos.get("direction", "")
        symbol = PARAMS['symbol']

        self.state["pending_close"] = {
            "bot_type": bot_type,
            "ticket": int(ticket),
            "requested_outcome": reason,
            "requested_at_jst": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not self.save_state():
            logging.critical(
                f"[Bot {bot_type}] Close request blocked because pending_close "
                "could not be persisted."
            )
            return False

        live_pos = None
        try:
            live_pos = self.executor.get_position(ticket)
        except Exception as e:
            logging.warning(f"[Bot {bot_type}] Failed to read live position before close for CSV details: {e}")
        
        success = self.executor.close_position(ticket)
        if success:
            log_lot, log_price, log_pnl = self.get_exit_log_values(
                symbol, pos, success, current_bid, current_ask, live_pos
            )
            logging.info(f"[Bot {bot_type}] Successfully closed position. Ticket: {ticket}, Lot: {log_lot}, Exit Price: {log_price:.5f}, Profit: {log_pnl}")
            self.log_trade_csv(f"EXIT_{reason}", ticket, symbol, direction, log_lot, log_price, log_pnl, reason)
        else:
            logging.warning(f"[Bot {bot_type}] Failed to close ticket {ticket} via EA. Keeping state so the bot can retry.")
            self.log_trade_csv(f"EXIT_FAIL_{reason}", ticket, symbol, direction, lot, 0.0, 0.0, reason)
            pos["last_close_fail_reason"] = reason
            pos["last_close_fail_time"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
            self.save_state()
            return success

        return success

if __name__ == "__main__":
    bot = S14TradingBot()
    bot.start()
