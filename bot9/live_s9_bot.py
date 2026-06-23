# ==============================================================================
# STRATEGY s9 CONCEPT: Robust US Indices Tokyo Session M5 ORB Live Trading Bot (v5)
# 【戦略s9コンセプト: 米国株価指数・東京セッションM5 ORB実運用ボット・v5改良版】
# ------------------------------------------------------------------------------
# - Base Strategy: 東京セッション（JST 9:00 - 10:00）の高値安値ブレイクアウト。
# - Traded Assets:
#   ['USTECm', 'US30m', 'JP225m', 'USOILm', 'US500m']
# - Risk Management:
#   * VolFilter: 東京レンジ幅が日次ATRの 15%〜70% の日のみ取引（だまし・値幅枯渇の回避）。
#   * ATR動的TP/SL: 過去5日間の日次平均レンジ（ATR）ベースで決済幅を決定。
#   * 建値移動（BE）: 含み益が初期SLの50%に達したら、SLを建値に移動。
#   * 段階的トレール（PPL）: 含み益がTP目標の75%に達したら、SLをTP目標の50%位置にロック。
#   * タイムクローズ: 最適保持時間（JST 16:00）に強制成行手仕舞い。
# - Dynamic Lot Sizing: 各エントリー時点のSL幅に基づき、
#   損切り時の損失が「$10固定」となるようにロットサイズを逆算・決定。
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

