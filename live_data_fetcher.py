import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone
import pytz
from live_config import MT5_PATH
from base_interfaces import BaseDataManager

class MT5DataManager(BaseDataManager):
    def __init__(self, path=MT5_PATH):
        self.path = path
        
    def connect(self) -> bool:
        if not mt5.initialize(path=self.path):
            print(f"MT5 initialize failed: {mt5.last_error()}")
            return False
        return True
        
    def disconnect(self):
        mt5.shutdown()
        
    def get_historical_data(self, mt5_symbol, timeframe, num_bars):
        """
        Fetch historical bars from MT5.
        `timeframe`: mt5.TIMEFRAME_H1 or mt5.TIMEFRAME_M15
        """
        # Select symbol
        if not mt5.symbol_select(mt5_symbol, True):
            print(f"Symbol {mt5_symbol} not found or failed to select.")
            return None
            
        rates = mt5.copy_rates_from_pos(mt5_symbol, timeframe, 0, num_bars)
        if rates is None or len(rates) == 0:
            return None
            
        # Convert to pandas DataFrame
        df = pd.DataFrame(rates)
        
        # MT5 time is in seconds, convert to datetime
        # MT5 usually uses its server time. We'll leave it as naive or localize it appropriately.
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        
        # Keep consistent names with our backtest code
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'tick_volume': 'Volume'}, inplace=True)
        
        return df[['Open', 'High', 'Low', 'Close', 'Volume']]

if __name__ == "__main__":
    fetcher = MT5DataManager()
    if fetcher.connect():
        df = fetcher.get_historical_data("US500m", mt5.TIMEFRAME_H1, 100)
        print("US500m H1 Data:\n", df.tail())
        fetcher.disconnect()
