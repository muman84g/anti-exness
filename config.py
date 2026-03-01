"""
Configuration for the Correlation Divergence Backtest.
Tickers, categories, and strategy parameters.
"""

# ── Tickers (mirrored from financial-dashboard/app.py) ──────────────────────

TICKERS_INFO = {
    # Indices (futures for extended hours)
    "Nikkei 225":   {"symbol": "NIY=F",      "category": "Index"},
    "S&P 500":      {"symbol": "ES=F",       "category": "Index"},
    "Nasdaq 100":   {"symbol": "NQ=F",       "category": "Index"},
    "Dow Jones":    {"symbol": "YM=F",       "category": "Index"},
    #"Russell 2000": {"symbol": "RTY=F",      "category": "Index"},

    # Forex
    "USD/JPY":  {"symbol": "JPY=X",      "category": "Forex"},
    "GBP/JPY":  {"symbol": "GBPJPY=X",   "category": "Forex"},
    "AUD/JPY":  {"symbol": "AUDJPY=X",   "category": "Forex"},
    "EUR/JPY":  {"symbol": "EURJPY=X",   "category": "Forex"},
    "CHF/JPY":  {"symbol": "CHFJPY=X",   "category": "Forex"},
    "NZD/JPY":  {"symbol": "NZDJPY=X",   "category": "Forex"},
    "CAD/JPY":  {"symbol": "CADJPY=X",   "category": "Forex"},
    "CHF/JPY":  {"symbol": "CHFJPY=X",   "category": "Forex"},
    "HKD/JPY":  {"symbol": "HKDJPY=X",   "category": "Forex"},
    "GBP/USD":  {"symbol": "GBPUSD=X",   "category": "Forex"},
    "GBP/AUD":  {"symbol": "GBPAUD=X",   "category": "Forex"},
    "GBP/CHF":  {"symbol": "GBPCHF=X",   "category": "Forex"},
    "AUD/USD":  {"symbol": "AUDUSD=X",   "category": "Forex"},
    #"NZD/USD":  {"symbol": "NZDUSD=X",   "category": "Forex"},
    #"EUR/USD":  {"symbol": "EURUSD=X",   "category": "Forex"},
    "EUR/GBP":  {"symbol": "EURGBP=X",   "category": "Forex"},
    "EUR/CAD":  {"symbol": "EURCAD=X",   "category": "Forex"},
    "EUR/CHF":  {"symbol": "EURCHF=X",   "category": "Forex"},
    "USD/CHF":  {"symbol": "CHF=X",      "category": "Forex"},
    "USD/CAD":  {"symbol": "CAD=X",      "category": "Forex"},
    "AUD/NZD":  {"symbol": "AUDNZD=X",   "category": "Forex"},
    "AUD/CAD":  {"symbol": "AUDCAD=X",   "category": "Forex"},
    "AUD/CHF":  {"symbol": "AUDCHF=X",   "category": "Forex"},
    "NZD/CAD":  {"symbol": "NZDCAD=X",   "category": "Forex"},
    "NZD/CHF":  {"symbol": "NZDCHF=X",   "category": "Forex"},
    "CAD/CHF":  {"symbol": "CADCHF=X",   "category": "Forex"},

    # Energy
    "Crude Oil":   {"symbol": "CL=F",  "category": "Energy"},
    "Nat Gas":     {"symbol": "NG=F",  "category": "Energy"},
    "Brent Crude": {"symbol": "BZ=F",  "category": "Energy"},
    "Gasoline":    {"symbol": "RB=F",  "category": "Energy"},
    "Heating Oil": {"symbol": "HO=F",  "category": "Energy"},

    # Metals
    "Gold":      {"symbol": "GC=F",  "category": "Metal"},
    "Silver":    {"symbol": "SI=F",  "category": "Metal"},
    "Copper":    {"symbol": "HG=F",  "category": "Metal"},
    "Platinum":  {"symbol": "PL=F",  "category": "Metal"},
    "Palladium": {"symbol": "PA=F",  "category": "Metal"},
    #"Aluminium": {"symbol": "AL=F",  "category": "Metal"},ないっぽい

    # Agriculture
    #"Corn":    {"symbol": "ZC=F",  "category": "Agri"},
    #"Soybean": {"symbol": "ZS=F",  "category": "Agri"},
    #"Wheat":   {"symbol": "ZW=F",  "category": "Agri"},
    #"Sugar":   {"symbol": "SB=F",  "category": "Agri"},
    #"Coffee":  {"symbol": "KC=F",  "category": "Agri"},
    #"Cocoa":   {"symbol": "CC=F",  "category": "Agri"},

    # Crypto
    "Bitcoin":  {"symbol": "BTC-USD",  "category": "Crypto"},
    "Ethereum": {"symbol": "ETH-USD",  "category": "Crypto"},
    "Solana":   {"symbol": "SOL-USD",  "category": "Crypto"},
    "XRP":      {"symbol": "XRP-USD",  "category": "Crypto"},
    "BNB":      {"symbol": "BNB-USD",  "category": "Crypto"},
    "Dogecoin": {"symbol": "DOGE-USD", "category": "Crypto"},

    # Tech Stocks (High correlation sector)
    # "NVDA":     {"symbol": "NVDA",     "category": "Tech"},
    # "AMD":      {"symbol": "AMD",      "category": "Tech"},
    # "MSFT":     {"symbol": "MSFT",     "category": "Tech"},
    # "GOOGL":    {"symbol": "GOOGL",    "category": "Tech"},
    # "META":     {"symbol": "META",     "category": "Tech"},
    # "TSLA":     {"symbol": "TSLA",     "category": "Tech"},

    # More Forex
    # "AUD/USD":  {"symbol": "AUDUSD=X", "category": "Forex"},
    # "USD/CAD":  {"symbol": "CAD=X",    "category": "Forex"},
}

