# ==============================================================================
# STRATEGY s12 CONCEPT: A-Rank Multi-Anomaly Pinpoint Live Trading Bot (v1.3)
# 【戦略s12コンセプト: Aランクアノマリー実運用並行稼働ボット - 堅牢性検証済】
# ------------------------------------------------------------------------------
# ■ 堅牢性検証サマリー (2021-2026年 Train/Val/Holdout 分割 + 動的スプレッド適用)
#
# 1. NZDCHFm (SHORT) - 【アクティブ稼働推奨 (Lot: 0.05)】
#    - エントリー: 金曜 JST 02:55
#    - ホールド: 84本 (7時間, JST 09:55 決済)
#    - 利確/損切: TP 0.0 ATR / SL 0.0 ATR (純時間決済)
#    - 全期間期待損益: +5.50% | Train: +2.79% | Val: +0.73% | Holdout: +1.98%
#    - 勝率: 47.86% (勝ち134回/負け144回) | PF: 1.27
#    - 平均トレード損益: +0.0196% (決済スプレッド2.0 pips差引後で約+1.2 pipsの期待値)
#    - 最大逆行損失 (Max Loss): -0.61%
#    - 根拠: JST 09:55決済はロールオーバー窓（夏: 05:50-07:15 / 冬: 06:50-08:15）を
#            完全に抜けた東京・シドニー市場高流動性時間帯のため、スプレッド拡大や
#            スリッページ損失リスクが極めて低いです。
#
# 2. GBPNZDm (SHORT) - 【停止推奨 (Lot: 0.0)】
#    - エントリー: 水曜 JST 04:25
#    - ホールド: 96本 (8時間, JST 12:25 決済)
#    - 全期間期待損益: -2.21% (Holdoutは+0.04%と微益だが長期的エッジが消失)
#    - 根拠: ロールオーバー時のスプレッド拡大（実態35pips以上）をバックテストに
#            厳格反映した結果、長期的なエッジが維持できないことが判明。
#
# 3. USDJPYm (LONG) - 【停止推奨 (Lot: 0.0)】
#    - エントリー: 木曜 JST 03:05
#    - ホールド: 24本 (2時間, JST 05:05 決済)
#    - 全期間期待損益: -2.22% (Train期間の損失が大きくトータルマイナス)
#    - 根拠: ロバスト検証および動的スプレッド負荷試験をクリアできず。
#
# ※ スプレッド保護: 各ペア決済時、スプレッドが max_spread_pips (5.0 pips) を
#    超えている場合は自動的にクローズを次サイクルへ延期する安全弁ロジックを搭載。
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

# ============================================================
# テクニカル指標計算ヘルパー
# ============================================================
def calculate_rsi(prices, period=14):
    deltas = np.diff(prices)
    if len(deltas) < period:
        return np.ones_like(prices) * 50.0
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0: rs = 999.0
    else: rs = up / down
    rsi = np.zeros_like(prices)
    rsi[:period] = 100. - 100. / (1. + rs)
    for i in range(period, len(prices)):
        delta = deltas[i-1]
        upval = delta if delta > 0 else 0.
        downval = -delta if delta < 0 else 0.
        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        if down == 0: rs = 999.0
        else: rs = up / down
        rsi[i] = 100. - 100. / (1. + rs)
    return rsi

# ============================================================
# 米国夏時間 (DST) およびスプレッド危険時間帯判定ヘルパー
# ============================================================
def is_dst_us(dt):
    """
    JST時間 dt が米国夏時間 (DST) の期間内かどうかを判定する。
    米国DST：3月第2日曜日 16:00 JST 〜 11月第1日曜日 15:00 JST
    """
    year = dt.year
    first_march = datetime(year, 3, 1, tzinfo=JST)
    first_sunday_offset = (6 - first_march.weekday()) % 7
    second_sunday = datetime(year, 3, 1 + first_sunday_offset + 7, 16, 0, tzinfo=JST)
    
    first_nov = datetime(year, 11, 1, tzinfo=JST)
    first_sunday_offset_nov = (6 - first_nov.weekday()) % 7
    first_sunday_nov = datetime(year, 11, 1 + first_sunday_offset_nov, 15, 0, tzinfo=JST)
    
    return second_sunday <= dt < first_sunday_nov

