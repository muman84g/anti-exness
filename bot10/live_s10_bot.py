# ==============================================================================
# STRATEGY s10 CONCEPT: Selected Climax Reversal Strategy Live Trading Bot (v1)
# 【戦略s10コンセプト: 選定2銘柄クライマックス反転戦略実運用ボット】
# ------------------------------------------------------------------------------
# - Traded Assets:
#   1. USOILm (Crude Oil)
#   2. US500m (S&P 500)
# - Timeframe: M5 (5分足)
# - Trigger: 確定足ベースの逆張りシグナル判定（Confirmation = False）
# - Session Filter: JST 16:00 - 02:59 の間に確定したシグナルでエントリー
# - Weekend Close: 土曜早朝 JST 05:00 以降は強制クローズ
# - Time Close: エントリー後 96バー（8時間）で決済
# - Risk Sizing: 各エントリー時点のSL幅に基づき、損失が $10 固定となるようロットを逆算
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
LOG_FILE = os.path.join(LOG_DIR, "s10_bot.log")

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
# s10 Configuration
# ============================================================
POLL_INTERVAL_SECONDS = 15  # ポジションの価格監視間隔
STATE_FILE = os.path.join(script_dir, "s10_bot_state.json")
FIXED_RISK_USD = 10.0      # 1トレードあたりの許容リスク（10ドル）
MAX_LOT_LIMIT = 2.0

TRADED_SYMBOLS = ['USOILm', 'US500m']

# 各アセットの最適パラメータ (バックテスト結果に基づく)
PARAMS = {
    'USOILm': {
        'multiplier': 1000.0,
        'spread_pct': 0.0003,
        'dev_type': 'Percent',
        'dev_threshold': 0.6,
        'vol_z_threshold': 2.0,
        'exit_type': 'MA_Dev_0.2',
    },
    'US500m': {
        'multiplier': 50.0,
        'spread_pct': 0.00005,
        'dev_type': 'ATR',
        'dev_threshold': 2.0,
        'vol_z_threshold': 2.5,
        'exit_type': 'MA_Cross',
    }
}

# ============================================================
# リスクベース・ロット計算用乗数
# ============================================================
def get_lot_multiplier_usd(symbol):
    if symbol == 'USOILm':
        return 1000.0
    elif symbol == 'US500m':
        return 50.0
    return 1.0

# ============================================================
# 時間判定ヘルパー
# ============================================================
def is_weekend_jst(dt_jst):
    # weekday: 0=月曜, 5=土曜, 6=日曜
    # 土曜 05:00 JST 以降、または日曜は週末クローズ対象
    if dt_jst.weekday() == 5 and dt_jst.hour >= 5:
        return True
    if dt_jst.weekday() == 6:
        return True
    return False

def is_in_session_jst(dt_jst):
    # セッションフィルター: JST 16:00 - 02:59
    hour = dt_jst.hour
    return (hour >= 16) or (hour <= 2)

