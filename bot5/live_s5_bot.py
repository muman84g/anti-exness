# ==============================================================================
# STRATEGY S5 CONCEPT: Multi-Asset Relative Strength & FX Correlation
# 【戦略S5コンセプト: 複数資産の相対力 ＆ 円クロス相関モデル】
# ------------------------------------------------------------------------------
# - Concept (EN): Combines a physical commodity (Copper: XCUUSDm) and Japanese Yen
#   exchange rates (USDJPYm, EURJPYm) to measure global risk sentiment.
#   It tracks (Copper + USDJPY) / EURJPY. When this index rises above +0.05%
#   during the Tokyo session, it indicates a risk-on momentum shift, triggering
#   a BUY signal on Crude Oil (USOILm) to ride the energy demand pickup.
# - コンセプト (JA): 実需ベースの物理コモディティ（銅: XCUUSDm）と、日本円の主要通貨ペア
#   （USDJPY, EURJPY）を組み合わせた合成指標 `(銅 + USDJPY) / EURJPY` から、グローバルな
#   市場のリスクセンチメント（リスクオン・オフ）を測定します。東京セッション中にこの相対指数が
#   +0.05%を上抜けた場合、エネルギー実需の拡大を期待して、原油（USOILm）を遅延なし（0分）で
#   買いエントリーします。
#
# - Target Instrument (対象銘柄): Crude Oil (USOILm / 原油)
#   Formula Components (指標構成要素): Copper (XCUUSDm), USDJPYm, EURJPYm
# - Configuration (設定): Tokyo Session (9-15 JST), Lookback 60m, Threshold +0.05%
# - Execution (執行): 0-minute entry delay (即時エントリー), 60-minute holding period (60分保有).
# ==============================================================================

import os
import sys
# --- Docker/Wine Path Setup ---
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import time
import json
import logging
import traceback
from datetime import datetime, timezone, timedelta
import pandas as pd
import pytz

