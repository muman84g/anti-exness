# ==============================================================================
# STRATEGY s12 CONCEPT: A-Rank Multi-Anomaly Pinpoint Live Trading Bot (v1)
# 【戦略s12コンセプト: Aランクアノマリー実運用並行稼働ボット】
# ------------------------------------------------------------------------------
# - USDJPYm: Thursday 05:00 JST -> SELL (Hold 48 bars, TP 3.0 ATR, SL None)
# - GBPNZDm: Wednesday 02:50 JST -> SELL (Hold 48 bars, TP None, SL None)
# - NZDCHFm: Friday 02:55 JST -> SELL (Hold 48 bars, TP None, SL None)
# - Weekend Close: Saturday 05:00 JST以降は強制クローズ
# - Lot Sizing: パラメータファイルでペアごとに直接ロットサイズを設定・調整可能
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
LOG_FILE = os.path.join(LOG_DIR, "s12_bot.log")

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

# 状態管理ファイル・パラメータファイルの定義
STATE_FILE = os.path.join(script_dir, "s12_bot_state.json")
PARAMS_FILE = os.path.join(script_dir, "s12_params.json")

DEFAULT_PARAMS = {
    "strategies": {
        "USDJPYm": {
            "day": "Thursday",
            "time": "05:00",
            "direction": "SHORT",
            "hold_bars": 48,
            "tp_mult": 3.0,
            "sl_mult": 0.0,
            "lot_size": 0.1
        },
        "GBPNZDm": {
            "day": "Wednesday",
            "time": "02:50",
            "direction": "SHORT",
            "hold_bars": 48,
            "tp_mult": 0.0,
            "sl_mult": 0.0,
            "lot_size": 0.05
        },
        "NZDCHFm": {
            "day": "Friday",
            "time": "02:55",
            "direction": "SHORT",
            "hold_bars": 48,
            "tp_mult": 0.0,
            "sl_mult": 0.0,
            "lot_size": 0.05
        }
    },
    "general": {
        "poll_interval_seconds": 15
    }
}

def load_params():
    if os.path.exists(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, "r") as f:
                params = json.load(f)
            logging.info(f"Successfully loaded parameters from {PARAMS_FILE}")
            # strategyおよびgeneralキーの存在補完
            if "strategies" not in params:
                params["strategies"] = DEFAULT_PARAMS["strategies"].copy()
            if "general" not in params:
                params["general"] = DEFAULT_PARAMS["general"].copy()
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

# ============================================================
# 週末時間判定ヘルパー (土曜 05:00 JST 以降および日曜)
# ============================================================
def is_weekend_jst(dt_jst):
    if dt_jst.weekday() == 5 and dt_jst.hour >= 5:
        return True
    if dt_jst.weekday() == 6:
        return True
    return False

