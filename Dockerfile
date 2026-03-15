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
# exeインストーラはWine上でサイレント失敗しやすいため、embeddable zip版を直接展開します
# Python 3.10以上はWineのCryptGenRandomバグの影響を受けやすいため、安定した3.9.13を使用します
# PythonのC-runtime依存(ucrt)を解決するため、先にwinetricksでwin10環境とvcrun2015をインストールします
RUN xvfb-run -a wine wineboot --init && \
    sleep 5 && \
    xvfb-run -a winetricks -q win10 vcrun2015 && \
    sleep 5 && \
    mkdir -p /root/.wine/drive_c/Python39 && \
    wget -q -O /tmp/python-3.9.13-embed.zip https://www.python.org/ftp/python/3.9.13/python-3.9.13-embed-amd64.zip && \
    unzip -q /tmp/python-3.9.13-embed.zip -d /root/.wine/drive_c/Python39/ && \
    # get-pip.py を使って pip をインストール
    wget -q -O /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py && \
    xvfb-run -a wine "C:\Python39\python.exe" Z:\\tmp\\get-pip.py && \
    # embed用設定変更（pipが動作するようにpython39._pthのコメントアウトを外す）
    sed -i 's/^#import site/import site/' /root/.wine/drive_c/Python39/python39._pth && \
    rm /tmp/python-3.9.13-embed.zip /tmp/get-pip.py

# ── Wine内の Python に MetaTrader5 と rpyc をインストール ────
RUN xvfb-run -a wine "C:\Python39\python.exe" -m pip install --upgrade pip && \
    xvfb-run -a wine "C:\Python39\python.exe" -m pip install MetaTrader5 rpyc mt5linux

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
