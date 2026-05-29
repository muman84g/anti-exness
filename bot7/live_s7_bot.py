# ==============================================================================
# STRATEGY s7 CONCEPT: Optimized Lead-Lag Cross-Market ML Filter
# 【戦略s7コンセプト: グリッドサーチ最適化 先行指標クロスマーケット・アノマリー】
# ------------------------------------------------------------------------------
# - Concept: S7のバグ（未来リーク・ダブルシフト問題）を完全に修正。
#   新たにグリッドサーチで発見された「最強の先行指標マッピング」と、
#   Zスコア特徴量（価格比率・ボラティリティスプレッド）を導入。
# - ML Filter: LightGBMモデルはJST 9:00以降のアノマリー時間帯に対し、
#   直前に確定した（形成中ではない）5分足特徴量を用いて学習・予測を行います。
# - Execution: JST H:00 に成行エントリーし、JST H:55 に全決済します。
# ==============================================================================
import os
import sys
import time
import json
import logging
import traceback
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import yfinance as yf
import lightgbm as lgb
import warnings

warnings.filterwarnings('ignore')

# スクリプト自身の絶対パス
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# ログ設定
LOG_DIR = os.path.join(script_dir, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "s7_bot.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ── Docker環境からのインポート ──
from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor, ORDER_TYPE_BUY, ORDER_TYPE_SELL

# ============================================================
# s7 Configuration
# ============================================================
POLL_INTERVAL_SECONDS = 5 * 60
STATE_FILE = os.path.join(script_dir, "s7_bot_state.json")
ML_THRESHOLD = 0.55
RISK_USD = 10.0

# Exness MT5用のシンボルマッピング
SYMBOL_MAPPING = {
    'HG_FUT': 'XCUUSDm',
    'CL_FUT': 'USOILm',
    'SI_FUT': 'XAGUSDm',
    'USDJPY_FX': 'USDJPYm',
    'GBPJPY_FX': 'GBPJPYm',
    'EURJPY_FX': 'EURJPYm',
    'CADJPY_FX': 'CADJPYm',
    'IDX_N225': 'JP225m',
    'EURUSD_FX': 'EURUSDm',
    'GBPUSD_FX': 'GBPUSDm'
}

# yfinanceデータ取得用マッピング
TICKER_MAPPING = {
    'HG_FUT': 'HG=F',
    'CL_FUT': 'CL=F',
    'SI_FUT': 'SI=F',
    'USDJPY_FX': 'USDJPY=X',
    'GBPJPY_FX': 'GBPJPY=X',
    'EURJPY_FX': 'EURJPY=X',
    'CADJPY_FX': 'CADJPY=X',
    'IDX_N225': '^N225',
    'EURUSD_FX': 'EURUSD=X',
    'GBPUSD_FX': 'GBPUSD=X'
}

# グリッドサーチで発見された最適先行指標マッピング
LEAD_MAP = {
    'HG_FUT': 'IDX_N225',
    'CL_FUT': 'SI_FUT',
    'SI_FUT': 'GBPJPY_FX',
    'USDJPY_FX': 'GBPUSD_FX',
    'GBPJPY_FX': 'EURUSD_FX',
    'EURJPY_FX': 'CL_FUT',
    'CADJPY_FX': 'CL_FUT',
    'IDX_N225': 'GBPJPY_FX'
}

# ============================================================
# 1. 特徴量生成エンジン (s7 完全版)
# ============================================================
def create_features_5m_grid(df_open, df_high, df_low, df_close, col, lead_col):
    df_feats = pd.DataFrame(index=df_close.index)
    
    # 対象銘柄自身の基本特徴量
    df_feats['ret_1'] = df_close[col].pct_change(1)
    df_feats['ret_3'] = df_close[col].pct_change(3)
    df_feats['ret_6'] = df_close[col].pct_change(6)
    df_feats['ret_12'] = df_close[col].pct_change(12)
    df_feats['ret_36'] = df_close[col].pct_change(36)
    
    df_feats['vol_12'] = df_feats['ret_1'].rolling(12).std()
    df_feats['vol_72'] = df_feats['ret_1'].rolling(72).std()
    
    ema_20 = df_close[col].ewm(span=20).mean()
    ema_60 = df_close[col].ewm(span=60).mean()
    df_feats['dist_ema_20'] = (df_close[col] - ema_20) / ema_20
    df_feats['dist_ema_60'] = (df_close[col] - ema_60) / ema_60
    
    body = (df_close[col] - df_open[col]).abs()
    total_range = df_high[col] - df_low[col]
    df_feats['body_ratio'] = body / (total_range + 1e-8)
    
    # カレンダー
    jst_index = df_close.index + pd.Timedelta(hours=9)
    df_feats['weekday'] = jst_index.weekday
    df_feats['hour'] = jst_index.hour
    df_feats['day'] = jst_index.day
    
    # 先行銘柄からのクロスマーケット特徴量
    if lead_col and lead_col in df_close.columns:
        lead_ret = df_close[lead_col].pct_change(1)
        # ダブルシフト修正済み (lag0 = 当該足でのリターン)
        df_feats['lead_ret_lag0'] = lead_ret
        df_feats['lead_ret_lag1'] = lead_ret.shift(1)
        df_feats['lead_ret_lag2'] = lead_ret.shift(2)
        df_feats['lead_ret_lag3'] = lead_ret.shift(3)
        df_feats['lead_ret_lag6'] = lead_ret.shift(6)
        df_feats['lead_ret_lag12'] = lead_ret.shift(12)
        
        atr_lead = (df_high[lead_col] - df_low[lead_col]).rolling(12).mean()
        atr_target = (df_high[col] - df_low[col]).rolling(12).mean()
        df_feats['lead_target_vol_ratio'] = atr_lead / (atr_target + 1e-8)
        
        ratio = df_close[col] / (df_close[lead_col] + 1e-8)
        for w in [144, 432]:
            r_mean = ratio.rolling(w).mean()
            r_std = ratio.rolling(w).std()
            df_feats[f'ratio_zscore_{w}'] = (ratio - r_mean) / (r_std + 1e-8)
            
        vol_diff = atr_lead - atr_target
        vd_mean = vol_diff.rolling(144).mean()
        vd_std = vol_diff.rolling(144).std()
        df_feats['vol_spread_z'] = (vol_diff - vd_mean) / (vd_std + 1e-8)
        
    return df_feats.fillna(0)

# ============================================================
# 2. セッション管理 & アノマリー検出
# ============================================================
def get_active_session(t, window_map, flying_minutes):
    h_start = t.floor('h')
    dir1 = window_map.get(h_start.hour, None)
    if dir1 is not None:
        session_start = h_start - pd.Timedelta(minutes=flying_minutes)
        session_exit = h_start + pd.Timedelta(minutes=55)
        if session_start <= t <= session_exit:
            return h_start, dir1
            
    next_h = h_start + pd.Timedelta(hours=1)
    dir2 = window_map.get(next_h.hour, None)
    if dir2 is not None:
        session_start = next_h - pd.Timedelta(minutes=flying_minutes)
        session_exit = next_h + pd.Timedelta(minutes=55)
        if session_start <= t <= session_exit:
            return next_h, dir2
            
    return None, None

def find_high_prob_windows(s_open, s_close, min_count=20):
    hours = s_close.index.floor('h')
    h_open = s_open.groupby(hours).first()
    h_close = s_close.groupby(hours).last()
    
    df_h = pd.DataFrame(index=h_open.index)
    df_h['open'] = h_open
    df_h['close'] = h_close
    
    jst_index_h = df_h.index + pd.Timedelta(hours=9)
    df_h['jst_hour'] = jst_index_h.hour
    df_h['is_up'] = (df_h['close'] > df_h['open']).astype(int)
    df_h = df_h[df_h['jst_hour'] >= 9]
    
    stats = df_h.groupby(['jst_hour'])['is_up'].agg(['count', 'mean'])
    stats['mean'] = stats['mean'] * 100
    
    bullish = stats[(stats['mean'] >= 60.0) & (stats['count'] >= min_count)]
    bearish = stats[(stats['mean'] <= 40.0) & (stats['count'] >= min_count)]
    
    windows = []
    for h, row in bullish.iterrows():
        windows.append({'jst_hour': h, 'direction': 'LONG'})
    for h, row in bearish.iterrows():
        windows.append({'jst_hour': h, 'direction': 'SHORT'})
        
    return windows

def train_lightgbm_filters(df_open, df_high, df_low, df_close, high_prob_windows):
    models = {}
    jst_index = df_close.index + pd.Timedelta(hours=9)
    
    cost_map = {}
    for c in df_close.columns:
        base = 0.0001
        if any(s in c for s in ['GC', 'SI', 'HG']): base = 0.0003
        elif 'CL' in c: base = 0.0002
        elif 'NQ' in c: base = 0.00015
        cost_map[c] = base + 0.0001
        
    for col in LEAD_MAP.keys():
        windows = high_prob_windows.get(col, [])
        if not windows: continue
            
        window_map = {w['jst_hour']: w['direction'] for w in windows}
        lead_col = LEAD_MAP.get(col)
        df_feats = create_features_5m_grid(df_open, df_high, df_low, df_close, col, lead_col)
        
        X_list, y_list = [], []
        active_session_id = None
        session_entries = 0
        open_positions = []
        
        for idx in range(1, len(df_close)):
            t = jst_index[idx]
            session_id, direction = get_active_session(t, window_map, flying_minutes=0)
            
            if session_id != active_session_id:
                if open_positions:
                    exit_price = df_close[col].iloc[idx-1]
                    for pos in open_positions:
                        pnl = (exit_price - pos['entry_price']) / pos['entry_price'] if pos['direction'] == 'LONG' else (pos['entry_price'] - exit_price) / pos['entry_price']
                        pnl -= cost_map.get(col, 0.0002)
                        X_list.append(pos['feat'])
                        y_list.append(1 if pnl > 0 else 0)
                    open_positions = []
                active_session_id = session_id
                session_entries = 0
                
            if active_session_id is None: continue
                
            if t.minute == 55 and open_positions:
                exit_price = df_close[col].iloc[idx]
                for pos in open_positions:
                    pnl = (exit_price - pos['entry_price']) / pos['entry_price'] if pos['direction'] == 'LONG' else (pos['entry_price'] - exit_price) / pos['entry_price']
                    pnl -= cost_map.get(col, 0.0002)
                    X_list.append(pos['feat'])
                    y_list.append(1 if pnl > 0 else 0)
                open_positions = []
            elif t.minute == 0:
                if session_entries < 1:
                    open_positions.append({
                        'direction': direction,
                        'entry_price': df_open[col].iloc[idx],
                        'feat': df_feats.iloc[idx-1].values  # 確定した前足(idx-1)を使用！
                    })
                    session_entries += 1
                    
        if len(y_list) >= 30:
            X = np.array(X_list)
            y = np.array(y_list)
            train_data = lgb.Dataset(X, label=y)
            params = {
                'objective': 'binary', 'metric': 'binary_logloss', 'boosting_type': 'gbdt',
                'learning_rate': 0.05, 'num_leaves': 15, 'max_depth': 4, 'min_data_in_leaf': 10,
                'verbose': -1, 'random_state': 42
            }
            model = lgb.train(params, train_data, num_boost_round=50)
            models[col] = model
            logging.info(f"Trained s7 ML Model for {col}. Trade samples: {len(y_list)}")
            
            # モデルの保存
            model_path = os.path.join(script_dir, f"s7_lgbm_model_{col}.txt")
            model.save_model(model_path)
            
    return models

# ============================================================
# 3. yfinanceからのデータダウンロード (再学習用)
# ============================================================
def load_yfinance_data():
    logging.info("Downloading latest 60 days of 5m data from yfinance...")
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=59)
    
    data_open, data_close, data_high, data_low = {}, {}, {}, {}
    
    for col, ticker in TICKER_MAPPING.items():
        try:
            df = yf.download(ticker, start=start_date, end=end_date, interval="5m", progress=False)
            if df.empty:
                logging.warning(f"No yf data for {col} ({ticker})")
                continue
                
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
                
            df.index = df.index.tz_convert('UTC')
            data_open[col] = df['Open']
            data_high[col] = df['High']
            data_low[col] = df['Low']
            data_close[col] = df['Close']
        except Exception as e:
            logging.warning(f"Error fetching {col}: {e}")
            
    df_open = pd.DataFrame(data_open).ffill().bfill()
    df_high = pd.DataFrame(data_high).ffill().bfill()
    df_low = pd.DataFrame(data_low).ffill().bfill()
    df_close = pd.DataFrame(data_close).ffill().bfill()
    
    return df_open, df_high, df_low, df_close