# Convenience maps
TICKERS    = {k: v["symbol"]   for k, v in TICKERS_INFO.items()}
CATEGORIES = {k: v["category"] for k, v in TICKERS_INFO.items()}

# ── Strategy Parameters ─────────────────────────────────────────────────────

# Long-term correlation (calculated on 1H bars)
LT_CORR_WINDOW_DAYS  = 14        # rolling window for long-term correlation
LT_CORR_THRESHOLD    = 0.5       # Standard threshold (Commodities, Forex, Indices)
HIGH_CORR_THRESHOLD  = 0.75      # Stricter threshold for volatile assets
HIGH_VOL_CATEGORIES  = []#["Tech", "Crypto"]
CROSS_CATEGORY_ONLY  = False     # False = allow same-category pairs (ENABLED)

# Z-score thresholds
ZSCORE_ENTRY = 1.2               # |Z| >= this -> entry signal (OPTIMIZED from 1.2)
ZSCORE_EXIT  = 0.3               # |Z| <= this -> close (OPTIMIZED from 0.0)
ZSCORE_STOP  = 3.0               # |Z| >= this -> stop-loss (divergence widening)
TIME_STOP_HOURS = 24             # Close position if not profitable after this many hours

# Spread lookback for Z-score calculation (in 15-min bars)
ZSCORE_LOOKBACK_BARS = 48        # 48 × 15min = 12 hours

# Half-life filter (Ornstein-Uhlenbeck)
MAX_HALF_LIFE_MINUTES = 360      # 6H — skip pairs whose spread reverts too slowly (OPTIMIZED from 8H)

# Walk-forward
TRAIN_WINDOW_DAYS = 20           # training window for pair selection
TRADE_WINDOW_DAYS = 1            # out-of-sample trading window

# Transaction costs
SPREAD_COST = 0.0002             # round-trip cost (0.02%)

# Capital management
INITIAL_CAPITAL   = 100_000      # initial capital (JPY)
# RISK_PER_TRADE    = 0.05       # (Deprecated for Phase 4) Fixed 5% allocation
TARGET_RISK_PCT   = 0.01         # Target risk 1% of capital per trade (Volatility Sizing)
MAX_POSITIONS     = 10            # max concurrent open positions

