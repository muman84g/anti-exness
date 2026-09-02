"""Credential-free example; the live module reads host environment variables."""

import os


MT5_LOGIN = int(os.environ.get("BOT24_MT5_LOGIN", "0"))
MT5_PASSWORD = os.environ.get("BOT24_MT5_PASSWORD", "")
MT5_SERVER = os.environ.get("BOT24_MT5_SERVER", "UNCONFIGURED")

MIN_LOT_OVERRIDES = {
    "XAUUSD": 0.01,
}
