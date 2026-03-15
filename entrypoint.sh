#!/bin/bash
# ============================================================
# entrypoint.sh: Exness MT5 Bot 起動スクリプト
# ============================================================
# 起動順序:
#   1. Xvfb（仮想ディスプレイ）
#   2. MT5 ターミナル（Wine経由）
#   3. mt5linux rpyc サーバー（MT5とPythonのブリッジ）
#   4. live_main.py（Bot本体）
# ============================================================

set -e

echo "======================================"
echo "  Exness MT5 Bot コンテナ起動"
echo "======================================"

# ── 1. Xvfb（仮想ディスプレイ）の起動 ──────────────────────
echo "[1/4] Xvfb を起動中..."
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x16 &
XVFB_PID=$!
sleep 3
echo "      Xvfb 起動完了 (PID: $XVFB_PID)"

# ── 2. MT5 ターミナルの起動（Wine経由）──────────────────────
# MT5_TERMINAL_PATH: Wine でインストールされた MT5 の実行ファイル
MT5_TERMINAL="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/terminal64.exe"

if [ ! -f "$MT5_TERMINAL" ]; then
    echo "ERROR: MT5 ターミナルが見つかりません: $MT5_TERMINAL"
    echo "       Dockerfile の MT5 インストール手順を確認してください。"
    exit 1
fi

echo "[2/4] MT5 ターミナルを Wine で起動中..."
DISPLAY=:99 wine "$MT5_TERMINAL" &
MT5_PID=$!
echo "      MT5 起動完了 (PID: $MT5_PID)"
# MT5 の初期化に時間がかかるため待機
sleep 20

# ── 3. mt5linux rpyc サーバーの起動 ─────────────────────────
# Python(Linux)からのリクエストを受け付けるため、Wine環境内のWindows用Pythonで
# mt5linux のバックグラウンドサーバーを立ち上げます。
echo "[3/4] mt5linux rpyc サーバーを Wine 内で起動中..."
DISPLAY=:99 WINEPREFIX=/root/.wine wine "C:\users\root\AppData\Local\Programs\Python\Python310\python.exe" -m mt5linux &
MT5LINUX_PID=$!
sleep 10
echo "      mt5linux サーバー起動完了 (PID: $MT5LINUX_PID)"
echo "      --- mt5linux server logs ---"
cat /tmp/mt5linux_server.log
echo "      ----------------------------"

# ── 4. Bot 本体の起動 ────────────────────────────────────────
echo "[4/4] live_main.py を起動中..."
cd /app
exec python3 live_main.py
