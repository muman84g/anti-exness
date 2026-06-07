import os
import time
import pandas as pd
from datetime import datetime, timezone
import pytz
import logging
from live_config import MT5_PATH, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
from base_interfaces import BaseDataManager
from ea_bridge import ea_bridge

class MT5DataManager(BaseDataManager):
    def __init__(self, path=MT5_PATH):
        self.path = path
        
    def bridge_heartbeat_age_seconds(self):
        heartbeat_file = getattr(ea_bridge, "heartbeat_file", None)
        if not heartbeat_file or not os.path.exists(heartbeat_file):
            return None
        try:
            return time.time() - os.path.getmtime(heartbeat_file)
        except OSError:
            return None

    def connect(self) -> bool:
        # Start the file IPC bridge and wait for EA.
        ea_bridge.start_server()

        for attempt in range(1, 6):
            res = ea_bridge.send_command("ECHO|", timeout=10)
            if res == "OK|Alive":
                logging.info("Successfully connected to MT5 EA Bridge.")
                return True

            hb_age = self.bridge_heartbeat_age_seconds()
            hb_status = "missing" if hb_age is None else f"{hb_age:.1f}s old"
            logging.warning(
                "EA Bridge ping failed attempt %d/5: response=%s heartbeat=%s",
                attempt,
                res,
                hb_status,
            )
            time.sleep(3)

        logging.error("Failed to communicate with MT5 EA Bridge after retries.")
        return False
        
    def disconnect(self):
        pass
        
    def get_historical_data(self, mt5_symbol, timeframe, num_bars):
        """
        Fetch historical bars from MT5 via EA Bridge.
        `timeframe`: standard mt5 integer like 16385 for H1
        """
        # Send HIST request
        res = ea_bridge.send_command(f"HIST|{mt5_symbol}|{timeframe}|{num_bars}")
        
        if not res or not res.startswith("OK|"):
            print(f"EA failed to get historical data for {mt5_symbol}: {res}")
            return None
            
        data_str = res[3:] # drop "OK|"
        if not data_str:
            return None
            
        rates = []
        for line in data_str.split("|"):
            if not line.strip(): continue
            parts = line.split(",")
            rates.append({
                "time": parts[0],
                "Open": float(parts[1]),
                "High": float(parts[2]),
                "Low": float(parts[3]),
                "Close": float(parts[4]),
                "Volume": int(parts[5])
            })
            
        df = pd.DataFrame(rates)
        try:
            df['time'] = pd.to_datetime(df['time'], format='%Y.%m.%d %H:%M')
        except ValueError:
            df['time'] = pd.to_datetime(df['time'])
        df.set_index('time', inplace=True)
        df = df[~df.index.duplicated(keep='last')].sort_index()
        if not df.empty:
            logging.info(
                "HIST %s tf=%s bars=%d range=%s -> %s",
                mt5_symbol,
                timeframe,
                len(df),
                df.index[0],
                df.index[-1],
            )
        return df[['Open', 'High', 'Low', 'Close', 'Volume']]

if __name__ == "__main__":
    fetcher = MT5DataManager()
    if fetcher.connect():
        df = fetcher.get_historical_data("US500m", 16385, 100)
        if df is not None:
            print("US500m H1 Data:\n", df.tail())
        fetcher.disconnect()
