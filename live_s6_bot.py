# ==============================================================================
# STRATEGY S6 CONCEPT: Nasdaq Volatility Spillover with Dynamic Z-Score Band
# 【戦略S6コンセプト: ナスダック・ボラティリティ・スピルオーバー ＆ Zスコア動的バンドモデル】
# ------------------------------------------------------------------------------
# - Concept (EN): Uses Nasdaq's (USTECm) 5-minute volatility (smoothed over 60 min)
#   to detect sudden spikes in market-wide fear and volume. When this volatility
#   breaks above its 24-hour dynamic Bollinger-style band (+1.5 standard deviations)
#   during the Lon-NY overlap session, it signals institutional risk reallocation,
#   triggering an immediate BUY on Crude Oil (USOILm) to capture high-volume breakouts.
# - コンセプト (JA): ナスダック（USTECm）の5分足ボラティリティ（60分移動平均で平滑化）から、
#   市場全体のリスクセンチメント急悪化（恐怖や出来高の急増）を測定します。ロンドン・NYの
#   重複セッション中に、このボラティリティが「過去24時間の移動平均＋1.5σ標準偏差」で構成される
#   動的閾値（Zスコア）を上抜けた瞬間、機関投資家のリスク資産リバランシング（資金大移動）が発生
#   したと判断し、強いモメンタムブレイクアウトを狙って原油（USOILm）を遅延なし（0分）で買いエントリーします。
#
# - Target Instrument (対象銘柄): Crude Oil (USOILm / 原油)
#   Trigger Instrument (トリガー銘柄): Nasdaq 100 Index (USTECm / ナスダック)
# - Configuration (設定): Lon-NY Overlap Session, Lookback 60m, Z-Score Window 24h (288 bars),
#   Dynamic Threshold +1.5σ.
# - Execution (執行): 0-minute entry delay (即時エントリー), 60-minute holding period (60分保有).
# ==============================================================================

import os
import time
import json
import logging
import traceback
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import pytz

# ログ設定
LOG_DIR = "/app/logs"
if os.name == 'nt':
    LOG_DIR = "Z:/app/logs"
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "s6_bot.log")

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
# S6 Strategy Configuration (backtest_grid.py の結果から設定)
# ============================================================
# S6: NQ_FUT_volatility | Lon_NY(Overlap) | Normal | 60min LB | 1.5σ | 0delay | 60hold | CL_FUT
# IS: 825 trades, WR 62.2%, PF 1.59, ROI 191.3%
# OOS: 528 trades, WR 57.8%, PF 1.36, ROI 68.4%, FINAL BAL ¥168,413

# 1. ターゲット銘柄 (実際に売買する銘柄)
TARGET_SYMBOL = "USOILm"       # CL_FUT (原油) のExnessシンボル

# 2. 特徴量 (Feature) の計算に使う銘柄
#    NQ_FUT_volatility = (NQ High - NQ Low) / NQ Close * 100
FEATURE_SYMBOL = "USTECm"      # NQ_FUT (ナスダック) のExnessシンボル

# 3. パラメーター
SESSION_NAME = "Lon_NY(Overlap)"   # ロンドン＋NY重複セッション
LOGIC_TYPE = "Normal"              # "Normal", "Trend(1H)", "Trend(4H)", "Trend(1H+4H)", "Pullback(Drop0.05%)", "Breakout(Rise0.05%)"

POLL_INTERVAL_SECONDS = 5 * 60
LOOKBACK_BARS = 12                 # Lookback_Min / 5 (60分 / 5 = 12)
THRESHOLD_SIGMA = 1.5              # Zスコア閾値 (σ)
ZSCORE_WINDOW = 288                # Zスコア計算ウィンドウ (過去24時間 = 288本)
ENTRY_DELAY_MIN = 0                # エントリー遅延 (分)
HOLD_PERIOD_MIN = 60               # 保有期間 (分)
RISK_USD = 10.0                    # 1トレードの許容リスク (USD)

# MTF (多重タイムフレーム) トレンドフィルター設定
# LOGIC_TYPE が "Trend(1H)", "Trend(4H)", "Trend(1H+4H)" の時のみ使用
MTF_SMA_1H_BARS = 240              # 1H足SMA20相当 (5分足240本)
MTF_SMA_4H_BARS = 960              # 4H足SMA20相当 (5分足960本)

STATE_FILE = "s6_bot_state.json"

