# Project Handover: Exness Live Bot

## 現在の進捗状況
- **MT5 (Windows)**: Windows環境での動作確認済み。
- **MetaApi Bridge**: `metaapi_bridge.py` 実装済み（ただし月額$30のため保留）。
- **mt5linux方式**: CentOS 9 の Wine 8.0 で `wow64` 問題により断念。
  - 原因: CentOS 9 は wine.i686 (32bit) パッケージを提供しておらず、Wine の wow64 モードが動作しない。
- **Git管理**: ローカルリポジトリ初期化・`.gitignore` 設定済み。
- **Docker ファイル**: 作成完了 ✅
  - `Dockerfile` (Ubuntu 22.04 + Wine32 + MT5 + mt5linux)
  - `entrypoint.sh` (起動スクリプト)
  - `docker-compose.yml` (自動再起動設定)
  - `deploy/setup_centos.sh` (Docker インストール手順)
- **`live_config.py`**: `MT5_PATH` を Windows/Linux 自動切り替えに対応

## 現在の進捗状況 (2026/03/13 更新)
- **MT5 (Windows)**: Windows環境での動作確認済み。
- **Docker 移行**: CentOS 9 での運用のため、Docker (Ubuntu 22.04 + Wine32 + MT5) 環境に移行中。
- **CentOS サーバー設定**: 
  - Docker, docker-compose のインストール完了 (`setup_centos.sh` 実行済み)
  - リポジトリのクローン (`git pull`) 完了
  - `live_config.py` の配置完了
- **Docker ビルド**: 実行完了したが、コンテナ起動時に**「MT5 ターミナルが見つかりません」**というエラーが発生中。
  - 原因: Dockerビルド時に `mt5setup.exe /auto` が完了する前に次のステップに進んでしまい、Wine環境にMT5がインストールされていない。

## 次のステップ（明日やること）

この問題を解決するため、`Dockerfile` の MT5 インストール部分をより確実な方法（`xvfb-run` と `cmd /c start /wait`）に書き換える必要があります。

### Step 1: Dockerfile の修正と Push（Windows PC から）
Windows PC の `Dockerfile` を開き、中身を以下の内容に**すべて上書き**して保存し、GitHub に Push してください。

```dockerfile
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
    DISPLAY=:99 xvfb-run -a wine cmd /c "start /wait /tmp/mt5setup.exe /auto" && \
    sleep 30 && \
    rm /tmp/mt5setup.exe

# ── Bot ファイルのコピー ─────────────────────────────────────
WORKDIR /app
COPY . /app/
RUN chmod +x /app/entrypoint.sh

# ── 起動スクリプト ───────────────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]
```

**Git Push コマンド:**
```bash
git add Dockerfile
git commit -m "Fix MT5 installation using xvfb-run and start wait"
git push
```

### Step 2: CentOS サーバーでの再ビルド

CentOS サーバーに SSH ログインし、以下のコマンドを実行します。

```bash
cd /home/muu/python_program/anti-exness

# 1. 変更をサーバーに取り込む
git pull

# 2. 失敗しているコンテナを停止・削除
sudo docker-compose down

# 3. キャッシュを使わずに完全に再ビルド（15分ほどかかります）
sudo docker-compose build --no-cache

# 4. コンテナを起動
sudo docker-compose up -d

# 5. ログを確認
sudo docker logs -f exness-bot
```

### Step 3: 動作確認
ログに `Starting Exness MT5 Live Bot...` と表示されれば完了です。

## アンティグラビティへの引き継ぎ指示
このファイルを読み込んだ後、「CentOS サーバーでの Docker デプロイ中、MT5のインストールエラーで止まっています。Dockerfileの修正から手伝って」と伝えてください。