# Time-of-day filter (UTC hours)
ACTIVE_HOURS_START = 0           # 6:00 UTC = 15:00 JST
ACTIVE_HOURS_END   = 22          # 22:00 UTC = 07:00 JST (covers Tokyo+London+NY)

# ── Risk Management Improvements (Phase 2) ──────────────────────────────────

# 1. Blacklist (Exclude pairs containing these currencies or specific symbols)
# Based on backtest, GBP and CHF pairs often underperform in mean-reversion.
BLACKLIST_CURRENCIES = []#["GBP", "CHF", "HKD"]
BLACKLIST_PAIRS      = [] # Specific pair names to exclude e.g. "Gold|Silver"

# 2. Sector Exposure Limits
# Limit maximum concurrent positions per sector to avoid concentration risk.
# (Default MAX_POSITIONS is global limit, this is per-sector limit)
SECTOR_LIMITS = {
    "Energy": 2,    # High concentration risk in backtest
    "Forex":  6,
    "Index":  2,
    "Metal":  2,
    "Agri":   2,
    "Crypto": 1,
    "Tech":   1,
}

# Data settings
DATA_PERIOD_LT = "3mo"           # yfinance period for 1H data
DATA_PERIOD_ST = "60d"           # yfinance period for 15m data (yfinance max=60d)
INTERVAL_LT    = "1h"            # long-term data interval
INTERVAL_ST    = "15m"           # short-term data interval

# Output directory
import os
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# ── Optimization Parameters ──────────────────────────────────────────────────
# optimize.py will use these lists to perform grid search
PARAM_GRID = {
    # 1. Strategy Parameters (Correlation)
    "lt_corr_window": [14],         # Rolling window for long-term correlation (Days)
    "min_corr": [0.5],         # Standard threshold
    "high_corr": [0.75],            # High correlation threshold
    
    # 2. Z-Score Rules
    "zscore_entry": [1.0, 1.2, 1.5],
    "zscore_exit":  [0.0, 0.15, 0.3, 0.45],
    "zscore_stop":  [2.0, 3.0, 4.0],  # Optimize stop loss
    "time_stop_hours": [24, 48],    # Time-based stop (extended for 1H bars)
    
    # 3. Filters
    "lookback_window": [48],    # Z-score lookback bars
    "max_half_life": [360],    # Mean reversion speed (minutes)
    
    # 4. Management & Time
    "max_positions": [10],           # Max concurrent positions
    "active_hours_start": [0, 6],      # Trading start hour (UTC)
}

#2026-02-20 03:50:45,942 
#Optimization complete in 6049.7s.
#2026-02-20 03:50:45,964 Top 3 Results:
#min_corr,zscore_entry,zscore_exit,max_half_life,lookback_window,time_stop_hours,trades,return,sharpe,profit_factor,drawdown
#0.55,1.2,0.5,360,24,24,104,0.52,5.41,3.02,-0.1
#0.55,1.2,0.5,360,24,48,104,0.52,5.41,3.02,-0.1
#0.55,1.2,0.5,360,24,12,104,0.52,5.4,3.0,-0.1

#2026-02-20 04:28:06,011 [30/7290] Return: 0.88% 
# | Trades: 217 | 
# Params: {
# 'lt_corr_window': 14, 
# 'min_corr': 0.5, 
# 'high_corr': 0.75, 
# 'zscore_entry': 1.0, 
# 'zscore_exit': 0.3, 
# 'time_stop_hours': 24, 
# 'lookback_window': 48, 
# 'max_half_life': 360, 
# 'max_positions': 8,
# 'active_hours_start': 0, 
# 'trades': 217, 
# 'return': np.float64(0.88), 
# 'sharpe': np.float64(5.7), 
# 'profit_factor': np.float64(2.23), 
# 'drawdown': np.float64(-0.18)}