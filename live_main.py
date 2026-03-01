import time
import logging
from datetime import datetime, timezone
import pytz
import pandas as pd
import MetaTrader5 as mt5

from config import TICKERS_INFO
from live_config import YF_TO_MT5, MT5_TO_YF, USE_META_API
from live_data_fetcher import MT5DataManager
from live_executor import MT5Executor
from metaapi_bridge import MetaApiBridge
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

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
        
    def start(self):
        logging.info("Starting Exness MT5 Live Bot...")
        if not self.dm.connect():
            logging.error("Failed to connect to MT5. Exiting.")
            return

        try:
            while True:
                # Calculate sleep time to align with 00, 15, 30, 45 minute marks
                now = datetime.now()
                minutes_since_last_mark = now.minute % 15
                seconds_until_next_mark = (15 - minutes_since_last_mark) * 60 - now.second - (now.microsecond / 1_000_000)
                
                # If we are too close (e.g. within 1 second) to the mark, wait a tiny bit more 
                # to ensure the bar is actually closed on the server side.
                if seconds_until_next_mark < 1:
                    seconds_until_next_mark += 15 * 60

                logging.info(f"Next cycle at {now.minute + (15 - minutes_since_last_mark):02d}:00. Waiting {seconds_until_next_mark:.2f} seconds...")
                time.sleep(seconds_until_next_mark)

                self.run_cycle()
        except KeyboardInterrupt:
            logging.info("Bot stopped by user.")
        finally:
            self.dm.disconnect()

    def run_cycle(self):
        now = time.time()
        
        # 1. Check if we need to recalculate correlations and retrain ML models
        # Recalculate if it's been 24h OR if we simply have no active pairs yet.
        last_calc = self.state.state.get("last_calc_time")
        if not self.active_pairs or last_calc is None or (now - last_calc) > (CORR_UPDATE_INTERVAL_HOURS * 3600):
            logging.info("--- Correlation & ML Training Cycle ---")
            self.recalculate_pairs()
            self.state.state["last_calc_time"] = now
            self.state.save()

        if not self.active_pairs:
            logging.warning("No active pairs found. Skipping 15m cycle.")
            return

        logging.info("--- 15m Evaluation Cycle ---")
        # 2. Fetch 15m data for active pairs and test signals
        self.evaluate_signals()

    def fetch_all_data(self, timeframe=mt5.TIMEFRAME_H1, num_bars=500):
        data_dict = {}
        for yf_sym, mt5_sym in YF_TO_MT5.items():
            df = self.dm.get_historical_data(mt5_sym, timeframe, num_bars)
            if df is not None and len(df) > 0:
                data_dict[yf_sym] = df['Close']
        if not data_dict:
             return pd.DataFrame()
        
        # Create DataFrame and fill missing values slightly to help with correlation
        # We don't dropna() here because different assets have different trading hours.
        # pairs.py will handle pairwise NaNs for correlation.
        df_combined = pd.DataFrame(data_dict)
        return df_combined.ffill().bfill()

    def recalculate_pairs(self):
        """Find highly cointegrated pairs using H1 data and train ML models."""
        logging.info("Fetching H1 data to find pairs...")
        # Get ~30 days of H1 data (24 * 30 = 720 bars)
        df_h1 = self.fetch_all_data(mt5.TIMEFRAME_H1, 720)
        
        if df_h1.empty:
            logging.error("Could not fetch H1 data.")
            return

        # Use the existing backtest logic to find pairs
        pairs = find_cointegrated_pairs(df_h1, min_corr=MIN_CORR_THRESHOLD)
        logging.info(f"Found {len(pairs)} cointegrated pairs.")
        
        # Limit to top 5 pairs for demo safety
        self.active_pairs = pairs[:5]
        logging.info(f"Selected pairs for trading: {[p['pair'] for p in self.active_pairs]}")

        # Train ML models for these pairs using 15m data
        logging.info("Fetching M15 data to train ML models...")
        # 30 days of M15 data = 4 * 24 * 30 = 2880 bars
        df_m15 = self.fetch_all_data(mt5.TIMEFRAME_M15, 2880)

        self.models.clear()
        for pair in self.active_pairs:
            leg1 = pair['leg1']
            leg2 = pair['leg2']
            if leg1 in df_m15.columns and leg2 in df_m15.columns:
                pair_data = df_m15[[leg1, leg2]].copy()
                spread = calculate_spread_history(pair_data, leg1, leg2)
                
                features = create_ml_features(spread)
                labels = create_labels(spread, ZSCORE_ENTRY) # 1.2 target
                
                df_ml = pd.concat([features, labels], axis=1).dropna()
                X = df_ml.drop(columns=['Target'])
                y = df_ml['Target']
                
                if len(X) > 100: # Need enough data to train
                     model = train_ml_model(X, y)
                     self.models[pair['pair']] = model
                     logging.info(f"Trained ML model for {pair['pair']}")

    def evaluate_signals(self):
        """Fetch latest M15 data, check for open positions to exit, and check for new entries."""
        # 100 bars is enough for rolling z-score calculate
        df_m15 = self.fetch_all_data(mt5.TIMEFRAME_M15, 100)
        if df_m15.empty:
            return

        open_pairs = self.state.get_open_pairs()

        for pair in self.active_pairs:
            p_name = pair['pair']
            leg1 = pair['leg1']
            leg2 = pair['leg2']
            
            if leg1 not in df_m15.columns or leg2 not in df_m15.columns:
                continue
                
            pair_data = df_m15[[leg1, leg2]].copy()
            spread = calculate_spread_history(pair_data, leg1, leg2)
            zscores = compute_rolling_zscore(spread, window=48)
            
            if len(zscores) == 0:
                continue
                
            current_z = zscores.iloc[-1]
            current_spread = spread['Spread'].iloc[-1]
            
            # --- EXIT LOGIC ---
            if p_name in open_pairs:
                pos_info = open_pairs[p_name]
                entry_z = pos_info['entry_zscore']
                
                # Check reversion
                close_signal = False
                if entry_z > 0 and current_z <= ZSCORE_EXIT:
                    close_signal = True
                elif entry_z < 0 and current_z >= -ZSCORE_EXIT:
                    close_signal = True
                    
                if close_signal:
                    logging.info(f"[{p_name}] Exit signal triggered. Current Z: {current_z:.2f}")
                    # Close both legs
                    self.executor.close_position(pos_info['leg1_ticket'])
                    self.executor.close_position(pos_info['leg2_ticket'])
                    self.state.remove_open_pair(p_name)
                continue # If already open, skip entry logic

            # --- ENTRY LOGIC ---
            if abs(current_z) >= ZSCORE_ENTRY:
                # Ask ML model
                if p_name in self.models:
                    features = create_ml_features(spread).iloc[[-1]] # Get last row
                    features.fillna(0, inplace=True)
                    prob = self.models[p_name].predict_proba(features)[0][1]
                    logging.info(f"[{p_name}] Z={current_z:.2f} | ML Prob={prob:.2f}")
                    
                    if prob > 0.6: # High confidence
                        self.execute_pair_trade(pair, current_z, current_spread)
                else:
                    # Fallback to pure stat-arb if model failed to train
                    logging.info(f"[{p_name}] Z={current_z:.2f} | StatArb entry")
                    self.execute_pair_trade(pair, current_z, current_spread)

    def execute_pair_trade(self, pair, zscore, spread_val):
        p_name = pair['pair']
        leg1 = pair['leg1']
        leg2 = pair['leg2']
        
        mt5_leg1 = YF_TO_MT5.get(leg1)
        mt5_leg2 = YF_TO_MT5.get(leg2)
        
        if not mt5_leg1 or not mt5_leg2:
            return
            
        # Direction
        if zscore > 0:
            # Spread is high: short leg1, long leg2
            leg1_type = mt5.ORDER_TYPE_SELL
            leg2_type = mt5.ORDER_TYPE_BUY
        else:
            # Spread is low: long leg1, short leg2
            leg1_type = mt5.ORDER_TYPE_BUY
            leg2_type = mt5.ORDER_TYPE_SELL

        # Get positions sizes (Naive implementation for demo)
        lot1 = self.executor.calculate_lot_size(mt5_leg1, RISK_USD, 0) 
        lot2 = self.executor.calculate_lot_size(mt5_leg2, RISK_USD, 0)
        
        logging.info(f"Opening {p_name}: {mt5_leg1} ({'SELL' if leg1_type==mt5.ORDER_TYPE_SELL else 'BUY'}) & {mt5_leg2} ({'SELL' if leg2_type==mt5.ORDER_TYPE_SELL else 'BUY'})")
        
        # Execute
        ticket1 = self.executor.open_position(mt5_leg1, leg1_type, lot1)
        ticket2 = self.executor.open_position(mt5_leg2, leg2_type, lot2)
        
        if ticket1 and ticket2:
            self.state.add_open_pair(p_name, mt5_leg1, ticket1, leg1_type, mt5_leg2, ticket2, leg2_type, zscore, spread_val)
        else:
            logging.error(f"Failed to open complete pair {p_name}.")
            # Note: in real production, if one leg fails, we should immediately close the other.

if __name__ == "__main__":
    bot = LiveBot()
    bot.start()
