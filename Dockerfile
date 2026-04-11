# ============================================================
# Dockerfile: Exness MT5 Live Bot (Ubuntu + Wine + MT5)
# ============================================================
FROM ubuntu:24.04

# ── 環境変数 ────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV WINEPREFIX=/root/.wine
#ENV WINEARCH=win64
ENV PYTHONHASHSEED=0
ENV WINEDEBUG=-all
ENV WINEDLLOVERRIDES="mscoree,mshtml=;winedbg.exe=d"

# ── 基本パッケージのインストール ─────────────────────────────
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        software-properties-common wget curl gnupg2 ca-certificates \
        xvfb winetricks python3 python3-pip supervisor cabextract unzip imagemagick \
        wine64 wine32 x11vnc xdotool novnc websockify net-tools && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ── 安定版 Wine 9.0 (Ubuntu標準) の利用 ──────────────────
# WineHQリポジトリはNobleでの整合性が低いため、OS標準パッケージを優先
# 必要に応じて明示的なダウングレードが必要な場合はここに記述するが、
# 現時点では noble 標準の 9.0 を利用

# ── アプリケーションファイルの準備 ──────────────────────────
WORKDIR /app
COPY . /app/

# ── Python インストーラと埋め込みZIPの取得 ───────────────────
RUN wget -q -O /tmp/python-3.9.13-embed.zip https://www.python.org/ftp/python/3.9.13/python-3.9.13-embed-amd64.zip && \
    wget -q -O /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py

# ── 【一括セットアップ】Wine環境・Python・依存関係・EAコンパイル ──
# 複数の RUN 命令で Xvfb を抜き差しすると不安定になるため、1つのセッションで完結させる
RUN xvfb-run --auto-servernum --server-args="-screen 0 1024x768x24" sh -c "\
    echo '--- Initializing Wine Prefix (Modern) ---' && \
    rm -rf /root/.wine && \
    wineboot --init && wineserver -w && \
    \
    echo '--- Installing MT5 Files into Prefix ---' && \
    mkdir -p \"/root/.wine/drive_c/Program Files/MetaTrader 5\" && \
    cp -r \"/app/MetaTrader 5/.\" \"/root/.wine/drive_c/Program Files/MetaTrader 5/\" && \
    \
    echo '--- Installing Windows Runtimes ---' && \
    winetricks -q win7 vcrun2015 && wineserver -w && \
    \
    echo '--- Setting up Python 3.9 (Embed) ---' && \
    mkdir -p /root/.wine/drive_c/Python39 && \
    unzip -q /tmp/python-3.9.13-embed.zip -d /root/.wine/drive_c/Python39/ && \
    wine 'C:/Python39/python.exe' 'Z:/tmp/get-pip.py' && wineserver -w && \
    sed -i 's/^#import site/import site/' /root/.wine/drive_c/Python39/python39._pth && \
    \
    echo '--- Installing Python Dependencies ---' && \
    wine 'C:/Python39/python.exe' -m pip install --upgrade pip && \
    wine 'C:/Python39/python.exe' -m pip install \
        MetaTrader5==5.0.43 \"numpy<2\" pandas pytz scikit-learn lightgbm statsmodels rpyc mt5linux && \
    wineserver -w && \
    \
    echo '--- Unified Setup Complete ---' && \
    wineserver -w"

# ── Bot起動用ランチャースクリプトの設定 ──────────────────────
RUN echo 'import sys, os' > /root/.wine/drive_c/Python39/launcher.py && \
    echo 'sys.path.insert(0, r"Z:\\app")' >> /root/.wine/drive_c/Python39/launcher.py && \
    echo 'os.chdir(r"Z:\\app")' >> /root/.wine/drive_c/Python39/launcher.py && \
    echo 'exec(open(r"Z:\\app\\live_main.py", encoding="utf-8").read())' >> /root/.wine/drive_c/Python39/launcher.py

RUN chmod +x /app/entrypoint.sh && \
    rm /tmp/python-3.9.13-embed.zip /tmp/get-pip.py

# ── 最終構成 ───────────────────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]