def get_sltp_window_status(dt_jst):
    """
    JST時間 dt_jst に基づいて、SL/TPの退避・復旧・通常のウィンドウ状態を判定する。
    戻り値: 'evacuate' (退避), 'restore' (復旧), 'normal' (通常)
    夏時間 (DST) のロールオーバーは JST 06:00 のため、07:15前後にスプレッドが落ち着いたら復旧・決済を行う。
    冬時間 (通常) のロールオーバーは JST 07:00 のため、08:15前後にスプレッドが落ち着いたら復旧・決済を行う。
    """
    is_dst = is_dst_us(dt_jst)
    time_float = dt_jst.hour + dt_jst.minute / 60.0 + dt_jst.second / 3600.0
    
    if is_dst:
        # 夏時間 (DST)
        # 退避: JST 05:50 〜 07:15
        if 5.833 <= time_float < 7.25:
            return 'evacuate'
        # 復旧: JST 07:15 〜 07:45
        elif 7.25 <= time_float < 7.75:
            return 'restore'
    else:
        # 冬時間 (通常)
        # 退避: JST 06:50 〜 08:15
        if 6.833 <= time_float < 8.25:
            return 'evacuate'
        # 復旧: JST 08:15 〜 08:45
        elif 8.25 <= time_float < 8.75:
            return 'restore'
            
    return 'normal'


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
            "direction": "LONG",
            "hold_bars": 48,
            "tp_mult": 3.0,
            "sl_mult": 0.0,
            "lot_size": 0.0,
            "filter_type": "None",
            "filter_param": 0.0
        },
        "GBPNZDm": {
            "day": "Wednesday",
            "time": "02:50",
            "direction": "SHORT",
            "hold_bars": 48,
            "tp_mult": 0.0,
            "sl_mult": 0.0,
            "lot_size": 0.05,
            "filter_type": "None",
            "filter_param": 0.0
        },
        "NZDCHFm": {
            "day": "Friday",
            "time": "02:55",
            "direction": "SHORT",
            "hold_bars": 48,
            "tp_mult": 0.0,
            "sl_mult": 0.0,
            "lot_size": 0.05,
            "filter_type": "None",
            "filter_param": 0.0
        }
    },
    "general": {
        "poll_interval_seconds": 15,
        "max_spread_pips": {
            "USDJPYm": 3.0,
            "GBPNZDm": 15.0,
            "NZDCHFm": 5.0
        }
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
            
            # 各通貨ペアの個別フィルタキー補完
            for sym, strat in params["strategies"].items():
                strat.setdefault("filter_type", "None")
                strat.setdefault("filter_param", 0.0)
                
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
        csv_file = os.path.join(LOG_DIR, "s12_trade_errors.csv" if action.startswith("EXIT_FAIL_") else "s12_trades.csv")
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
                        # ロットサイズが0.0以下の場合はエントリーをスキップし、日付ロックのみかける
                        if s_conf.get("lot_size", 0.0) <= 0.0:
                            logging.info(f"[{sym}] Anomaly Time Triggered but lot size is <= 0.0 ({s_conf.get('lot_size')}). Skipping entry.")
                            if "last_entry_date" not in self.state:
                                self.state["last_entry_date"] = {}
                            self.state["last_entry_date"][sym] = today_str
                            self.save_state()
                            continue
                            
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

        # ── 【動的フィルター適用】 ───────────────────
        filter_type = s_conf.get("filter_type", "None")
        filter_param = s_conf.get("filter_param", 0.0)
        direction = s_conf["direction"]
        
        if filter_type != "None":
            logging.info(f"[{symbol}] Applying dynamic entry filter: {filter_type} (param={filter_param})")
            
            if filter_type == "Trend":
                current_close = float(df["Close"].iloc[-2])
                prev_close = float(df["Close"].iloc[-290]) # 288本前 (直近24時間)
                trend_val = current_close - prev_close
                logging.info(f"[{symbol}] Trend Filter Check: trend={trend_val:+.5f}, threshold={filter_param:+.5f}")
                if direction == "SHORT" and trend_val <= filter_param:
                    logging.info(f"[{symbol}] Trend Filter triggered: Skipping SHORT entry (trend={trend_val:+.5f} <= threshold={filter_param:+.5f}).")
                    return False
                elif direction == "LONG" and trend_val >= -filter_param:
                    logging.info(f"[{symbol}] Trend Filter triggered: Skipping LONG entry (trend={trend_val:+.5f} >= threshold={-filter_param:+.5f}).")
                    return False
                    
            elif filter_type == "RSI":
                prices = df["Close"].values
                rsi_arr = calculate_rsi(prices, 14)
                rsi_val = float(rsi_arr[-2]) # 直近の確定足
                logging.info(f"[{symbol}] RSI Filter Check: rsi_val={rsi_val:.2f}, threshold={filter_param:.2f}")
                if direction == "SHORT" and rsi_val < filter_param:
                    logging.info(f"[{symbol}] RSI Filter triggered: Skipping SHORT entry (rsi_val={rsi_val:.2f} < threshold={filter_param:.2f}).")
                    return False
                elif direction == "LONG" and rsi_val > filter_param:
                    logging.info(f"[{symbol}] RSI Filter triggered: Skipping LONG entry (rsi_val={rsi_val:.2f} > threshold={filter_param:.2f}).")
                    return False
                    
            elif filter_type == "SMA":
                close_prices = df["Close"].values
                sma_val = float(np.mean(close_prices[-51:-1]))
                entry_close = float(df["Close"].iloc[-2])
                logging.info(f"[{symbol}] SMA Filter Check: close={entry_close:.5f}, SMA_50={sma_val:.5f}")
                if direction == "SHORT" and entry_close <= sma_val:
                    logging.info(f"[{symbol}] SMA Filter triggered: Skipping SHORT entry (close={entry_close:.5f} <= SMA={sma_val:.5f}).")
                    return False
                elif direction == "LONG" and entry_close >= sma_val:
                    logging.info(f"[{symbol}] SMA Filter triggered: Skipping LONG entry (close={entry_close:.5f} >= SMA={sma_val:.5f}).")
                    return False
                    
            elif filter_type == "BB":
                close_prices = df["Close"].values
                last_20 = close_prices[-21:-1]
                bb_mean = float(np.mean(last_20))
                bb_std = float(np.std(last_20))
                bb_u_val = bb_mean + 2.0 * bb_std
                bb_l_val = bb_mean - 2.0 * bb_std
                entry_close = float(df["Close"].iloc[-2])
                logging.info(f"[{symbol}] BB Filter Check: close={entry_close:.5f}, BB_Upper={bb_u_val:.5f}, BB_Lower={bb_l_val:.5f}")
                if direction == "SHORT" and entry_close < bb_u_val - atr_val * filter_param:
                    logging.info(f"[{symbol}] BB Filter triggered: Skipping SHORT entry (close={entry_close:.5f} < UpperBB={bb_u_val - atr_val * filter_param:.5f}).")
                    return False
                elif direction == "LONG" and entry_close > bb_l_val + atr_val * filter_param:
                    logging.info(f"[{symbol}] BB Filter triggered: Skipping LONG entry (close={entry_close:.5f} > LowerBB={bb_l_val + atr_val * filter_param:.5f}).")
                    return False

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

        # スプレッド(pips)の計算
        point = getattr(info, "point", 0.00001)
        if point <= 0:
            point = 0.00001
        pips_multiplier = point * 10.0
        current_spread_pips = (current_ask - current_bid) / pips_multiplier
        
        # パラメータから最大スプレッドを取得 (デフォルト値は安全側に設定)
        max_spread_pips = PARAMS["general"].get("max_spread_pips", {}).get(symbol, 5.0)
        
        # ウィンドウ状態の取得
        window_status = get_sltp_window_status(now_jst)

        # --------------------------------------------------------
        # SL/TP退避・復旧ロジック
        # --------------------------------------------------------
        # A. 退避処理
        if window_status == 'evacuate':
            if not pos.get("sltp_evacuated", False):
                # 設定されていたSL/TPの値をローカルへ退避
                if sl_price > 0.0 or tp_price > 0.0:
                    pos["saved_sl_price"] = sl_price
                    pos["saved_tp_price"] = tp_price
                    pos["sltp_evacuated"] = True
                    
                    logging.info(f"[{symbol}] Rollover window entered. Evacuating SL/TP. Saved SL: {sl_price:.5f}, TP: {tp_price:.5f}")
                    
                    # MT5サーバー側のSL/TPをクリア(0.0)
                    success = self.executor.modify_position_sl_tp(ticket, 0.0, 0.0)
                    if success:
                        pos["sl_price"] = 0.0
                        pos["tp_price"] = 0.0
                    else:
                        logging.error(f"[{symbol}] Failed to clear SL/TP on server. Will retry next cycle.")
                    self.save_state()

        # B. 復旧・成行決済判定処理
        elif window_status == 'restore' and pos.get("sltp_evacuated", False):
            # スプレッドが平常値に戻っているか確認
            if current_spread_pips <= max_spread_pips:
                saved_sl = pos.get("saved_sl_price", 0.0)
                saved_tp = pos.get("saved_tp_price", 0.0)
                
                # 退避期間中に価格が到達していたか判定
                should_market_close = False
                close_reason = ""
                
                if direction == "LONG":
                    if saved_sl > 0.0 and current_bid <= saved_sl:
                        should_market_close = True
                        close_reason = "SL_LATENT"
                    elif saved_tp > 0.0 and current_bid >= saved_tp:
                        should_market_close = True
                        close_reason = "TP_LATENT"
                else: # SHORT
                    if saved_sl > 0.0 and current_ask >= saved_sl:
                        should_market_close = True
                        close_reason = "SL_LATENT"
                    elif saved_tp > 0.0 and current_ask <= saved_tp:
                        should_market_close = True
                        close_reason = "TP_LATENT"
                        
                if should_market_close:
                    logging.info(f"[{symbol}] Price breached SL/TP during rollover. Triggering immediate Market Close ({close_reason}). Bid: {current_bid:.5f}, Ask: {current_ask:.5f}, Saved SL: {saved_sl:.5f}, TP: {saved_tp:.5f}")
                    self.close_and_cleanup(symbol, ticket, close_reason)
                    return
                else:
                    # 通常通りSL/TPを再設定
                    logging.info(f"[{symbol}] Spread normalized ({current_spread_pips:.1f} <= {max_spread_pips:.1f} pips). Restoring SL/TP. SL: {saved_sl:.5f}, TP: {saved_tp:.5f}")
                    success = self.executor.modify_position_sl_tp(ticket, saved_sl, saved_tp)
                    if success:
                        pos["sl_price"] = saved_sl
                        pos["tp_price"] = saved_tp
                        pos["sltp_evacuated"] = False
                        self.save_state()
                    else:
                        logging.error(f"[{symbol}] Failed to restore SL/TP on server. Will retry.")
            else:
                logging.info(f"[{symbol}] In restore window but spread is still high: {current_spread_pips:.1f} pips (threshold: {max_spread_pips:.1f}). Deferring restoration.")

        # --------------------------------------------------------
        # ポジション決済判定（週末・時間決済）
        # --------------------------------------------------------
        # A. 週末強制決済 (土曜 05:00 JST 以降)
        if is_weekend_jst(now_jst):
            if window_status == 'evacuate' or current_spread_pips > max_spread_pips:
                logging.info(f"[{symbol}] Weekend close deferred due to high spread or rollover window. Spread: {current_spread_pips:.1f} pips.")
            else:
                logging.info(f"[{symbol}] Weekend close triggered JST={now_jst.strftime('%Y-%m-%d %H:%M:%S')}. Closing ticket {ticket}.")
                self.close_and_cleanup(symbol, ticket, "WEEKEND")
                return

        # B. 時間強制決済 (hold_bars 分相当経過したか)
        elapsed_seconds = (now_jst - entry_time).total_seconds()
        if elapsed_seconds >= hold_bars * 5 * 60:
            if window_status == 'evacuate' or current_spread_pips > max_spread_pips:
                logging.info(f"[{symbol}] Time close deferred due to high spread or rollover window. Spread: {current_spread_pips:.1f} pips (Threshold: {max_spread_pips:.1f}), Window: {window_status}. Deferring...")
            else:
                logging.info(f"[{symbol}] Time close triggered. Elapsed: {elapsed_seconds}s (Hold constraint: {hold_bars*5*60}s). Closing ticket {ticket}.")
                self.close_and_cleanup(symbol, ticket, "TIME")
                return

        # C. リアルタイム TP / SL 監視 (退避中は sl_price/tp_price が 0 になるため誤作動しない)
        close_position = False
        exit_reason = ""

        if direction == "LONG":
            if sl_price > 0.0 and current_bid <= sl_price:
                close_position = True
                exit_reason = "SL"
            elif tp_price > 0.0 and current_bid >= tp_price:
                close_position = True
                exit_reason = "TP"
        else: # SHORT
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
