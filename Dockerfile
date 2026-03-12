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
    sleep 2 && \
    DISPLAY=:99 WINEPREFIX=/root/.wine wine wineboot --init && \
    sleep 5 && \
    wget -q -O /tmp/mt5setup.exe "https://download.mql5.com/cdn/web/metaquotes.ltd.official/mt5/mt5setup.exe" && \
    # wgetでダウンロードしたexeを wine 経由で start /wait を使って実行
    cd /tmp && DISPLAY=:99 xvfb-run -a wine cmd /c "start /wait mt5setup.exe /auto" && \
    sleep 30 && \
    rm /tmp/mt5setup.exe

# ── Bot ファイルのコピー ─────────────────────────────────────
WORKDIR /app
COPY . /app/
RUN chmod +x /app/entrypoint.sh

# ── 起動スクリプト ───────────────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]
