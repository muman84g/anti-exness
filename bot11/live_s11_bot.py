# ==============================================================================
# STRATEGY s11 CONCEPT: USTECm -> US500m Lead-Lag Divergence Strategy Live Trading Bot (v1)
# 【戦略s11コンセプト: USTECm -> US500m 先行遅行ダイバージェンス追従戦略実運用ボット】
# ------------------------------------------------------------------------------
# - Leading Asset: USTECm (Nasdaq)
# - Lagging Asset: US500m (S&P 500) - ※取引対象は遅行銘柄のみ
# - Timeframe: M5 (5分足)
# - Trigger: Z-score の乖離（スプレッド = Lead_Z - Lag_Z）が閾値を突破した際、
#            遅行銘柄が先行銘柄を追従（スプレッド収束）する方向に逆張りエントリー。
# - Weekend Guard: 土曜02:00 JST以降は新規entry禁止、土曜02:30 JST以降は強制クローズ
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

JST = timezone(timedelta(hours=9), "JST")

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
    'strategy_profile': 'mr_only_weekend_w288_corr63_z1.6',
    'window_z': 288,               # Z-score平滑窓幅 (24時間)
    'z_entry': 1.6,                # focused grid first candidate
    'z_exit': 0.0,                 # 決済収束閾値
    'exit_type': 'MEAN_REVERSION', # 決済ロジック ("MEAN_REVERSION", "TIME", "FIXED_RR", "LEAD_STALL")
    'mean_reversion_mode': 'zero_cross',
    'corr_window': 63,
    'history_bars': 350,
    'max_hold_bars': 24,           # 最大保有バー数 (2時間)
    'sl_mult': 1.0,                # 損切りATR乗数 (1.0 ATR)
    'tp_mult': 1.5,                # 利確ATR乗数
    'tp_atr_mult': 1.5,
    'use_protective_exits': False,
    'use_time_exit': False,
    'use_sl': False,
    'use_tp': False,
    'use_be': False,               # 建値移動 (BE) の有無
    'use_entry_pct_limit_close': True,
    'entry_pct_limit_close_pct': 0.01,
    'multiplier': 50.0,            # US500m コントラクトサイズ
    'use_symbol_trade_value': True, # Prefer broker tick value/tick size for risk sizing
    'max_lot_limit': 2.0,           # Hard cap for live lot sizing
    'spread_pct': 0.00005,         # US500m 標準スプレッド (0.005%)
    'sync_on_start_without_entry': True,  # On startup/restart, sync the latest bar without opening a catch-up trade
    'max_completed_bar_age_minutes': 15,  # Reject signal bars older than this many minutes
    'max_completed_bar_future_minutes': 2, # Reject signal bars too far ahead of local JST clock
    'weekend_entry_block_weekday': 5,      # Saturday, Python weekday convention
    'weekend_entry_block_hour': 2,
    'weekend_entry_block_minute': 0,
    'weekend_force_close_hour': 2,
    'weekend_force_close_minute': 30,
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
def get_lot_multiplier_usd(symbol, price, usdjpy_rate=150.0):
    if symbol == 'JP225m':
        return 1.0 / usdjpy_rate
    if symbol in ['USTECm', 'US500m', 'US30m']:
        return 1.0
    if symbol == 'USOILm':
        return 100.0
    return 100000.0


def is_at_or_after_time(dt_jst, hour, minute):
    return dt_jst.hour > hour or (dt_jst.hour == hour and dt_jst.minute >= minute)


def is_weekend_entry_block_jst(dt_jst):
    # 土曜02:00 JST以降、または日曜は新規entry禁止
    block_weekday = int(PARAMS.get("weekend_entry_block_weekday", 5))
    block_hour = int(PARAMS.get("weekend_entry_block_hour", 2))
    block_minute = int(PARAMS.get("weekend_entry_block_minute", 0))
    if dt_jst.weekday() == block_weekday and is_at_or_after_time(dt_jst, block_hour, block_minute):
        return True
    if dt_jst.weekday() == 6:
        return True
    return False


