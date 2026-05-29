# ==============================================================================
# STRATEGY S7 CONCEPT: Multi-Asset Hourly Bias & ML (LightGBM) Filter
# 【戦略S7コンセプト: 精鋭8銘柄 時間帯アノマリー＆機械学習フィルター】
# ------------------------------------------------------------------------------
# - Concept: JST 9:00以降の流動性が安定した時間帯に限定し、8つの優位性の高い銘柄
#   （銅、原油、銀、USDJPY、GBPJPY、EURJPY、CADJPY、日経225）の時間帯別アノマリー
#   （陽線/陰線になりやすい時間）を狙い撃ちします。
# - CentOS Dynamic Boot: CentOS等の外部サーバー上でも単体で完結して動作するよう、
#   起動時に yfinance から直接過去60日分の5分足データを動的ダウンロードして再学習します。
# - ML Filter: 直近のプライスアクション特徴量から予測勝率が55%以上の場合のみエントリーします。
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

# スクリプト自身の絶対パス (CentOS上での相対パス問題を防ぐ)
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# ログ設定 (スクリプトと同階層にlogsフォルダを作成)
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
# S7 Configuration
# ============================================================
POLL_INTERVAL_SECONDS = 5 * 60
STATE_FILE = os.path.join(script_dir, "s7_bot_state.json")
ML_THRESHOLD = 0.55
RISK_USD = 10.0  # 1トレードあたりの許容リスク

# Exness MT5用のシンボルマッピング
# ※デモ口座の語尾にmがつく設定と完全に一致させています
SYMBOL_MAPPING = {
    'HG_FUT': 'XCUUSDm',    # 銅 (Copper)
    'CL_FUT': 'USOILm',     # 原油 (WTI)
    'SI_FUT': 'XAGUSDm',    # 銀 (Silver)
    'USDJPY_FX': 'USDJPYm',
    'GBPJPY_FX': 'GBPJPYm',
    'EURJPY_FX': 'EURJPYm',
    'CADJPY_FX': 'CADJPYm',
    'IDX_N225': 'JP225m'    # 日経225
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
    'IDX_N225': '^N225'
}

# ============================================================
# 1. 特徴量生成エンジン (完全に自己完結)
# ============================================================
def lives7_create_features_5m(df_open, df_high, df_low, df_close, col):
    """5分足マイクロ特徴量の生成 (リーク無しの時系列特徴量)"""
    df_feats = pd.DataFrame(index=df_close.index)
    
    # 複数スパンの変化率
    df_feats['ret_1'] = df_close[col].pct_change(1)
    df_feats['ret_3'] = df_close[col].pct_change(3)
    df_feats['ret_6'] = df_close[col].pct_change(6)
    df_feats['ret_12'] = df_close[col].pct_change(12)
    df_feats['ret_36'] = df_close[col].pct_change(36)
    
    # 複数スパンのボラティリティ
    df_feats['vol_12'] = df_feats['ret_1'].rolling(12).std()
    df_feats['vol_72'] = df_feats['ret_1'].rolling(72).std()
    
    # 指数移動平均(EMA)からの乖離率
    ema_20 = df_close[col].ewm(span=20).mean()
    ema_60 = df_close[col].ewm(span=60).mean()
    df_feats['dist_ema_20'] = (df_close[col] - ema_20) / ema_20
    df_feats['dist_ema_60'] = (df_close[col] - ema_60) / ema_60
    
    # ローソク足の実体・髭比率
    body = (df_close[col] - df_open[col]).abs()
    total_range = df_high[col] - df_low[col]
    df_feats['body_ratio'] = body / (total_range + 1e-8)
    
    # カレンダーアノマリー特徴量 (JST換算)
    jst_index = df_close.index + pd.Timedelta(hours=9)
    df_feats['weekday'] = jst_index.weekday
    df_feats['hour'] = jst_index.hour
    df_feats['day'] = jst_index.day
    
    return df_feats.fillna(0)

# ============================================================
# 2. セッション管理 & アノマリー検出ヘルパー
# ============================================================
def get_active_session(t, window_map, flying_minutes):
    """JST基準でのセッション状態の判定"""
    h_start = t.floor('h')
    
    # 1. 現在のJST時間帯がアノマリー時間かチェック
    dir1 = window_map.get(h_start.hour, None)
    if dir1 is not None:
        session_start = h_start - pd.Timedelta(minutes=flying_minutes)
        session_exit = h_start + pd.Timedelta(minutes=55)
        if session_start <= t <= session_exit:
            return h_start, dir1
            
    # 2. 次のJST時間帯がアノマリー時間かチェック (フライング判定用)
    next_h = h_start + pd.Timedelta(hours=1)
    dir2 = window_map.get(next_h.hour, None)
    if dir2 is not None:
        session_start = next_h - pd.Timedelta(minutes=flying_minutes)
        session_exit = next_h + pd.Timedelta(minutes=55)
        if session_start <= t <= session_exit:
            return next_h, dir2
            
    return None, None

