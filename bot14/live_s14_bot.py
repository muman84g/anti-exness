# -*- coding: utf-8 -*-
#プロ口座専用
# ==============================================================================
# STRATEGY s14 CONCEPT: GBPUSD Robust Move-Catcher Grid Strategy Live Trading Bot (v1)
# 【戦略s14コンセプト: GBPUSD 逆張りグリッド＋二数列分解管理モンテカルロ法実運用ボット】
# ------------------------------------------------------------------------------
# - Instrument: GBPUSD (GBP/USD Pro Account)
# - Logic: Bot A (Always in market, reverses on TP, continues on SL)
#          Bot B (Grid hedging bot, triggers at S ± W/2 with counter-trend entry)
# - Weekend: block new entries from Saturday 04:00 JST, close positions at 04:30, restart Monday 08:00
# - News Filter: Avoids major high-impact news window (e.g., ±2 hours)
# - Money Management: Decomposed Monte Carlo with Goodman backup, lot cap at 8
# ==============================================================================
import os
import sys
import time
import json
import logging
import traceback
import csv
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
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "s14_bot.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Import dependencies
from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_BUY, ORDER_TYPE_SELL

# ============================================================
# Bot Configuration & Path Variables
# ============================================================
POLL_INTERVAL_SECONDS = 5  # Scan price intervals
STATE_FILE = os.path.join(script_dir, "s14_bot_state.json")
PARAMS_FILE = os.path.join(script_dir, "s14_params.json")

