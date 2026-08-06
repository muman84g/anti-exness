import logging

import pandas as pd
from base_interfaces import BaseDataManager
from ea_bridge import ea_bridge

try:
    from live_config import MT5_PATH, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
except Exception:
    MT5_PATH = ""
    MT5_LOGIN = 0
    MT5_PASSWORD = ""
    MT5_SERVER = ""

class MT5DataManager(BaseDataManager):
    def __init__(self, path=MT5_PATH):
        self.path = path

    def connect(self) -> bool:
        # Start the Python server and wait for EA
        print("Starting file IPC bridge for EA Bridge...")
        ea_bridge.start_server()

        # Ping the EA to verify connection
        res = ea_bridge.send_command("ECHO|")
        if res == "OK|Alive":
            print("Successfully connected to MT5 EA Bridge!")
            return True

        print("Failed to communicate with MT5 EA Bridge.")
        return False

    def disconnect(self):
        pass

    def get_historical_data(self, mt5_symbol, timeframe, num_bars, broker_timezone="UTC"):
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

        for line_no, line in enumerate(data_str.split("|"), start=1):
            line = line.strip()

            if not line:
                continue

            parts = [part.strip() for part in line.split(",")]

            # time, Open, High, Low, Close, Volume の6列が必要
            if len(parts) < 6:
                logging.warning(
                    "Skipping malformed HIST row for %s: "
                    "line_no=%s columns=%s row=%r",
                    mt5_symbol,
                    line_no,
                    len(parts),
                    line,
                )
                continue

            try:
                rates.append({
                    "time": parts[0],
                    "Open": float(parts[1]),
                    "High": float(parts[2]),
                    "Low": float(parts[3]),
                    "Close": float(parts[4]),
                    "Volume": int(float(parts[5])),
                })
            except (ValueError, TypeError, IndexError) as exc:
                logging.warning(
                    "Skipping invalid HIST row for %s: "
                    "line_no=%s row=%r error=%s",
                    mt5_symbol,
                    line_no,
                    line,
                    exc,
                )
                continue

        if not rates:
            logging.error(
                "No valid historical rows returned for %s. "
                "Raw response length=%s",
                mt5_symbol,
                len(data_str),
            )
            return None

        df = pd.DataFrame(rates)

        try:
            idx = pd.DatetimeIndex(pd.to_datetime(df['time'], format='%Y.%m.%d %H:%M'))
        except ValueError:
            idx = pd.DatetimeIndex(pd.to_datetime(df['time']))
        try:
            if idx.tz is None:
                idx = idx.tz_localize(str(broker_timezone), ambiguous="infer", nonexistent="shift_forward")
            idx = idx.tz_convert("UTC")
        except Exception as exc:
            logging.error(
                "Failed to localize MT5 bar timestamps for %s using timezone %s: %s",
                mt5_symbol,
                broker_timezone,
                exc,
            )
            return None
        df.index = idx
        return df[['Open', 'High', 'Low', 'Close', 'Volume']]

if __name__ == "__main__":
    fetcher = MT5DataManager()
    if fetcher.connect():
        df = fetcher.get_historical_data("XAUUSD", 16385, 100)
        if df is not None:
            print("XAUUSD H1 Data:\n", df.tail())
        fetcher.disconnect()