# ログ設定
LOG_DIR = os.path.join(script_dir, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "s5_bot.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_BUY

# ============================================================
# S5 Strategy Configuration (ダッシュボードの結果から設定)
# ============================================================

# 1. ターゲット銘柄 (実際に売買する銘柄)
TARGET_SYMBOL = "USOILm" # CL_FUT (原油) のExnessシンボルなど

# 2. 特徴量 (Feature) の計算に使う銘柄
FEATURE_SYMBOL_1 = "XCUUSDm" # HG_FUT (銅)
FEATURE_SYMBOL_2 = "USDJPYm" # USDJPY_FX
FEATURE_SYMBOL_3 = "EURJPYm" # EURJPY_FX

# 3. パラメーター
SESSION_NAME = "Tokyo(9-15)" # "All", "Tokyo(9-15)", "London(8-16)", "NY(8-16)", "Lon_NY(Overlap)"
LOGIC_TYPE = "Normal"        # "Normal", "TrendFollow(SMA100)", "Pullback(Drop0.05%)", "Breakout(Rise0.05%)"

POLL_INTERVAL_SECONDS = 5 * 60
LOOKBACK_BARS = 12           # Lookback_Min / 5 (例: 60分なら12)
THRESHOLD_PCT = 0.05         # 閾値 (%)
ENTRY_DELAY_MIN = 0          # エントリー遅延 (分)
HOLD_PERIOD_MIN = 60         # 保有期間 (分)
RISK_USD = 10.0              # 1トレードの許容リスク (USD)

STATE_FILE = os.path.join(script_dir, "s5_bot_state.json")

class S5LiveBot:
    def __init__(self):
        logging.info("Initializing S5 MT5 Live Bot...")
        
        # 設定バリデーション
        if LOGIC_TYPE in ("Pullback(Drop0.05%)", "Breakout(Rise0.05%)") and ENTRY_DELAY_MIN == 0:
            logging.error(f"INVALID CONFIG: LOGIC_TYPE='{LOGIC_TYPE}' requires ENTRY_DELAY_MIN > 0 (currently 0).")
            logging.error("Pullback/Breakout logic needs a waiting period to measure price change.")
            raise ValueError(f"LOGIC_TYPE '{LOGIC_TYPE}' is incompatible with ENTRY_DELAY_MIN=0")
        
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        
        self.state = {
            "pending_signal_ts": 0,
            "signal_price": 0.0,    # 遅延ロジック検証用
            "active_ticket": None,
            "entry_ts": 0
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
        csv_file = os.path.join(LOG_DIR, "s5_trades.csv")
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
        logging.info("Starting S5 Live Bot...")
        if not self.dm.connect(): return

        try:
            while True:
                now = datetime.now(timezone.utc)
                current_ts = int(now.timestamp())
                next_run_ts = (current_ts // POLL_INTERVAL_SECONDS + 1) * POLL_INTERVAL_SECONDS
                wait_time = next_run_ts - current_ts
                
                logging.info(f"Waiting {wait_time:.2f} seconds for next 5M candle...")
                time.sleep(wait_time)
                
                logging.info(f"--- S5 Cycle Starting ({datetime.now().strftime('%H:%M:%S')}) ---")
                self.run_cycle()
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def run_cycle(self):
        now_utc = datetime.now(timezone.utc)
        
        # 1. イグジット処理
        if self.state["active_ticket"]:
            elapsed_min = (now_utc.timestamp() - self.state["entry_ts"]) / 60.0
            if elapsed_min >= HOLD_PERIOD_MIN:
                logging.info(f"Hold period ({HOLD_PERIOD_MIN}m) reached. Closing position {self.state['active_ticket']}.")
                success = self.executor.close_position(self.state["active_ticket"])
                if success:
                    logging.info("Position closed successfully.")
                    self.log_trade_csv(
                        "EXIT", 
                        self.state["active_ticket"], 
                        TARGET_SYMBOL, 
                        lot_size=success.lot, 
                        entry_price=success.open_price, 
                        exit_price=success.close_price, 
                        pnl=success.profit
                    )
                else:
                    logging.warning("Failed to close position (maybe already closed by SL/TP or manual).")
                self.state["active_ticket"] = None
                self.state["entry_ts"] = 0
                self.save_state()
            else:
                logging.info(f"Holding position... {elapsed_min:.1f}m / {HOLD_PERIOD_MIN}m elapsed.")
            return

        # 2. エントリー待機処理 (遅延 ＆ ロジックフィルター)
        if self.state["pending_signal_ts"] > 0:
            elapsed_delay = (now_utc.timestamp() - self.state["pending_signal_ts"]) / 60.0
            if elapsed_delay >= ENTRY_DELAY_MIN:
                logging.info(f"Entry delay ({ENTRY_DELAY_MIN}m) reached. Verifying final Logic...")
                
                df_target = self.dm.get_historical_data(TARGET_SYMBOL, 5, 20)
                if df_target is None: return
                current_price = df_target['Close'].iloc[-1]
                
                # Logicチェック
                if self.state["signal_price"] > 0:
                    ret_delay = (current_price - self.state["signal_price"]) / self.state["signal_price"]
                    
                    if LOGIC_TYPE == "Pullback(Drop0.05%)" and ret_delay >= -0.0005:
                        logging.info(f"Logic Failed: Did not drop 0.05%. (Change: {ret_delay*100:.3f}%)")
                        self.state["pending_signal_ts"] = 0; self.save_state(); return
                        
                    if LOGIC_TYPE == "Breakout(Rise0.05%)" and ret_delay <= 0.0005:
                        logging.info(f"Logic Failed: Did not rise 0.05%. (Change: {ret_delay*100:.3f}%)")
                        self.state["pending_signal_ts"] = 0; self.save_state(); return

                # エントリー実行
                lot_size = self.executor.calculate_lot_size(TARGET_SYMBOL, RISK_USD, 100)
                ticket = self.executor.open_position(TARGET_SYMBOL, ORDER_TYPE_BUY, lot_size)
                
                if ticket:
                    logging.info(f"Successfully entered position. Ticket: {ticket}")
                    self.state["active_ticket"] = ticket
                    self.state["entry_ts"] = now_utc.timestamp()
                    self.log_trade_csv("ENTRY", ticket, TARGET_SYMBOL, lot_size=lot_size, entry_price=ticket.price)
                self.state["pending_signal_ts"] = 0
                self.save_state()
                return
            else:
                logging.info(f"Waiting for entry delay... {elapsed_delay:.1f}m / {ENTRY_DELAY_MIN}m elapsed.")
                return

        # 3. セッションフィルター
        if SESSION_NAME != "All":
            now_tokyo = now_utc.astimezone(pytz.timezone('Asia/Tokyo'))
            now_london = now_utc.astimezone(pytz.timezone('Europe/London'))
            now_ny = now_utc.astimezone(pytz.timezone('America/New_York'))
            
            in_session = False
            if SESSION_NAME == "Tokyo(9-15)" and (9 <= now_tokyo.hour < 15): in_session = True
            elif SESSION_NAME == "London(8-16)" and (8 <= now_london.hour < 16): in_session = True
            elif SESSION_NAME == "NY(8-16)" and (8 <= now_ny.hour < 16): in_session = True
            elif SESSION_NAME == "Lon_NY(Overlap)" and ((8 <= now_london.hour < 16) or (8 <= now_ny.hour < 16)): in_session = True
            
            if not in_session:
                logging.info(f"Current time is outside {SESSION_NAME}. Skipping.")
                return

        # 4. データ取得と特微量計算
        df1 = self.dm.get_historical_data(FEATURE_SYMBOL_1, 5, 200)
        df2 = self.dm.get_historical_data(FEATURE_SYMBOL_2, 5, 200)
        df3 = self.dm.get_historical_data(FEATURE_SYMBOL_3, 5, 200)
        
        if df1 is None or df2 is None or df3 is None: return
        
        # 式のカスタマイズ (ダッシュボードの特微量に合わせる)
        # ここでは (FEATURE_1 + FEATURE_2) / FEATURE_3 を想定
        feature_series = (df1['Close'] + df2['Close']) / df3['Close']
        
        # 変化率の計算
        processed_series = feature_series.pct_change(periods=LOOKBACK_BARS) * 100
        
        if len(processed_series) < 2 or pd.isna(processed_series.iloc[-1]): return

        prev_val = processed_series.iloc[-2]
        curr_val = processed_series.iloc[-1]

        # 5. シグナル判定
        if prev_val <= THRESHOLD_PCT and curr_val > THRESHOLD_PCT:
            logging.info(f"+++ SIGNAL TRIGGERED! +++ Feature crossed above {THRESHOLD_PCT}%")
            
            # TrendFollow(SMA100)の即時フィルター
            if LOGIC_TYPE == "TrendFollow(SMA100)":
                df_target = self.dm.get_historical_data(TARGET_SYMBOL, 5, 200)
                if df_target is not None:
                    sma100 = df_target['Close'].rolling(100).mean().iloc[-1]
                    if df_target['Close'].iloc[-1] <= sma100:
                        logging.info("Filtered out by TrendFollow(SMA100). Price <= SMA100.")
                        return

            self.state["pending_signal_ts"] = now_utc.timestamp()
            
            # 遅延がある場合は現在の価格を記録しておく
            if ENTRY_DELAY_MIN > 0:
                df_target = self.dm.get_historical_data(TARGET_SYMBOL, 5, 10)
                if df_target is not None:
                    self.state["signal_price"] = float(df_target['Close'].iloc[-1])
            
            self.save_state()
            
            if ENTRY_DELAY_MIN == 0:
                logging.info("ENTRY_DELAY_MIN is 0. Will execute entry in the next tick.")
            else:
                logging.info(f"Will wait {ENTRY_DELAY_MIN} minutes before entry verification.")
        else:
            logging.info(f"No signal. Feature = {curr_val:.3f}% (Threshold: {THRESHOLD_PCT}%)")

if __name__ == "__main__":
    try:
        bot = S5LiveBot()
        bot.start()
    except Exception as e:
        logging.error(f"CRITICAL CRASH: {e}")
        logging.error(traceback.format_exc())