class s10TradingBot:
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
                self.ensure_state_shape()
                logging.info("Successfully loaded state file.")
            except Exception as e:
                logging.error(f"Error loading state file: {e}")
                self.init_empty_state()
        else:
            self.init_empty_state()

    def ensure_state_shape(self):
        if not isinstance(self.state, dict):
            self.state = {}
        self.state.setdefault("active_tickets", {})
        self.state.setdefault("positions", {})
        self.state.setdefault("last_processed_bar_time", {})

    def init_empty_state(self):
        self.state = {
            "active_tickets": {},
            "positions": {},
            "last_processed_bar_time": {}
        }
        self.save_state()

    def save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self.state, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as e:
            logging.error(f"Failed to save state: {e}")
            return False

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

    def log_trade_csv(self, action, ticket, symbol, direction="", lot_size=0, price=0.0, pnl=0.0, reason=""):
        csv_file = os.path.join(LOG_DIR, "s10_trade_errors.csv" if action.startswith("EXIT_FAIL_") else "s10_trades.csv")
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

    def calculate_features(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["MA_50"] = df["Close"].rolling(50).mean()
        df["MA_200"] = df["Close"].rolling(200).mean()
        df["Dev_MA_50"] = (df["Close"] - df["MA_50"]) / df["MA_50"] * 100.0
        df["Range"] = df["High"] - df["Low"]
        df["ATR_24"] = df["Range"].rolling(24).mean()
        df["Dev_ATR"] = (df["Close"] - df["MA_50"]) / (df["ATR_24"] + 1e-8)
        df["Vol_Mean_24"] = df["Volume"].rolling(24).mean()
        df["Vol_Std_24"] = df["Volume"].rolling(24).std()
        df["Vol_Z"] = (df["Volume"] - df["Vol_Mean_24"]) / (df["Vol_Std_24"] + 1e-8)
        df["Body"] = (df["Close"] - df["Open"]).abs()
        df["Lower_Shadow"] = df[["Open", "Close"]].min(axis=1) - df["Low"]
        df["Upper_Shadow"] = df["High"] - df[["Open", "Close"]].max(axis=1)
        return df

    def start(self):
        logging.info("Starting s10 Climax Reversal Live Bot execution loop...")
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
        
        # 1. リアルタイムポジション管理（週末決済、時間決済、SL/TP決済、BE移動）
        for symbol in list(self.state["active_tickets"].keys()):
            self.manage_existing_position(symbol, now_jst)

        # 2. 各銘柄について最新データを取得し、確定足シグナル判定とインジケータ決済を判定
        for symbol in TRADED_SYMBOLS:
            try:
                # 5分足(timeframe=5)で過去250本取得（MA_200等を十分カバー）
                df = self.dm.get_historical_data(symbol, 5, 250)
                if df is None or df.empty or len(df) < 200:
                    logging.warning(f"[{symbol}] Not enough historical data.")
                    continue

                # タイムスタンプをJSTにローカライズ（クラッシュ防止）
                if df.index.tz is None:
                    df.index = df.index.tz_localize('UTC').tz_convert('Asia/Tokyo')
                else:
                    df.index = df.index.tz_convert('Asia/Tokyo')
                
                # 確定バー（最新から1つ前のバー）の時刻を取得
                last_completed_bar_time = df.index[-2].strftime("%Y-%m-%d %H:%M:%S")
                recorded_processed_time = self.state["last_processed_bar_time"].get(symbol)

                # 新しい確定バーが出現した場合のみシグナル判定・インジケータ決済判定
                if recorded_processed_time != last_completed_bar_time:
                    logging.info(f"[{symbol}] New completed bar detected at {last_completed_bar_time}. Running evaluation...")
                    
                    df_feats = self.calculate_features(df)
                    
                    # 確定バーおよび現在のポジション状況に応じた判定
                    self.evaluate_completed_bar(symbol, df_feats, last_completed_bar_time, now_jst)
                    
                    self.state["last_processed_bar_time"][symbol] = last_completed_bar_time
                    self.save_state()

            except Exception as e:
                logging.error(f"[{symbol}] Error in cycle processing: {e}")
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
        entry_time = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)

        # A. 週末強制決済
        if is_weekend_jst(now_jst):
            logging.info(f"[{symbol}] Weekend close triggered JST={now_jst.strftime('%Y-%m-%d %H:%M:%S')}. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, "WEEKEND")
            return

        # B. 時間強制決済 (8時間 = 480分)
        elapsed_seconds = (now_jst - entry_time).total_seconds()
        if elapsed_seconds >= 8 * 3600:
            logging.info(f"[{symbol}] Time close triggered (held 8h). Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, "TIME")
            return

        # C. リアルタイム SL/TP 監視 ＆ BE移動
        close_position = False
        exit_reason = ""

        if direction == "LONG":
            # 最高値の更新
            pos["max_seen_p"] = max(pos.get("max_seen_p", entry_price), current_bid)
            
            # 建値移動 (BE)
            if not be_active and pos["max_seen_p"] >= (entry_price + atr):
                logging.info(f"[{symbol}] Breakeven triggered for LONG. Moving SL from {sl_price:.4f} to {entry_price:.4f}")
                if self.executor.modify_position_sl_tp(ticket, entry_price, tp_price):
                    pos["be_active"] = True
                else:
                    logging.warning(f"[{symbol}] Server-side BE modify failed. Local BE guard remains active and will retry.")
                pos["sl_price"] = entry_price
                self.save_state()
                sl_price = entry_price

            # SL/TP判定
            if current_bid <= sl_price:
                close_position = True
                exit_reason = "SL"
            elif current_bid >= tp_price:
                close_position = True
                exit_reason = "TP"

        else:  # SHORT
            # 最安値の更新
            pos["min_seen_p"] = min(pos.get("min_seen_p", entry_price), current_ask)

            # 建値移動 (BE)
            if not be_active and pos["min_seen_p"] <= (entry_price - atr):
                logging.info(f"[{symbol}] Breakeven triggered for SHORT. Moving SL from {sl_price:.4f} to {entry_price:.4f}")
                if self.executor.modify_position_sl_tp(ticket, entry_price, tp_price):
                    pos["be_active"] = True
                else:
                    logging.warning(f"[{symbol}] Server-side BE modify failed. Local BE guard remains active and will retry.")
                pos["sl_price"] = entry_price
                self.save_state()
                sl_price = entry_price

            # SL/TP判定 (SHORTの決済買戻しは Ask 価格)
            if current_ask >= sl_price:
                close_position = True
                exit_reason = "SL"
            elif current_ask <= tp_price:
                close_position = True
                exit_reason = "TP"

        if close_position:
            logging.info(f"[{symbol}] Realtime exit triggered: {exit_reason}. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, exit_reason)

    def evaluate_completed_bar(self, symbol, df_feats, bar_time_str, now_jst):
        ticket = self.state["active_tickets"].get(symbol)
        pos = self.state["positions"].get(symbol)
        
        # 1つ前の確定バー(インデックス -2)を参照
        row = df_feats.iloc[-2]
        
        # 必要な特徴量が NaN の場合は処理をスキップ（データ不足によるエラー回避）
        required_cols = ["MA_50", "MA_200", "Dev_MA_50", "Dev_ATR", "Vol_Z", "ATR_24"]
        if any(pd.isna(row.get(col, np.nan)) for col in required_cols):
            logging.warning(f"[{symbol}] Some features are NaN at completed bar {bar_time_str}. Skipping evaluation.")
            return
        
        # ────────────── インジケータ決済の判定 ──────────────
        if ticket and pos:
            direction = pos["direction"]
            exit_triggered = False
            
            cfg = PARAMS[symbol]
            exit_type = cfg["exit_type"]
            
            if exit_type == "MA_Cross":
                ma50 = row["MA_50"]
                close_px = row["Close"]
                if direction == "LONG" and close_px >= ma50:
                    exit_triggered = True
                elif direction == "SHORT" and close_px <= ma50:
                    exit_triggered = True
                    
            elif exit_type.startswith("MA_Dev_"):
                dev50 = row["Dev_MA_50"]
                thr = float(exit_type.split("_")[-1])
                if direction == "LONG" and dev50 >= thr:
                    exit_triggered = True
                elif direction == "SHORT" and dev50 <= -thr:
                    exit_triggered = True

            if exit_triggered:
                logging.info(f"[{symbol}] Indicator Exit ({exit_type}) triggered at bar close. Closing ticket {ticket}.")
                self.close_and_cleanup(symbol, ticket, "INDICATOR_TP")
                return # 決済したため、同じサイクルでの新規判定はスキップ

        # ────────────── 新規シグナル判定 ──────────────
        if not ticket:
            # 週末期間中はエントリー不可
            if is_weekend_jst(now_jst):
                return

            # セッションフィルター判定 (確定バーのタイムスタンプが JST 16:00 - 02:59)
            bar_time_dt = df_feats.index[-2]
            if not is_in_session_jst(bar_time_dt):
                return

            # シグナル条件の抽出
            ma50 = row["MA_50"]
            ma200 = row["MA_200"]
            dev_ma50 = row["Dev_MA_50"]
            dev_atr = row["Dev_ATR"]
            vol_z = row["Vol_Z"]
            atr = row["ATR_24"]
            
            body = row["Body"]
            low_shadow = row["Lower_Shadow"]
            up_shadow = row["Upper_Shadow"]
            rng = row["Range"]
            
            downtrend = ma50 < ma200
            uptrend = ma50 > ma200
            
            cfg = PARAMS[symbol]
            dev_threshold = cfg["dev_threshold"]
            vol_z_threshold = cfg["vol_z_threshold"]
            
            # クライマックス条件
            if cfg["dev_type"] == "ATR":
                climax_down = (dev_atr < -dev_threshold) and (vol_z > vol_z_threshold)
                climax_up = (dev_atr > dev_threshold) and (vol_z > vol_z_threshold)
            else: # Percent
                climax_down = (dev_ma50 < -dev_threshold) and (vol_z > vol_z_threshold)
                climax_up = (dev_ma50 > dev_threshold) and (vol_z > vol_z_threshold)
                
            # ピンバー条件
            pinbar_down = (low_shadow > 1.2 * body) and (low_shadow > 0.4 * rng)
            pinbar_up = (up_shadow > 1.2 * body) and (up_shadow > 0.4 * rng)
            
            long_sig = downtrend and climax_down and pinbar_down
            short_sig = uptrend and climax_up and pinbar_up
            
            if long_sig or short_sig:
                direction = "LONG" if long_sig else "SHORT"
                logging.info(f"[{symbol}] Signal detected: {direction} at completed bar {bar_time_str} (Price: {row['Close']})")
                
                # エントリー処理
                self.execute_entry(symbol, direction, row, atr)

    def execute_entry(self, symbol, direction, row, atr):
        info = self.executor.get_symbol_info(symbol)
        if not info:
            logging.error(f"[{symbol}] Failed to get symbol info for entry.")
            return

        current_ask = info.ask
        current_bid = info.bid

        # 仮のエントリー価格（発注用）
        entry_estimate = current_ask if direction == "LONG" else current_bid

        # 損切り幅 sl_d の決定 (確定バーの高低を基準に [0.5 * atr, 2.5 * atr] で制限)
        sig_lo = row["Low"]
        sig_hi = row["High"]
        
        if direction == "LONG":
            sl_d = max(entry_estimate - sig_lo, 0.5 * atr)
            sl_d = min(sl_d, 2.5 * atr)
        else:
            sl_d = max(sig_hi - entry_estimate, 0.5 * atr)
            sl_d = min(sl_d, 2.5 * atr)
        min_stop_d = getattr(info, "stops_level", 0) * getattr(info, "point", 0.0)
        if min_stop_d > 0:
            sl_d = max(sl_d, min_stop_d)

        # ロット計算
        price_unit_value = getattr(info, "price_unit_value", 0.0)
        if price_unit_value <= 0:
            price_unit_value = get_lot_multiplier_usd(symbol)
        sl_usd_per_lot = sl_d * price_unit_value
        
        if sl_usd_per_lot > 0:
            target_lot = FIXED_RISK_USD / sl_usd_per_lot
        else:
            target_lot = info.volume_min

        # ロット制限と丸め（安全のためのハード上限 max_lot_limit を設定）
        target_lot = max(info.volume_min, min(target_lot, info.volume_max, MAX_LOT_LIMIT))
        target_lot = round(target_lot / info.volume_step) * info.volume_step
        target_lot = round(target_lot, 2)

        # 発注
        order_type = ORDER_TYPE_BUY if direction == "LONG" else ORDER_TYPE_SELL
        if direction == "LONG":
            initial_sl_px = entry_estimate - sl_d
            initial_tp_px = entry_estimate + 1.5 * sl_d
        else:
            initial_sl_px = entry_estimate + sl_d
            initial_tp_px = entry_estimate - 1.5 * sl_d
        ticket = self.executor.open_position(
            symbol,
            order_type,
            target_lot,
            sl=initial_sl_px,
            tp=initial_tp_px,
            digits=getattr(info, "digits", 5),
        )

        if ticket:
            actual_entry_price = float(ticket.price)
            
            # 発注成立時の実際のエントリー価格を基準に SL/TP を最終決定
            if direction == "LONG":
                sl_d = max(actual_entry_price - sig_lo, 0.5 * atr)
                sl_d = min(sl_d, 2.5 * atr)
                sl_px = actual_entry_price - sl_d
                tp_px = actual_entry_price + 1.5 * sl_d
            else:
                sl_d = max(sig_hi - actual_entry_price, 0.5 * atr)
                sl_d = min(sl_d, 2.5 * atr)
                sl_px = actual_entry_price + sl_d
                tp_px = actual_entry_price - 1.5 * sl_d
            self.executor.modify_position_sl_tp(ticket, sl_px, tp_px)

            # 状態更新
            now_jst = datetime.now(JST)
            now_jst_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
            
            self.state["active_tickets"][symbol] = ticket
            self.state["positions"][symbol] = {
                "ticket": ticket,
                "direction": direction,
                "entry_time": now_jst_str,
                "entry_price": actual_entry_price,
                "sl_price": float(sl_px),
                "tp_price": float(tp_px),
                "atr": float(atr),
                "risk_price_unit_value": float(price_unit_value),
                "risk_usd_per_lot": float(sl_usd_per_lot),
                "be_active": False,
                "lot_size": float(target_lot),
                "max_seen_p": actual_entry_price,
                "min_seen_p": actual_entry_price
            }
            self.save_state()
            
            logging.info(f"[{symbol}] Position opened successfully. Ticket: {ticket}, Lot: {target_lot}, Entry: {actual_entry_price:.4f}, SL: {sl_px:.4f}, TP: {tp_px:.4f}")
            self.log_trade_csv("ENTRY", ticket, symbol, direction, target_lot, actual_entry_price)
        else:
            logging.error(f"[{symbol}] Failed to open position.")

    def close_and_cleanup(self, symbol, ticket, reason):
        pos = self.state["positions"].get(symbol)
        lot = pos["lot_size"] if pos else 0.0
        direction = pos["direction"] if pos else ""
        
        success = self.executor.close_position(ticket)
        if success:
            if getattr(success, "already_closed", False):
                logging.warning(
                    f"[{symbol}] Ticket {ticket} was already absent on MT5. "
                    "Cleaning local state after dedicated absence confirmation; "
                    "exact close price and PnL are unavailable."
                )
                self.log_trade_csv(
                    f"EXIT_{reason}_UNKNOWN",
                    ticket,
                    symbol,
                    direction,
                    lot,
                    None,
                    None,
                    f"{reason}:MT5_ABSENT_CONFIRMED",
                )
            else:
                logging.info(f"[{symbol}] Successfully closed position for {symbol} (Reason: {reason}). Ticket: {ticket}, PnL: {success.profit}")
                self.log_trade_csv(f"EXIT_{reason}", ticket, symbol, direction, lot, success.close_price, success.profit, reason)
        else:
            logging.warning(f"[{symbol}] Failed to close ticket {ticket} via EA. Keeping state so the bot can retry.")
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
    bot = s10TradingBot()
    bot.start()
