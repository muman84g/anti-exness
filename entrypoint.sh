#!/bin/bash
# ============================================================
# entrypoint.sh: Exness MT5 Bot 起動スクリプト
# ============================================================
# 起動順序:
#   1. Xvfb（仮想ディスプレイ）
#   2. MT5 ターミナル（Wine経由、/portable モード）
#   3. live_main.py（Wine Python で直接実行）
# ============================================================

set -e

echo "======================================"
echo "  Exness MT5 Bot コンテナ起動"
echo "======================================"

# ── 1. Xvfb（仮想ディスプレイ）の起動 ──────────────────────
echo "[1/3] Xvfb を起動中..."
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x16 &
XVFB_PID=$!
sleep 3
echo "      Xvfb 起動完了 (PID: $XVFB_PID)"

# ── 2. MT5 ターミナルの起動（Wine経由）──────────────────────
MT5_TERMINAL="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/terminal64.exe"

if [ ! -f "$MT5_TERMINAL" ]; then
    echo "ERROR: MT5 ターミナルが見つかりません: $MT5_TERMINAL"
    echo "       Dockerfile の MT5 インストール手順を確認してください。"
    exit 1
fi

echo "[2/3] MT5 ターミナルを Wine で起動中..."
DISPLAY=:99 wine "$MT5_TERMINAL" /portable &
MT5_PID=$!
echo "      MT5 起動完了 (PID: $MT5_PID)"
# MT5 の初期化（ブローカーログイン含む）に時間がかかるため十分に待機
sleep 120

# ── 3. コンテナの常駐化 ──────────────
echo "[3/3] Dockerコンテナを常駐化し、CentOSホストからのTCP接続を待機します..."
tail -f /dev/null