class s12TradingBot:
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
                logging.info("Successfully loaded state file.")
            except Exception as e:
                logging.error(f"Error loading state file: {e}")
                self.init_empty_state()
        else:
            self.init_empty_state()

    def init_empty_state(self):
        self.state = {
            "active_tickets": {},
            "positions": {},
            "last_entry_date": {}
        }
        self.save_state()

    def save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save state: {e}")

    def log_trade_csv(self, action, ticket, symbol, direction="", lot_size=0.0, price=0.0, pnl=0.0, reason=""):
        csv_file = os.path.join(LOG_DIR, "s12_trades.csv")
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

    def start(self):
        poll_interval = PARAMS["general"].get("poll_interval_seconds", 15)
        logging.info(f"Starting s12 Multi-Anomaly Live Bot execution loop. Poll interval: {poll_interval}s...")
        if not self.dm.connect():
            logging.error("Failed to connect via EA Bridge. Exit.")
            return

        try:
            while True:
                self.run_cycle()
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def run_cycle(self):
        now_jst = datetime.now(JST)
        
        # 1. アクティブな全ポジションの管理（週末決済、時間決済、SL/TP決済）
        active_symbols = list(self.state["active_tickets"].keys())
        for symbol in active_symbols:
            try:
                self.manage_existing_position(symbol, now_jst)
            except Exception as e:
                logging.error(f"Error managing position for {symbol}: {e}")
                logging.error(traceback.format_exc())

        # 2. 時間トリガーによるエントリー判定
        for sym, s_conf in PARAMS["strategies"].items():
            try:
                # すでにアクティブなポジションがある場合はスキップ
                if sym in self.state["active_tickets"]:
                    continue
                    
                day = s_conf["day"]
                t_val = s_conf["time"]
                direction = s_conf["direction"]
                
                current_day = now_jst.strftime("%A")
                current_time = now_jst.strftime("%H:%M")
                
                # 曜日と時刻が完全に一致している場合
                if current_day == day and current_time == t_val:
                    today_str = now_jst.strftime("%Y-%m-%d")
                    last_ent_date = self.state.get("last_entry_date", {}).get(sym)
                    
                    # 本日まだエントリーしていない場合のみ実行
                    if last_ent_date != today_str:
                        logging.info(f"[{sym}] Anomaly Time Triggered: {current_day} {current_time} ({direction})")
                        success = self.execute_anomaly_entry(sym, s_conf, now_jst)
                        
                        # エントリーが成功した場合のみ日付ロックをかける
                        if success:
                            if "last_entry_date" not in self.state:
                                self.state["last_entry_date"] = {}
                            self.state["last_entry_date"][sym] = today_str
                            self.save_state()
            except Exception as e:
                logging.error(f"Error in anomaly trigger check for {sym}: {e}")
                logging.error(traceback.format_exc())

    def execute_anomaly_entry(self, symbol, s_conf, now_jst):
        logging.info(f"[{symbol}] Running entry process. Fetching historical data to calculate ATR...")
        
        # 過去データ取得 (5分足で過去350本。288本ATRをカバー)
        df_raw = self.dm.get_historical_data(symbol, 5, 350)
        if df_raw is None or df_raw.empty:
            logging.error(f"[{symbol}] Failed to fetch historical data. Cannot compute ATR. Entry aborted.")
            return False
            
        if len(df_raw) < 289:
            logging.error(f"[{symbol}] Not enough historical bars to compute 288-period ATR. Bars: {len(df_raw)}. Entry aborted.")
            return False
            
        # ATR計算 (288期間の平均レンジ)
        df = df_raw.copy()
        df["Range"] = df["High"] - df["Low"]
        df["ATR"] = df["Range"].rolling(288).mean().bfill()
        
        # 直近の確定バーのATRを取得
        atr_val = float(df["ATR"].iloc[-2])
        logging.info(f"[{symbol}] Calculated ATR_288: {atr_val:.5f} (using completed bar).")

        info = self.executor.get_symbol_info(symbol)
        if not info:
            logging.error(f"[{symbol}] Failed to get symbol info from MT5. Entry aborted.")
            return False

        lot_size = s_conf["lot_size"]
        
        # ロットサイズの上限・下限・ステップ調整
        lot_size = max(info.volume_min, min(lot_size, info.volume_max))
        lot_size = round(lot_size / info.volume_step) * info.volume_step
        lot_size = round(lot_size, 2)

        direction = s_conf["direction"]
        order_type = ORDER_TYPE_SELL if direction == "SHORT" else ORDER_TYPE_BUY
        
        tp_mult = s_conf["tp_mult"]
        sl_mult = s_conf["sl_mult"]
        entry_estimate = info.bid if direction == "SHORT" else info.ask
        tp_price = 0.0
        sl_price = 0.0
        if direction == "SHORT":
            tp_price = entry_estimate - tp_mult * atr_val if tp_mult > 0 else 0.0
            sl_price = entry_estimate + sl_mult * atr_val if sl_mult > 0 else 0.0
        else:
            tp_price = entry_estimate + tp_mult * atr_val if tp_mult > 0 else 0.0
            sl_price = entry_estimate - sl_mult * atr_val if sl_mult > 0 else 0.0

        logging.info(f"[{symbol}] Sending order to MT5. Direction: {direction}, Lot: {lot_size}, SL: {sl_price:.5f}, TP: {tp_price:.5f}")
        ticket = self.executor.open_position(symbol, order_type, lot_size, sl=sl_price, tp=tp_price)
        
        if ticket:
            actual_entry_price = float(ticket.price)
            
            # SL と TP のレート算出
            if direction == "SHORT":
                tp_price = actual_entry_price - tp_mult * atr_val if tp_mult > 0 else 0.0
                sl_price = actual_entry_price + sl_mult * atr_val if sl_mult > 0 else 0.0
            else: # LONG (将来用)
                tp_price = actual_entry_price + tp_mult * atr_val if tp_mult > 0 else 0.0
                sl_price = actual_entry_price - sl_mult * atr_val if sl_mult > 0 else 0.0
            if sl_price > 0.0 or tp_price > 0.0:
                self.executor.modify_position_sl_tp(ticket, sl_price, tp_price)

            # 状態の保存
            now_jst_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
            self.state["active_tickets"][symbol] = ticket
            self.state["positions"][symbol] = {
                "ticket": ticket,
                "direction": direction,
                "entry_time": now_jst_str,
                "entry_price": actual_entry_price,
                "sl_price": float(sl_price),
                "tp_price": float(tp_price),
                "atr": float(atr_val),
                "lot_size": float(lot_size),
                "hold_bars": int(s_conf["hold_bars"])
            }
            self.save_state()
            
            logging.info(f"[{symbol}] Position opened. Ticket: {ticket}, Entry Price: {actual_entry_price:.5f}, SL: {sl_price:.5f}, TP: {tp_price:.5f}")
            self.log_trade_csv("ENTRY", ticket, symbol, direction, lot_size, actual_entry_price)
            return True
        else:
            logging.error(f"[{symbol}] EA order placement failed.")
            return False

    def manage_existing_position(self, symbol, now_jst):
        ticket = self.state["active_tickets"].get(symbol)
        pos = self.state["positions"].get(symbol)
        if not ticket or not pos:
            return

        info = self.executor.get_symbol_info(symbol)
        if not info:
            return

        current_ask = info.ask
        current_bid = info.bid

        direction = pos["direction"]
        entry_price = pos["entry_price"]
        sl_price = pos["sl_price"]
        tp_price = pos["tp_price"]
        entry_time_str = pos["entry_time"]
        entry_time = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        hold_bars = pos["hold_bars"]

        # A. 週末強制決済 (土曜 05:00 JST 以降)
        if is_weekend_jst(now_jst):
            logging.info(f"[{symbol}] Weekend close triggered JST={now_jst.strftime('%Y-%m-%d %H:%M:%S')}. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, "WEEKEND")
            return

        # B. 時間強制決済 (hold_bars 分相当経過したか)
        # M5足で hold_bars 本分 ＝ hold_bars * 5分
        elapsed_seconds = (now_jst - entry_time).total_seconds()
        if elapsed_seconds >= hold_bars * 5 * 60:
            logging.info(f"[{symbol}] Time close triggered. Elapsed: {elapsed_seconds}s (Hold constraint: {hold_bars*5*60}s). Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, "TIME")
            return

        # C. リアルタイム TP / SL 監視
        close_position = False
        exit_reason = ""

        if direction == "LONG":
            # LONG決済(売り)はBidで行う
            if sl_price > 0.0 and current_bid <= sl_price:
                close_position = True
                exit_reason = "SL"
            elif tp_price > 0.0 and current_bid >= tp_price:
                close_position = True
                exit_reason = "TP"
        else: # SHORT
            # SHORT決済(買戻し)はAskで行う
            if sl_price > 0.0 and current_ask >= sl_price:
                close_position = True
                exit_reason = "SL"
            elif tp_price > 0.0 and current_ask <= tp_price:
                close_position = True
                exit_reason = "TP"

        if close_position:
            logging.info(f"[{symbol}] Realtime exit triggered: {exit_reason}. Current Ask: {current_ask:.5f}, Bid: {current_bid:.5f}. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, exit_reason)

    def close_and_cleanup(self, symbol, ticket, reason):
        pos = self.state["positions"].get(symbol)
        lot = pos["lot_size"] if pos else 0.0
        direction = pos["direction"] if pos else ""
        
        success = self.executor.close_position(ticket)
        if success:
            logging.info(f"[{symbol}] Closed position via EA (Reason: {reason}). Ticket: {ticket}, Profit: {success.profit}")
            self.log_trade_csv(f"EXIT_{reason}", ticket, symbol, direction, lot, success.close_price, success.profit, reason)
        else:
            logging.warning(f"[{symbol}] EA close request failed for ticket {ticket}. Keeping state so the bot can retry.")
            self.log_trade_csv(f"EXIT_FAIL_{reason}", ticket, symbol, direction, lot, 0.0, 0.0, reason)
            if pos is not None:
                pos["last_close_fail_reason"] = reason
                pos["last_close_fail_time"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
                self.save_state()
            return
            
        if symbol in self.state["active_tickets"]:
            del self.state["active_tickets"][symbol]
        if symbol in self.state["positions"]:
            del self.state["positions"][symbol]
        self.save_state()

if __name__ == "__main__":
    bot = s12TradingBot()
    bot.start()
