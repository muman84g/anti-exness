# ==============================================================================
# STRATEGY s8 CONCEPT: Robust Auto-Screened Portfolio Live Trading Bot
# 【戦略s8コンセプト: 自動選別ロバストポートフォリオ・実運用ボット】
# ------------------------------------------------------------------------------
# - Base Strategy: ヒストリカルデータで厳格に3分割バックテストを行い、
#   検証期間（Validation）を無事通過した「本質的に頑健な1アセット（銅）」のみを取引します。
# - Traded Assets:
#   1. XCUUSDm (工業用銅) - 確率閾値 0.51
# - ML Pipeline: バックテストと100%同一の特徴量生成エンジン（V23）、
#   事前計算されたWinsorization限度値、および相関除去済みの特徴量リストを適用。
# - Dynamic Lot Sizing: 過去7日間のヒストリカルボラティリティに基づき、
#   1トレードあたりの許容リスク（$10固定）をターゲットにロットサイズを動的に計算。
# ==============================================================================
import os
import sys
import time
import json
import logging
import traceback
import csv
import sqlite3
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np
import lightgbm as lgb
import pytz
import warnings

warnings.filterwarnings('ignore')

# スクリプト自身の絶対パス
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# ログ設定
LOG_DIR = os.path.join(script_dir, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "s8_bot.log")

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
# s8 Configuration
# ============================================================
POLL_INTERVAL_SECONDS = 5 * 60
STATE_FILE = os.path.join(script_dir, "s8_bot_state.json")
USE_ML = True           # 機械学習フィルターを使用するか（Falseの場合はアノマリー時間帯にすべてエントリー）
USE_DYNAMIC_RISK = True
RISK_PERCENT = 0.02      # 1トレードあたりの残高に対する許容リスク割合（2%）
FIXED_RISK_USD = 10.0    # 動的リスク無効時の固定リスク金額（10ドル）
HOLD_BARS = 48   # 4時間保有（5分足×48本）

# トレード対象アセットと各閾値
TRADED_SYMBOLS = ['XCUUSDm']
THRESHOLDS = {
    'XCUUSDm': 0.54
}

# Final bot8 live profile: baseline NY20 entry + XCU high-zone lot throttling.
# Backtest-equivalent lot plan: normal 0.2 lot, high-zone 0.1 lot.
USE_ML = False
USE_DYNAMIC_RISK = False
FIXED_RISK_USD = 10.0
HOLD_BARS = 48
BASE_LOT_SIZE = {'XCUUSDm': 0.20}
HIGH_ZONE_LOT_SIZE = {'XCUUSDm': 0.10}
HIGH_ZONE_LOOKBACK_BARS = 17280
HIGH_ZONE_MIN_BARS = 12000
HIGH_ZONE_QUANTILE = 0.80
XCU_CACHE_INIT_BARS = 30000
XCU_CACHE_UPDATE_BARS = 5000
XCU_CACHE_TIMEOUT_SECONDS = 60
XCU_CACHE_UPDATE_MINUTE = 55
XCU_CACHE_MAX_STALE_DAYS = 10
CACHE_DIR = os.path.join(script_dir, "cache")
MARKET_CACHE_DB = os.path.join(CACHE_DIR, "s8_market_cache.sqlite")

# タイムゾーン定義
TZ_MAP = {
    'XCUUSDm': 'America/New_York'
}

# 特徴量生成に必要な全シンボルのリスト（バスケット・先行指標用）
REQUIRED_SYMBOLS = [
    'XCUUSDm', 'JP225m', 'EURUSDm', 'GBPUSDm', 'AUDUSDm', 'NZDUSDm', 
    'USDCADm', 'USDCHFm', 'USDJPYm', 'GBPJPYm', 'EURJPYm', 'CADJPYm', 
    'AUDJPYm', 'CHFJPYm', 'NZDJPYm', 'USTECm', 'US500m', 'US30m', 
    'XAUUSDm', 'XAGUSDm', 'USOILm'
]

# バスケット構成の定義 (V23と同一)
USD_BASKET = {'EURUSDm': -1.0, 'GBPUSDm': -1.0, 'AUDUSDm': -1.0, 'NZDUSDm': -1.0, 'USDCADm': 1.0, 'USDCHFm': 1.0, 'USDJPYm': 1.0}
JPY_BASKET = {'USDJPYm': 1.0, 'GBPJPYm': 1.0, 'EURJPYm': 1.0, 'CADJPYm': 1.0, 'AUDJPYm': 1.0, 'CHFJPYm': 1.0, 'NZDJPYm': 1.0}
EQ_BASKET = {'JP225m': 1.0, 'USTECm': 1.0, 'US500m': 1.0, 'US30m': 1.0}
COMM_BASKET = {'XAUUSDm': 1.0, 'XAGUSDm': 1.0, 'USOILm': 1.0, 'XCUUSDm': 1.0}

# 先行指標マッピング (V23と同一)
LEAD_MAP = {
    'XCUUSDm': 'JP225m'
}