class S6LiveBot:
    def __init__(self):
        logging.info("Initializing S6 MT5 Live Bot...")
        
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
        csv_file = os.path.join(LOG_DIR, "s6_trades.csv")
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
        logging.info("Starting S6 Live Bot...")
        logging.info(f"Config: Feature={FEATURE_SYMBOL}(volatility), Target={TARGET_SYMBOL}")
        logging.info(f"Config: Session={SESSION_NAME}, Logic={LOGIC_TYPE}")
        logging.info(f"Config: LB={LOOKBACK_BARS}, Sigma={THRESHOLD_SIGMA}, Delay={ENTRY_DELAY_MIN}m, Hold={HOLD_PERIOD_MIN}m")
        if not self.dm.connect(): return

        try:
            while True:
                now = datetime.now(timezone.utc)
                current_ts = int(now.timestamp())
                next_run_ts = (current_ts // POLL_INTERVAL_SECONDS + 1) * POLL_INTERVAL_SECONDS
                wait_time = next_run_ts - current_ts
                
                logging.info(f"Waiting {wait_time:.2f} seconds for next 5M candle...")
                time.sleep(wait_time)
                
                logging.info(f"--- S6 Cycle Starting ({datetime.now().strftime('%H:%M:%S')}) ---")
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
                
                # Logicチェック (Pullback/Breakout用)
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
            now_london = now_utc.astimezone(pytz.timezone('Europe/London'))
            now_ny = now_utc.astimezone(pytz.timezone('America/New_York'))
            now_tokyo = now_utc.astimezone(pytz.timezone('Asia/Tokyo'))
            
            in_session = False
            if SESSION_NAME == "Tokyo(9-15)" and (9 <= now_tokyo.hour < 15): in_session = True
            elif SESSION_NAME == "London(8-16)" and (8 <= now_london.hour < 16): in_session = True
            elif SESSION_NAME == "NY(8-16)" and (8 <= now_ny.hour < 16): in_session = True
            elif SESSION_NAME == "Lon_NY(Overlap)" and ((8 <= now_london.hour < 16) or (8 <= now_ny.hour < 16)): in_session = True
            
            if not in_session:
                logging.info(f"Current time is outside {SESSION_NAME}. Skipping.")
                return

        # 4. データ取得と特徴量計算
        #    NQ_FUT_volatility = (High - Low) / Close * 100
        #    Zスコアの計算に十分なバー数（288 + lookback + バッファ）を取得する
        required_bars = ZSCORE_WINDOW + LOOKBACK_BARS + 50
        df_feat = self.dm.get_historical_data(FEATURE_SYMBOL, 5, required_bars)
        
        if df_feat is None or len(df_feat) < ZSCORE_WINDOW + LOOKBACK_BARS:
            logging.warning(f"Insufficient data for feature calculation. Got {len(df_feat) if df_feat is not None else 0} bars, need {ZSCORE_WINDOW + LOOKBACK_BARS}.")
            return
        
        # ボラティリティ特徴量: (High - Low) / Close * 100
        volatility_series = (df_feat['High'] - df_feat['Low']) / df_feat['Close'] * 100
        
        # 変化率の特微量 (pct/volatility): lookback期間の移動平均で平滑化
        processed_series = volatility_series.rolling(LOOKBACK_BARS).mean()
        
        # Zスコア閾値の計算: 過去288本の平均 + N×標準偏差
        rolling_mean = processed_series.rolling(ZSCORE_WINDOW).mean()
        rolling_std = processed_series.rolling(ZSCORE_WINDOW).std()
        
        dynamic_threshold = rolling_mean + THRESHOLD_SIGMA * rolling_std
        
        if pd.isna(processed_series.iloc[-1]) or pd.isna(dynamic_threshold.iloc[-1]):
            logging.warning("Feature or threshold contains NaN. Waiting for more data.")
            return

        prev_val = processed_series.iloc[-2]
        curr_val = processed_series.iloc[-1]
        prev_thresh = dynamic_threshold.iloc[-2]
        curr_thresh = dynamic_threshold.iloc[-1]

        # 5. MTF (多重タイムフレーム) トレンドフィルター
        if LOGIC_TYPE in ("Trend(1H)", "Trend(4H)", "Trend(1H+4H)"):
            # ターゲット銘柄の長期データを取得してSMAを計算
            mtf_bars_needed = max(MTF_SMA_1H_BARS, MTF_SMA_4H_BARS) + 50
            df_target_mtf = self.dm.get_historical_data(TARGET_SYMBOL, 5, mtf_bars_needed)
            
            if df_target_mtf is None or len(df_target_mtf) < MTF_SMA_4H_BARS:
                logging.warning("Insufficient data for MTF trend filter.")
                return
            
            current_price = df_target_mtf['Close'].iloc[-1]
            sma_1h = df_target_mtf['Close'].rolling(MTF_SMA_1H_BARS).mean().iloc[-1]
            sma_4h = df_target_mtf['Close'].rolling(MTF_SMA_4H_BARS).mean().iloc[-1]
            
            trend_1h_up = current_price > sma_1h
            trend_4h_up = current_price > sma_4h
            
            if LOGIC_TYPE == "Trend(1H)" and not trend_1h_up:
                logging.info(f"Filtered by Trend(1H): Price {current_price:.2f} <= SMA240 {sma_1h:.2f}")
                return
            elif LOGIC_TYPE == "Trend(4H)" and not trend_4h_up:
                logging.info(f"Filtered by Trend(4H): Price {current_price:.2f} <= SMA960 {sma_4h:.2f}")
                return
            elif LOGIC_TYPE == "Trend(1H+4H)" and not (trend_1h_up and trend_4h_up):
                logging.info(f"Filtered by Trend(1H+4H): 1H={trend_1h_up}, 4H={trend_4h_up}")
                return

        # 6. シグナル判定 (Zスコア・ブレイクアウト)
        #    前回: 閾値以下 → 今回: 閾値を上抜け（ゴールデンクロス）
        if prev_val <= prev_thresh and curr_val > curr_thresh:
            logging.info(f"+++ SIGNAL TRIGGERED! +++ Feature={curr_val:.4f} crossed above dynamic threshold={curr_thresh:.4f} ({THRESHOLD_SIGMA}σ)")
            logging.info(f"    Rolling Mean={rolling_mean.iloc[-1]:.4f}, Rolling Std={rolling_std.iloc[-1]:.4f}")
            
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
            logging.info(f"No signal. Feature={curr_val:.4f}, Threshold(+{THRESHOLD_SIGMA}σ)={curr_thresh:.4f}")

if __name__ == "__main__":
    try:
        bot = S6LiveBot()
        bot.start()
    except Exception as e:
        logging.error(f"CRITICAL CRASH: {e}")
        logging.error(traceback.format_exc())
