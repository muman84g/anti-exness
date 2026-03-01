import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import time
from live_config import MT5_PATH, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER

def test_mt5_connection():
    # Attempt to initialize MT5 with explicit path
    print("Initialize MT5...")
    if not mt5.initialize(path=MT5_PATH, login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        print(f"initialize() failed, error code = {mt5.last_error()}")
        return
    
    # Check version and account info
    print(f"MT5 version: {mt5.version()}")
    account_info = mt5.account_info()
    if account_info is None:
        print("Failed to get account info. Please make sure you are logged into the Exness Demo account in the MT5 Terminal.")
        mt5.shutdown()
        return

    print(f"Connected successfully!")
    print(f"Broker: {account_info.company}")
    print(f"Account Server: {account_info.server}")
    print(f"Account Login: {account_info.login}")
    print(f"Balance: {account_info.balance} {account_info.currency}")
    
    # Test symbols for Standard Exness Demo (usually end with 'm')
    test_symbols = ["US500m", "USDJPYm", "BTCUSDm", "XAUUSDm"]
    print("\nTesting data fetching for some symbols...")

    for symbol in test_symbols:
        # We need to select the symbol in Market Watch first
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            print(f"Symbol {symbol} not found. Is the symbol correct for your account type?")
            continue
            
        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                print(f"Failed to fetch {symbol}")
                continue
                
        # Get latest tick
        tick = mt5.symbol_info_tick(symbol)
        if tick:
            print(f"[{symbol}] Latest Bid: {tick.bid}, Ask: {tick.ask}")
        else:
            print(f"[{symbol}] Failed to get tick data.")

    print("\nShutting down MT5 connection...")
    mt5.shutdown()

if __name__ == "__main__":
    test_mt5_connection()
