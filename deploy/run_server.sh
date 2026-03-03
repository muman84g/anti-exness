#!/bin/bash
# =============================================================================
# run_server.sh  --  Wine 内の MT5 から mt5linux ソケットサーバーを起動する
# =============================================================================
# 別ターミナルでこのスクリプトを起動してから、live_main.py を実行してください。
# =============================================================================

export DISPLAY=:99

# Xvfb（仮想ディスプレイ）が起動していなければ起動
if ! pgrep -x Xvfb > /dev/null; then
    echo "Xvfb を起動しています..."
    Xvfb :99 -screen 0 1024x768x16 &
    sleep 2
fi

echo "mt5linux のソケットサーバーを起動しています..."
echo "（別ターミナルで live_main.py を実行してください）"
echo "（Ctrl+C でサーバーを停止します）"
echo ""

# mt5linux はこのコマンドで Wine 内の MT5 と通信するブリッジサーバーを起動する
python3 -c "
from mt5linux import MetaTrader5
mt5 = MetaTrader5(host='localhost', port=18812)
print('サーバー起動中... (Ctrl+C で停止)')
import time
try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    print('サーバーを停止しました。')
"
