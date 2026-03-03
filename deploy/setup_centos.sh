#!/bin/bash
# =============================================================================
# setup_centos.sh  --  Exness MT5 Bot: CentOS 7/8 セットアップスクリプト
# =============================================================================
# 実行方法:
#   chmod +x setup_centos.sh
#   sudo bash setup_centos.sh
# =============================================================================

set -e  # エラーが出たら即終了

echo "========================================"
echo "  Exness Bot CentOS セットアップ開始"
echo "========================================"

# --- 1. 基本パッケージのインストール ---
echo "[1/7] システムパッケージのインストール..."
yum update -y
yum groupinstall -y "Development Tools"
yum install -y wget curl git \
    xorg-x11-server-Xvfb \
    cabextract \
    xdg-utils \
    python3 python3-pip

# --- 2. Wine のインストール ---
echo "[2/7] Wine のインストール..."
yum install -y epel-release
yum install -y wine

# Wine のバージョン確認
wine --version

# --- 3. 仮想ディスプレイ (Xvfb) の設定 ---
# MT5 は GUI アプリのため、仮想ディスプレイが必要
echo "[3/7] 仮想ディスプレイ (Xvfb) の設定..."
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x16 &
sleep 2

# --- 4. Wine の初期化 ---
echo "[4/7] Wine の初期化..."
WINEPREFIX=~/.wine DISPLAY=:99 winecfg /v win10 2>/dev/null || true
sleep 3

# --- 5. mt5linux のインストール ---
echo "[5/7] mt5linux のインストール..."
pip3 install mt5linux

# --- 6. Bot の依存ライブラリのインストール ---
echo "[6/7] Bot の依存ライブラリのインストール..."
pip3 install pandas pytz scikit-learn lightgbm yfinance statsmodels

# --- 7. MetaTrader 5 (Windows版) のダウンロードと Wine へのインストール ---
echo "[7/7] MetaTrader 5 のインストール..."
MT5_INSTALLER="mt5setup.exe"
MT5_DOWNLOAD_URL="https://download.mql5.com/cdn/web/metaquotes.ltd.official/mt5/mt5setup.exe"

if [ ! -f "$MT5_INSTALLER" ]; then
    echo "MT5インストーラーをダウンロード中..."
    wget -O "$MT5_INSTALLER" "$MT5_DOWNLOAD_URL"
fi

DISPLAY=:99 wine "$MT5_INSTALLER" /auto
sleep 10

echo ""
echo "========================================"
echo "  セットアップ完了！"
echo "========================================"
echo ""
echo "次のステップ:"
echo "  1. MT5 を起動して Exness アカウントにログインしてください:"
echo "     DISPLAY=:99 wine ~/.wine/drive_c/Program\\ Files/MetaTrader\\ 5/terminal64.exe &"
echo ""
echo "  2. MT5 にログイン後、接続テストを実行してください:"
echo "     python3 test_mt5linux.py"
echo ""
echo "  3. 接続が確認できたらボットを起動:"
echo "     python3 live_main.py"
