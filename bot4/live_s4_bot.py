# ==============================================================================
# STRATEGY S4 CONCEPT: Cross-Market Commodity Momentum
# 【戦略S4コンセプト: コモディティ相関モメンタムモデル】
# ------------------------------------------------------------------------------
# - Concept (EN): Uses Copper (XCUUSDm) 120-minute momentum (smoothed with a 60-min MA)
#   as a leading indicator for global industrial demand. When momentum crosses
#   above +0.5% during the London session, it signals a buying opportunity for
#   Silver (XAGUSDm) due to its lag in price discovery.
# - コンセプト (JA): 銅（XCUUSDm）の120分モメンタム（60分移動平均で平滑化）を世界的な
#   実需（工業需要）の先行指標として監視します。ロンドン時間中（8-16 UTC）に銅のモメンタムが
#   +0.5%を上抜けた場合、価格決定の遅れ（ラグ）が発生しやすい銀（XAGUSDm）の割安を狙い、
#   30分遅延後に買いエントリーを行います。
#
# - Target Instrument (対象銘柄): Silver (XAGUSDm / 銀)
#   Trigger Instrument (トリガー銘柄): Copper (XCUUSDm / 銅)
# - Configuration (設定): London Session (8-16 UTC), Lookback 120m, Smooth 60m, Threshold +0.5%
# - Execution (執行): 30-minute entry delay (30分遅延エントリー), 60-minute holding period (60分保有).
# ==============================================================================

import os
import sys
import logging
from datetime import datetime, timezone, timedelta
import traceback
import time
import pytz
import pandas as pd
import json
# --- Docker/Wine Path Setup ---
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
os.chdir(script_dir)