def find_high_prob_windows(df_open, df_close, min_count=20):
    """Trainデータから優位性の高い時間帯(9:00 JST以降)を抽出"""
    hours = df_close.index.floor('h')
    high_prob_windows = {}
    
    for col in df_close.columns:
        h_open = df_open[col].groupby(hours).first()
        h_close = df_close[col].groupby(hours).last()
        
        df_h = pd.DataFrame(index=h_open.index)
        df_h['open'] = h_open
        df_h['close'] = h_close
        
        jst_index_h = df_h.index + pd.Timedelta(hours=9)
        df_h['jst_hour'] = jst_index_h.hour
        df_h['is_up'] = (df_h['close'] > df_h['open']).astype(int)
        
        # JST 9:00以前（朝のスプレッド拡大時間）を完全に除外
        df_h = df_h[df_h['jst_hour'] >= 9]
        
        stats = df_h.groupby(['jst_hour'])['is_up'].agg(['count', 'mean'])
        stats['mean'] = stats['mean'] * 100
        
        bullish_windows = stats[(stats['mean'] >= 60.0) & (stats['count'] >= min_count)]
        bearish_windows = stats[(stats['mean'] <= 40.0) & (stats['count'] >= min_count)]
        
        windows = []
        for h, row in bullish_windows.iterrows():
            windows.append({'jst_hour': h, 'direction': 'LONG'})
        for h, row in bearish_windows.iterrows():
            windows.append({'jst_hour': h, 'direction': 'SHORT'})
            
        high_prob_windows[col] = windows
        
    return high_prob_windows

def train_lightgbm_filters(df_open, df_high, df_low, df_close, high_prob_windows):
    """LightGBMモデルを訓練して各アセットのモデル辞書を返す"""
    models = {}
    jst_index = df_close.index + pd.Timedelta(hours=9)
    
    # 往復取引コストの設定
    cost_map = {}
    for c in df_close.columns:
        base = 0.0001
        if any(s in c for s in ['GC', 'SI', 'HG']):
            base = 0.0003
        elif 'CL' in c:
            base = 0.0002
        elif 'NQ' in c:
            base = 0.00015
        cost_map[c] = base + 0.0001
        
    for col in df_close.columns:
        windows = high_prob_windows.get(col, [])
        if not windows:
            continue
            
        window_map = {w['jst_hour']: w['direction'] for w in windows}
        df_feats = lives7_create_features_5m(df_open, df_high, df_low, df_close, col)
        
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
                        if pos['direction'] == 'LONG':
                            pnl = (exit_price - pos['entry_price']) / pos['entry_price']
                        else:
                            pnl = (pos['entry_price'] - exit_price) / pos['entry_price']
                        pnl -= cost_map.get(col, 0.0002)
                        y_val = 1 if pnl > 0 else 0
                        X_list.append(pos['feat'])
                        y_list.append(y_val)
                    open_positions = []
                    
                active_session_id = session_id
                session_entries = 0
            
            if active_session_id is None:
                continue
                
            if t.minute == 55 and open_positions:
                exit_price = df_close[col].iloc[idx]
                for pos in open_positions:
                    if pos['direction'] == 'LONG':
                        pnl = (exit_price - pos['entry_price']) / pos['entry_price']
                    else:
                        pnl = (pos['entry_price'] - exit_price) / pos['entry_price']
                    pnl -= cost_map.get(col, 0.0002)
                    y_val = 1 if pnl > 0 else 0
                    X_list.append(pos['feat'])
                    y_list.append(y_val)
                open_positions = []
                
            elif t.minute == 0:
                if session_entries < 1:
                    open_positions.append({
                        'direction': direction,
                        'entry_price': df_open[col].iloc[idx],
                        'feat': df_feats.iloc[idx].values
                    })
                    session_entries += 1
        
        if open_positions:
            exit_price = df_close[col].iloc[-1]
            for pos in open_positions:
                if pos['direction'] == 'LONG':
                    pnl = (exit_price - pos['entry_price']) / pos['entry_price']
                else:
                    pnl = (pos['entry_price'] - exit_price) / pos['entry_price']
                pnl -= cost_map.get(col, 0.0002)
                y_val = 1 if pnl > 0 else 0
                X_list.append(pos['feat'])
                y_list.append(y_val)
            open_positions = []
                
        if len(y_list) >= 50:
            X = np.array(X_list)
            y = np.array(y_list)
            
            win_ratio = y.mean() * 100
            logging.info(f"  {col}: 訓練用サンプル = {len(y)}回 ( baseline勝率: {win_ratio:.1f}% )")
            
            train_data = lgb.Dataset(X, label=y)
            params = {
                'objective': 'binary',
                'metric': 'binary_logloss',
                'boosting_type': 'gbdt',
                'learning_rate': 0.05,
                'num_leaves': 15,
                'max_depth': 4,
                'min_data_in_leaf': 10,
                'verbose': -1,
                'random_state': 42
            }
            
            model = lgb.train(params, train_data, num_boost_round=50)
            models[col] = model
        else:
            logging.warning(f"  {col}: トレードサンプル数が不足しているためMLフィルターを適用しません (サンプル数: {len(y_list)}回)")
            
    return models

