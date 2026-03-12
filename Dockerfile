# ============================================================
# Dockerfile: Exness MT5 Live Bot (Ubuntu + Wine + mt5linux)
# ============================================================
# ベースイメージ: Ubuntu 22.04
# 理由: wine.i686 (32bit) が使えるため CentOS の wow64 問題を回避できる
FROM ubuntu:22.04

# ── 環境変数 ────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win64

# ── 必要なパッケージのインストール ──────────────────────────
# Wine のインストールに 32bit アーキテクチャのサポートが必要
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y \
        # 仮想ディスプレイ (MT5 は GUI アプリのため必須)
        xvfb \
        # Wine (32bit サポート込み)
        wine \
        wine32 \
        wine64 \
        winetricks \
        # ダウンロード・ユーティリティ
        wget \
        curl \
        # Python 3
        python3 \
        python3-pip \
        # プロセス管理
        supervisor \
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

# ── MT5 インストーラーのダウンロードと Wine へのインストール ─
# Xvfb が必要なため RUN で一時的に起動する
RUN Xvfb :99 -screen 0 1024x768x16 &\
    sleep 2 && \
    # Wine を初期化 (初回起動時の設定を自動化)
    DISPLAY=:99 WINEPREFIX=/root/.wine wine wineboot --init 2>/dev/null || true && \
    sleep 5 && \
    # MT5 インストーラーをダウンロード
    wget -q -O /tmp/mt5setup.exe "https://download.mql5.com/cdn/web/metaquotes.ltd.official/mt5/mt5setup.exe" && \
    # MT5 をサイレントインストール
    DISPLAY=:99 wine /tmp/mt5setup.exe /auto && \
    sleep 15 && \
    rm /tmp/mt5setup.exe

# ── Bot ファイルのコピー ─────────────────────────────────────
WORKDIR /app
COPY . /app/

# entrypoint.sh に実行権限を付与
RUN chmod +x /app/entrypoint.sh

# ── 起動スクリプト ───────────────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]
