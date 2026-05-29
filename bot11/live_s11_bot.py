# ==============================================================================
# STRATEGY s11 CONCEPT: USTECm -> US500m Lead-Lag Divergence Strategy Live Trading Bot (v1)
# 【戦略s11コンセプト: USTECm -> US500m 先行遅行ダイバージェンス追従戦略実運用ボット】
# ------------------------------------------------------------------------------
# - Leading Asset: USTECm (Nasdaq)
# - Lagging Asset: US500m (S&P 500) - ※取引対象は遅行銘柄のみ
# - Timeframe: M5 (5分足)
# - Trigger: Z-score の乖離（スプレッド = Lead_Z - Lag_Z）が閾値を突破した際、
#            遅行銘柄が先行銘柄を追従（スプレッド収束）する方向に逆張りエントリー。
# - Weekend Close: 土曜早朝 JST 05:00 以降は強制クローズ
# - Risk Sizing: エントリー時点の ATR に基づき、1トレード損切り時の損失が $10 固定となるようロットサイズを逆算
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

# スクリプト自身の絶対パス
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# ログ設定
LOG_DIR = os.path.join(script_dir, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "s11_bot.log")

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
# s11 Configuration (デフォルトは PnL Max 構成)
# ============================================================
POLL_INTERVAL_SECONDS = 15  # ポジションの価格監視間隔
STATE_FILE = os.path.join(script_dir, "s11_bot_state.json")
PARAMS_FILE = os.path.join(script_dir, "s11_params.json")
FIXED_RISK_USD = 10.0      # 1トレードあたりの許容リスク（10ドル）

DEFAULT_PARAMS = {
    'lead_symbol': 'USTECm',
    'lag_symbol': 'US500m',
    'window_z': 288,               # Z-score平滑窓幅 (24時間)
    'z_entry': 1.0,                # エントリー乖離閾値 (おすすめ: 1.2, PnL Max: 0.6)
    'z_exit': 0.0,                 # 決済収束閾値
    'exit_type': 'MEAN_REVERSION', # 決済ロジック ("MEAN_REVERSION", "TIME", "FIXED_RR", "LEAD_STALL")
    'max_hold_bars': 24,           # 最大保有バー数 (2時間)
    'sl_mult': 1.0,                # 損切りATR乗数 (1.0 ATR)
    'tp_mult': 1.5,                # 利確ATR乗数
    'use_be': True,                # 建値移動 (BE) の有無
    'multiplier': 50.0,            # US500m コントラクトサイズ
    'spread_pct': 0.00005,         # US500m 標準スプレッド (0.005%)
}

def load_params():
    if os.path.exists(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, "r") as f:
                params = json.load(f)
            logging.info(f"Successfully loaded parameters from {PARAMS_FILE}")
            # デフォルトキーの補完
            for k, v in DEFAULT_PARAMS.items():
                if k not in params:
                    params[k] = v
            return params
        except Exception as e:
            logging.error(f"Error loading {PARAMS_FILE}, using default parameters: {e}")
            return DEFAULT_PARAMS.copy()
    else:
        # デフォルトファイルを自動生成して保存
        try:
            with open(PARAMS_FILE, "w") as f:
                json.dump(DEFAULT_PARAMS, f, indent=4)
            logging.info(f"Created default parameters file at {PARAMS_FILE}")
        except Exception as e:
            logging.error(f"Failed to create default parameters file: {e}")
        return DEFAULT_PARAMS.copy()

PARAMS = load_params()

# ============================================================
# 時間判定ヘルパー
# ============================================================
def is_weekend_jst(dt_jst):
    # 土曜 05:00 JST 以降、または日曜は週末クローズ対象
    if dt_jst.weekday() == 5 and dt_jst.hour >= 5:
        return True
    if dt_jst.weekday() == 6:
        return True
    return False