# ============================================================
# 1. ヘルパー関数 & 特徴量生成エンジン (V23 100% 互換)
# ============================================================
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0/period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-8)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)

def calculate_pmacd(series):
    ema_12 = series.ewm(span=12, adjust=False).mean()
    ema_26 = series.ewm(span=26, adjust=False).mean()
    pmacd = (ema_12 - ema_26) / (ema_26 + 1e-8)
    signal = pmacd.ewm(span=9, adjust=False).mean()
    hist = pmacd - signal
    return pmacd, signal, hist

def calculate_bollinger_b(series, period=20, num_std=2.0):
    ma = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = ma + num_std * std
    lower = ma - num_std * std
    b_pos = (series - lower) / (upper - lower + 1e-8)
    return b_pos.fillna(0.5)

def get_basket_return(df_close, symbols_weights, period):
    valid_returns = []
    total_weight = 0.0
    for sym, weight in symbols_weights.items():
        if sym in df_close.columns:
            ret = df_close[sym].pct_change(period)
            valid_returns.append(ret * weight)
            total_weight += abs(weight)
    if not valid_returns:
        return pd.Series(0.0, index=df_close.index)
    return sum(valid_returns) / (total_weight + 1e-8)

def calculate_garman_klass_vol(df_open, df_high, df_low, df_close, col, window=1440):
    log_hl = np.log(df_high[col] / (df_low[col] + 1e-8))
    log_co = np.log(df_close[col] / (df_open[col] + 1e-8))
    gk = 0.5 * (log_hl ** 2) - (2 * np.log(2) - 1) * (log_co ** 2)
    gk_vol = np.sqrt(gk.rolling(window).mean().clip(lower=0))
    first_valid = gk_vol.dropna().iloc[0] if not gk_vol.dropna().empty else 0.001
    return gk_vol.fillna(first_valid)

def calculate_volume_zscore(df_volume, col, window=288):
    vol = df_volume[col].astype(float)
    vol_mean = vol.rolling(window).mean()
    vol_std = vol.rolling(window).std()
    vol_z = (vol - vol_mean) / (vol_std + 1e-8)
    return vol_z.fillna(0.0)

def calculate_vwap_dist(df_close, df_volume, col, window=288):
    price = df_close[col]
    volume = df_volume[col]
    pv = price * volume
    rolling_pv = pv.rolling(window).sum()
    rolling_vol = volume.rolling(window).sum()
    vwap = rolling_pv / (rolling_vol + 1e-8)
    dist = (price - vwap) / (vwap + 1e-8)
    return dist.fillna(0.0)

