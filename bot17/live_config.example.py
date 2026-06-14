import os
import platform


if platform.system() == "Windows":
    MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
else:
    MT5_PATH = os.path.expanduser("~/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe")

# Keep credentials out of this repository. The current EA bridge works by file IPC
# and does not use these values, but live_data_fetcher imports the names.
MT5_LOGIN = int(os.environ.get("BOT17_MT5_LOGIN", "0") or "0")
MT5_PASSWORD = os.environ.get("BOT17_MT5_PASSWORD", "")
MT5_SERVER = os.environ.get("BOT17_MT5_SERVER", "")

MIN_LOT_OVERRIDES = {
    "GBPUSD": 0.01,
    "USDJPY": 0.01,
}
