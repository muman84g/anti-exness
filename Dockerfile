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

# ── Windows Python のインストール (Wine内) ─────────────────────
# mt5linux は Wine 内の Windows Python で動作する MetaTrader5 ライブラリと通信するため必須です
RUN wget -q -O /tmp/python-installer.exe https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe && \
    xvfb-run -a wine /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 && \
    sleep 30 && \
    rm /tmp/python-installer.exe

# ── Wine内の Python に MetaTrader5 と rpyc をインストール ────
RUN xvfb-run -a wine "C:\Program Files\Python310\python.exe" -m pip install --upgrade pip && \
    xvfb-run -a wine "C:\Program Files\Python310\python.exe" -m pip install MetaTrader5 rpyc

# ── MT5の事前インストール済みディレクトリのコピー ────────
# MT5のサイレントインストーラはWine上で動作が非常に不安定なため、
# Windows側で既にインストール済みの「MetaTrader 5」フォルダをコンテナに直接コピーします。
RUN mkdir -p "/root/.wine/drive_c/Program Files/MetaTrader 5"

# ── Bot ファイルのコピー ─────────────────────────────────────
WORKDIR /app
COPY . /app/
# Windows側から持ってきたMT5本体をWine構成内に配置
COPY ["MetaTrader 5", "/root/.wine/drive_c/Program Files/MetaTrader 5/"]
RUN chmod +x /app/entrypoint.sh

# ── 起動スクリプト ───────────────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]
