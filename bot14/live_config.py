import os
import platform

# ── MT5 Details ─────────────────────────────────────────────────────────────
if platform.system() == "Windows":
    MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
else:
    MT5_PATH = os.path.expanduser("~/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe")

# ── MT5 Credentials ─────────────────────────────────────────────────────────
# 【注意】実運用するプロ口座（リアル口座）の接続情報を正しく設定してください。
# ※デフォルトは以前のテスト用デモ口座情報になっています。
MT5_LOGIN    = 277474586
MT5_PASSWORD = "@Soccer84g"
MT5_SERVER   = "Exness-MT5Trial5"  # 本番リアル口座の場合は例: "Exness-MT5Real10" に変更

# ── Local State Database ────────────────────────────────────────────────────
LIVE_STATE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_trades.json")

# ── Symbol Trade Lot Limits (Min Lot Overrides) ──────────────────────────────
MIN_LOT_OVERRIDES = {
    "GBPUSDm": 0.01,
}
