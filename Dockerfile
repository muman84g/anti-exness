# ============================================================
# Dockerfile: Exness MT5 Live Bot (Ubuntu + Wine + mt5linux)
# ============================================================
FROM ubuntu:22.04

# ── 環境変数 ────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64
ENV PYTHONHASHSEED=0

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
        unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Windows Python のインストール (Wine内) ─────────────────────
# embeddable zip版を直接展開 + pip セットアップ
RUN xvfb-run -a wine wineboot --init && \
    sleep 5 && \
    xvfb-run -a winetricks -q win10 vcrun2015 && \
    sleep 5 && \
    mkdir -p /root/.wine/drive_c/Python39 && \
    wget -q -O /tmp/python-3.9.13-embed.zip https://www.python.org/ftp/python/3.9.13/python-3.9.13-embed-amd64.zip && \
    unzip -q /tmp/python-3.9.13-embed.zip -d /root/.wine/drive_c/Python39/ && \
    wget -q -O /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py && \
    xvfb-run -a wine "C:\Python39\python.exe" Z:\\tmp\\get-pip.py && \
    sed -i 's/^#import site/import site/' /root/.wine/drive_c/Python39/python39._pth && \
    rm /tmp/python-3.9.13-embed.zip /tmp/get-pip.py

# ── Bot起動用ランチャースクリプト ────────────────────────────
# Wine embedded Python は ._pth / PYTHONPATH でのパス追加が不安定なため、
# ランチャースクリプトで sys.path を明示的に設定してから Bot を起動する。
RUN echo 'import sys, os' > /root/.wine/drive_c/Python39/launcher.py && \
    echo 'sys.path.insert(0, r"Z:\\app")' >> /root/.wine/drive_c/Python39/launcher.py && \
    echo 'os.chdir(r"Z:\\app")' >> /root/.wine/drive_c/Python39/launcher.py && \
    echo 'exec(open(r"Z:\\app\\live_main.py", encoding="utf-8").read())' >> /root/.wine/drive_c/Python39/launcher.py

# ── Wine内の Python に全ての依存ライブラリをインストール ──────
# Bot本体を直接Wine Pythonで実行するため、全依存をここにインストール
RUN xvfb-run -a wine "C:\Python39\python.exe" -m pip install --upgrade pip && \
    xvfb-run -a wine "C:\Python39\python.exe" -m pip install \
        MetaTrader5==5.0.43 \
        pandas \
        pytz \
        scikit-learn \
        lightgbm \
        statsmodels \
        rpyc \
        mt5linux

# ── MT5の事前インストール済みディレクトリのコピー ────────
RUN mkdir -p "/root/.wine/drive_c/Program Files/MetaTrader 5"

# ── Bot ファイルのコピー ─────────────────────────────────────
WORKDIR /app
COPY . /app/
# Windows側から持ってきたMT5本体をWine構成内に配置
COPY ["MetaTrader 5", "/root/.wine/drive_c/Program Files/MetaTrader 5/"]
RUN chmod +x /app/entrypoint.sh

# ── 起動スクリプト ───────────────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]
