# ============================================================
# Dockerfile: Exness MT5 Live Bot (Ubuntu + Wine + mt5linux)
# ============================================================
FROM ubuntu:22.04

# ── 環境変数 ────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64

# ── 必要なパッケージのインストール ──────────────────────────
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y \
        xvfb \
        wine \
        wine32 \
        wine64 \
        winetricks \
        wget \
        curl \
        python3 \
        python3-pip \
        supervisor \
        cabextract \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Python 依存ライブラリのインストール ─────────────────────
RUN pip3 install --no-cache-dir \
    mt5linux \
    pandas \
    pytz \
    scikit-learn \
    lightgbm \
    statsmodels \
    rpyc

# ── MT5のインストール (winetricks経由の確実な方法) ────────
# Xvfbを起動しながらWinetricks経由でサイレントインストール
RUN Xvfb :99 -screen 0 1024x768x16 & \
    XVFB_PID=$! && \
    sleep 2 && \
    DISPLAY=:99 WINEPREFIX=/root/.wine wine wineboot --init && \
    sleep 5 && \
    wget -q -O /tmp/mt5setup.exe "https://download.mql5.com/cdn/web/metaquotes.ltd.official/mt5/mt5setup.exe" && \
    # MT5インストーラをバックグラウンドで起動し、インストール完了を監視する
    DISPLAY=:99 WINEPREFIX=/root/.wine wine /tmp/mt5setup.exe /auto & \
    echo "Waiting for terminal64.exe to be created..." && \
    timeout 180 bash -c 'until [ -f "/root/.wine/drive_c/Program Files/MetaTrader 5/terminal64.exe" ]; do sleep 2; done' && \
    echo "terminal64.exe found! Waiting 60 seconds for installation to finalize..." && \
    sleep 60 && \
    echo "Killing wine processes..." && \
    WINEPREFIX=/root/.wine wineserver -k || true && \
    kill $XVFB_PID || true && \
    rm -f /tmp/mt5setup.exe /tmp/.X99-lock

# ── Bot ファイルのコピー ─────────────────────────────────────
WORKDIR /app
COPY . /app/
RUN chmod +x /app/entrypoint.sh

# ── 起動スクリプト ───────────────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]