# ============================================================
# 4. s7 メインボットクラス
# ============================================================
class s7TradingBot:
    def __init__(self):
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        self.models = {}
        self.high_prob_windows = {}
        self.selected_cols = list(LEAD_MAP.keys())
        
        self.state = {"active_tickets": {}}
        self.load_state()
        
    def bootstrap_models(self):
        df_open, df_high, df_low, df_close = load_yfinance_data()
        
        logging.info("Detecting s7 Anomaly Windows...")
        for col in self.selected_cols:
            if col in df_open.columns:
                self.high_prob_windows[col] = find_high_prob_windows(df_open[col], df_close[col], min_count=20)
                
        logging.info("Training s7 LightGBM Models...")
        self.models = train_lightgbm_filters(df_open, df_high, df_low, df_close, self.high_prob_windows)

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    self.state = json.load(f)
            except Exception as e:
                logging.error(f"Error loading state: {e}")

    def save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f)

    def log_trade_csv(self, action, ticket, symbol, direction="", lot_size=0, price=0.0, pnl=0.0):
        import csv
        csv_file = os.path.join(LOG_DIR, "s7_trades.csv")
        file_exists = os.path.isfile(csv_file)
        
        now_utc = datetime.now(timezone.utc)
        now_jst = now_utc + timedelta(hours=9)
        
        try:
            with open(csv_file, mode='a', newline='', encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp_JST", "Action", "Ticket", "Symbol", "Direction", "LotSize", "Price", "PnL"])
                writer.writerow([
                    now_jst.strftime("%Y-%m-%d %H:%M:%S"), action, ticket, symbol, direction, lot_size, price, pnl
                ])
        except Exception as e:
            logging.error(f"Failed to write to CSV: {e}")

    def start(self):
        logging.info("Starting s7 Live Bot execution loop...")
        if not self.dm.connect(): return

        try:
            while True:
                now = datetime.now(timezone.utc)
                current_ts = int(now.timestamp())
                next_run_ts = (current_ts // POLL_INTERVAL_SECONDS + 1) * POLL_INTERVAL_SECONDS
                wait_time = next_run_ts - current_ts
                
                logging.info(f"Waiting {wait_time:.2f} seconds for next 5M candle...")
                time.sleep(wait_time)
                
                logging.info(f"--- s7 Cycle Starting ({datetime.now().strftime('%H:%M:%S')}) ---")
                self.run_cycle()
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def run_cycle(self):
        now_utc = datetime.now(timezone.utc)
        now_jst = now_utc + timedelta(hours=9)
        minute = now_jst.minute
        
        # 4.1. 強制決済フェーズ (JST H:55)
        if minute >= 55:
            if not self.state["active_tickets"]:
                logging.info("No active positions to close at H:55.")
                return
                
            logging.info("H:55 reached. Closing all active positions.")
            for col, ticket in list(self.state["active_tickets"].items()):
                symbol = SYMBOL_MAPPING.get(col, col)
                success = self.executor.close_position(ticket)
                if success:
                    logging.info(f"Successfully closed position for {symbol}. PnL: {success.profit}")
                    self.log_trade_csv("EXIT", ticket, symbol, price=success.close_price, pnl=success.profit)
                else:
                    logging.warning(f"Failed to close position {ticket} for {symbol}.")
                del self.state["active_tickets"][col]
                
            self.save_state()
            return

        # 4.2. エントリー判定フェーズ (毎時最初のおよそ5分以内)
        if minute < 5:
            if now_jst.hour < 9:
                logging.info("Current JST is before 9:00. Skipping entry.")
                return
                
            for col in self.selected_cols:
                if col in self.state["active_tickets"]:
                    continue
                    
                exness_symbol = SYMBOL_MAPPING.get(col)
                if not exness_symbol: continue
                    
                windows = self.high_prob_windows.get(col, [])
                window_map = {w['jst_hour']: w['direction'] for w in windows}
                direction = window_map.get(now_jst.hour)
                
                if not direction: continue
                model = self.models.get(col)
                if not model: continue
                    
                # ターゲットデータ取得
                df_live_target = self.dm.get_historical_data(exness_symbol, 5, 500)
                if df_live_target is None or len(df_live_target) < 450:
                    logging.warning(f"Insufficient live data for target {exness_symbol}")
                    continue
                    
                df_open = pd.DataFrame({col: df_live_target['Open']})
                df_high = pd.DataFrame({col: df_live_target['High']})
                df_low = pd.DataFrame({col: df_live_target['Low']})
                df_close = pd.DataFrame({col: df_live_target['Close']})
                
                # 先行指標データ取得
                lead_col = LEAD_MAP.get(col)
                if lead_col:
                    lead_symbol = SYMBOL_MAPPING.get(lead_col)
                    df_live_lead = self.dm.get_historical_data(lead_symbol, 5, 500)
                    if df_live_lead is not None and len(df_live_lead) >= 450:
                        df_open[lead_col] = df_live_lead['Open']
                        df_high[lead_col] = df_live_lead['High']
                        df_low[lead_col] = df_live_lead['Low']
                        df_close[lead_col] = df_live_lead['Close']
                    else:
                        logging.warning(f"Failed to load lead data for {lead_symbol}")
                        continue
                        
                # 特徴量生成
                df_feats = create_features_5m_grid(df_open, df_high, df_low, df_close, col, lead_col)
                
                # 【重要】未来リーク防止＆確定期データ参照
                # df_liveの末尾(iloc[-1])は現在形成中の足であるため、1つ前の確定足(iloc[-2])を参照する
                feat_vector = df_feats.iloc[-2].values.reshape(1, -1)
                
                prob = model.predict(feat_vector)[0]
                logging.info(f"[{exness_symbol}] Anomaly: {direction}, ML Prob: {prob:.3f} (Lead: {lead_col})")
                
                if prob >= ML_THRESHOLD:
                    logging.info(f"Signal FIRE! Executing {direction} for {exness_symbol}")
                    
                    # リスクに基づくロット計算
                    sl_pips = 100.0 if "JPY" in exness_symbol else 50.0
                    vol_val = self.executor.get_symbol_info(exness_symbol)
                    lot_step = 0.01
                    if vol_val:
                        lot_step = vol_val.volume_step
                        
                    lot_size = lot_step * 1
                    
                    order_type = ORDER_TYPE_BUY if direction == 'LONG' else ORDER_TYPE_SELL
                    res = self.executor.open_position(exness_symbol, order_type, lot_size)
                    
                    if res:
                        logging.info(f"Trade Success: Ticket {res} at {res.price}")
                        self.state["active_tickets"][col] = res
                        self.save_state()
                        self.log_trade_csv("ENTRY", res, exness_symbol, direction, lot_size, res.price)
                    else:
                        logging.error(f"Trade Execution Failed for {exness_symbol}")
                        
if __name__ == "__main__":
    bot = s7TradingBot()
    bot.bootstrap_models()
    bot.start()