# ============================================================
# 3. yfinanceからの自動データ取得 & 訓練エンジン (CentOS対応)
# ============================================================
def download_and_train_s7():
    """CentOS環境に対応するため、起動時にyfinanceから直接60日分の5分足データを取得し、オンデマンドで訓練を行う。
    過去データは常に同一フォルダ・同一ファイル名に上書き保存し、フォルダ内が肥大化するのを防ぐ。
    また、ダウンロード失敗時はローカルキャッシュからロードする頑健なフォールバック設計。
    """
    logging.info("Starting dynamic yfinance data download (60d, 5m)...")
    
    # データキャッシュフォルダの設定
    CACHE_DIR = os.path.join(script_dir, "s7_data_cache")
    os.makedirs(CACHE_DIR, exist_ok=True)
    
    data_open, data_high, data_low, data_close = {}, {}, {}, {}
    
    for col, ticker in TICKER_MAPPING.items():
        # 常に同一ファイル名で上書き保存し、重複ファイルを作らない
        cache_file = os.path.join(CACHE_DIR, f"s7_raw_{col}.csv")
        
        logging.info(f"Downloading {ticker} from yfinance...")
        df = pd.DataFrame()
        try:
            df = yf.download(ticker, period="60d", interval="5m", progress=False, auto_adjust=True)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)
                df = df[~df.index.isna()]
                df = df.apply(pd.to_numeric, errors='coerce')
                
                # キャッシュファイルを常に上書き保存 (フォルダ肥大化防止)
                df.to_csv(cache_file)
                logging.info(f"Successfully downloaded {ticker} ({len(df)} rows) and saved to cache.")
            else:
                logging.error(f"Downloaded empty DataFrame for {ticker}")
        except Exception as e:
            logging.error(f"Failed to download {ticker} from yfinance: {e}")
            
        # ダウンロード失敗、またはデータが空の場合のローカルキャッシュ・フォールバック
        if df.empty:
            if os.path.exists(cache_file):
                logging.info(f"Attempting to load local cache for {col} from: {cache_file}")
                try:
                    df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
                    logging.info(f"Successfully loaded cache for {col} ({len(df)} rows)")
                except Exception as cache_err:
                    logging.error(f"Failed to read local cache file for {col}: {cache_err}")
            else:
                logging.error(f"No local cache file found for {col} at {cache_file}")
                
        if not df.empty:
            data_open[col] = df['Open']
            data_high[col] = df['High']
            data_low[col] = df['Low']
            data_close[col] = df['Close']
        else:
            logging.critical(f"No training data available for {col} (yfinance failed and no local cache found).")
            
    df_open = pd.DataFrame(data_open)
    df_high = pd.DataFrame(data_high)
    df_low = pd.DataFrame(data_low)
    df_close = pd.DataFrame(data_close)
    
    # 欠損値補間
    for d in [df_open, df_high, df_low, df_close]:
        d.dropna(how='all', inplace=True)
        d.ffill(inplace=True)
        d.bfill(inplace=True)
        
    logging.info("Data fetch complete. Building models...")
    high_prob_windows = find_high_prob_windows(df_open, df_close, min_count=20)
    models = train_lightgbm_filters(df_open, df_high, df_low, df_close, high_prob_windows)
    
    logging.info("S7 initialization and training finished.")
    return list(TICKER_MAPPING.keys()), high_prob_windows, models