def create_features_5m_scale_invariant(df_open, df_high, df_low, df_close, df_volume, col):
    """V23のシミュレーションと完全に同一の特徴量配列を生成します。"""
    df_feats = pd.DataFrame(index=df_close.index)
    
    df_feats['ret_1'] = df_close[col].pct_change(1)
    df_feats['ret_3'] = df_close[col].pct_change(3)
    df_feats['ret_6'] = df_close[col].pct_change(6)
    df_feats['ret_12'] = df_close[col].pct_change(12)
    df_feats['ret_36'] = df_close[col].pct_change(36)
    
    df_feats['vol_12'] = df_feats['ret_1'].rolling(12).std()
    df_feats['vol_72'] = df_feats['ret_1'].rolling(72).std()
    df_feats['vol_ratio_12_72'] = df_feats['vol_12'] / (df_feats['vol_72'] + 1e-8)
    
    df_feats['norm_ret_3'] = df_feats['ret_3'] / (df_feats['vol_12'] + 1e-8)
    df_feats['norm_ret_12'] = df_feats['ret_12'] / (df_feats['vol_72'] + 1e-8)
    
    ema_20 = df_close[col].ewm(span=20).mean()
    ema_60 = df_close[col].ewm(span=60).mean()
    df_feats['dist_ema_20'] = (df_close[col] - ema_20) / (ema_20 + 1e-8)
    df_feats['dist_ema_60'] = (df_close[col] - ema_60) / (ema_60 + 1e-8)
    df_feats['ema_cross'] = (ema_20 - ema_60) / (ema_60 + 1e-8)
    
    body = (df_close[col] - df_open[col]).abs()
    total_range = df_high[col] - df_low[col]
    df_feats['body_ratio'] = body / (total_range + 1e-8)
    
    df_feats['hl_range_12'] = (df_high[col].rolling(12).max() - df_low[col].rolling(12).min()) / (df_close[col] + 1e-8)
    df_feats['hl_range_288'] = (df_high[col].rolling(288).max() - df_low[col].rolling(288).min()) / (df_close[col] + 1e-8)
    
    df_feats[f'ret_{HOLD_BARS}'] = df_close[col].pct_change(HOLD_BARS)
    df_feats[f'vol_{HOLD_BARS}'] = df_feats['ret_1'].rolling(HOLD_BARS).std()
    df_feats[f'norm_ret_{HOLD_BARS}'] = df_feats[f'ret_{HOLD_BARS}'] / (df_feats[f'vol_{HOLD_BARS}'] + 1e-8)
    
    df_feats['rsi_14'] = calculate_rsi(df_close[col], period=14)
    df_feats['rsi_mom'] = df_feats['rsi_14'] - df_feats['rsi_14'].shift(3)
    
    pmacd, signal, hist = calculate_pmacd(df_close[col])
    df_feats['pmacd'] = pmacd
    df_feats['pmacd_signal'] = signal
    df_feats['pmacd_hist'] = hist
    df_feats['bollinger_b'] = calculate_bollinger_b(df_close[col])
    
    df_feats['gk_vol_12'] = calculate_garman_klass_vol(df_open, df_high, df_low, df_close, col, window=12)
    df_feats['gk_vol_288'] = calculate_garman_klass_vol(df_open, df_high, df_low, df_close, col, window=288)
    df_feats['gk_vol_ratio'] = df_feats['gk_vol_12'] / (df_feats['gk_vol_288'] + 1e-8)
    
    df_feats['vol_z_12'] = calculate_volume_zscore(df_volume, col, window=12)
    df_feats['vol_z_72'] = calculate_volume_zscore(df_volume, col, window=72)
    df_feats['vol_z_288'] = calculate_volume_zscore(df_volume, col, window=288)
    df_feats['vol_accel'] = df_feats['vol_z_12'] - df_feats['vol_z_288']
    df_feats['vol_accel_med'] = df_feats['vol_z_12'] - df_feats['vol_z_72']
    
    df_feats['dist_vwap_288'] = calculate_vwap_dist(df_close, df_volume, col, window=288)
    
    tz_name = TZ_MAP.get(col, 'Asia/Tokyo')
    local_index = df_close.index.tz_convert(tz_name)
    local_hour = local_index.hour
    local_weekday = local_index.weekday
    
    df_feats['hour_sin'] = np.sin(2 * np.pi * local_hour / 24.0)
    df_feats['hour_cos'] = np.cos(2 * np.pi * local_hour / 24.0)
    df_feats['day_sin'] = np.sin(2 * np.pi * local_weekday / 7.0)
    df_feats['day_cos'] = np.cos(2 * np.pi * local_weekday / 7.0)
    
    lead_col = LEAD_MAP.get(col, None)
    if lead_col and lead_col in df_close.columns:
        lead_ret = df_close[lead_col].pct_change(1)
        df_feats['lead_ret_lag0'] = lead_ret
        df_feats['lead_ret_lag1'] = lead_ret.shift(1)
        df_feats['lead_ret_lag2'] = lead_ret.shift(2)
        df_feats['lead_ret_lag3'] = lead_ret.shift(3)
        df_feats['lead_ret_lag6'] = lead_ret.shift(6)
        df_feats['lead_ret_lag12'] = lead_ret.shift(12)
        
        vol_lead = lead_ret.rolling(12).std()
        vol_lead_72 = lead_ret.rolling(72).std()
        vol_target = df_feats['ret_1'].rolling(12).std()
        df_feats['lead_target_vol_ratio'] = vol_lead / (vol_target + 1e-8)
        df_feats['lead_norm_ret_3'] = lead_ret.pct_change(3) / (vol_lead + 1e-8)
        
        df_feats['ret_spread_3'] = df_feats['ret_3'] - lead_ret.pct_change(3)
        df_feats['ret_spread_12'] = df_feats['ret_12'] - lead_ret.pct_change(12)
        df_feats['ret_spread_36'] = df_feats['ret_36'] - lead_ret.pct_change(36)
        
        lead_ret_12 = df_close[lead_col].pct_change(12)
        lead_ret_36 = df_close[lead_col].pct_change(36)
        df_feats['norm_ret_spread_12'] = (df_feats['ret_12'] / (df_feats['vol_72'] + 1e-8)) - (lead_ret_12 / (vol_lead_72 + 1e-8))
        df_feats['norm_ret_spread_36'] = (df_feats['ret_36'] / (df_feats['vol_72'] + 1e-8)) - (lead_ret_36 / (vol_lead_72 + 1e-8))
        
        ratio = df_close[col] / (df_close[lead_col] + 1e-8)
        for w in [144, 432]:
            r_mean = ratio.rolling(w).mean()
            r_std = ratio.rolling(w).std()
            df_feats[f'ratio_zscore_{w}'] = (ratio - r_mean) / (r_std + 1e-8)
            
        vol_diff = vol_lead - vol_target
        vd_mean = vol_diff.rolling(144).mean()
        vd_std = vol_diff.rolling(144).std()
        df_feats['vol_spread_z'] = (vol_diff - vd_mean) / (vd_std + 1e-8)
        
        df_feats[f'lead_ret_{HOLD_BARS}'] = df_close[lead_col].pct_change(HOLD_BARS)
        df_feats[f'lead_vol_{HOLD_BARS}'] = lead_ret.rolling(HOLD_BARS).std()
        df_feats[f'lead_norm_ret_{HOLD_BARS}'] = df_feats[f'lead_ret_{HOLD_BARS}'] / (df_feats[f'lead_vol_{HOLD_BARS}'] + 1e-8)
        
        df_feats['lead_rsi_14'] = calculate_rsi(df_close[lead_col], period=14)
        df_feats['lead_rsi_mom'] = df_feats['lead_rsi_14'] - df_feats['lead_rsi_14'].shift(3)
        df_feats['rsi_diff_14'] = df_feats['rsi_14'] - df_feats['lead_rsi_14']
        
        l_pmacd, l_signal, l_hist = calculate_pmacd(df_close[lead_col])
        df_feats['lead_pmacd'] = l_pmacd
        df_feats['lead_pmacd_hist'] = l_hist
        df_feats['lead_bollinger_b'] = calculate_bollinger_b(df_close[lead_col])
        df_feats['pmacd_diff'] = df_feats['pmacd'] - df_feats['lead_pmacd']
        
        df_feats['lead_gk_vol_12'] = calculate_garman_klass_vol(df_open, df_high, df_low, df_close, lead_col, window=12)
        df_feats['lead_gk_vol_288'] = calculate_garman_klass_vol(df_open, df_high, df_low, df_close, lead_col, window=288)
        df_feats['lead_gk_vol_ratio'] = df_feats['lead_gk_vol_12'] / (df_feats['lead_gk_vol_288'] + 1e-8)
        df_feats['lead_vol_z_288'] = calculate_volume_zscore(df_volume, lead_col, window=288)
        df_feats['lead_dist_vwap_288'] = calculate_vwap_dist(df_close, df_volume, lead_col, window=288)

    usd_basket = {
        'EURUSDm': -1.0, 'GBPUSDm': -1.0, 'AUDUSDm': -1.0, 'NZDUSDm': -1.0,
        'USDJPYm': 1.0, 'USDCADm': 1.0, 'USDCHFm': 1.0
    }
    jpy_basket = {
        'USDJPYm': -1.0, 'GBPJPYm': -1.0, 'EURJPYm': -1.0, 'AUDJPYm': -1.0,
        'CHFJPYm': -1.0, 'NZDJPYm': -1.0, 'CADJPYm': -1.0
    }
    eq_basket = {
        'US500m': 1.0, 'USTECm': 1.0, 'US30m': 1.0, 'JP225m': 1.0
    }
    comm_basket = {
        'XAUUSDm': 1.0, 'XAGUSDm': 1.0, 'USOILm': 1.0, 'XCUUSDm': 1.0
    }
    
    for period in [12, 48]:
        df_feats[f'basket_usd_ret_{period}'] = get_basket_return(df_close, usd_basket, period)
        df_feats[f'basket_jpy_ret_{period}'] = get_basket_return(df_close, jpy_basket, period)
        df_feats[f'basket_eq_ret_{period}'] = get_basket_return(df_close, eq_basket, period)
        df_feats[f'basket_comm_ret_{period}'] = get_basket_return(df_close, comm_basket, period)
        df_feats[f'usd_basket_spread_{period}'] = df_feats[f'ret_{period}'] - df_feats[f'basket_usd_ret_{period}']
        df_feats[f'jpy_basket_spread_{period}'] = df_feats[f'ret_{period}'] - df_feats[f'basket_jpy_ret_{period}']
        
    if 'XAUUSDm' in df_close.columns and 'XAGUSDm' in df_close.columns:
        gs_ratio = df_close['XAUUSDm'] / (df_close['XAGUSDm'] + 1e-8)
        df_feats['gs_ratio_ret_12'] = gs_ratio.pct_change(12)
        df_feats['gs_ratio_ret_48'] = gs_ratio.pct_change(48)
    else:
        df_feats['gs_ratio_ret_12'] = 0.0
        df_feats['gs_ratio_ret_48'] = 0.0
        
    return df_feats.fillna(0)