DEFAULT_PARAMS = {
    'symbol': 'GBPUSD',
    'W_pips': 44.0,
    'b_trigger_ratio': 0.40,
    'lot_multiplier': 0.01,
    'max_bet_units': 12,
    'initial_sequence': [2, 2, 2],
    'weekend_filter': True,
    'weekend_stop_hour_jst': 2,
    'weekend_entry_stop_weekday_jst': 5,
    'weekend_entry_stop_hour_jst': 2,
    'weekend_entry_stop_minute_jst': 0,
    'weekend_close_weekday_jst': 5,
    'weekend_close_hour_jst': 2,
    'weekend_close_minute_jst': 30,
    'monday_start_hour_jst': 8,
    'monday_start_minute_jst': 0,
    'news_filter': True,
    'avoidance_hours': 0.0,
    'news_file': 'macro_events_2026.json',
    'max_spread_pips': 2.0
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

PARAMS = load_params()

S14_MAGIC_A = 140014
S14_MAGIC_B = 140015
S14_COMMENT_A = "s14_A"
S14_COMMENT_B = "s14_B"

# ============================================================
# Decomposed Monte Carlo Logic Classes (from Backtest)
# ============================================================
class DecomposedMonteCarlo:
    def __init__(self, initial_sequence=[2, 2, 2]):
        self.initial_sequence = list(initial_sequence)
        self.seq = list(initial_sequence)
        self.state = "sequence"  # "sequence" or "goodman"
        self.goodman_streak = 0

    def get_bet_units(self):
        if self.state == "sequence":
            if len(self.seq) == 0:
                self.state = "goodman"
                self.goodman_streak = 0
                return 1
            elif len(self.seq) == 1:
                return self.seq[0]
            return self.seq[0] + self.seq[-1]
        else:  # "goodman"
            bets = [1, 2, 3, 5]
            if self.goodman_streak < len(bets):
                return bets[self.goodman_streak]
            return 5

    def on_win(self):
        stock_gained = 0
        if self.state == "sequence":
            if len(self.seq) >= 2:
                self.seq = self.seq[1:-1]
            else:
                self.seq = []
            if len(self.seq) == 0:
                self.state = "goodman"
                self.goodman_streak = 0
        else:  # "goodman"
            stock_gained = self.get_bet_units()
            self.goodman_streak += 1
        return stock_gained

    def on_lose(self, bet_units):
        if self.state == "sequence":
            self.seq.append(bet_units)
        else:  # "goodman"
            self.state = "sequence"
            self.seq = list(self.initial_sequence)
            self.goodman_streak = 0

    def to_dict(self):
        return {
            "initial_sequence": self.initial_sequence,
            "seq": self.seq,
            "state": self.state,
            "goodman_streak": self.goodman_streak
        }

    def from_dict(self, d):
        self.initial_sequence = list(d.get("initial_sequence", [2, 2, 2]))
        self.seq = list(d.get("seq", [2, 2, 2]))
        self.state = d.get("state", "sequence")
        self.goodman_streak = d.get("goodman_streak", 0)


class MonteCarloManager:
    def __init__(self, initial_sequence=[2, 2, 2]):
        self.mc_A = DecomposedMonteCarlo(initial_sequence)
        self.mc_B = DecomposedMonteCarlo(initial_sequence)
        self.stock = 0

    def to_dict(self):
        return {
            "mc_A": self.mc_A.to_dict(),
            "mc_B": self.mc_B.to_dict(),
            "stock": self.stock
        }

    def from_dict(self, d):
        if "mc_A" in d:
            self.mc_A.from_dict(d["mc_A"])
        if "mc_B" in d:
            self.mc_B.from_dict(d["mc_B"])
        self.stock = d.get("stock", 0)

    def average_sequences(self):
        if self.mc_A.state != "sequence" or self.mc_B.state != "sequence":
            return

        eligible_A = []
        keep_left_A = []
        if len(self.mc_A.seq) > 0:
            if self.mc_A.seq[0] == 0:
                eligible_A = self.mc_A.seq[1:]
                keep_left_A = [0]
            else:
                eligible_A = self.mc_A.seq
                keep_left_A = []

        eligible_B = []
        keep_left_B = []
        if len(self.mc_B.seq) > 0:
            if self.mc_B.seq[0] == 0:
                eligible_B = self.mc_B.seq[1:]
                keep_left_B = [0]
            else:
                eligible_B = self.mc_B.seq
                keep_left_B = []

        total_sum = sum(eligible_A) + sum(eligible_B)
        total_count = len(eligible_A) + len(eligible_B)

        if total_count > 0:
            base_val = total_sum // total_count
            rem = total_sum % total_count

            new_el_A = [base_val] * len(eligible_A)
            new_el_B = [base_val] * len(eligible_B)

            len_A = len(eligible_A)
            len_B = len(eligible_B)
            max_len = max(len_A, len_B)

            for idx in range(1, max_len + 1):
                if len_A > len_B:
                    if len_A - idx >= 0 and rem > 0:
                        new_el_A[len_A - idx] += 1
                        rem -= 1
                    if len_B - idx >= 0 and rem > 0:
                        new_el_B[len_B - idx] += 1
                        rem -= 1
                elif len_B > len_A:
                    if len_B - idx >= 0 and rem > 0:
                        new_el_B[len_B - idx] += 1
                        rem -= 1
                    if len_A - idx >= 0 and rem > 0:
                        new_el_A[len_A - idx] += 1
                        rem -= 1
                else:
                    if len_A - idx >= 0 and rem > 0:
                        new_el_A[len_A - idx] += 1
                        rem -= 1
                    if len_B - idx >= 0 and rem > 0:
                        new_el_B[len_B - idx] += 1
                        rem -= 1

            self.mc_A.seq = keep_left_A + new_el_A
            self.mc_B.seq = keep_left_B + new_el_B

    def redistribute_to_single_sequence(self, mc, val):
        if len(mc.seq) <= 1 or val <= 0:
            return
        n = len(mc.seq) - 1
        base = val // n
        rem = val % n
        for i in range(1, len(mc.seq)):
            mc.seq[i] += base
        for i in range(1, rem + 1):
            mc.seq[i] += 1

    def redistribute_to_both_sequences(self, val):
        E_A_len = len(self.mc_A.seq) - 1 if (self.mc_A.state == "sequence" and len(self.mc_A.seq) > 1) else 0
        E_B_len = len(self.mc_B.seq) - 1 if (self.mc_B.state == "sequence" and len(self.mc_B.seq) > 1) else 0

        val_A = val // 2
        val_B = val // 2
        rem = val % 2

        if rem > 0:
            if E_A_len > E_B_len:
                val_A += 1
            elif E_B_len > E_A_len:
                val_B += 1
            else:
                val_A += 1

        self.redistribute_to_single_sequence(self.mc_A, val_A)
        self.redistribute_to_single_sequence(self.mc_B, val_B)

    def update_mc(self, outcome_A, outcome_B, bet_A, bet_B):
        # Translate 'TP' -> 'WIN', 'SL' -> 'LOSE' for backward compatibility / backtest parity
        if outcome_A == 'TP': outcome_A = 'WIN'
        elif outcome_A == 'SL': outcome_A = 'LOSE'
        if outcome_B == 'TP': outcome_B = 'WIN'
        elif outcome_B == 'SL': outcome_B = 'LOSE'

        stock_gained = 0
        if outcome_A == "WIN":
            stock_gained += self.mc_A.on_win()
        elif outcome_A == "LOSE":
            self.mc_A.on_lose(bet_A)

        if outcome_B == "WIN":
            stock_gained += self.mc_B.on_win()
        elif outcome_B == "LOSE":
            self.mc_B.on_lose(bet_B)

        self.stock += stock_gained
        self.average_sequences()

        A_needs_zero = (outcome_A == "LOSE" and self.mc_A.state == "sequence" and len(self.mc_A.seq) > 0 and self.mc_A.seq[0] != 0)
        B_needs_zero = (outcome_B == "LOSE" and self.mc_B.state == "sequence" and len(self.mc_B.seq) > 0 and self.mc_B.seq[0] != 0)

        if A_needs_zero and B_needs_zero:
            val_A = self.mc_A.seq[0]
            val_B = self.mc_B.seq[0]
            len_A = len(self.mc_A.seq)
            len_B = len(self.mc_B.seq)

            if len_A >= len_B:
                if self.stock >= val_A:
                    self.stock -= val_A
                    self.mc_A.seq[0] = 0
                    A_needs_zero = False
                    if self.stock >= val_B:
                        self.stock -= val_B
                        self.mc_B.seq[0] = 0
                        B_needs_zero = False
            else:
                if self.stock >= val_B:
                    self.stock -= val_B
                    self.mc_B.seq[0] = 0
                    B_needs_zero = False
                    if self.stock >= val_A:
                        self.stock -= val_A
                        self.mc_A.seq[0] = 0
                        A_needs_zero = False
        elif A_needs_zero:
            val_A = self.mc_A.seq[0]
            if self.stock >= val_A:
                self.stock -= val_A
                self.mc_A.seq[0] = 0
                A_needs_zero = False
        elif B_needs_zero:
            val_B = self.mc_B.seq[0]
            if self.stock >= val_B:
                self.stock -= val_B
                self.mc_B.seq[0] = 0
                B_needs_zero = False

        if A_needs_zero and B_needs_zero:
            val_A = self.mc_A.seq[0]
            val_B = self.mc_B.seq[0]
            self.mc_A.seq[0] = 0
            self.mc_B.seq[0] = 0
            dist_val = val_A + val_B
            if self.mc_A.state == "sequence" and self.mc_B.state == "sequence":
                self.redistribute_to_both_sequences(dist_val)
            elif self.mc_A.state == "sequence":
                self.redistribute_to_single_sequence(self.mc_A, dist_val)
            elif self.mc_B.state == "sequence":
                self.redistribute_to_single_sequence(self.mc_B, dist_val)
        elif A_needs_zero:
            val_A = self.mc_A.seq[0]
            self.mc_A.seq[0] = 0
            if self.mc_B.state == "sequence":
                self.redistribute_to_both_sequences(val_A)
            else:
                self.redistribute_to_single_sequence(self.mc_A, val_A)
        elif B_needs_zero:
            val_B = self.mc_B.seq[0]
            self.mc_B.seq[0] = 0
            if self.mc_A.state == "sequence":
                self.redistribute_to_both_sequences(val_B)
            else:
                self.redistribute_to_single_sequence(self.mc_B, val_B)


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

def calculate_lot(bet_units, lot_multiplier, max_bet_units, symbol_info):
    capped_bet = min(bet_units, max_bet_units) if max_bet_units else bet_units
    raw_lot = capped_bet * lot_multiplier
    
    min_vol = symbol_info.volume_min
    max_vol = symbol_info.volume_max
    step_vol = symbol_info.volume_step
    
    lot = max(min_vol, min(raw_lot, max_vol))
    lot = round(lot / step_vol) * step_vol
    return round(lot, 2)

class s14TradingBot:
    def __init__(self):
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        self.state = {}
        self.mc_manager = MonteCarloManager(initial_sequence=PARAMS.get("initial_sequence", [2, 2, 2]))
        self.macro_times = []
        self.load_news_events()
        self.load_state()

    def load_news_events(self):
        news_file = PARAMS.get("news_file", "macro_events_2026.json")
        
        # Paths to search
        paths_to_try = [
            os.path.join(script_dir, news_file),
            os.path.join(os.path.dirname(script_dir), "data", news_file),
            os.path.join(r"C:\Users\muuma\.gemini\antigravity\scratch\anti-backtest\data", news_file)
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
        if not PARAMS.get("news_filter", True) or not self.macro_times:
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
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state_data = json.load(f)
                
                # Check properties inside state_data
                self.state = state_data
                if "mc_manager" in state_data:
                    self.mc_manager.from_dict(state_data["mc_manager"])
                
                logging.info("Successfully loaded state file and restored Monte Carlo state.")
            except Exception as e:
                logging.error(f"Error loading state file: {e}")
                self.init_empty_state()
        else:
            self.init_empty_state()

    def init_empty_state(self):
        self.state = {
            "pos_A": None,
            "pos_B": None,
            "next_direction_A": "LONG",
            "waiting_B": True,
            "S": None,
            "mc_manager": {}
        }
        self.mc_manager = MonteCarloManager(initial_sequence=PARAMS.get("initial_sequence", [2, 2, 2]))
        self.save_state()

    def save_state(self):
        try:
            self.state["mc_manager"] = self.mc_manager.to_dict()
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save state: {e}")

    def log_trade_csv(self, action, ticket, symbol, direction="", lot_size=0.0, price=0.0, pnl=0.0, reason=""):
        csv_file = os.path.join(LOG_DIR, "s14_trades.csv")
        file_exists = os.path.isfile(csv_file)
        
        now_jst = datetime.now(JST)
        
        try:
            with open(csv_file, mode='a', newline='', encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp_JST", "Action", "Ticket", "Symbol", "Direction", "LotSize", "Price", "PnL", "Reason"])
                writer.writerow([
                    now_jst.strftime("%Y-%m-%d %H:%M:%S"), action, ticket, symbol, direction, lot_size, price, pnl, reason
                ])
        except Exception as e:
            logging.error(f"Failed to write trade log to CSV: {e}")

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

    def live_position_to_state(self, live_pos, W, now_jst):
        direction = live_pos.direction
        entry_price = float(live_pos.open_price)
        if direction == "LONG":
            fallback_tp = entry_price + W
            fallback_sl = entry_price - W
        else:
            fallback_tp = entry_price - W
            fallback_sl = entry_price + W

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

    def refresh_state_position_from_live(self, pos_key, live_pos):
        pos = self.state.get(pos_key)
        if not pos:
            return False

        changed = False
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

        return changed

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
        pos_key = "pos_A" if bot_type == "A" else "pos_B"
        ticket = pos.get("ticket")
        direction = pos.get("direction", "")
        lot = pos.get("lot_size", 0.0)
        outcome = self.infer_missing_position_outcome(pos, current_bid, current_ask, pip_val)

        logging.warning(
            f"[Bot {bot_type}] State ticket {ticket} is not present in MT5 positions. "
            f"Classified as {outcome}; cleaning local state."
        )

        if outcome in {"WIN", "LOSE"}:
            if bot_type == "A":
                self.mc_manager.update_mc(outcome, None, pos.get("bet_units", 0), 0)
                self.state["next_direction_A"] = "SHORT" if outcome == "WIN" and direction == "LONG" else (
                    "LONG" if outcome == "WIN" and direction == "SHORT" else direction
                )
            else:
                self.mc_manager.update_mc(None, outcome, 0, pos.get("bet_units", 0))
                self.state["waiting_B"] = True
                self.state["S"] = current_bid if direction == "LONG" else current_ask
            self.log_trade_csv(f"EXIT_SYNC_{outcome}", ticket, PARAMS["symbol"], direction, lot, 0.0, 0.0, outcome)
            block_entries_this_cycle = False
        else:
            # Manual/external closes cannot be scored safely, so do not mutate MC state.
            if bot_type == "A":
                self.state["next_direction_A"] = direction or "LONG"
            else:
                self.state["waiting_B"] = True
                self.state["S"] = current_bid if direction == "LONG" else current_ask
            self.log_trade_csv("EXIT_SYNC_MANUAL", ticket, PARAMS["symbol"], direction, lot, 0.0, 0.0, "MANUAL_OR_EXTERNAL")
            block_entries_this_cycle = True

        self.state[pos_key] = None
        return True, block_entries_this_cycle

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

        for bot_type, pos_key in (("A", "pos_A"), ("B", "pos_B")):
            pos = self.state.get(pos_key)
            if not pos:
                continue

            ticket = int(pos.get("ticket", 0))
            live_pos = live_by_ticket.get(ticket)
            if live_pos:
                managed_tickets.add(ticket)
                if self.refresh_state_position_from_live(pos_key, live_pos):
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

            pos_key = "pos_A" if bot_type == "A" else "pos_B"
            if self.state.get(pos_key) is not None:
                continue

            self.state[pos_key] = self.live_position_to_state(live_pos, W, now_jst)
            managed_tickets.add(ticket)
            changed = True
            logging.warning(f"[Bot {bot_type}] Adopted live MT5 position ticket {ticket} into local state.")

            if bot_type == "A":
                self.state["next_direction_A"] = None
                if self.state.get("S") is None:
                    self.state["S"] = float(live_pos.open_price)
            else:
                self.state["waiting_B"] = False

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

        if changed:
            self.save_state()

        return not block_entries_this_cycle

    def start(self):
        logging.info("Starting s14 Move-Catcher Live Bot execution loop...")
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
        try:
            self._run_cycle_core()
        except Exception as e:
            logging.error(f"Error in execution cycle: {e}")
            logging.error(traceback.format_exc())

    def _run_cycle_core(self):
        now_utc = datetime.now(timezone.utc)
        now_jst = pd.Timestamp(now_utc).tz_convert('Asia/Tokyo')
        
        symbol = PARAMS['symbol']
        W_pips = PARAMS['W_pips']
        b_trigger_ratio = float(PARAMS.get('b_trigger_ratio', 0.40))
        
        if "JPY" in symbol:
            pip_val = 0.01
        else:
            pip_val = 0.0001
            
        W = W_pips * pip_val

        weekend_entry_blocked = False
        weekend_close_window = False
        if PARAMS.get("weekend_filter", True):
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
                if self.close_and_cleanup('A', self.state['pos_A']['ticket'], "WEEKEND"):
                    closed_any = True
                else:
                    return
            if self.state["pos_B"]:
                logging.info(f"[Bot B] Weekend forced close triggered. Closing ticket {self.state['pos_B']['ticket']}")
                if self.close_and_cleanup('B', self.state['pos_B']['ticket'], "WEEKEND"):
                    closed_any = True
                else:
                    return

            if closed_any or self.state["S"] is not None or self.state["next_direction_A"] != "LONG":
                self.state["pos_A"] = None
                self.state["pos_B"] = None
                self.state["next_direction_A"] = "LONG"
                self.state["waiting_B"] = True
                self.state["S"] = None
                self.save_state()
            return

        # Get latest market info (Bid / Ask / min_vol etc.)
        info = self.executor.get_symbol_info(symbol)
        if not info:
            logging.warning(f"Failed to fetch symbol info for {symbol}. Skipping cycle.")
            return

        current_ask = info.ask
        current_bid = info.bid
        current_spread = current_ask - current_bid
        max_spread = PARAMS.get("max_spread_pips", 0.8) * pip_val
        position_sync_ok = self.sync_positions_with_mt5(
            symbol, W, current_bid, current_ask, pip_val, now_jst
        )

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
                close_res = self.close_and_cleanup('A', ticket_A, outcome_A)
                if not close_res:
                    return
                
                # Apply Monte Carlo Updates
                self.mc_manager.update_mc(outcome_A, None, pos_A['bet_units'], 0)
                
                # Determine Next A Entry Direction
                if outcome_A == "WIN":
                    # Reverse direction
                    self.state["next_direction_A"] = "SHORT" if direction_A == "LONG" else "LONG"
                else: # LOSE (SL)
                    # Keep same direction
                    self.state["next_direction_A"] = direction_A
                
                self.state["pos_A"] = None
                self.save_state()

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
                close_res = self.close_and_cleanup('B', ticket_B, outcome_B)
                if not close_res:
                    return
                
                # Apply Monte Carlo Updates
                self.mc_manager.update_mc(None, outcome_B, 0, pos_B['bet_units'])
                
                self.state["pos_B"] = None
                self.state["waiting_B"] = True
                # Set new S to execution price (use close_price if success, fallback to trigger price)
                if close_res and getattr(close_res, 'close_price', 0.0) > 0:
                    self.state["S"] = close_res.close_price
                else:
                    self.state["S"] = current_bid if direction_B == "LONG" else current_ask
                self.save_state()

        # D. New Position / Activation Triggers (If not suspended by spread or news)
        # Check spread safety
        spread_ok = current_spread <= max_spread
        if not spread_ok:
            # We don't skip entire cycle if monitoring exits, but we block entries
            pass

        # --- Bot A Entry Trigger ---
        if self.state["pos_A"] is None and self.state["next_direction_A"] is not None:
            if position_sync_ok and not in_news and spread_ok and not weekend_entry_blocked:
                next_dir = self.state["next_direction_A"]
                bet_units = self.mc_manager.mc_A.get_bet_units()
                lot = calculate_lot(bet_units, PARAMS['lot_multiplier'], PARAMS['max_bet_units'], info)
                
                order_type = ORDER_TYPE_BUY if next_dir == "LONG" else ORDER_TYPE_SELL
                expected_px = current_ask if next_dir == "LONG" else current_bid
                if next_dir == "LONG":
                    initial_tp = expected_px + W
                    initial_sl = expected_px - W
                else:
                    initial_tp = expected_px - W
                    initial_sl = expected_px + W
                logging.info(f"[Bot A] Preparing to open {next_dir} | Bet Units: {bet_units} | Lot: {lot}")
                
                ticket = self.executor.open_position(
                    symbol,
                    order_type,
                    lot,
                    sl=initial_sl,
                    tp=initial_tp,
                    magic=S14_MAGIC_A,
                    comment=f"{S14_COMMENT_A}:{bet_units}",
                )
                if ticket:
                    exec_px = float(ticket.price)
                    if next_dir == "LONG":
                        tp = exec_px + W
                        sl = exec_px - W
                    else:
                        tp = exec_px - W
                        sl = exec_px + W
                    self.executor.modify_position_sl_tp(ticket, sl, tp)
                        
                    self.state["pos_A"] = {
                        "ticket": int(ticket),
                        "direction": next_dir,
                        "entry_time": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_price": exec_px,
                        "tp": float(tp),
                        "sl": float(sl),
                        "bet_units": int(bet_units),
                        "lot_size": float(lot)
                    }
                    self.state["next_direction_A"] = None
                    # Initialize Bot B baseline reference S to this entry price
                    if self.state["S"] is None:
                        self.state["S"] = exec_px
                    
                    self.save_state()
                    self.log_trade_csv("ENTRY_A", int(ticket), symbol, next_dir, lot, exec_px)
                else:
                    logging.error("[Bot A] Order failed to execute.")
            else:
                if not position_sync_ok:
                    logging.info("[Bot A] Entry postponed: MT5 position sync is not clean.")
                elif weekend_entry_blocked:
                    logging.info("[Bot A] Entry postponed: weekend entry stop window.")
                elif in_news:
                    logging.info("[Bot A] Entry postponed: currently in NEWS window.")
                elif not spread_ok:
                    logging.info(f"[Bot A] Entry postponed: Spread too wide ({current_spread/pip_val:.1f} pips > {max_spread/pip_val:.1f} pips limit).")

        # --- Bot B Activation Trigger ---
        if self.state["waiting_B"] and self.state["pos_B"] is None:
            S = self.state["S"]
            if S is None:
                # Fallback to current price if S is uninitialized
                self.state["S"] = current_ask
                self.save_state()
                S = current_ask
                
            trigger_up = S + W * b_trigger_ratio
            trigger_dn = S - W * b_trigger_ratio
            
            trigger_direction = ""
            if current_ask >= trigger_up:
                # Price moved up -> counter-trend Sell
                trigger_direction = "SHORT"
            elif current_bid <= trigger_dn:
                # Price moved down -> counter-trend Buy
                trigger_direction = "LONG"
                
            if trigger_direction:
                if position_sync_ok and not in_news and spread_ok and not weekend_entry_blocked:
                    bet_units = self.mc_manager.mc_B.get_bet_units()
                    lot = calculate_lot(bet_units, PARAMS['lot_multiplier'], PARAMS['max_bet_units'], info)
                    
                    order_type = ORDER_TYPE_BUY if trigger_direction == "LONG" else ORDER_TYPE_SELL
                    expected_px = current_ask if trigger_direction == "LONG" else current_bid
                    if trigger_direction == "LONG":
                        initial_tp = expected_px + W
                        initial_sl = expected_px - W
                    else:
                        initial_tp = expected_px - W
                        initial_sl = expected_px + W
                    logging.info(f"[Bot B] Triggered activation {trigger_direction} (S: {S:.5f}, Current: {current_bid if trigger_direction == 'LONG' else current_ask:.5f}) | Bet Units: {bet_units} | Lot: {lot}")
                    
                    ticket = self.executor.open_position(
                        symbol,
                        order_type,
                        lot,
                        sl=initial_sl,
                        tp=initial_tp,
                        magic=S14_MAGIC_B,
                        comment=f"{S14_COMMENT_B}:{bet_units}",
                    )
                    if ticket:
                        exec_px = float(ticket.price)
                        if trigger_direction == "LONG":
                            tp = exec_px + W
                            sl = exec_px - W
                        else:
                            tp = exec_px - W
                            sl = exec_px + W
                        self.executor.modify_position_sl_tp(ticket, sl, tp)
                            
                        self.state["pos_B"] = {
                            "ticket": int(ticket),
                            "direction": trigger_direction,
                            "entry_time": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
                            "entry_price": exec_px,
                            "tp": float(tp),
                            "sl": float(sl),
                            "bet_units": int(bet_units),
                            "lot_size": float(lot)
                        }
                        self.state["waiting_B"] = False
                        self.save_state()
                        self.log_trade_csv("ENTRY_B", int(ticket), symbol, trigger_direction, lot, exec_px)
                    else:
                        logging.error("[Bot B] Activation order failed.")
                else:
                    if not position_sync_ok:
                        logging.info("[Bot B] Activation postponed: MT5 position sync is not clean.")
                    elif weekend_entry_blocked:
                        logging.info("[Bot B] Activation postponed: weekend entry stop window.")
                    elif in_news:
                        logging.info("[Bot B] Activation postponed: currently in NEWS window.")
                    elif not spread_ok:
                        logging.info(f"[Bot B] Activation postponed: Spread too wide ({current_spread/pip_val:.1f} pips > {max_spread/pip_val:.1f} pips limit).")

    def close_and_cleanup(self, bot_type, ticket, reason):
        pos_key = "pos_A" if bot_type == 'A' else "pos_B"
        pos = self.state[pos_key]
        if not pos:
            return None
            
        lot = pos.get("lot_size", 0.0)
        direction = pos.get("direction", "")
        symbol = PARAMS['symbol']
        
        success = self.executor.close_position(ticket)
        if success:
            logging.info(f"[Bot {bot_type}] Successfully closed position. Ticket: {ticket}, Lot: {lot}, Exit Price: {success.close_price:.5f}, Profit: {success.profit}")
            self.log_trade_csv(f"EXIT_{reason}", ticket, symbol, direction, lot, success.close_price, success.profit, reason)
        else:
            logging.warning(f"[Bot {bot_type}] Failed to close ticket {ticket} via EA. Keeping state so the bot can retry.")
            self.log_trade_csv(f"EXIT_FAIL_{reason}", ticket, symbol, direction, lot, 0.0, 0.0, reason)
            pos["last_close_fail_reason"] = reason
            pos["last_close_fail_time"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
            self.save_state()
            return success
            
        self.state[pos_key] = None
        self.save_state()
        return success

if __name__ == "__main__":
    bot = s14TradingBot()
    bot.start()
