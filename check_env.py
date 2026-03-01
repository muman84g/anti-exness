import MetaTrader5 as mt5
import os
import sys
from live_config import MT5_PATH, LIVE_STATE_DB

def check_environment():
    print("--- ConoHa Server Environment Check ---")
    
    # 1. OS Check
    print(f"OS: {sys.platform}")
    if sys.platform != "win32":
        print("[ERROR] MetaTrader5 library only works on Windows.")
    else:
        print("[OK] Running on Windows.")

    # 2. Python Libraries
    packages = ["pandas", "MetaTrader5", "yfinance", "pytz", "sklearn", "lightgbm"]
    print("\nChecking Python packages:")
    for package in packages:
        try:
            __import__(package)
            print(f"[OK] {package} is installed.")
        except ImportError:
            print(f"[MISSING] {package} is not installed.")

    # 3. MT5 Path Check
    print(f"\nChecking MT5 Path: {MT5_PATH}")
    if os.path.exists(MT5_PATH):
        print(f"[OK] MT5 Terminal found at {MT5_PATH}")
    else:
        print(f"[ERROR] MT5 Terminal NOT found. Please check 'live_config.py'.")

    # 4. State File Permissions
    print(f"\nChecking State DB Path: {LIVE_STATE_DB}")
    db_dir = os.path.dirname(LIVE_STATE_DB)
    if os.access(db_dir, os.W_OK):
        print(f"[OK] Directory '{db_dir}' is writable.")
    else:
        print(f"[ERROR] Directory '{db_dir}' is NOT writable.")

    # 5. MT5 Connection Test
    print("\nTesting MT5 Connection...")
    if not mt5.initialize(path=MT5_PATH):
        print(f"[ERROR] Connection failed: {mt5.last_error()}")
    else:
        print("[OK] Successfully connected to MT5.")
        account_info = mt5.account_info()
        if account_info:
            print(f"Connected to: {account_info.company} (Login: {account_info.login})")
        mt5.shutdown()

    print("\n--- Check Completed ---")

if __name__ == "__main__":
    check_environment()
