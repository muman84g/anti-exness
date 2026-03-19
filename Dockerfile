# ============================================================
# Dockerfile: Exness MT5 Live Bot (Ubuntu + Wine + mt5linux)
# ============================================================
FROM ubuntu:22.04

# ── 環境変数 ────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV WINEPREFIX=/root/.wine
# WINEARCH=win64 は Wine 9.x ではデフォルトなので明示不要（指定するとwineboot失敗の原因に）
ENV PYTHONHASHSEED=0

# ── 必要なパッケージのインストール ──────────────────────────
# Wine 6.0.3 (Ubuntu default) has known IPC bugs with newer MT5.
# Use WineHQ stable (8.x+) for proper named pipe support.
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y \
        xvfb \
        wget \
        curl \
        gnupg2 \
        software-properties-common \
        python3 \
        python3-pip \
        supervisor \
        cabextract \
        unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── WineHQ 公式リポジトリから安定版 Wine (8.x系列) をインストール ──
# Wine 9.x は Docker/headless 環境での wineboot に致命的なバグがあるため 8.0 に固定します。
RUN mkdir -pm755 /etc/apt/keyrings && \
    wget -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key && \
    wget -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/jammy/winehq-jammy.sources && \
    apt-get update && \
    apt-get install -y --install-recommends winehq-stable=8.0.2~jammy wine-stable=8.0.2~jammy wine-stable-i386=8.0.2~jammy wine-stable-amd64=8.0.2~jammy winetricks && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── Python 依存ライブラリのインストール ─────────────────────
RUN pip3 install --no-cache-dir \
    mt5linux \
    pandas \
    pytz \
    scikit-learn \
    lightgbm \
    statsmodels \
    rpyc \
    metaapi_cloud_sdk

# ── Wine 初期化 + Windows Python のインストール ───────────────
# Wine 9.x: WINEDLLOVERRIDES="rpcss=" でrpcssサービス起動をスキップして初期化。
# Xvfbを手動起動してからwine コマンドを実行する。
RUN rm -f /tmp/.X99-lock && \
    Xvfb :99 -screen 0 1024x768x16 -ac & \
    sleep 5 && \
    WINEDLLOVERRIDES="rpcss=" DISPLAY=:99 wineboot --init && \
    sleep 10 && \
    WINEDLLOVERRIDES="rpcss=" DISPLAY=:99 winetricks -q win10 vcrun2015 && \
    sleep 5 && \
    mkdir -p /root/.wine/drive_c/Python39 && \
    wget -q -O /tmp/python-3.9.13-embed.zip https://www.python.org/ftp/python/3.9.13/python-3.9.13-embed-amd64.zip && \
    unzip -q /tmp/python-3.9.13-embed.zip -d /root/.wine/drive_c/Python39/ && \
    wget -q -O /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py && \
    WINEDLLOVERRIDES="rpcss=" DISPLAY=:99 wine "C:\Python39\python.exe" Z:\\tmp\\get-pip.py && \
    sed -i 's/^#import site/import site/' /root/.wine/drive_c/Python39/python39._pth && \
    rm /tmp/python-3.9.13-embed.zip /tmp/get-pip.py

# ── Wine内の Python に MetaTrader5 と rpyc をインストール ────
RUN WINEDLLOVERRIDES="rpcss=" DISPLAY=:99 wine "C:\Python39\python.exe" -m pip install --upgrade pip && \
    WINEDLLOVERRIDES="rpcss=" DISPLAY=:99 wine "C:\Python39\python.exe" -m pip install MetaTrader5 rpyc mt5linux

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
