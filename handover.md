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
- **Docker ビルドとMT5インストールの問題と解決手段**: 
  - Dockerコンテナ内で `mt5setup.exe` (Webインストーラ) をWine経由で実行しようとしたが、バックグラウンドでの展開処理が正常に完了しないというWine特有のトラブルに遭遇。
  - **解決策**: Windows側で既にインストール済みの「MetaTrader 5」フォルダをそのままLinuxサーバーにコピーし、Dockerfile内でコンテナ内に配置（COPY）する堅牢な方式へと根本からアプローチを変更しました。
  - これに伴う Dockerfile、entrypoint.sh、.gitignore の修正はすべて完了し、GitHubの `main` ブランチにPush済みです。

## 次のステップ（デスクトップPCでやること）

MT5のアプリ本体ディレクトリ（約400MB）はサイズが大きすぎるため、Gitリポジトリの管理からは意図的に除外（`.gitignore`に追加）しています。
そのため、デスクトップPCから手動でCentOSサーバーの作業ディレクトリにMT5本体を転送してから、サーバー上で再ビルドを行う必要があります。

### Step 1: デスクトップPCから CentOS サーバーへ MT5 本体を転送

デスクトップPCの PowerShell または コマンドプロンプトを開き、以下のコマンドでデスクトップPC側の MT5 インストールフォルダをサーバーへ直接アップロード（SCP転送）してください。
※パスワードを聞かれたら、CentOSサーバーの `muu` ユーザーのパスワードを入力してください。容量が大きいため転送には数分かかります。

```powershell
scp -r "C:\Program Files\MetaTrader 5" muu@118-27-2-117:/home/muu/python_program/anti-exness/
```

### Step 2: CentOS サーバーでの再ビルド

転送が完了したら、CentOS サーバーに SSH ログインし、以下のコマンドを実行します。

```bash
cd /home/muu/python_program/anti-exness

# 1. GitHub から最新の Dockerfile や起動スクリプトを取り込む
git pull

# 2. 失敗している古いコンテナを停止・削除
sudo docker-compose down

# 3. キャッシュを使わずに完全に再ビルド
# （ここで、先ほどSCPで転送した MT5 フォルダがコンテナ内に組み込まれます）
sudo docker-compose build --no-cache

# 4. コンテナを起動
sudo docker-compose up -d

# 5. ログを確認
sudo docker logs -f exness-bot
```

### Step 3: 動作確認
ログで `[2/4] MT5 ターミナルを Wine で起動中...` を通過し、`mt5linux サーバー準備完了` などのメッセージが出力されていれば大成功です！

## アンティグラビティへの引き継ぎ指示
このファイルを読み込んだ後、「ノートPCからデスクトップPCに移動して手順に従いSCP転送と再ビルドを試みた結果（またはエラー内容）を報告します。続きをサポートして」と伝えてください。