# --- Logging Setup ---
LOG_DIR = os.path.join(script_dir, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "s4_bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)


# 依存モジュールのインポート (同じフォルダ内のモジュールを再利用)
from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_BUY

# --- MT5 Symbols for Strategy S4 ---
# Exnessスタンダードデモ口座のシンボル (必要に応じて変更)
COPPER_SYMBOL = "XCUUSDm" # 銅 (HG_FUT)
SILVER_SYMBOL = "XAGUSDm" # 銀 (SI_FUT)

# --- Strategy Parameters (from Backtest) ---
POLL_INTERVAL_SECONDS = 5 * 60 # 5分おきに実行
LOOKBACK_BARS = 24 # pct120m なので 120分 = 24本 (5分足)
SMOOTHING_BARS = 12 # Lookback_Min 60 なので 60分 = 12本
THRESHOLD_PCT = 0.5
ENTRY_DELAY_MIN = 30
HOLD_PERIOD_MIN = 60
RISK_USD = 10.0 # 1回のトレードのリスク許容額 (USD)

STATE_FILE = os.path.join(script_dir, "s4_bot_state.json")

class S4LiveBot:
    def __init__(self):
        logging.info("Initializing S4 MT5 Live Bot (Native Windows/Linux Bridge)...")
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        
        # State management (JSONベースで再起動しても状態を保持)
        self.state = {
            "pending_signal_ts": 0, # エントリー待機中のシグナル発生時間
            "active_ticket": None,  # 現在保有中のチケットID
            "entry_ts": 0           # エントリー時間
        }
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                self.state = json.load(f)
                logging.info(f"Loaded state: {self.state}")

    def save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f)

    def log_trade_csv(self, action, ticket, symbol, lot_size=0, entry_price=0.0, exit_price=0.0, pnl=0.0):
        import csv
        csv_file = os.path.join(LOG_DIR, "s4_trades.csv")
        file_exists = os.path.isfile(csv_file)
        
        # UTCから日本時間(JST)に変換
        now_utc = datetime.now(timezone.utc)
        now_jst = now_utc + timedelta(hours=9)
        
        try:
            with open(csv_file, mode='a', newline='', encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp_JST", "Action", "Ticket", "Symbol", "LotSize", "EntryPrice", "ExitPrice", "PnL"])
                writer.writerow([
                    now_jst.strftime("%Y-%m-%d %H:%M:%S"),
                    action,
                    ticket,
                    symbol,
                    lot_size,
                    entry_price,
                    exit_price,
                    pnl
                ])
            logging.info(f"Trade logged to CSV: {action} {symbol} Lot:{lot_size} EP:{entry_price} XP:{exit_price} PnL:{pnl}")
        except Exception as e:
            logging.error(f"Failed to write to CSV: {e}")

    def start(self):
        logging.info("Starting S4 Live Bot...")
        
        if not self.dm.connect():
            logging.error("Failed to connect to MT5. Exiting.")
            return

        try:
            while True:
                now = datetime.now(timezone.utc)
                current_ts = int(now.timestamp())
                next_run_ts = (current_ts // POLL_INTERVAL_SECONDS + 1) * POLL_INTERVAL_SECONDS
                wait_time = next_run_ts - current_ts
                
                next_run_dt = datetime.fromtimestamp(next_run_ts, tz=timezone.utc).astimezone(pytz.timezone('Asia/Tokyo'))
                logging.info(f"Next cycle at {next_run_dt.strftime('%H:%M:%S')}. Waiting {wait_time:.2f} seconds...")
                
                time.sleep(wait_time)
                
                logging.info(f"--- Execution Cycle Starting ({datetime.now().strftime('%H:%M:%S')}) ---")
                self.run_cycle()
                
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def run_cycle(self):
        now_utc = datetime.now(timezone.utc)
        
        # 1. イグジット条件のチェック (保有時間 HOLD_PERIOD_MIN が経過したか)
        if self.state["active_ticket"]:
            # MQL5ブリッジの制約上、チケットがまだ存在するか確認するのが確実ですが、
            # ここでは時間ベースでクローズします
            elapsed_min = (now_utc.timestamp() - self.state["entry_ts"]) / 60.0
            if elapsed_min >= HOLD_PERIOD_MIN:
                logging.info(f"Hold period ({HOLD_PERIOD_MIN}m) reached. Closing position {self.state['active_ticket']}.")
                success = self.executor.close_position(self.state["active_ticket"])
                if success:
                    logging.info("Position closed successfully.")
                    self.log_trade_csv(
                        "EXIT", 
                        self.state["active_ticket"], 
                        SILVER_SYMBOL, 
                        lot_size=success.lot, 
                        entry_price=success.open_price, 
                        exit_price=success.close_price, 
                        pnl=success.profit
                    )
                else:
                    logging.warning("Failed to close position (maybe already closed by SL/TP or manual).")
                
                # 成功・失敗に関わらず状態をリセット
                self.state["active_ticket"] = None
                self.state["entry_ts"] = 0
                self.save_state()
            else:
                logging.info(f"Holding position... {elapsed_min:.1f}m / {HOLD_PERIOD_MIN}m elapsed.")
            
            # 保有中は新規エントリーやシグナル検知をしない
            return

        # 2. エントリー待機中のチェック (シグナル発生から ENTRY_DELAY_MIN 経過したか)
        if self.state["pending_signal_ts"] > 0:
            elapsed_delay = (now_utc.timestamp() - self.state["pending_signal_ts"]) / 60.0
            if elapsed_delay >= ENTRY_DELAY_MIN:
                logging.info(f"Entry delay ({ENTRY_DELAY_MIN}m) reached. Entering BUY on {SILVER_SYMBOL}.")
                
                # エントリー実行 (Buy)
                lot_size = self.executor.calculate_lot_size(SILVER_SYMBOL, RISK_USD, 100)
                ticket = self.executor.open_position(SILVER_SYMBOL, ORDER_TYPE_BUY, lot_size)
                
                if ticket:
                    logging.info(f"Successfully entered position. Ticket: {ticket}")
                    self.state["active_ticket"] = ticket
                    self.state["entry_ts"] = now_utc.timestamp()
                    self.log_trade_csv("ENTRY", ticket, SILVER_SYMBOL, lot_size=lot_size, entry_price=ticket.price)
                else:
                    logging.error("Failed to open position.")
                
                # 待機状態をリセット
                self.state["pending_signal_ts"] = 0
                self.save_state()
                return
            else:
                logging.info(f"Waiting for entry delay... {elapsed_delay:.1f}m / {ENTRY_DELAY_MIN}m elapsed.")
                return

        # 3. 新規シグナルの検知
        # 3.1 時間帯フィルタ (London: 8:00 - 15:59 UTC)
        hour_utc = now_utc.hour
        if not (8 <= hour_utc < 16):
            logging.info(f"Current hour {hour_utc} is outside London session (8-15). Skipping signal check.")
            return

        # 3.2 データ取得 (銅 5分足)
        # 必要な足数: 24 (pct120m) + 12 (rolling) + 1 (shift1) = 最低37本。余裕を持って100本取得
        df = self.dm.get_historical_data(COPPER_SYMBOL, 5, 100) # 5m timeframe
        if df is None or len(df) < 50:
            logging.warning(f"Failed to fetch data for {COPPER_SYMBOL} or insufficient data.")
            return

        close = df['Close']
        
        # 3.3 特徴量計算 (pct120m)
        # 120分前(24本前)からの変化率(%)
        pct_120m = (close - close.shift(LOOKBACK_BARS)) / close.shift(LOOKBACK_BARS) * 100
        
        # 3.4 平滑化 (12本の移動平均)
        smoothed = pct_120m.rolling(SMOOTHING_BARS).mean()
        
        if len(smoothed) < 2 or pd.isna(smoothed.iloc[-1]) or pd.isna(smoothed.iloc[-2]):
            return

        # 3.5 クロスオーバー判定 (前回 <= 0.5 かつ 今回 > 0.5)
        prev_val = smoothed.iloc[-2]
        curr_val = smoothed.iloc[-1]
        
        if prev_val <= THRESHOLD_PCT and curr_val > THRESHOLD_PCT:
            logging.info(f"+++ SIGNAL TRIGGERED! +++ {COPPER_SYMBOL} smoothed pct crossed above {THRESHOLD_PCT}% (Prev: {prev_val:.3f}, Curr: {curr_val:.3f})")
            self.state["pending_signal_ts"] = now_utc.timestamp()
            self.save_state()
            logging.info(f"Will wait {ENTRY_DELAY_MIN} minutes before entering.")
        else:
            logging.info(f"No signal. {COPPER_SYMBOL} smoothed pct = {curr_val:.3f}% (Threshold: {THRESHOLD_PCT}%)")

if __name__ == "__main__":
    try:
        bot = S4LiveBot()
        bot.start()
    except Exception as e:
        logging.error(f"CRITICAL CRASH: {e}")
        logging.error(traceback.format_exc())