def is_weekend_force_close_jst(dt_jst):
    # 土曜02:30 JST以降、または日曜は保有positionを強制close
    block_weekday = int(PARAMS.get("weekend_entry_block_weekday", 5))
    close_hour = int(PARAMS.get("weekend_force_close_hour", 2))
    close_minute = int(PARAMS.get("weekend_force_close_minute", 30))
    if dt_jst.weekday() == block_weekday and is_at_or_after_time(dt_jst, close_hour, close_minute):
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
        self.log_effective_params()

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
        self.state.setdefault("last_processed_bar_time", None)
        self.state.setdefault("last_close_fail_signature", {})

    def init_empty_state(self):
        self.state = {
            "active_tickets": {},
            "positions": {},
            "last_processed_bar_time": None,
            "last_close_fail_signature": {},
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

    def log_effective_params(self):
        keys = [
            "lead_symbol", "lag_symbol", "strategy_profile", "window_z", "z_entry", "z_exit",
            "exit_type", "mean_reversion_mode", "corr_window", "history_bars",
            "max_hold_bars", "sl_mult", "tp_mult", "tp_atr_mult",
            "use_protective_exits", "use_time_exit", "use_sl", "use_tp", "use_be",
            "use_entry_pct_limit_close", "entry_pct_limit_close_pct",
            "spread_pct", "max_lot_limit", "sync_on_start_without_entry",
            "max_completed_bar_age_minutes", "max_completed_bar_future_minutes",
            "weekend_entry_block_hour", "weekend_entry_block_minute",
            "weekend_force_close_hour", "weekend_force_close_minute",
        ]
        compact = {key: PARAMS.get(key) for key in keys}
        logging.info(f"Effective s11 params: {compact}")

    def mark_bar_processed(self, bar_time_str):
        self.state["last_processed_bar_time"] = bar_time_str
        self.save_state()

    def normalize_bar_timestamp(self, ts):
        bar_ts = pd.Timestamp(ts)
        if bar_ts.tzinfo is None:
            bar_ts = bar_ts.tz_localize("Asia/Tokyo")
        else:
            bar_ts = bar_ts.tz_convert("Asia/Tokyo")
        return bar_ts

    def completed_bar_age_minutes(self, bar_ts, now_jst):
        return (pd.Timestamp(now_jst) - bar_ts).total_seconds() / 60.0

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
        csv_file = os.path.join(LOG_DIR, "s11_trade_errors.csv" if action.startswith("EXIT_FAIL_") else "s11_trades.csv")
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

    def calculate_entry_pct_limit_close_price(self, direction, entry_price, digits=None):
        if not PARAMS.get("use_entry_pct_limit_close", False):
            return 0.0
        pct = float(PARAMS.get("entry_pct_limit_close_pct", 0.0) or 0.0)
        if pct <= 0 or entry_price <= 0:
            return 0.0
        price = entry_price * (1.0 + pct) if direction == "LONG" else entry_price * (1.0 - pct)
        if digits is not None:
            return round(price, int(digits))
        return price

    def ensure_entry_pct_limit_close(self, symbol, ticket, pos, info):
        target_tp = self.calculate_entry_pct_limit_close_price(
            pos.get("direction", ""),
            float(pos.get("entry_price", 0.0) or 0.0),
            getattr(info, "digits", 5),
        )
        if target_tp <= 0:
            return False

        point = float(getattr(info, "point", 0.0) or 0.0)
        current_tp = float(pos.get("entry_pct_limit_close_price") or pos.get("tp_price") or 0.0)
        if current_tp and abs(current_tp - target_tp) <= max(point, 1e-8):
            return True

        sl_price = float(pos.get("sl_price", 0.0) or 0.0)
        if self.executor.modify_position_sl_tp(ticket, sl_price, target_tp):
            pos["tp_price"] = float(target_tp)
            pos["entry_pct_limit_close_price"] = float(target_tp)
            pos["entry_pct_limit_close_pct"] = float(PARAMS.get("entry_pct_limit_close_pct", 0.0) or 0.0)
            pos["entry_pct_limit_close_applied"] = True
            self.save_state()
            logging.info(f"[{symbol}] Entry-percent limit close TP set to {target_tp}.")
            return True

        logging.warning(f"[{symbol}] Failed to set entry-percent limit close TP for ticket {ticket}.")
        return False

    def record_close_failure(self, symbol, ticket, reason, direction, lot_size):
        signature = f"{ticket}:{reason}"
        signatures = self.state.setdefault("last_close_fail_signature", {})
        if signatures.get(symbol) != signature:
            self.log_trade_csv(
                f"EXIT_FAIL_{reason}",
                ticket,
                symbol,
                direction,
                lot_size,
                None,
                None,
                reason,
            )
            signatures[symbol] = signature
        pos = self.state.get("positions", {}).get(symbol)
        if pos is not None:
            pos["last_close_fail_reason"] = reason
            pos["last_close_fail_time"] = datetime.now(JST).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        self.save_state()

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
        now_jst = datetime.now(JST)
        lag_sym = PARAMS['lag_symbol']
        lead_sym = PARAMS['lead_symbol']
        
        # 1. リアルタイムポジション管理（週末決済、時間決済、SL/TP決済、BE移動）
        if lag_sym in self.state["active_tickets"]:
            self.manage_existing_position(lag_sym, now_jst)

        # 2. 先行・遅行の最新ヒストリカルデータを取得し、同期させてシグナル判定
        try:
            # 5分足(timeframe=5)で過去350本取得（Z-score平滑288本窓をカバー）
            corr_window = int(PARAMS.get("corr_window", 63))
            min_history_bars = int(PARAMS["window_z"]) + max(corr_window, 1) - 1
            history_bars = int(PARAMS.get("history_bars", max(350, min_history_bars)))
            history_bars = max(history_bars, min_history_bars)
            df_lead_raw = self.dm.get_historical_data(lead_sym, 5, history_bars)
            df_lag_raw = self.dm.get_historical_data(lag_sym, 5, history_bars)
            
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
            common_idx = df_lead.index.intersection(df_lag.index).sort_values()
            if len(common_idx) < 5:
                logging.warning("No overlapping timestamps found.")
                return
                
            df_lead_sync = df_lead.loc[common_idx]
            df_lag_sync = df_lag.loc[common_idx]
            
            # 最新の確定バーの時刻
            last_completed_bar_ts = self.normalize_bar_timestamp(common_idx[-2])
            last_completed_bar_time = last_completed_bar_ts.strftime("%Y-%m-%d %H:%M:%S")
            bar_age_minutes = self.completed_bar_age_minutes(last_completed_bar_ts, now_jst)
            recorded_processed_time = self.state.get("last_processed_bar_time")

            # 新しい確定バーが出現した場合のみシグナル評価
            if recorded_processed_time != last_completed_bar_time:
                max_age = float(PARAMS.get("max_completed_bar_age_minutes", 15))
                max_future = float(PARAMS.get("max_completed_bar_future_minutes", 2))
                logging.info(
                    f"New completed bar detected at {last_completed_bar_time} "
                    f"(age={bar_age_minutes:.2f}m)."
                )

                if recorded_processed_time is None and PARAMS.get("sync_on_start_without_entry", True):
                    logging.info(
                        "Startup sync: marking latest completed bar as processed without signal evaluation "
                        f"(bar={last_completed_bar_time}, age={bar_age_minutes:.2f}m)."
                    )
                    self.mark_bar_processed(last_completed_bar_time)
                    return

                if bar_age_minutes > max_age:
                    logging.warning(
                        f"Skipping stale completed bar {last_completed_bar_time}: "
                        f"age={bar_age_minutes:.2f}m exceeds max_completed_bar_age_minutes={max_age}."
                    )
                    self.mark_bar_processed(last_completed_bar_time)
                    return

                if bar_age_minutes < -max_future:
                    logging.warning(
                        f"Skipping future-dated completed bar {last_completed_bar_time}: "
                        f"age={bar_age_minutes:.2f}m is earlier than allowed future skew {-max_future:.2f}m."
                    )
                    self.mark_bar_processed(last_completed_bar_time)
                    return
                
                # 相関係数の符号確認
                corr_df = pd.DataFrame({
                    "lead_z": df_lead_sync["Z"],
                    "lag_z": df_lag_sync["Z"],
                }).dropna().tail(corr_window)
                if len(corr_df) < 2:
                    logging.warning(
                        f"Not enough valid Z rows for corr_window={corr_window}; using corr_sign=1."
                    )
                    c_val = np.nan
                else:
                    c_val = corr_df["lead_z"].corr(corr_df["lag_z"])
                corr_sign = np.sign(c_val) if not pd.isna(c_val) else 1.0
                if corr_sign == 0: corr_sign = 1.0
                logging.info(
                    f"Correlation sign calculated with valid_z_rows={len(corr_df)} "
                    f"corr_window={corr_window} corr={c_val if not pd.isna(c_val) else 'nan'} "
                    f"sign={corr_sign:.0f}"
                )
                
                self.evaluate_completed_bar(
                    df_lead_sync,
                    df_lag_sync,
                    corr_sign,
                    last_completed_bar_time,
                    now_jst,
                    bar_age_minutes,
                )
                
                self.mark_bar_processed(last_completed_bar_time)

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
        entry_time = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)

        entry_pct_limit_tp = 0.0
        if PARAMS.get("use_entry_pct_limit_close", False):
            if not self.ensure_entry_pct_limit_close(symbol, ticket, pos, info):
                if self.executor.confirm_position_absent(ticket) is True:
                    self.close_and_cleanup(symbol, ticket, "ENTRY_PCT_LIMIT_ABSENT")
                    return
            entry_pct_limit_tp = float(pos.get("entry_pct_limit_close_price") or pos.get("tp_price") or 0.0)
            sl_price = pos["sl_price"]
            tp_price = pos["tp_price"]

        # A. 週末強制決済
        if is_weekend_force_close_jst(now_jst):
            logging.info(f"[{symbol}] Weekend close triggered JST={now_jst.strftime('%Y-%m-%d %H:%M:%S')}. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, "WEEKEND")
            return

        # B. 時間強制決済 (max_hold_bars 分相当)
        elapsed_seconds = (now_jst - entry_time).total_seconds()
        # M5足で max_hold_bars 分経過しているか
        if (PARAMS.get('use_time_exit', True) or PARAMS.get('exit_type') == "TIME") and elapsed_seconds >= PARAMS['max_hold_bars'] * 5 * 60:
            logging.info(f"[{symbol}] Time close triggered. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, "TIME")
            return

        if entry_pct_limit_tp:
            if direction == "LONG" and current_bid >= entry_pct_limit_tp:
                logging.info(f"[{symbol}] Entry-percent limit close reached for LONG. Closing ticket {ticket}.")
                self.close_and_cleanup(symbol, ticket, "ENTRY_PCT_LIMIT")
                return
            if direction == "SHORT" and current_ask <= entry_pct_limit_tp:
                logging.info(f"[{symbol}] Entry-percent limit close reached for SHORT. Closing ticket {ticket}.")
                self.close_and_cleanup(symbol, ticket, "ENTRY_PCT_LIMIT")
                return

        # C. リアルタイム SL/TP 監視 & 建値移動 (FIXED_RR)
        use_protective = PARAMS.get('use_protective_exits', True) or PARAMS.get('exit_type') == "FIXED_RR"
        if not use_protective:
            return
        use_sl = PARAMS.get('use_sl', True)
        use_tp = PARAMS.get('use_tp', True)
        use_be = PARAMS.get('use_be', True) and use_sl

        close_position = False
        exit_reason = ""

        if direction == "LONG":
            pos["max_seen_p"] = max(pos.get("max_seen_p", entry_price), current_bid)
            
            # 建値移動 (BE)
            if use_be and not be_active and pos["max_seen_p"] >= (entry_price + atr):
                logging.info(f"[{symbol}] Breakeven triggered for LONG. Moving SL from {sl_price:.4f} to {entry_price:.4f}")
                if self.executor.modify_position_sl_tp(ticket, entry_price, tp_price):
                    pos["be_active"] = True
                else:
                    logging.warning(f"[{symbol}] Server-side BE modify failed. Local BE guard remains active and will retry.")
                pos["sl_price"] = entry_price
                self.save_state()
                sl_price = entry_price

            if use_sl and sl_price and current_bid <= sl_price:
                close_position = True
                exit_reason = "SL"
            elif use_tp and tp_price and current_bid >= tp_price:
                close_position = True
                exit_reason = "TP"

        else:  # SHORT
            pos["min_seen_p"] = min(pos.get("min_seen_p", entry_price), current_ask)

            # 建値移動 (BE)
            if use_be and not be_active and pos["min_seen_p"] <= (entry_price - atr):
                logging.info(f"[{symbol}] Breakeven triggered for SHORT. Moving SL from {sl_price:.4f} to {entry_price:.4f}")
                new_sl = entry_price * (1.0 + PARAMS['spread_pct'])
                if self.executor.modify_position_sl_tp(ticket, new_sl, tp_price):
                    pos["be_active"] = True
                else:
                    logging.warning(f"[{symbol}] Server-side BE modify failed. Local BE guard remains active and will retry.")
                pos["sl_price"] = new_sl
                self.save_state()
                sl_price = pos["sl_price"]

            if use_sl and sl_price and current_ask >= sl_price:
                close_position = True
                exit_reason = "SL"
            elif use_tp and tp_price and current_ask <= tp_price:
                close_position = True
                exit_reason = "TP"

        if close_position:
            logging.info(f"[{symbol}] Realtime exit triggered: {exit_reason}. Closing ticket {ticket}.")
            self.close_and_cleanup(symbol, ticket, exit_reason)

    def evaluate_completed_bar(self, df_lead, df_lag, corr_sign, bar_time_str, now_jst, bar_age_minutes):
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

        logging.info(
            f"[{lag_sym}] Signal evaluation bar={bar_time_str} age={bar_age_minutes:.2f}m "
            f"lead_z={z_lead_val:.4f} lag_z={z_lag_val:.4f} spread_z={spread:.4f} "
            f"corr_sign={corr_sign:.0f} z_entry={PARAMS['z_entry']} z_exit={PARAMS['z_exit']} "
            f"exit_type={PARAMS['exit_type']} mean_reversion_mode={PARAMS.get('mean_reversion_mode')} "
            f"window_z={PARAMS['window_z']} corr_window={PARAMS.get('corr_window')}"
        )

        # ────────────── インジケータ決済の判定 ──────────────
        if ticket and pos:
            direction = pos["direction"]
            exit_triggered = False
            exit_reason = ""
            
            if PARAMS['exit_type'] == "MEAN_REVERSION":
                if PARAMS.get("mean_reversion_mode", "zero_cross") == "bot_abs":
                    exit_triggered = abs(spread) <= PARAMS['z_exit']
                else:
                    adj_spread = corr_sign * spread
                    if direction == "LONG":
                        exit_triggered = adj_spread <= PARAMS['z_exit']
                    else:
                        exit_triggered = adj_spread >= -PARAMS['z_exit']
                if exit_triggered:
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
            if is_weekend_entry_block_jst(now_jst):
                logging.info(f"[{lag_sym}] Weekend entry block JST={now_jst.strftime('%Y-%m-%d %H:%M:%S')}. Skipping new entry.")
                return

            # シグナル判定
            sig_dir = ""
            if corr_sign * spread >= PARAMS['z_entry']:
                sig_dir = "LONG"
            elif corr_sign * spread <= -PARAMS['z_entry']:
                sig_dir = "SHORT"
                
            if sig_dir:
                logging.info(
                    f"[{lag_sym}] Divergence Signal detected: {sig_dir} at completed bar {bar_time_str} "
                    f"(Spread Z: {spread:.4f}, age={bar_age_minutes:.2f}m, threshold={PARAMS['z_entry']})"
                )
                signal_context = {
                    "signal_bar_time": bar_time_str,
                    "signal_bar_age_minutes": float(bar_age_minutes),
                    "signal_spread_z": float(spread),
                    "signal_lead_z": float(z_lead_val),
                    "signal_lag_z": float(z_lag_val),
                    "signal_corr_sign": float(corr_sign),
                    "signal_window_z": int(PARAMS["window_z"]),
                    "signal_corr_window": int(PARAMS.get("corr_window", 63)),
                    "signal_z_entry": float(PARAMS["z_entry"]),
                    "signal_exit_type": PARAMS["exit_type"],
                    "signal_mean_reversion_mode": PARAMS.get("mean_reversion_mode", "zero_cross"),
                }
                self.execute_entry(lag_sym, sig_dir, row_lag, atr, signal_context)

    def execute_entry(self, symbol, direction, row_lag, atr, signal_context=None):
        signal_context = signal_context or {}
        info = self.executor.get_symbol_info(symbol)
        if not info:
            logging.error(f"[{symbol}] Failed to get symbol info for entry.")
            return

        current_ask = info.ask
        current_bid = info.bid
        expected_entry_price = current_ask if direction == "LONG" else current_bid
        digits = getattr(info, "digits", 5)

        # ロット計算用損切り幅 sl_d (標準 1.0 ATR)
        sl_d = max(PARAMS['sl_mult'] * atr, 0.0001)
        min_stop_d = getattr(info, "stops_level", 0) * getattr(info, "point", 0.0)
        if min_stop_d > 0:
            sl_d = max(sl_d, min_stop_d)

        # ロット計算
        price_unit_value = getattr(info, "price_unit_value", 0.0)
        if not PARAMS.get('use_symbol_trade_value', True) or price_unit_value <= 0:
            price_unit_value = get_lot_multiplier_usd(symbol, expected_entry_price)

        sl_usd_per_lot = sl_d * price_unit_value
        if sl_usd_per_lot > 0:
            target_lot = FIXED_RISK_USD / sl_usd_per_lot
        else:
            target_lot = info.volume_min

        max_lot_limit = PARAMS.get('max_lot_limit', 2.0)  # 安全上限
        target_lot = max(info.volume_min, min(target_lot, info.volume_max, max_lot_limit))
        target_lot = round(target_lot / info.volume_step) * info.volume_step
        target_lot = round(target_lot, 2)

        order_type = ORDER_TYPE_BUY if direction == "LONG" else ORDER_TYPE_SELL

        use_sl = PARAMS.get('use_sl', True)
        use_tp = PARAMS.get('use_tp', True)
        tp_atr_mult = float(PARAMS.get('tp_atr_mult', PARAMS.get('tp_mult', 1.5)))
        tp_d = max(tp_atr_mult * atr, 0.0001)
        if direction == "LONG":
            raw_sl_px = expected_entry_price - sl_d
            raw_tp_px = expected_entry_price + tp_d
        else:
            raw_sl_px = (expected_entry_price + sl_d) * (1.0 + PARAMS['spread_pct'])
            raw_tp_px = expected_entry_price - tp_d
        sl_px = raw_sl_px if use_sl else 0.0
        tp_px = raw_tp_px if use_tp else 0.0
        entry_pct_limit_tp_px = self.calculate_entry_pct_limit_close_price(direction, expected_entry_price, digits)
        if entry_pct_limit_tp_px:
            tp_px = entry_pct_limit_tp_px

        ticket = self.executor.open_position(
            symbol,
            order_type,
            target_lot,
            sl=sl_px,
            tp=tp_px,
            digits=digits,
        )

        if ticket:
            actual_entry_price = float(ticket.price)
            if actual_entry_price <= 0:
                actual_entry_price = expected_entry_price

            entry_pct_limit_tp_px = self.calculate_entry_pct_limit_close_price(direction, actual_entry_price, digits)
            if entry_pct_limit_tp_px:
                point = float(getattr(info, "point", 0.0) or 0.0)
                if not tp_px or abs(entry_pct_limit_tp_px - tp_px) > max(point, 1e-8):
                    if self.executor.modify_position_sl_tp(ticket, sl_px, entry_pct_limit_tp_px):
                        tp_px = entry_pct_limit_tp_px
                    else:
                        logging.warning(
                            f"[{symbol}] Initial entry-percent limit close TP modify failed. "
                            f"Keeping existing TP={tp_px}."
                        )
                else:
                    tp_px = entry_pct_limit_tp_px
                
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
                "min_seen_p": actual_entry_price,
                "signal_bar_time": signal_context.get("signal_bar_time"),
                "signal_bar_age_minutes": signal_context.get("signal_bar_age_minutes"),
                "signal_spread_z": signal_context.get("signal_spread_z"),
                "signal_lead_z": signal_context.get("signal_lead_z"),
                "signal_lag_z": signal_context.get("signal_lag_z"),
                "signal_corr_sign": signal_context.get("signal_corr_sign"),
                "signal_window_z": signal_context.get("signal_window_z"),
                "signal_corr_window": signal_context.get("signal_corr_window"),
                "signal_z_entry": signal_context.get("signal_z_entry"),
                "signal_exit_type": signal_context.get("signal_exit_type"),
                "signal_mean_reversion_mode": signal_context.get("signal_mean_reversion_mode"),
                "use_sl": bool(use_sl),
                "use_tp": bool(use_tp),
                "use_time_exit": bool(PARAMS.get('use_time_exit', True)),
                "use_protective_exits": bool(PARAMS.get('use_protective_exits', True)),
                "raw_sl_price": float(raw_sl_px),
                "raw_tp_price": float(raw_tp_px),
                "entry_pct_limit_close_price": float(entry_pct_limit_tp_px) if entry_pct_limit_tp_px else 0.0,
                "entry_pct_limit_close_pct": float(PARAMS.get("entry_pct_limit_close_pct", 0.0) or 0.0),
                "entry_pct_limit_close_applied": bool(entry_pct_limit_tp_px and tp_px),
                "lot_price_unit_value_source": "broker" if getattr(info, "price_unit_value", 0.0) > 0 and PARAMS.get('use_symbol_trade_value', True) else "bot9_fallback",
            }
            self.save_state()
            
            logging.info(
                f"[{symbol}] Position opened successfully. Ticket: {ticket}, Lot: {target_lot}, "
                f"Entry: {actual_entry_price:.4f}, SL: {self.state['positions'][symbol]['sl_price']:.4f}, "
                f"TP: {self.state['positions'][symbol]['tp_price']:.4f}, SignalBar: {signal_context.get('signal_bar_time')}, "
                f"SignalSpreadZ: {signal_context.get('signal_spread_z')}"
            )
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
                logging.info(f"[{symbol}] Successfully closed position (Reason: {reason}). Ticket: {ticket}, PnL: {success.profit}")
                self.log_trade_csv(f"EXIT_{reason}", ticket, symbol, direction, lot, success.close_price, success.profit, reason)
        else:
            logging.warning(f"[{symbol}] Failed to close ticket {ticket} via EA. Keeping state so the bot can retry.")
            self.record_close_failure(symbol, ticket, reason, direction, lot)
            return
            
        if symbol in self.state["active_tickets"]:
            del self.state["active_tickets"][symbol]
        if symbol in self.state["positions"]:
            del self.state["positions"][symbol]
        self.state.setdefault("last_close_fail_signature", {}).pop(symbol, None)
        self.save_state()

if __name__ == "__main__":
    bot = s11TradingBot()
    bot.start()
