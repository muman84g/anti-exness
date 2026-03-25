# Removed mt5 import
import pandas as pd
from datetime import datetime, timezone
import pytz
from live_config import MT5_PATH, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
from base_interfaces import BaseDataManager
from ea_bridge import ea_bridge # New EA bridge server

class MT5DataManager(BaseDataManager):
    def __init__(self, path=MT5_PATH):
        self.path = path
        
    def connect(self) -> bool:
        # Start the Python server and wait for EA
        print("Starting TCP Server for EA Bridge...")
        ea_bridge.start_server()
        
        # Ping the EA to verify connection
        res = ea_bridge.send_command("ECHO|")
        if res == "OK|Alive":
            print("Successfully connected to MT5 EA Bridge!")
            return True
            
        print("Failed to communicate with MT5 EA Bridge.")
        return False
        
    def disconnect(self):
        if ea_bridge.server:
            ea_bridge.server.close()
        if ea_bridge.client_socket:
            ea_bridge.client_socket.close()
        
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
        for line in data_str.split(";"):
            if not line.strip(): continue
            parts = line.split(",")
            rates.append({
                "time": int(parts[0]),
                "Open": float(parts[1]),
                "High": float(parts[2]),
                "Low": float(parts[3]),
                "Close": float(parts[4]),
                "Volume": int(parts[5])
            })
            
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        return df[['Open', 'High', 'Low', 'Close', 'Volume']]

if __name__ == "__main__":
    fetcher = MT5DataManager()
    if fetcher.connect():
        df = fetcher.get_historical_data("US500m", 16385, 100) # 16385 = H1
        if df is not None:
            print("US500m H1 Data:\n", df.tail())
        fetcher.disconnect()
