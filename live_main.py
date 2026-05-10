import os
import logging
from datetime import datetime, timezone

# --- Logging Setup ---
# Must be configured BEFORE importing other modules that might call basicConfig
LOG_FILE = "Z:/app/bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),  # ファイルに保存
        logging.StreamHandler()                            # 画面にも表示
    ]
)

import time
import pytz
import pandas as pd
from mt5_compat import mt5  # Cross-platform wrapper (auto-selects Windows/Linux)

from config import TICKERS_INFO
from live_config import YF_TO_MT5, MT5_TO_YF, USE_META_API
from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor
try:
    from metaapi_bridge import MetaApiBridge
except ImportError:
    MetaApiBridge = None  # MetaApi not available (e.g. running in Wine Python)
from live_state import LiveState

from ml_strategy import create_ml_features, create_labels, train_ml_model
from pairs import find_cointegrated_pairs
from spread import calculate_spread_history, compute_rolling_zscore

# --- Settings ---
POLL_INTERVAL_SECONDS = 60 * 15 # Run every 15 minutes
CORR_UPDATE_INTERVAL_HOURS = 24
MIN_CORR_THRESHOLD = 0.5
ZSCORE_ENTRY = 1.2
ZSCORE_EXIT = 0.3
RISK_USD = 5.0 # Fixed risk per trade for demo purposes
TRAIN_WINDOW_DAYS = 30
