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
echo "[1/3] Xvfb を起動中 (Display: ${DISPLAY:-:99})..."
export WINEDEBUG=-all
rm -f /tmp/.X${DISPLAY#:}*-lock
Xvfb ${DISPLAY:-:99} -screen 0 1024x768x16 &
XVFB_PID=$!
sleep 3
echo "      Xvfb 起動完了 (PID: $XVFB_PID)"

# ── 1.1 VNC サーバーの起動 ──────────────────────────────
VNC_PORT=${VNC_PORT:-5900}
NOVNC_PORT=${NOVNC_PORT:-6080}

echo "[1.1/3] VNC サーバーを起動中 (Port: $VNC_PORT, PW: trading)..."
x11vnc -display ${DISPLAY:-:99} -passwd trading -rfbport $VNC_PORT -forever -shared &
sleep 2

echo "[1.2/3] noVNC (Web GUI) サーバーを起動中 (Port: $NOVNC_PORT)..."
websockify --web /usr/share/novnc/ $NOVNC_PORT localhost:$VNC_PORT &
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
export DISPLAY=${DISPLAY:-:99}
# export WINEARCH=win64
export WINEPREFIX=/root/.wine
wine regedit /S /app/hide_wine.reg && wineserver -w
echo "      レジストリ適用完了"

# ── 2. MT5 ターミナルの起動（Wine経由）──────────────────────
MT5_DIR="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5"
MT5_TERMINAL="${MT5_DIR}/terminal64.exe"

if [ ! -f "$MT5_TERMINAL" ]; then
    echo "ERROR: MT5 ターミナルが見つかりません: $MT5_TERMINAL"
    echo "       Dockerfile の MT5 インストール手順を確認してください。"
    exit 1
fi

echo "[2/3] MT5 ターミナルを Wine で起動中 (EA自動セット)..."
# アップデートダイアログを物理的に無効化
rm -rf "${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/WebInstall"
EXPERTS_DIR="${MT5_DIR}/MQL5/Experts"
STARTUP_CONFIG_WINE="Z:\\app\\startup.ini"
BRIDGE_EXPERT_NAME="${EA_BRIDGE_EXPERT_NAME:-BotBridge}"
BRIDGE_SOURCE_FILE="${EA_BRIDGE_SOURCE_FILE:-}"

if [ -n "$BRIDGE_SOURCE_FILE" ] && [ -f "$BRIDGE_SOURCE_FILE" ]; then
    echo "[2/3] Installing selected bridge source: ${BRIDGE_EXPERT_NAME}"
    mkdir -p "$EXPERTS_DIR"
    cp "$BRIDGE_SOURCE_FILE" "$EXPERTS_DIR/${BRIDGE_EXPERT_NAME}.mq5"
    rm -f "$EXPERTS_DIR/${BRIDGE_EXPERT_NAME}.ex5"
elif [ -f "$EXPERTS_DIR/${BRIDGE_EXPERT_NAME}.mq5" ]; then
    echo "[2/3] Selected bridge source already exists: ${BRIDGE_EXPERT_NAME}"
elif [ "$BRIDGE_EXPERT_NAME" != "BotBridge" ]; then
    echo "WARNING: selected bridge source not found for ${BRIDGE_EXPERT_NAME}: ${BRIDGE_SOURCE_FILE}"
fi

if [ "$BRIDGE_EXPERT_NAME" != "BotBridge" ]; then
    STARTUP_SYMBOL="${EA_BRIDGE_STARTUP_SYMBOL:-GBPUSD}"
    STARTUP_PERIOD="${EA_BRIDGE_STARTUP_PERIOD:-H1}"
    cat > /tmp/startup_selected_bridge.ini <<EOF
[Experts]
Enabled=1
AllowLiveTrading=1
AllowDllImport=1

[StartUp]
Symbol=${STARTUP_SYMBOL}
Period=${STARTUP_PERIOD}
Expert=${BRIDGE_EXPERT_NAME}
ExpertParameters=
EOF
    STARTUP_CONFIG_WINE="Z:\\tmp\\startup_selected_bridge.ini"
fi
echo "[2/3] Compiling BotBridge EA..."
METAEDITOR="${WINEPREFIX}/drive_c/Program Files/MetaTrader 5/MetaEditor64.exe"
if [ -f "$METAEDITOR" ]; then
    (
        cd "${WINEPREFIX}/drive_c/Program Files/MetaTrader 5"
        timeout 90s wine MetaEditor64.exe /portable /compile:MQL5\\Experts\\BotBridge.mq5 /log:MQL5\\Experts\\BotBridge_startup_compile.log || true
        if [ "$BRIDGE_EXPERT_NAME" != "BotBridge" ] && [ -f "MQL5/Experts/${BRIDGE_EXPERT_NAME}.mq5" ]; then
            timeout 90s wine MetaEditor64.exe /portable /compile:MQL5\\Experts\\${BRIDGE_EXPERT_NAME}.mq5 /log:MQL5\\Experts\\${BRIDGE_EXPERT_NAME}_startup_compile.log || true
        fi
    )
fi
if [ "$BRIDGE_EXPERT_NAME" != "BotBridge" ] && [ ! -f "$EXPERTS_DIR/${BRIDGE_EXPERT_NAME}.ex5" ]; then
    echo "ERROR: selected bridge did not compile: ${BRIDGE_EXPERT_NAME}"
    if [ -f "$EXPERTS_DIR/${BRIDGE_EXPERT_NAME}_startup_compile.log" ]; then
        tail -n 80 "$EXPERTS_DIR/${BRIDGE_EXPERT_NAME}_startup_compile.log" || true
    fi
    exit 1
fi
echo "      BotBridge compile step finished"

DISPLAY=${DISPLAY:-:99} wine "$MT5_TERMINAL" /portable /experts /config:"${STARTUP_CONFIG_WINE}" &
MT5_PID=$!
echo "      MT5 起動完了 (PID: $MT5_PID)"
# MT5 の初期化（ブローカーログイン含む）に時間がかかるため十分に待機
sleep 30

# ── 3. コンテナの常駐化 ──────────────
echo "[3/3] 疎通確認用テストを実行し、CentOSホストからのTCP接続を待機します..."
python3 /app/test_local_connectivity.py &

tail -f /dev/null
