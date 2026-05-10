import os
import logging
from datetime import datetime, timezone
import traceback

# --- Logging Setup ---
# Must be configured BEFORE importing other modules that might call basicConfig
LOG_FILE = "Z:/app/logs/bot.log"
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

class LiveBot:
    def __init__(self):
        if USE_META_API:
            logging.info("Initializing MetaApi (Linux Bridge)...")
            bridge = MetaApiBridge()
            self.dm = bridge
            self.executor = bridge
        else:
            logging.info("Initializing Local MT5 (Windows Native)...")
            self.dm = MT5DataManager()
            self.executor = MT5Executor(self.dm)
            
        self.state = LiveState()
        self.active_pairs = [] # List of pairs currently deemed highly correlated
        self.models = {} # ML models for pairs
        
    def log_trade_csv(self, action, p_name, zscore, spread_val, leg1, t1, type1, lot1, leg2, t2, type2, lot2):
        import csv
        csv_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.csv")
        file_exists = os.path.isfile(csv_file)
        with open(csv_file, mode='a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "Action", "Pair", "ZScore", "Spread", "Leg1", "Ticket1", "Type1", "Lot1", "Leg2", "Ticket2", "Type2", "Lot2"])
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, p_name, round(zscore, 4), round(spread_val, 5), leg1, t1, type1, lot1, leg2, t2, type2, lot2])

    def start(self):
        logging.info("Starting Exness MT5 Live Bot...")
        
        if not self.dm.connect():
            logging.error("Failed to connect to MT5. Exiting.")
            return

        try:
            # First, check for exit conditions on any open positions
            self.check_exit_conditions()
            
            # Update active pairs and train models if it's time
            self.update_strategy_if_needed()
            
            # Periodically check for new entries
            while True:
                now = datetime.now(timezone.utc)
                # Align to POLL_INTERVAL (e.g. every 15 mins)
                # Next run at the next 15-min mark
                current_ts = int(now.timestamp())
                next_run_ts = (current_ts // POLL_INTERVAL_SECONDS + 1) * POLL_INTERVAL_SECONDS
                wait_time = next_run_ts - current_ts
                
                next_run_dt = datetime.fromtimestamp(next_run_ts, tz=timezone.utc).astimezone(pytz.timezone('Asia/Tokyo'))
                logging.info(f"Next cycle at {next_run_dt.strftime('%M:%S')}. Waiting {wait_time:.2f} seconds...")
                
                time.sleep(wait_time)
                
                # Execution cycle
                logging.info(f"--- Execution Cycle Starting ({datetime.now().strftime('%H:%M:%S')}) ---")
                self.check_exit_conditions()
                self.check_entry_conditions()
                
                # Check if it's time for daily re-training
                self.update_strategy_if_needed()
                
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def update_strategy_if_needed(self):
        # Implementation of pair finding and training...
        last_update = self.state.get_last_update_time()
        if last_update is None or (datetime.now() - last_update).total_seconds() > CORR_UPDATE_INTERVAL_HOURS * 3600:
            logging.info("--- Correlation & ML Training Cycle ---")
            logging.info("Fetching H1 data to find pairs...")
            
            # 1. Find correlated pairs
            # For simplicity, we use the symbols from config
            all_symbols = list(YF_TO_MT5.keys())
            
            # Fetch H1 data (e.g. 30 days)
            h1_data = {}
            for yf_sym in all_symbols:
                mt5_sym = YF_TO_MT5[yf_sym]
                df = self.dm.get_historical_data(mt5_sym, 16385, 24 * TRAIN_WINDOW_DAYS) # 16385 = H1
                if df is not None:
                    h1_data[yf_sym] = df['Close']
            
            if len(h1_data) < 2:
                logging.warning("Not enough data to find pairs.")
                return

            h1_df = pd.DataFrame(h1_data).dropna()
            # find_cointegrated_pairs returns list of dicts with keys 'a' and 'b'
            pairs_data = find_cointegrated_pairs(h1_df, min_corr=MIN_CORR_THRESHOLD)
            pairs = [(p['a'], p['b']) for p in pairs_data]
            
            logging.info(f"Found {len(pairs)} cointegrated pairs.")
            self.active_pairs = pairs
            self.state.set_active_pairs(pairs)
            
            # 2. Train ML models for each pair
            if not pairs:
                logging.info("Selected pairs for trading: []")
                return
                
            logging.info(f"Selected pairs for trading: {pairs}")
            logging.info("Fetching M15 data to train ML models...")
            
            for p1, p2 in pairs:
                # Fetch M15 data
                mt5_sym1 = YF_TO_MT5[p1]
                mt5_sym2 = YF_TO_MT5[p2]
                
                df1 = self.dm.get_historical_data(mt5_sym1, 15, 4 * 24 * TRAIN_WINDOW_DAYS) # 15 = M15
                df2 = self.dm.get_historical_data(mt5_sym2, 15, 4 * 24 * TRAIN_WINDOW_DAYS)
                
                if df1 is not None and df2 is not None:
                    common_idx = df1.index.intersection(df2.index)
                    s1 = df1.loc[common_idx, 'Close']
                    s2 = df2.loc[common_idx, 'Close']
                    
                    spread_df = calculate_spread_history(s1, s2)
                    spread_df = compute_rolling_zscore(spread_df)
                    
                    # ML Features
                    df_ml = create_ml_features(spread_df)
                    df_ml = create_labels(df_ml)
                    
                    model = train_ml_model(df_ml)
                    self.models[f"{p1}_{p2}"] = model
            
            self.state.set_last_update_time(datetime.now())

    def check_entry_conditions(self):
        if not self.active_pairs:
            logging.warning("No active pairs found. Skipping 15m cycle.")
            return
            
        for p1, p2 in self.active_pairs:
            pair_key = f"{p1}_{p2}"
            if self.state.is_pair_open(p1, p2):
                continue
                
            # Fetch latest M15 data for spread calculation
            mt5_sym1 = YF_TO_MT5[p1]
            mt5_sym2 = YF_TO_MT5[p2]
            
            df1 = self.dm.get_historical_data(mt5_sym1, 15, 100)
            df2 = self.dm.get_historical_data(mt5_sym2, 15, 100)
            
            if df1 is None or df2 is None: continue
            
            common_idx = df1.index.intersection(df2.index)
            s1 = df1.loc[common_idx, 'Close']
            s2 = df2.loc[common_idx, 'Close']
            
            spread_df = calculate_spread_history(s1, s2)
            spread_df = compute_rolling_zscore(spread_df)
            
            latest = spread_df.iloc[-1]
            zscore = latest['zscore']
            
            # Entry Logic
            action = None
            if zscore > ZSCORE_ENTRY:
                action = "SELL_SPREAD" # Sell P1, Buy P2
            elif zscore < -ZSCORE_ENTRY:
                action = "BUY_SPREAD" # Buy P1, Sell P2
                
            if action:
                # Optional: Check ML model prediction
                model = self.models.get(pair_key)
                if model:
                    ml_feat = create_ml_features(spread_df.tail(20)).iloc[-1:]
                    # Drop label if present
                    if 'target' in ml_feat.columns: ml_feat = ml_feat.drop(columns=['target'])
                    pred_prob = model.predict_proba(ml_feat)[0][1] # Prob of reverting (1)
                    
                    if pred_prob < 0.5:
                        logging.info(f"ML vetoed entry for {pair_key} (Prob: {pred_prob:.2f})")
                        continue

                # Execute Trade
                logging.info(f"ENTRY SIGNAL: {action} for {pair_key} (ZScore: {zscore:.2f})")
                res = self.executor.execute_pair_trade(p1, p2, action, RISK_USD)
                if res:
                    # Log to CSV
                    self.log_trade_csv(
                        "ENTRY_" + action, pair_key, zscore, latest['spread'],
                        p1, res['ticket1'], res['type1'], res['lot1'],
                        p2, res['ticket2'], res['type2'], res['lot2']
                    )
                    self.state.open_position(p1, p2, res)

    def check_exit_conditions(self):
        open_positions = self.state.get_open_positions()
        for pos in open_positions:
            p1, p2 = pos['p1'], pos['p2']
            pair_key = f"{p1}_{p2}"
            
            mt5_sym1 = YF_TO_MT5[p1]
            mt5_sym2 = YF_TO_MT5[p2]
            
            df1 = self.dm.get_historical_data(mt5_sym1, 15, 100)
            df2 = self.dm.get_historical_data(mt5_sym2, 15, 100)
            
            if df1 is None or df2 is None: continue
            
            common_idx = df1.index.intersection(df2.index)
            s1 = df1.loc[common_idx, 'Close']
            s2 = df2.loc[common_idx, 'Close']
            
            spread_df = calculate_spread_history(s1, s2)
            spread_df = compute_rolling_zscore(spread_df)
            
            zscore = spread_df.iloc[-1]['zscore']
            
            # Exit if zscore crosses zero or hits ZSCORE_EXIT
            should_exit = False
            original_action = pos['action']
            
            if original_action == "SELL_SPREAD" and zscore <= ZSCORE_EXIT:
                should_exit = True
            elif original_action == "BUY_SPREAD" and zscore >= -ZSCORE_EXIT:
                should_exit = True
                
            if should_exit:
                logging.info(f"EXIT SIGNAL: for {pair_key} (ZScore: {zscore:.2f})")
                res = self.executor.close_pair_trade(pos)
                if res:
                    self.log_trade_csv(
                        "EXIT", pair_key, zscore, spread_df.iloc[-1]['spread'],
                        p1, res['ticket1'], "CLOSE", 0,
                        p2, res['ticket2'], "CLOSE", 0
                    )
                    self.state.close_position(p1, p2)

if __name__ == "__main__":
    try:
        bot = LiveBot()
        bot.start()
    except Exception as e:
        logging.error(f"CRITICAL CRASH: {e}")
        logging.error(traceback.format_exc())
