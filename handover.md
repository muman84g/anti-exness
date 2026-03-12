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

## 次のステップ（CentOS サーバーで実施）

### 手順

**Step 1: git push（Windows PC から）**
```bash
git add .
git commit -m "Add Docker support (Ubuntu + Wine + mt5linux)"
git push origin main
```

**Step 2: CentOS でリポジトリを取得し Docker をセットアップ**
```bash
sudo bash setup_centos.sh
# → 終了後、一度ログアウト・再ログインして docker グループを反映
```

**Step 3: live_config.py をサーバーに手動コピー**
```bash
# Windows PC から実行
scp live_config.py <user>@<server_ip>:/app/live_config.py
```

**Step 4: Docker イメージをビルド（初回は15〜30分）**
```bash
cd /app
docker build -t exness-bot .
```

**Step 5: コンテナを起動**
```bash
docker-compose up -d
```

**Step 6: ログを確認**
```bash
docker logs -f exness-bot
# "Starting Exness MT5 Live Bot..." が出れば成功
```

## 注意事項
- `live_config.py` は `.gitignore` で除外されているため、別PCに手動コピーが必要。
  - MT5_LOGIN, MT5_PASSWORD, MT5_SERVER（認証情報）
  - MT5_PATH は自動切り替え対応済み（手動設定不要）
  - USE_META_API = False（Docker + mt5linux 方式を使う）
- Xvfb は Docker コンテナ内で自動起動（サーバー側の設定不要）。
- 現在の戦略パラメーター（最適化済み）: ZSCORE_ENTRY=1.2, ZSCORE_EXIT=0.3, MAX_POSITIONS=10

## アンティグラビティへの引き継ぎ指示
このファイルを読み込んだ後、「CentOS サーバーへの Docker デプロイで詰まっているので続きを手伝って」と伝えてください。