# ============================================================
# 4. Live Bot クラス
# ============================================================
class S7LiveBot:
    def __init__(self):
        logging.info("Initializing S7 MT5 Live Bot...")
        
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        
        # CentOSサーバー対応の完全自律動的ダウンロード&再学習
        self.selected_cols, self.high_prob_windows, self.models = download_and_train_s7()
        
        self.state = {
            "active_tickets": {}  # col_name -> ticket_id
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
        logging.info("Starting S7 Live Bot execution loop...")
        if not self.dm.connect(): return

        try:
            while True:
                now = datetime.now(timezone.utc)
                current_ts = int(now.timestamp())
                next_run_ts = (current_ts // POLL_INTERVAL_SECONDS + 1) * POLL_INTERVAL_SECONDS
                wait_time = next_run_ts - current_ts
                
                logging.info(f"Waiting {wait_time:.2f} seconds for next 5M candle...")
                time.sleep(wait_time)
                
                logging.info(f"--- S7 Cycle Starting ({datetime.now().strftime('%H:%M:%S')}) ---")
                self.run_cycle()
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def run_cycle(self):
        now_utc = datetime.now(timezone.utc)
        now_jst = now_utc + timedelta(hours=9)
        
        minute = now_jst.minute
        
        # ---------------------------------------------------------
        # 4.1. 強制決済フェーズ (JST H:55)
        # ---------------------------------------------------------
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

        # ---------------------------------------------------------
        # 4.2. エントリー判定フェーズ (毎時最初のおよそ5分以内のサイクル)
        # ---------------------------------------------------------
        if minute < 5:
            if now_jst.hour < 9:
                logging.info("Current JST is before 9:00. Skipping entry.")
                return
                
            for col in self.selected_cols:
                if col in self.state["active_tickets"]:
                    continue # すでに該当アセットのポジション保有中
                    
                exness_symbol = SYMBOL_MAPPING.get(col)
                if not exness_symbol:
                    continue
                    
                # 4.2.1 アノマリー判定
                windows = self.high_prob_windows.get(col, [])
                window_map = {w['jst_hour']: w['direction'] for w in windows}
                direction = window_map.get(now_jst.hour)
                
                if not direction:
                    continue # アノマリーではない時間
                    
                model = self.models.get(col)
                if not model:
                    continue
                    
                # 4.2.2 直近の5Mデータ取得 (特徴量生成用に100本)
                df_live = self.dm.get_historical_data(exness_symbol, 5, 120)
                if df_live is None or len(df_live) < 100:
                    logging.warning(f"Insufficient live data for {exness_symbol}")
                    continue
                    
                # 自己完結型の lives7_create_features_5m にモックを流す
                df_open = pd.DataFrame({col: df_live['Open']})
                df_high = pd.DataFrame({col: df_live['High']})
                df_low = pd.DataFrame({col: df_live['Low']})
                df_close = pd.DataFrame({col: df_live['Close']})
                
                try:
                    df_feats = lives7_create_features_5m(df_open, df_high, df_low, df_close, col)
                    feat_vector = df_feats.iloc[-1].values.reshape(1, -1)
                    
                    # 4.2.3 機械学習による判定
                    prob = model.predict(feat_vector)[0]
                    logging.info(f"[{exness_symbol}] Anomaly: {direction}, ML Prob: {prob:.3f}")
                    
                    if prob >= ML_THRESHOLD:
                        logging.info(f"+++ S7 ENTRY SIGNAL! +++ {exness_symbol} {direction} (Prob: {prob:.3f} >= {ML_THRESHOLD})")
                        
                        order_type = ORDER_TYPE_BUY if direction == 'LONG' else ORDER_TYPE_SELL
                        # ロット計算 (許容リスク10ドル、ストップ幅100pips相当)
                        lot = self.executor.calculate_lot_size(exness_symbol, RISK_USD, 100)
                        
                        ticket = self.executor.open_position(exness_symbol, order_type, lot)
                        if ticket:
                            logging.info(f"Successfully entered {direction} on {exness_symbol}. Ticket: {ticket}")
                            self.state["active_tickets"][col] = int(ticket)
                            self.save_state()
                            self.log_trade_csv("ENTRY", int(ticket), exness_symbol, direction, lot_size=lot, price=ticket.price)
                except Exception as e:
                    logging.error(f"Error executing logic for {col}: {e}")
                    logging.error(traceback.format_exc())

if __name__ == "__main__":
    try:
        bot = S7LiveBot()
        bot.start()
    except Exception as e:
        logging.error(f"CRITICAL CRASH: {e}")
        logging.error(traceback.format_exc())
