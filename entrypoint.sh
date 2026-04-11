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
export WINEDEBUG=-all
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x16 &
XVFB_PID=$!
sleep 3
echo "      Xvfb 起動完了 (PID: $XVFB_PID)"

# ── 1.1 VNC サーバーの起動 ──────────────────────────────
echo "[1.1/3] VNC サーバーを起動中 (PW: trading)..."
x11vnc -display :99 -passwd trading -rfbport 5900 -forever -shared &
sleep 2

echo "[1.2/3] noVNC (Web GUI) サーバーを起動中..."
websockify --web /usr/share/novnc/ 6080 localhost:5900 &
echo "      VNC 起動完了"

# ── 1.2 winedbg の物理的な無効化 ───────────────────────────
echo "[1.2/3] winedbg を物理的に無効化中..."
find /usr -name "*winedbg*" -exec mv {} {}.bak \; 2>/dev/null || true
find /opt/wine-staging -name "*winedbg*" -exec mv {} {}.bak \; 2>/dev/null || true
# WinePrefix内の実体も無効化
if [ -f "$WINEPREFIX/drive_c/windows/system32/winedbg.exe" ]; then
    mv "$WINEPREFIX/drive_c/windows/system32/winedbg.exe" "$WINEPREFIX/drive_c/windows/system32/winedbg.exe.bak"
fi
echo "      winedbg 無効化完了"

# ── 1.5 Wine レジストリ調整（アンチデバッグ回避） ───────────
echo "[1.5/3] Wine レジストリ調整を適用中..."
export DISPLAY=:99
# export WINEARCH=win64
export WINEPREFIX=/root/.wine
wine regedit /S /app/hide_wine.reg && wineserver -w
echo "      レジストリ適用完了"

# ── 2. MT5 ターミナルの起動（Wine経由）──────────────────────
MT5_TERMINAL="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/terminal64.exe"

if [ ! -f "$MT5_TERMINAL" ]; then
    echo "ERROR: MT5 ターミナルが見つかりません: $MT5_TERMINAL"
    echo "       Dockerfile の MT5 インストール手順を確認してください。"
    exit 1
fi

echo "[2/3] MT5 ターミナルを Wine で起動中 (EA自動セット)..."
# アップデートダイアログを物理的に無効化
rm -rf "${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/WebInstall"
DISPLAY=:99 wine "$MT5_TERMINAL" /portable /experts /config:Z:\\app\\startup.ini &
MT5_PID=$!
echo "      MT5 起動完了 (PID: $MT5_PID)"
# MT5 の初期化（ブローカーログイン含む）に時間がかかるため十分に待機
sleep 30

# ── 3. コンテナの常駐化 ──────────────
echo "[3/3] 疎通確認用テストを実行し、CentOSホストからのTCP接続を待機します..."
python3 /app/test_local_connectivity.py &

tail -f /dev/null