# スクリプト自身の絶対パス
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# ログ設定
LOG_DIR = os.path.join(script_dir, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "s9_bot.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# 依存モジュールのインポート
from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_BUY, ORDER_TYPE_SELL

# ============================================================
# s9 Configuration (adopted primary_with_combo_us500_only)
# ============================================================
POLL_INTERVAL_SECONDS = 5  # 常時価格監視（ブレイクアウト・TP/SL）のため5秒に設定
STATE_FILE = os.path.join(script_dir, "s9_bot_state.json")
RISK_USD = 10.0  # 1トレードあたりの許容リスク金額
MAX_LOT_LIMIT = 2.0

TRADED_SYMBOLS = ['USTECm', 'US30m', 'JP225m', 'USOILm', 'US500m']

# Adopted live params from the validated bot9 primary candidate.
PARAMS = {
    'USTECm': {
        'close_hour': 18,
        'tp_mult': 1.0,
        'sl_mult': 0.4,
        'be_ratio': 0.5,
        'exit_mode': 'current_sl_tp_be_ppl',
        'min_range_ratio': 0.20,
        'max_range_ratio': 0.55,
    },
    'US30m': {
        'close_hour': 16,
        'tp_mult': 0.8,
        'sl_mult': 0.4,
        'be_ratio': 0.5,
        'exit_mode': 'no_ppl',
        'min_range_ratio': 0.20,
        'max_range_ratio': 0.55,
    },
    'JP225m': {
        'close_hour': 21,
        'tp_mult': 1.2,
        'sl_mult': 0.4,
        'be_ratio': 0.5,
        'exit_mode': 'current_sl_tp_be_ppl',
        'min_range_ratio': 0.20,
        'max_range_ratio': 0.55,
    },
    'USOILm': {
        'close_hour': 21,
        'tp_mult': 1.0,
        'sl_mult': 0.5,
        'be_ratio': 0.5,
        'exit_mode': 'current_sl_tp_be_ppl',
        'min_range_ratio': 0.20,
        'max_range_ratio': 0.55,
    },
    'US500m': {
        'close_hour': 21,
        'tp_mult': 1.2,
        'sl_mult': 0.4,
        'be_ratio': 0.5,
        'exit_mode': 'time_only',
        'min_range_ratio': 0.15,
        'max_range_ratio': 0.70,
    },
    'NZDCADm': {
        'close_hour': 21,
        'tp_mult': 0.8,
        'sl_mult': 0.4,
        'be_ratio': 0.5,
        'exit_mode': 'plain_sl_tp',
        'min_range_ratio': 0.20,
        'max_range_ratio': 0.60,
    },
}

# スプレッドコストマッピング (スリッページ等考慮用の仮定値)
COST_MAP = {
    'USTECm': 0.00015,
    'US30m': 0.00015,
    'JP225m': 0.00015,
    'USOILm': 0.00015,
    'US500m': 0.00015,
    'NZDCADm': 0.00015,
}

# ============================================================
# 1. 1ロットあたりの価値（USD換算）の取得 (S9用)
# ============================================================
def get_lot_multiplier_usd(symbol, price, usdjpy_rate):
    if symbol == 'JP225m': return 1.0 / usdjpy_rate
    elif symbol in ['USTECm', 'US500m', 'US30m']: return 1.0
    elif symbol == 'USOILm': return 100.0
    return 100000.0

def saturday_entry_forbidden(now_jst):
    return now_jst.weekday() == 5 and (now_jst.hour > 2 or (now_jst.hour == 2 and now_jst.minute >= 0))

def saturday_force_close_due(now_jst):
    return now_jst.weekday() == 5 and (now_jst.hour > 2 or (now_jst.hour == 2 and now_jst.minute >= 30))

def exit_flags(exit_mode):
    return {
        'use_sl': exit_mode in ('current_sl_tp_be_ppl', 'no_ppl', 'plain_sl_tp'),
        'use_tp': exit_mode in ('current_sl_tp_be_ppl', 'no_ppl', 'plain_sl_tp'),
        'use_be': exit_mode in ('current_sl_tp_be_ppl', 'no_ppl'),
        'use_ppl': exit_mode == 'current_sl_tp_be_ppl',
    }

def entry_guard_reason(symbol, now_jst, range_ratio=None, direction=None):
    if saturday_entry_forbidden(now_jst):
        return "Saturday entry is forbidden at/after 02:00 JST"

    if symbol == 'NZDCADm' and now_jst.weekday() in (0, 1):
        return "NZDCADm Monday/Tuesday JST entry is forbidden"
    if symbol == 'USOILm' and now_jst.weekday() in (3, 4):
        return "USOILm Thursday/Friday JST entry is forbidden"
    if symbol == 'JP225m' and now_jst.month in (2, 7):
        return "JP225m February/July entry is forbidden"

    if symbol == 'US500m':
        if direction == 'LONG':
            return "US500m LONG entry is forbidden"
        if range_ratio is not None and not (range_ratio <= 0.18 or range_ratio >= 0.34):
            return "US500m Tokyo_Range_Ratio must be <= 0.18 or >= 0.34"

    return None

class s9TradingBot:
    def __init__(self):
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        
        self.state = {}
        self.load_state()
        
    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    self.state = json.load(f)
                logging.info(f"Loaded existing state for date: {self.state.get('date')}")
            except Exception as e:
                logging.error(f"Error loading state: {e}")
                self.state = {}
        else:
            self.state = {}

    def save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save state: {e}")

    def ensure_state_shape(self):
        defaults = {
            "active_tickets": {},
            "range_high": {},
            "range_low": {},
            "range_ratio": {},
            "atr": {},
            "be_trigger_dist": {},
            "initial_sl": {},
            "tp_target": {},
            "be_active": {},
            "ppl_active": {},
            "ppl_trigger": {},
            "ppl_lock": {},
            "has_entered_today": {},
            "position_direction": {},
            "entry_price": {},
            "lot_size": {},
            "exit_mode": {},
        }
        for key, value in defaults.items():
            self.state.setdefault(key, value.copy())

    def log_trade_csv(self, action, ticket, symbol, direction="", lot_size=0, price=0.0, pnl=0.0):
        csv_file = os.path.join(LOG_DIR, "s9_trades.csv")
        file_exists = os.path.isfile(csv_file)
        
        now_jst = datetime.now(JST)
        price_value = "" if price is None else price
        pnl_value = "" if pnl is None else pnl
        
        try:
            with open(csv_file, mode='a', newline='', encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp_JST", "Action", "Ticket", "Symbol", "Direction", "LotSize", "Price", "PnL"])
                writer.writerow([
                    now_jst.strftime("%Y-%m-%d %H:%M:%S"), action, ticket, symbol, direction, lot_size, price_value, pnl_value
                ])
        except Exception as e:
            logging.error(f"Failed to write trade log to CSV: {e}")

    def start(self):
        logging.info("Starting s9 ORB Live Bot execution loop...")
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
        now_jst = datetime.now(JST)
        today_str = now_jst.strftime("%Y-%m-%d")
        date_rollover_with_active = self.state.get("date") != today_str and bool(self.state.get("active_tickets"))
        
        # 1. 新しい日の場合は状態を完全に初期化
        if self.state.get("date") != today_str:
            if date_rollover_with_active:
                logging.warning("New day detected, but active tickets remain. Preserving state until positions are closed.")
            else:
                logging.info(f"New day detected: {today_str}. Initializing daily state...")
                self.state = {
                    "date": today_str,
                    "active_tickets": {},
                    "range_high": {},
                    "range_low": {},
                    "range_ratio": {},
                    "atr": {},
                    "be_trigger_dist": {},
                    "initial_sl": {},
                    "tp_target": {},
                    "be_active": {},
                    "ppl_active": {},
                    "ppl_trigger": {},
                    "ppl_lock": {},
                    "has_entered_today": {},
                    "position_direction": {},
                    "entry_price": {},
                    "lot_size": {},
                    "exit_mode": {},
                }
                self.save_state()
        self.ensure_state_shape()

        # 各取引アセットの判定ループ
        for col in TRADED_SYMBOLS:
            if date_rollover_with_active and col not in self.state["active_tickets"]:
                continue

            param = PARAMS[col]
            close_hour = param['close_hour']
            
            # ── A. 強制タイムクローズ判定 (指定時間以降は取引不可 ＆ ポジション強制クローズ) ──
            time_close_due = now_jst.hour >= close_hour
            weekend_close_due = saturday_force_close_due(now_jst)
            if time_close_due or weekend_close_due:
                if col in self.state["active_tickets"]:
                    ticket = self.state["active_tickets"][col]
                    close_label = "WEEKEND" if weekend_close_due else "TIME"
                    close_text = "Saturday 02:30 JST" if weekend_close_due else f"JST {close_hour}:00"
                    logging.info(f"[{col}] {close_text} reached ({close_label} Close). Closing position (Ticket: {ticket}).")
                    success = self.executor.close_position(ticket)
                    if success:
                        if getattr(success, "already_closed", False):
                            logging.warning(
                                f"Position {ticket} for {col} was already closed on MT5. "
                                "Logging UNKNOWN exit instead of zero price/profit."
                            )
                            self.log_trade_csv(f"EXIT_{close_label}_UNKNOWN", ticket, col, price=None, pnl=None)
                        else:
                            logging.info(f"Successfully closed position for {col} ({close_label} Close). PnL: {success.profit}")
                            self.log_trade_csv(f"EXIT_{close_label}", ticket, col, price=success.close_price, pnl=success.profit)
                        del self.state["active_tickets"][col]
                        self.save_state()
                    else:
                        logging.warning(f"Failed to close position {ticket} for {col}. Keeping state so the bot can retry.")
                        self.state.setdefault("last_close_fail", {})[col] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
                        self.save_state()
                continue

            if saturday_entry_forbidden(now_jst) and col not in self.state["active_tickets"]:
                if not self.state["has_entered_today"].get(col, False):
                    logging.info(f"[{col}] Saturday entry lock is active from 02:00 JST. Skipping new entries.")
                    self.state["has_entered_today"][col] = True
                    self.save_state()
                continue

            # ── B. 東京レンジ高値安値・過去5日平均ATRの計算 ──
            # JST 10:00以降で、本日の基準価格情報が確定していない場合に実行
            if now_jst.hour >= 10 and col not in self.state["range_high"]:
                logging.info(f"[{col}] JST 10:00 reached. Calculating Tokyo Range and ATR...")
                try:
                    # 1. 東京レンジ（9:00〜10:00 JST）の取得 (5分足直近30本取得)
                    df_range = self.dm.get_historical_data(col, 5, 30)
                    if df_range is not None and not df_range.empty:
                        df_range.index = df_range.index.tz_localize('UTC').tz_convert('Asia/Tokyo')
                        df_today = df_range[df_range.index.date == now_jst.date()]
                        df_tokyo = df_today[df_today.index.hour == 9]
                        
                        if not df_tokyo.empty:
                            r_high = df_tokyo['High'].max()
                            r_low = df_tokyo['Low'].min()
                        else:
                            logging.warning(f"[{col}] Tokyo session (JST 9:00) bars not found yet. Will retry next loop.")
                            continue
                    else:
                        logging.warning(f"[{col}] Failed to fetch range data. Will retry next loop.")
                        continue

                    # 2. 過去5日間の平均日次レンジ（ATR）の取得 (5分足直近1600本取得)
                    df_atr_raw = self.dm.get_historical_data(col, 5, 1600)
                    if df_atr_raw is not None and len(df_atr_raw) >= 1400:
                        df_atr_raw.index = df_atr_raw.index.tz_localize('UTC').tz_convert('Asia/Tokyo')
                        daily_ranges = df_atr_raw.groupby(df_atr_raw.index.date).apply(lambda x: x['High'].max() - x['Low'].min())
                        completed_ranges = daily_ranges[daily_ranges.index < now_jst.date()]
                        if len(completed_ranges) >= 1:
                            atr_val = completed_ranges.tail(5).mean()
                        else:
                            logging.warning(f"[{col}] Not enough daily ranges completed. Will retry.")
                            continue
                    else:
                        logging.warning(f"[{col}] Failed to fetch ATR data. Will retry next loop.")
                        continue
                        
                    # Tokyo Range Width Filter (ボラティリティ比率制限)
                    tokyo_range_width = r_high - r_low
                    range_ratio = tokyo_range_width / atr_val
                    min_range_ratio = param['min_range_ratio']
                    max_range_ratio = param['max_range_ratio']
                    
                    self.state["range_high"][col] = float(r_high)
                    self.state["range_low"][col] = float(r_low)
                    self.state["range_ratio"][col] = float(range_ratio)
                    self.state["atr"][col] = float(atr_val)
                    self.state["be_active"][col] = False
                    self.state["ppl_active"][col] = False
                    
                    if range_ratio < min_range_ratio or range_ratio > max_range_ratio:
                        logging.info(f"[{col}] Tokyo range width ({tokyo_range_width:.2f}) is outside limits. Ratio: {range_ratio:.2f} (Limits: {min_range_ratio}-{max_range_ratio}). Skipping trading today.")
                        self.state["has_entered_today"][col] = True
                    elif entry_guard_reason(col, now_jst, range_ratio=range_ratio):
                        reason = entry_guard_reason(col, now_jst, range_ratio=range_ratio)
                        logging.info(f"[{col}] Entry guard blocked trading today: {reason}. Ratio: {range_ratio:.2f}")
                        self.state["has_entered_today"][col] = True
                    else:
                        logging.info(f"[{col}] Tokyo Range confirmed. Width: {tokyo_range_width:.2f}, ATR: {atr_val:.2f}, Ratio: {range_ratio:.2f} (Limits: {min_range_ratio}-{max_range_ratio})")
                        self.state["has_entered_today"][col] = False
                        
                    self.save_state()
                    
                except Exception as e:
                    logging.error(f"[{col}] Error precalculating daily ORB setup: {e}")
                    logging.error(traceback.format_exc())
                    continue

            # レンジ定義が未完了の場合はそれ以上の監視をスクリーニング
            if col not in self.state["range_high"]:
                continue

            # ── C. ブレイクアウト監視 ＆ ポジション管理フェーズ (JST 10:00以降) ──
            if now_jst.hour >= 10:
                info = self.executor.get_symbol_info(col)
                if not info:
                    continue
                current_ask = info.ask
                current_bid = info.bid
                
                # 1) ポジション未保有 ＆ 本日未取引の場合 ➡ ブレイクアウト監視
                if col not in self.state["active_tickets"] and not self.state["has_entered_today"].get(col, False):
                    r_high = self.state["range_high"][col]
                    r_low = self.state["range_low"][col]
                    atr = self.state["atr"][col]
                    
                    direction = ""
                    entry_price = 0.0
                    
                    # LONGブレイク条件: Ask価格が高値を上抜けた場合
                    if current_ask >= r_high and current_bid <= r_low:
                        logging.warning(f"[{col}] Whipsaw/Spread expansion detected (Ask >= High and Bid <= Low). Skipping entry.")
                    elif current_ask >= r_high:
                        direction = "LONG"
                        entry_price = current_ask
                    # SHORTブレイク条件: Bid価格が安値を下抜けた場合
                    elif current_bid <= r_low:
                        direction = "SHORT"
                        entry_price = current_bid
                        
                    if direction:
                        logging.info(f"[{col}] {direction} Breakout Detected! Ask={current_ask:.2f}, Bid={current_bid:.2f}, Range=[{r_low:.2f}, {r_high:.2f}]")

                        range_ratio = self.state.get("range_ratio", {}).get(col)
                        reason = entry_guard_reason(col, now_jst, range_ratio=range_ratio, direction=direction)
                        if reason:
                            logging.info(f"[{col}] Entry guard rejected {direction}: {reason}. No more entries today.")
                            self.state["has_entered_today"][col] = True
                            self.save_state()
                            continue
                        
                        tp_dist = atr * param['tp_mult']
                        sl_dist = atr * param['sl_mult']
                        flags = exit_flags(param.get('exit_mode', 'current_sl_tp_be_ppl'))
                        
                        # TP / SL / PPL のターゲット価格算出
                        if direction == "LONG":
                            init_sl = entry_price - sl_dist
                            tp_target = entry_price + tp_dist
                            ppl_trig = entry_price + 0.75 * tp_dist
                            ppl_lock = entry_price + 0.50 * tp_dist
                        else:
                            init_sl = entry_price + sl_dist
                            tp_target = entry_price - tp_dist
                            ppl_trig = entry_price - 0.75 * tp_dist
                            ppl_lock = entry_price - 0.50 * tp_dist
                            
                        # リスクベース・ロットサイズ計算 ($10 固定リスク)
                        usdjpy_rate = 150.0
                        jpy_info = self.executor.get_symbol_info('USDJPYm')
                        if jpy_info:
                            usdjpy_rate = jpy_info.bid
                            
                        multiplier = getattr(info, "price_unit_value", 0.0)
                        if multiplier <= 0:
                            multiplier = get_lot_multiplier_usd(col, entry_price, usdjpy_rate)
                        sl_usd_per_lot = sl_dist * multiplier
                        
                        if sl_usd_per_lot > 0:
                            target_lot = RISK_USD / sl_usd_per_lot
                        else:
                            target_lot = 0.01
                            
                        target_lot = max(info.volume_min, min(target_lot, info.volume_max, MAX_LOT_LIMIT))
                        target_lot = round(target_lot / info.volume_step) * info.volume_step
                        target_lot = round(target_lot, 2)
                        
                        # 成行発注の実行
                        order_type = ORDER_TYPE_BUY if direction == 'LONG' else ORDER_TYPE_SELL
                        order_sl = init_sl if flags['use_sl'] else 0.0
                        order_tp = tp_target if flags['use_tp'] else 0.0
                        ticket = self.executor.open_position(col, order_type, target_lot, sl=order_sl, tp=order_tp)
                        
                        if ticket:
                            logging.info(f"[{col}] Breakout Order Filled. Ticket: {ticket} Lot: {target_lot} Price: {ticket.price} ExitMode: {param.get('exit_mode')}")
                            self.state["active_tickets"][col] = ticket
                            self.state["has_entered_today"][col] = True
                            self.state["position_direction"][col] = direction
                            self.state["entry_price"][col] = float(ticket.price)
                            self.state["initial_sl"][col] = float(init_sl)
                            self.state["tp_target"][col] = float(tp_target)
                            self.state["be_trigger_dist"][col] = float(sl_dist * param['be_ratio'])
                            self.state["be_active"][col] = False
                            self.state["ppl_active"][col] = False
                            self.state["ppl_trigger"][col] = float(ppl_trig)
                            self.state["ppl_lock"][col] = float(ppl_lock)
                            self.state["lot_size"][col] = float(target_lot)
                            self.state["exit_mode"][col] = param.get('exit_mode', 'current_sl_tp_be_ppl')
                            self.save_state()
                            
                            self.log_trade_csv("ENTRY", ticket, col, direction, target_lot, ticket.price)
                        else:
                            logging.error(f"[{col}] Open Order failed.")
                            
                # 2) ポジション保有中の場合 ➡ TP / SL 判定およびトレール（BE/PPL）監視
                elif col in self.state["active_tickets"]:
                    direction = self.state["position_direction"][col]
                    entry_p = self.state["entry_price"][col]
                    sl_p = self.state["initial_sl"][col]
                    tp_p = self.state["tp_target"][col]
                    be_trig = self.state["be_trigger_dist"][col]
                    be_act = self.state["be_active"][col]
                    ppl_act = self.state.get("ppl_active", {}).get(col, False)
                    ppl_trig = self.state.get("ppl_trigger", {}).get(col, 0.0)
                    ppl_lock = self.state.get("ppl_lock", {}).get(col, 0.0)
                    ticket = self.state["active_tickets"][col]
                    exit_mode = self.state.get("exit_mode", {}).get(col, param.get('exit_mode', 'current_sl_tp_be_ppl'))
                    flags = exit_flags(exit_mode)
                    if not any(flags.values()):
                        continue
                    
                    close_trade = False
                    reason = ""
                    current_sl = sl_p
                    
                    if direction == "LONG":
                        # ① 段階的トレール（PPL）判定
                        if flags['use_ppl'] and not ppl_act and current_bid >= ppl_trig:
                            logging.info(f"[{col}] LONG Partial Profit Lock Triggered! Move SL to lock profit ({ppl_lock:.2f})")
                            self.executor.modify_position_sl_tp(ticket, ppl_lock, tp_p)
                            self.state["ppl_active"][col] = True
                            self.save_state()
                        # ② 建値移動（BE）判定
                        elif flags['use_be'] and not ppl_act and not be_act and (current_bid - entry_p) >= be_trig:
                            logging.info(f"[{col}] LONG Breakeven Triggered! Move SL to entry ({entry_p:.2f})")
                            self.executor.modify_position_sl_tp(ticket, entry_p, tp_p)
                            self.state["be_active"][col] = True
                            self.save_state()
                            
                        # 現在のアクティブSL判定
                        if flags['use_ppl'] and self.state.get("ppl_active", {}).get(col, False):
                            current_sl = ppl_lock
                            reason_candidate = "PARTIAL_LOCK"
                        elif flags['use_be'] and self.state.get("be_active", {}).get(col, False):
                            current_sl = entry_p
                            reason_candidate = "BREAKEVEN"
                        else:
                            current_sl = sl_p
                            reason_candidate = "STOP_LOSS"
                            
                        # エグジット判定
                        if flags['use_sl'] and current_bid <= current_sl:
                            close_trade = True
                            reason = reason_candidate
                        elif flags['use_tp'] and current_bid >= tp_p:
                            close_trade = True
                            reason = "TAKE_PROFIT"
                            
                    else: # SHORT
                        # ① 段階的トレール（PPL）判定
                        if flags['use_ppl'] and not ppl_act and current_ask <= ppl_trig:
                            logging.info(f"[{col}] SHORT Partial Profit Lock Triggered! Move SL to lock profit ({ppl_lock:.2f})")
                            self.executor.modify_position_sl_tp(ticket, ppl_lock, tp_p)
                            self.state["ppl_active"][col] = True
                            self.save_state()
                        # ② 建値移動（BE）判定
                        elif flags['use_be'] and not ppl_act and not be_act and (entry_p - current_ask) >= be_trig:
                            logging.info(f"[{col}] SHORT Breakeven Triggered! Move SL to entry Ask.")
                            self.executor.modify_position_sl_tp(ticket, entry_p * (1 + COST_MAP.get(col, 0.00015)), tp_p)
                            self.state["be_active"][col] = True
                            self.save_state()
                            
                        # 現在のアクティブSL判定
                        spread_pct = COST_MAP.get(col, 0.00015)
                        be_sl = entry_p * (1 + spread_pct)
                        
                        if flags['use_ppl'] and self.state.get("ppl_active", {}).get(col, False):
                            current_sl = ppl_lock
                            reason_candidate = "PARTIAL_LOCK"
                        elif flags['use_be'] and self.state.get("be_active", {}).get(col, False):
                            current_sl = be_sl
                            reason_candidate = "BREAKEVEN"
                        else:
                            current_sl = sl_p
                            reason_candidate = "STOP_LOSS"
                            
                        # エグジット判定
                        if flags['use_sl'] and current_ask >= current_sl:
                            close_trade = True
                            reason = reason_candidate
                        elif flags['use_tp'] and current_ask <= tp_p:
                            close_trade = True
                            reason = "TAKE_PROFIT"
                            
                    if close_trade:
                        logging.info(f"[{col}] Exit Triggered! Reason: {reason} (Bid={current_bid:.2f}, Ask={current_ask:.2f}, SL={current_sl:.2f}, TP={tp_p:.2f})")
                        success = self.executor.close_position(ticket)
                        if success:
                            if getattr(success, "already_closed", False):
                                logging.warning(
                                    f"Position {ticket} for {col} was already closed on MT5. "
                                    "Logging UNKNOWN exit instead of zero price/profit."
                                )
                                self.log_trade_csv(f"EXIT_{reason}_UNKNOWN", ticket, col, price=None, pnl=None)
                            else:
                                logging.info(f"Successfully closed position for {col} ({reason}). PnL: {success.profit}")
                                self.log_trade_csv(f"EXIT_{reason}", ticket, col, price=success.close_price, pnl=success.profit)
                            del self.state["active_tickets"][col]
                            self.save_state()
                        else:
                            logging.warning(f"Failed to close position {ticket} for {col}. Keeping state so the bot can retry.")
                            self.state.setdefault("last_close_fail", {})[col] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
                            self.save_state()

if __name__ == "__main__":
    bot = s9TradingBot()
    bot.start()
