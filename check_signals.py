import sys
import os
from datetime import datetime, timezone, timedelta
import pandas as pd
import pytz

# Setup path to import live_data_fetcher
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
os.chdir(script_dir)

from live_data_fetcher import MT5DataManager

def check_signals():
    print("Connecting to MT5 via File IPC...")
    dm = MT5DataManager()
    if not dm.connect():
        print("Failed to connect.")
        return

    # Fetch 5m data for Copper and Silver (last 1000 bars = ~3.4 days)
    copper_df = dm.get_historical_data("XCUUSDm", 5, 1000)
    silver_df = dm.get_historical_data("XAGUSDm", 5, 1000)
    
    dm.disconnect()

    if copper_df is None or silver_df is None:
        print("Failed to get data.")
        return

    print(f"Data fetched. Copper shape: {copper_df.shape}, Silver shape: {silver_df.shape}")

    # Calculate Copper signal
    close = copper_df['Close']
    LOOKBACK_BARS = 24
    SMOOTHING_BARS = 12
    THRESHOLD_PCT = 0.5
    
    pct_120m = (close - close.shift(LOOKBACK_BARS)) / close.shift(LOOKBACK_BARS) * 100
    smoothed = pct_120m.rolling(SMOOTHING_BARS).mean()

    # Find where signal triggered
    # Crossover condition: prev <= 0.5 and curr > 0.5
    copper_df['smoothed'] = smoothed
    copper_df['signal'] = (copper_df['smoothed'] > THRESHOLD_PCT) & (copper_df['smoothed'].shift(1) <= THRESHOLD_PCT)

    triggers = copper_df[copper_df['signal']]
    
    print("\n=== Recent Signal Triggers (UTC Time) ===")
    if triggers.empty:
        print("No signals found in the fetched data.")
    else:
        for idx, row in triggers.iterrows():
            # Check if within London session (8 to 15 UTC)
            if 8 <= idx.hour < 16:
                print(f"[VALID] Triggered at {idx} UTC | Smoothed Pct: {row['smoothed']:.3f}%")
                
                # Simulate entry 30 mins later
                entry_time = idx + timedelta(minutes=30)
                exit_time = entry_time + timedelta(minutes=60)
                
                # Get nearest silver prices
                try:
                    entry_price = silver_df.iloc[silver_df.index.get_indexer([entry_time], method='nearest')[0]]['Close']
                    exit_price = silver_df.iloc[silver_df.index.get_indexer([exit_time], method='nearest')[0]]['Close']
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                    
                    print(f"   -> Simulated Entry (Silver) at {entry_time} UTC: {entry_price:.3f}")
                    print(f"   -> Simulated Exit  (Silver) at {exit_time} UTC: {exit_price:.3f}")
                    print(f"   -> Result: {pnl_pct:.2f}%")
                except Exception as e:
                    print(f"   -> Failed to simulate trade: {e}")
                print("-" * 40)
            else:
                print(f"[IGNORED - Outside London] Triggered at {idx} UTC | Smoothed Pct: {row['smoothed']:.3f}%")

if __name__ == "__main__":
    check_signals()