class s11TradingBot:
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
            "last_processed_bar_time": None
        }
        self.save_state()

    def save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save state: {e}")

    def log_trade_csv(self, action, ticket, symbol, direction="", lot_size=0, price=0.0, pnl=0.0, reason=""):
        csv_file = os.path.join(LOG_DIR, "s11_trades.csv")
        file_exists = os.path.isfile(csv_file)
        
        now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
        
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

    def calculate_zscore_features(self, df: pd.DataFrame, window: int) -> pd.DataFrame:
        df = df.copy()
        df["MA_Z"] = df["Close"].rolling(window).mean()
        df["STD_Z"] = df["Close"].rolling(window).std()
        df["Z"] = (df["Close"] - df["MA_Z"]) / (df["STD_Z"] + 1e-8)
        
        # ATRの計算
        df["Range"] = df["High"] - df["Low"]
        df["ATR_24"] = df["Range"].rolling(24).mean()
        return df

    def start(self):
        logging.info("Starting s11 Lead-Lag Live Bot execution loop...")
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
        now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
        lag_sym = PARAMS['lag_symbol']
        lead_sym = PARAMS['lead_symbol']
        
        # 1. リアルタイムポジション管理（週末決済、時間決済、SL/TP決済、BE移動）
        if lag_sym in self.state["active_tickets"]:
            self.manage_existing_position(lag_sym, now_jst)

        # 2. 先行・遅行の最新ヒストリカルデータを取得し、同期させてシグナル判定
        try:
            # 5分足(timeframe=5)で過去350本取得（Z-score平滑288本窓をカバー）
            df_lead_raw = self.dm.get_historical_data(lead_sym, 5, 350)
            df_lag_raw = self.dm.get_historical_data(lag_sym, 5, 350)
            
            if df_lead_raw is None or df_lead_raw.empty or df_lag_raw is None or df_lag_raw.empty:
                logging.warning("Failed to fetch historical data for lead or lag symbols.")
                return

            if len(df_lead_raw) < PARAMS['window_z'] or len(df_lag_raw) < PARAMS['window_z']:
                logging.warning("Not enough historical bars to compute Z-score features.")
                return

            # JSTにローカライズ
            for df in [df_lead_raw, df_lag_raw]:
                if df.index.tz is None:
                    df.index = df.index.tz_localize('UTC').tz_convert('Asia/Tokyo')
                else:
                    df.index = df.index.tz_convert('Asia/Tokyo')

            # 特徴量計算
            df_lead = self.calculate_zscore_features(df_lead_raw, PARAMS['window_z'])
            df_lag = self.calculate_zscore_features(df_lag_raw, PARAMS['window_z'])
            
            # インデックス同期
            common_idx = df_lead.index.intersection(df_lag.index)
            if len(common_idx) < 5:
                logging.warning("No overlapping timestamps found.")
                return
                
            df_lead_sync = df_lead.loc[common_idx]
            df_lag_sync = df_lag.loc[common_idx]
            
            # 最新の確定バーの時刻
            last_completed_bar_time = common_idx[-2].strftime("%Y-%m-%d %H:%M:%S")
            recorded_processed_time = self.state.get("last_processed_bar_time")

            # 新しい確定バーが出現した場合のみシグナル評価
            if recorded_processed_time != last_completed_bar_time:
                logging.info(f"New completed bar detected at {last_completed_bar_time}. Running evaluation...")
                
                # 相関係数の符号確認
                c_val = df_lead_sync["Z"].corr(df_lag_sync["Z"])
                corr_sign = np.sign(c_val) if not pd.isna(c_val) else 1.0
                if corr_sign == 0: corr_sign = 1.0
                
                self.evaluate_completed_bar(df_lead_sync, df_lag_sync, corr_sign, last_completed_bar_time, now_jst)
                
                self.state["last_processed_bar_time"] = last_completed_bar_time
                self.save_state()

        except Exception as e:
            logging.error(f"Error in cycle processing: {e}")
            logging.error(traceback.format_exc())

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
        atr = pos["atr"]
        be_active = pos.get("be_active", False)
        entry_time_str = pos["entry_time"]
        entry_time = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

        # A. 週末強制決済
        if is_weekend_jst(now_jst):
            logging.info(f"[{symbol}] Weekend close triggered JST={now_jst.strftime('%Y-%m-%d %H:%M:%S')}. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, "WEEKEND")
            return

        # B. 時間強制決済 (max_hold_bars 分相当)
        elapsed_seconds = (now_jst - entry_time).total_seconds()
        # M5足で max_hold_bars 分経過しているか
        if elapsed_seconds >= PARAMS['max_hold_bars'] * 5 * 60:
            logging.info(f"[{symbol}] Time close triggered. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, "TIME")
            return

        # C. リアルタイム SL/TP 監視 & 建値移動 (FIXED_RR)
        close_position = False
        exit_reason = ""

        if direction == "LONG":
            pos["max_seen_p"] = max(pos.get("max_seen_p", entry_price), current_bid)
            
            # 建値移動 (BE)
            if PARAMS['use_be'] and not be_active and pos["max_seen_p"] >= (entry_price + atr):
                logging.info(f"[{symbol}] Breakeven triggered for LONG. Moving SL from {sl_price:.4f} to {entry_price:.4f}")
                pos["sl_price"] = entry_price
                pos["be_active"] = True
                self.save_state()
                sl_price = entry_price

            if current_bid <= sl_price:
                close_position = True
                exit_reason = "SL"
            elif current_bid >= tp_price:
                close_position = True
                exit_reason = "TP"

        else:  # SHORT
            pos["min_seen_p"] = min(pos.get("min_seen_p", entry_price), current_ask)

            # 建値移動 (BE)
            if PARAMS['use_be'] and not be_active and pos["min_seen_p"] <= (entry_price - atr):
                logging.info(f"[{symbol}] Breakeven triggered for SHORT. Moving SL from {sl_price:.4f} to {entry_price:.4f}")
                pos["sl_price"] = entry_price * (1.0 + PARAMS['spread_pct'])
                pos["be_active"] = True
                self.save_state()
                sl_price = pos["sl_price"]

            if current_ask >= sl_price:
                close_position = True
                exit_reason = "SL"
            elif current_ask <= tp_price:
                close_position = True
                exit_reason = "TP"

        if close_position:
            logging.info(f"[{symbol}] Realtime exit triggered: {exit_reason}. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, exit_reason)

    def evaluate_completed_bar(self, df_lead, df_lag, corr_sign, bar_time_str, now_jst):
        lag_sym = PARAMS['lag_symbol']
        ticket = self.state["active_tickets"].get(lag_sym)
        pos = self.state["positions"].get(lag_sym)
        
        # 1つ前の確定バー (インデックス -2) の参照
        row_lead = df_lead.iloc[-2]
        row_lag = df_lag.iloc[-2]
        
        # 特徴量の NaN チェック
        if pd.isna(row_lead["Z"]) or pd.isna(row_lag["Z"]) or pd.isna(row_lag["ATR_24"]):
            logging.warning(f"NaN features detected at completed bar {bar_time_str}. Skipping evaluation.")
            return

        z_lead_val = row_lead["Z"]
        z_lag_val = row_lag["Z"]
        spread = z_lead_val - z_lag_val
        atr = row_lag["ATR_24"]

        # ────────────── インジケータ決済の判定 ──────────────
        if ticket and pos:
            direction = pos["direction"]
            exit_triggered = False
            exit_reason = ""
            
            if PARAMS['exit_type'] == "MEAN_REVERSION":
                if abs(spread) <= PARAMS['z_exit']:
                    exit_triggered = True
                    exit_reason = "INDICATOR_TP"
            elif PARAMS['exit_type'] == "LEAD_STALL":
                # 先行銘柄が前バーに比べて逆行したか
                z_lead_prev = df_lead.iloc[-3]["Z"]
                if not pd.isna(z_lead_prev):
                    if direction == "LONG" and z_lead_val < z_lead_prev:
                        exit_triggered = True
                        exit_reason = "LEAD_STALL"
                    elif direction == "SHORT" and z_lead_val > z_lead_prev:
                        exit_triggered = True
                        exit_reason = "LEAD_STALL"

            if exit_triggered:
                logging.info(f"[{lag_sym}] Indicator Exit ({PARAMS['exit_type']}) triggered at completed bar. Closing ticket {ticket}.")
                self.close_and_cleanup(lag_sym, ticket, exit_reason)
                return

        # ────────────── 新規エントリー判定 ──────────────
        if not ticket:
            # 週末期間中はエントリー不可
            if is_weekend_jst(now_jst):
                return

            # シグナル判定
            sig_dir = ""
            if corr_sign * spread >= PARAMS['z_entry']:
                sig_dir = "LONG"
            elif corr_sign * spread <= -PARAMS['z_entry']:
                sig_dir = "SHORT"
                
            if sig_dir:
                logging.info(f"[{lag_sym}] Divergence Signal detected: {sig_dir} at completed bar {bar_time_str} (Spread Z: {spread:.4f})")
                self.execute_entry(lag_sym, sig_dir, row_lag, atr)

    def execute_entry(self, symbol, direction, row_lag, atr):
        info = self.executor.get_symbol_info(symbol)
        if not info:
            logging.error(f"[{symbol}] Failed to get symbol info for entry.")
            return

        current_ask = info.ask
        current_bid = info.bid

        # ロット計算用損切り幅 sl_d (標準 1.0 ATR)
        sl_d = max(PARAMS['sl_mult'] * atr, 0.0001)

        # ロット計算
        sl_usd_per_lot = sl_d * PARAMS['multiplier']
        if sl_usd_per_lot > 0:
            target_lot = FIXED_RISK_USD / sl_usd_per_lot
        else:
            target_lot = info.volume_min

        max_lot_limit = 2.0  # 安全上限
        target_lot = max(info.volume_min, min(target_lot, info.volume_max, max_lot_limit))
        target_lot = round(target_lot / info.volume_step) * info.volume_step
        target_lot = round(target_lot, 2)

        order_type = ORDER_TYPE_BUY if direction == "LONG" else ORDER_TYPE_SELL
        ticket = self.executor.open_position(symbol, order_type, target_lot)

        if ticket:
            actual_entry_price = float(ticket.price)
            
            # 実執行価格をベースに SL と TP を決定
            if direction == "LONG":
                sl_px = actual_entry_price - sl_d
                tp_px = actual_entry_price + PARAMS['tp_mult'] * sl_d
            else:
                sl_px = actual_entry_price + sl_d
                # SHORT決済買戻しは Ask価格のためスプレッド分を加算
                sl_px_ask = sl_px * (1.0 + PARAMS['spread_pct'])
                tp_px = actual_entry_price - PARAMS['tp_mult'] * sl_d
                
            # 状態更新
            now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
            now_jst_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
            
            self.state["active_tickets"][symbol] = ticket
            self.state["positions"][symbol] = {
                "ticket": ticket,
                "direction": direction,
                "entry_time": now_jst_str,
                "entry_price": actual_entry_price,
                "sl_price": float(sl_px) if direction == "LONG" else float(sl_px_ask),
                "tp_price": float(tp_px),
                "atr": float(atr),
                "be_active": False,
                "lot_size": float(target_lot),
                "max_seen_p": actual_entry_price,
                "min_seen_p": actual_entry_price
            }
            self.save_state()
            
            logging.info(f"[{symbol}] Position opened successfully. Ticket: {ticket}, Lot: {target_lot}, Entry: {actual_entry_price:.4f}, SL: {self.state['positions'][symbol]['sl_price']:.4f}, TP: {tp_px:.4f}")
            self.log_trade_csv("ENTRY", ticket, symbol, direction, target_lot, actual_entry_price)
        else:
            logging.error(f"[{symbol}] Failed to open position.")

    def close_and_cleanup(self, symbol, ticket, reason):
        pos = self.state["positions"].get(symbol)
        lot = pos["lot_size"] if pos else 0.0
        direction = pos["direction"] if pos else ""
        
        success = self.executor.close_position(ticket)
        if success:
            logging.info(f"[{symbol}] Successfully closed position (Reason: {reason}). Ticket: {ticket}, PnL: {success.profit}")
            self.log_trade_csv(f"EXIT_{reason}", ticket, symbol, direction, lot, success.close_price, success.profit, reason)
        else:
            logging.warning(f"[{symbol}] Failed to close ticket {ticket} via EA. Clean up state anyway to avoid loop lock.")
            self.log_trade_csv(f"EXIT_FAIL_{reason}", ticket, symbol, direction, lot, 0.0, 0.0, reason)
            
        if symbol in self.state["active_tickets"]:
            del self.state["active_tickets"][symbol]
        if symbol in self.state["positions"]:
            del self.state["positions"][symbol]
        self.save_state()

if __name__ == "__main__":
    bot = s11TradingBot()
    bot.start()
