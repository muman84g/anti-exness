import os
import sys
import platform
from mt5_compat import mt5
from live_config import LIVE_STATE_DB

def check_environment():
    print("--- Environment Check ---")
    
    # 1. OS Check
    print(f"OS: {platform.system()} ({sys.platform})")
    if platform.system() == "Windows":
        print("[OK] Running on Windows (native MT5 mode).")
        from live_config import MT5_PATH
        print(f"\nChecking MT5 Path: {MT5_PATH}")
        if os.path.exists(MT5_PATH):
            print(f"[OK] MT5 Terminal found at {MT5_PATH}")
        else:
            print(f"[ERROR] MT5 Terminal NOT found. Please check 'live_config.py'.")
    else:
        print("[OK] Running on Linux (mt5linux mode via Wine).")
        print("     Make sure deploy/run_server.sh is running in another terminal.")

    # 2. Python Libraries
    packages_common = ["pandas", "yfinance", "pytz", "sklearn", "lightgbm", "statsmodels"]
    packages_win    = ["MetaTrader5"]
    packages_linux  = ["mt5linux"]

    print("\nChecking Python packages:")
    for pkg in packages_common:
        try:
            __import__(pkg)
            print(f"[OK]      {pkg}")
        except ImportError:
            print(f"[MISSING] {pkg}  <-- pip3 install {pkg}")

    extra_pkgs = packages_win if platform.system() == "Windows" else packages_linux
    for pkg in extra_pkgs:
        try:
            __import__(pkg)
            print(f"[OK]      {pkg}")
        except ImportError:
            print(f"[MISSING] {pkg}  <-- pip3 install {pkg}")

    # 3. State File Permissions
    print(f"\nChecking State DB Path: {LIVE_STATE_DB}")
    db_dir = os.path.dirname(LIVE_STATE_DB)
    if os.access(db_dir, os.W_OK):
        print(f"[OK] Directory '{db_dir}' is writable.")
    else:
        print(f"[ERROR] Directory '{db_dir}' is NOT writable.")

    # 4. MT5 Connection Test
    print("\nTesting MT5 Connection...")
    from live_config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
    if platform.system() == "Windows":
        from live_config import MT5_PATH
        ok = mt5.initialize(path=MT5_PATH, login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
    else:
        ok = mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)

    if not ok:
        print(f"[ERROR] Connection failed: {mt5.last_error()}")
    else:
        print("[OK] Successfully connected to MT5.")
        account_info = mt5.account_info()
        if account_info:
            print(f"     Broker: {account_info.company} / Login: {account_info.login} / Balance: {account_info.balance}")
        mt5.shutdown()

    print("\n--- Check Completed ---")

if __name__ == "__main__":
    check_environment()