# ============================================================
# 2. リスクベース・ロットサイズ計算用変換倍率 (V23互換)
# ============================================================
def get_lot_multiplier_usd(symbol, price, usdjpy_rate):
    """通貨・銘柄ごとに1ロット動いた時のUSD価値を返します。"""
    if symbol == 'USOILm': return 1000.0
    elif symbol == 'XAGUSDm': return 5000.0
    elif symbol == 'XCUUSDm': return 100.0
    elif symbol == 'XAUUSDm': return 100.0
    elif symbol == 'JP225m': return 1.0 / usdjpy_rate
    elif symbol in ['USTECm', 'US30m']: return 10.0
    elif symbol == 'US500m': return 100.0
    elif symbol.endswith('JPYm') or symbol == 'USDJPYm': return 100000.0 / usdjpy_rate
    elif symbol.endswith('USDm'): return 100000.0
    elif symbol == 'USDCADm': return 100000.0 / price
    elif symbol == 'USDCHFm': return 100000.0 / price
    return 100000.0

# ============================================================
# 3. ボットメインクラス
# ============================================================
class s8TradingBot:
    def __init__(self):
        self.dm = MT5DataManager()
        self.executor = MT5Executor(self.dm)
        self.models = {}
        self.anomaly_windows = {}
        self.pipeline_meta = {}
        self.last_cache_update_hour = None
        
        self.state = {"active_tickets": {}}
        self.load_state()
        self.load_robust_models_and_meta()
        self.init_market_cache()
        
    def load_robust_models_and_meta(self):
        """事前学習済みのモデルと選別メタデータをロードします。"""
        logging.info("Loading pre-trained models and robust metadata...")
        
        # 1. アノマリー窓のロード
        windows_path = os.path.join(script_dir, "s8_anomaly_windows.json")
        if os.path.exists(windows_path):
            with open(windows_path, "r") as f:
                self.anomaly_windows = json.load(f)
            logging.info(f"Loaded robust anomaly windows for: {list(self.anomaly_windows.keys())}")
        else:
            logging.error(f"Anomaly windows file not found: {windows_path}")
            
        # 2. パイプラインメタデータ（Winsorization閾値、採用特徴量リスト）のロード
        meta_path = os.path.join(script_dir, "s8_pipeline_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, "r") as f:
                self.pipeline_meta = json.load(f)
            logging.info("Loaded pipeline metadata.")
        else:
            logging.error(f"Pipeline metadata file not found: {meta_path}")
            
        # 3. LightGBMモデルファイルのロード
        for col in TRADED_SYMBOLS:
            model_path = os.path.join(script_dir, f"s8_lgbm_model_{col}.txt")
            if os.path.exists(model_path):
                self.models[col] = lgb.Booster(model_file=model_path)
                logging.info(f"Successfully loaded LightGBM model for {col}.")
            else:
                logging.error(f"Model file not found for {col}: {model_path}")

    def init_market_cache(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        with sqlite3.connect(MARKET_CACHE_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS m5_bars (
                    symbol TEXT NOT NULL,
                    time_utc TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY (symbol, time_utc)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_m5_bars_symbol_time ON m5_bars(symbol, time_utc)"
            )
        logging.info(f"Market cache ready: {MARKET_CACHE_DB}")

    def update_market_cache(self, symbol, bars, reason="scheduled"):
        logging.info(f"[{symbol}] Updating M5 market cache ({reason}, bars={bars})...")
        df_hist = self.dm.get_historical_data(symbol, 5, bars, timeout=XCU_CACHE_TIMEOUT_SECONDS)
        if df_hist is None or df_hist.empty:
            logging.warning(f"[{symbol}] Market cache update failed: no history returned.")
            return False

        df_hist = df_hist.copy()
        if df_hist.index.tz is None:
            df_hist.index = df_hist.index.tz_localize("UTC")
        else:
            df_hist.index = df_hist.index.tz_convert("UTC")
        df_hist = df_hist.sort_index()
        df_hist = df_hist.loc[~df_hist.index.duplicated(keep="last")]

        rows = []
        for ts, row in df_hist.iterrows():
            rows.append(
                (
                    symbol,
                    ts.isoformat(),
                    float(row["Open"]),
                    float(row["High"]),
                    float(row["Low"]),
                    float(row["Close"]),
                    float(row["Volume"]),
                )
            )

        with sqlite3.connect(MARKET_CACHE_DB) as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO m5_bars
                (symbol, time_utc, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM m5_bars WHERE symbol = ?",
                (symbol,),
            ).fetchone()[0]
            latest = conn.execute(
                "SELECT MAX(time_utc) FROM m5_bars WHERE symbol = ?",
                (symbol,),
            ).fetchone()[0]
        logging.info(f"[{symbol}] Market cache updated. rows_added={len(rows)} total_rows={count} latest={latest}")
        return True

    def maybe_update_market_cache(self, now_utc):
        if now_utc.minute < XCU_CACHE_UPDATE_MINUTE:
            return
        hour_key = now_utc.strftime("%Y-%m-%d %H")
        if self.last_cache_update_hour == hour_key:
            return
        self.last_cache_update_hour = hour_key
        for symbol in TRADED_SYMBOLS:
            try:
                self.update_market_cache(symbol, XCU_CACHE_UPDATE_BARS, reason="hourly")
            except Exception as e:
                logging.warning(f"[{symbol}] Scheduled market cache update failed: {e}")
                logging.warning(traceback.format_exc())

    def get_cached_closes(self, symbol, limit):
        with sqlite3.connect(MARKET_CACHE_DB) as conn:
            rows = conn.execute(
                """
                SELECT time_utc, close
                FROM m5_bars
                WHERE symbol = ?
                ORDER BY time_utc DESC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        rows = list(reversed(rows))
        if not rows:
            return pd.Series(dtype="float64")
        index = pd.to_datetime([row[0] for row in rows], utc=True)
        return pd.Series([float(row[1]) for row in rows], index=index, dtype="float64")

    def get_cache_status(self, symbol):
        with sqlite3.connect(MARKET_CACHE_DB) as conn:
            row = conn.execute(
                "SELECT COUNT(*), MAX(time_utc) FROM m5_bars WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        count = int(row[0] or 0)
        latest = pd.to_datetime(row[1], utc=True) if row and row[1] else None
        return count, latest

    def determine_lot_size(self, symbol, current_price, sym_info, now_utc):
        base_lot = BASE_LOT_SIZE.get(symbol, 0.20)
        high_lot = HIGH_ZONE_LOT_SIZE.get(symbol, max(0.01, base_lot * 0.5))

        count, latest = self.get_cache_status(symbol)
        if count < HIGH_ZONE_MIN_BARS:
            logging.warning(f"[{symbol}] Cache has only {count} rows. Seeding cache before high-zone check...")
            try:
                self.update_market_cache(symbol, XCU_CACHE_INIT_BARS, reason="seed")
            except Exception as e:
                logging.warning(f"[{symbol}] Seed market cache update failed: {e}")
                logging.warning(traceback.format_exc())
            count, latest = self.get_cache_status(symbol)

        if count < HIGH_ZONE_MIN_BARS:
            logging.warning(f"[{symbol}] Cache still insufficient ({count} rows). Using safe high-zone lot: {high_lot}")
            target_lot = high_lot
            high_zone = True
            threshold = None
        elif latest is not None and (
            now_utc - latest.to_pydatetime()
        ).total_seconds() > XCU_CACHE_MAX_STALE_DAYS * 24 * 60 * 60:
            logging.warning(f"[{symbol}] Cache stale. latest={latest}. Using safe high-zone lot: {high_lot}")
            target_lot = high_lot
            high_zone = True
            threshold = None
        else:
            closes = self.get_cached_closes(symbol, HIGH_ZONE_LOOKBACK_BARS)
            threshold = float(closes.quantile(HIGH_ZONE_QUANTILE))
            high_zone = bool(current_price > threshold)
            target_lot = high_lot if high_zone else base_lot
            logging.info(
                f"[{symbol}] High-zone check: price={current_price:.5f}, "
                f"q{HIGH_ZONE_QUANTILE:.2f}={threshold:.5f}, rows={len(closes)}, "
                f"high_zone={high_zone}, target_lot={target_lot}"
            )

        if sym_info:
            target_lot = max(sym_info.volume_min, min(target_lot, sym_info.volume_max))
            target_lot = round(target_lot / sym_info.volume_step) * sym_info.volume_step
            target_lot = round(target_lot, 2)
        else:
            target_lot = max(0.01, round(target_lot, 2))

        return target_lot, high_zone, threshold

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
        csv_file = os.path.join(LOG_DIR, "s8_trades.csv")
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
        logging.info("Starting s8 Live Bot execution loop...")
        if not self.dm.connect():
            logging.error("Failed to connect via EA Bridge. Exit.")
            return

        try:
            while True:
                now = datetime.now(timezone.utc)
                current_ts = int(now.timestamp())
                next_run_ts = (current_ts // POLL_INTERVAL_SECONDS + 1) * POLL_INTERVAL_SECONDS
                wait_time = next_run_ts - current_ts
                
                logging.info(f"Waiting {wait_time:.2f} seconds for next 5M candle...")
                time.sleep(wait_time)
                
                logging.info(f"--- s8 Cycle Starting ({datetime.now().strftime('%H:%M:%S')}) ---")
                self.run_cycle()
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def run_cycle(self):
        now_utc = datetime.now(timezone.utc)
        now_jst = now_utc + timedelta(hours=9)
        minute = now_jst.minute
        
        # ── 1. 強制決済フェーズ ────────────────────────
        # A. 週末の強制クローズ判定 (土曜日 02:30 JST以降、市場閉鎖前の全決済)
        is_weekend_close = (now_jst.weekday() == 5 and (now_jst.hour > 2 or (now_jst.hour == 2 and now_jst.minute >= 30)))
        
        # B. 通常の時間決済判定 (JST H:55 〜 H:59)
        is_normal_close = (minute >= 55)
        
        if is_weekend_close or is_normal_close:
            if not self.state["active_tickets"]:
                if is_normal_close:
                    logging.info("No active positions to close at H:55.")
                self.maybe_update_market_cache(now_utc)
                return
                
            if is_weekend_close:
                logging.info(f"Weekend force close triggered at JST {now_jst.strftime('%Y-%m-%d %H:%M:%S')}. Closing all positions...")
            else:
                logging.info("H:55 reached. Checking positions for hold time exit...")
                
            for col, pos_data in list(self.state["active_tickets"].items()):
                # Handle legacy state (where pos_data was just ticket_id)
                if isinstance(pos_data, dict):
                    ticket = pos_data["ticket"]
                    entry_time_str = pos_data["entry_time"]
                    try:
                        entry_time = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except Exception:
                        entry_time = now_jst  # fallback
                else:
                    ticket = pos_data
                    entry_time = now_jst  # fallback
                    
                age_hours = (now_jst - entry_time).total_seconds() / 3600.0
                logging.info(f"[{col}] Position Ticket {ticket} age: {age_hours:.2f} hours (JST Entry: {entry_time.strftime('%Y-%m-%d %H:%M:%S')})")
                
                # 週末強制決済、または通常の3.5時間以上保有による決済
                if is_weekend_close or age_hours >= 3.5:
                    reason = "Weekend Force Close" if is_weekend_close else f"Hold Time Limit ({age_hours:.2f}h)"
                    logging.info(f"[{col}] Position closing. Reason: {reason}...")
                    success = self.executor.close_position(ticket)
                    if success:
                        logging.info(f"Successfully closed position for {col}. PnL: {success.profit}")
                        self.log_trade_csv("EXIT", ticket, col, price=success.close_price, pnl=success.profit)
                    else:
                        logging.warning(f"Failed to close position {ticket} for {col}.")
                    del self.state["active_tickets"][col]
                    
            self.save_state()
            self.maybe_update_market_cache(now_utc)
            return

        # ── 2. エントリー判定フェーズ (毎時最初の約5分以内) ──────────
        if minute < 5:
            logging.info(f"Entry scanning phase. Current JST: {now_jst.strftime('%H:%M')}")
            
            # トレード可否の判断に最新の1600件データを一括取得してバッファ化
            # (vol std計算に1440期間必要)
            logging.info("Fetching required symbols' data from MT5...")
            df_open_raw, df_high_raw, df_low_raw, df_close_raw, df_volume_raw = {}, {}, {}, {}, {}
            fetch_symbols = REQUIRED_SYMBOLS if USE_ML else TRADED_SYMBOLS
            
            success_count = 0
            for sym in fetch_symbols:
                df_hist = self.dm.get_historical_data(sym, 5, 1600)
                if df_hist is not None and len(df_hist) >= 1500:
                    df_open_raw[sym] = df_hist['Open']
                    df_high_raw[sym] = df_hist['High']
                    df_low_raw[sym] = df_hist['Low']
                    df_close_raw[sym] = df_hist['Close']
                    df_volume_raw[sym] = df_hist['Volume']
                    success_count += 1
                else:
                    logging.warning(f"Failed to fetch data for {sym}")
                    
            if success_count < len(fetch_symbols):
                logging.warning(f"Only successfully fetched {success_count}/{len(fetch_symbols)} symbols. Proceeding with caution.")
                
            # 各DataFrameの生成と同期 (バックテスト load_data 相当)
            df_close_df = pd.DataFrame(df_close_raw).ffill().bfill()
            if df_close_df.index.tz is None:
                df_close_df.index = df_close_df.index.tz_localize('UTC')
            else:
                df_close_df.index = df_close_df.index.tz_convert('UTC')
                
            df_open_df = pd.DataFrame(df_open_raw).reindex(df_close_df.index).ffill().bfill()
            df_high_df = pd.DataFrame(df_high_raw).reindex(df_close_df.index).ffill().bfill()
            df_low_df = pd.DataFrame(df_low_raw).reindex(df_close_df.index).ffill().bfill()
            df_volume_df = pd.DataFrame(df_volume_raw).reindex(df_close_df.index).fillna(0.0)
            
            # 各取引対象銘柄についてシグナルを判定
            for col in TRADED_SYMBOLS:
                if col in self.state["active_tickets"]:
                    logging.info(f"Already have open position in {col}. Skip.")
                    continue
                if col not in df_close_df.columns or df_close_df[col].dropna().empty:
                    logging.warning(f"[{col}] No usable latest history. Skip.")
                    continue
                    
                # 2.1. ローカル時間におけるアノマリー時間帯の判定
                tz_name = TZ_MAP.get(col, 'Asia/Tokyo')
                local_time = now_utc.astimezone(pytz.timezone(tz_name))
                local_hour = local_time.hour
                
                windows = self.anomaly_windows.get(col, [])
                window_map = {w['local_hour']: w['direction'] for w in windows}
                direction = window_map.get(local_hour)
                
                if not direction:
                    logging.info(f"[{col}] Local Hour {local_hour} ({tz_name}) is NOT an anomaly window. Skip.")
                    continue
                    
                # 2.1.5. 曜日・時間フィルターによる市場閉鎖時のスキップ (JSTベース)
                # 土曜 02:00 JST 以降 〜 日曜終日 〜 月曜 07:00 JST までエントリー禁止
                jst_weekday = now_jst.weekday()  # 0=月, 5=土, 6=日
                jst_hour = now_jst.hour
                
                if jst_weekday == 5:  # 土曜日
                    if jst_hour >= 2:
                        logging.info(f"[{col}] Saturday {now_jst.strftime('%H:%M')} JST is after weekend entry limit (02:00 JST). Skip.")
                        continue
                elif jst_weekday == 6:  # 日曜日
                    logging.info(f"[{col}] Sunday is market closed. Skip.")
                    continue
                elif jst_weekday == 0:  # 月曜日
                    if jst_hour < 7:
                        logging.info(f"[{col}] Monday morning before 07:00 JST is market closed/unstable. Skip.")
                        continue
                    
                model = self.models.get(col)
                meta = self.pipeline_meta.get(col)
                if USE_ML and (not model or not meta):
                    logging.warning(f"Model or pipeline metadata not loaded for {col}. Skip.")
                    continue
                    
                try:
                    execute_entry = False
                    if not USE_ML:
                        logging.info(f"[{col}] Inside anomaly window ({direction}). ML filter is disabled. Signal CONFIRMED! Executing entry...")
                        execute_entry = True
                    else:
                        # 2.2. 特徴量生成
                        logging.info(f"[{col}] Inside anomaly window ({direction}). Calculating ML features...")
                        df_feats = create_features_5m_scale_invariant(
                            df_open_df, df_high_df, df_low_df, df_close_df, df_volume_df, col
                        )
                        
                        # 未来リーク防止のため、1期前の「確定足」特徴ベクトルを取得
                        feat_row = df_feats.iloc[-2].copy()
                        
                        # 2.3. Winsorizationの適用（事前計算済みの閾値でクリップ）
                        winsor_limits = meta['winsor_limits']
                        for feature, limits in winsor_limits.items():
                            if feature in feat_row:
                                feat_row[feature] = np.clip(feat_row[feature], limits['lower'], limits['upper'])
                                
                        # 2.4. 相関除去フィルタ（不要特徴量をドロップ）
                        retained_features = meta['retained_features']
                        feat_vector = feat_row[retained_features].values.reshape(1, -1)
                        
                        # 2.5. 予測の実行
                        prob = model.predict(feat_vector)[0]
                        threshold = THRESHOLDS.get(col, 0.53)
                        logging.info(f"[{col}] Prediction Prob: {prob:.3f} | Target Threshold: {threshold:.2f}")
                        
                        if prob >= threshold:
                            logging.info(f"[{col}] Signal CONFIRMED! Executing entry...")
                            execute_entry = True
                            
                    if execute_entry:
                        # 取引制限情報の取得（余剰証拠金を読み込んで動的リスクを計算するため前段へ移動）
                        sym_info = self.executor.get_symbol_info(col)
                        
                        # リスク金額の決定 (動的％リスク or 固定リスク)
                        if USE_DYNAMIC_RISK and sym_info:
                            risk_usd = sym_info.margin_free * RISK_PERCENT
                            logging.info(f"[{col}] Dynamic Risk active. Free Margin: {sym_info.margin_free:.2f} USD, Risk %: {RISK_PERCENT*100:.1f}%, Calculated Risk: {risk_usd:.2f} USD")
                        else:
                            risk_usd = FIXED_RISK_USD
                            logging.info(f"[{col}] Fixed Risk active: {risk_usd:.2f} USD")
                            
                        # 2.6. ダイナミックロット計算（リスク金額をベースにボラティリティ調整）
                        # 過去1440期間（約5日間）のボラティリティ
                        diff_hold = df_close_df[col] - df_close_df[col].shift(HOLD_BARS)
                        std_hold = diff_hold.rolling(1440).std()
                        std_val = std_hold.iloc[-2]  # 最新確定足のボラ値
                        
                        current_price = df_close_df[col].iloc[-1]
                        usdjpy_rate = df_close_df['USDJPYm'].iloc[-1] if 'USDJPYm' in df_close_df.columns else 150.0
                        
                        multiplier = get_lot_multiplier_usd(col, current_price, usdjpy_rate)
                        std_usd_per_lot = std_val * multiplier
                        
                        if std_usd_per_lot > 0:
                            target_lot = risk_usd / std_usd_per_lot
                        else:
                            target_lot = 0.01
                            
                        # 取引制限情報の丸め
                        if sym_info:
                            target_lot = max(sym_info.volume_min, min(target_lot, sym_info.volume_max))
                            target_lot = round(target_lot / sym_info.volume_step) * sym_info.volume_step
                            target_lot = round(target_lot, 2)
                        else:
                            target_lot = max(0.01, round(target_lot, 2))

                        target_lot, high_zone, high_threshold = self.determine_lot_size(
                            col, current_price, sym_info, now_utc
                        )
                        if high_threshold is None:
                            logging.info(f"[{col}] Lot plan: lot={target_lot}, high_zone={high_zone}, threshold=unknown")
                        else:
                            logging.info(
                                f"[{col}] Lot plan: lot={target_lot}, high_zone={high_zone}, "
                                f"threshold={high_threshold:.5f}"
                            )
                            
                        # エントリー発注
                        order_type = ORDER_TYPE_BUY if direction == 'LONG' else ORDER_TYPE_SELL
                        ticket = self.executor.open_position(col, order_type, target_lot)
                        
                        if ticket:
                            logging.info(f"[{col}] Trade Filled. Ticket: {ticket} Lot: {target_lot} Price: {ticket.price}")
                            self.state["active_tickets"][col] = {
                                "ticket": int(ticket),
                                "entry_time": now_jst.strftime("%Y-%m-%d %H:%M:%S")
                             }
                            self.save_state()
                            self.log_trade_csv("ENTRY", ticket, col, direction, target_lot, ticket.price)
                        else:
                            logging.error(f"[{col}] Order execution failed.")
                            
                except Exception as e:
                    logging.error(f"Error processing strategy for {col}: {e}")
                    logging.error(traceback.format_exc())

if __name__ == "__main__":
    bot = s8TradingBot()
    bot.start()
