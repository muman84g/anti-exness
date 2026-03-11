# Project Handover: Exness Live Bot

## 現在の進捗状況
- **MT5 (Windows)**: Windows環境での動作確認済み。
- **MetaApi Bridge**: `metaapi_bridge.py` 実装済み（ただし月額$30のため保留）。
- **mt5linux方式**: CentOS 9 の Wine 8.0 で `wow64` 問題により断念。
  - 原因: CentOS 9 は wine.i686 (32bit) パッケージを提供しておらず、Wine の wow64 モードが動作しない。
- **Git管理**: ローカルリポジトリ初期化・`.gitignore` 設定済み。

## 次のステップ（ノートPCで作業再開する場合）

### 方針：Docker（Ubuntu コンテナ）で MT5 環境を構築

CentOS 9 上で Docker を使い、Ubuntu コンテナ内で Wine + mt5linux を動かす。
Ubuntu は wine.i686 が使えるため、wow64 問題が解決できる。

**想定構成:**
```
CentOS 9 サーバー（ConoHa）
├── 仮想通貨Bot （CentOS上で直接 python3 で動かす）
└── Docker（Ubuntu コンテナ）
    ├── Wine + MT5ターミナル
    └── MT5 Exness Bot（live_main.py）
```

### 手順

**Step 1: CentOS に Docker をインストール**
```bash
sudo yum install -y docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker muu
newgrp docker
```

**Step 2: Dockerfile を作成**
（アンティグラビティに「Dockerfileを作って」と依頼すること）

**Step 3: Ubuntuコンテナで Wine + Python + mt5linux をセットアップ**
（Dockerfile の中で自動化する予定）

**Step 4: MT5ターミナルを Wine で起動し、rpyc サーバーを起動**

**Step 5: live_main.py を起動してテスト**

## 注意事項
- `live_config.py` は `.gitignore` で除外されているため、別PCに手動コピーが必要。
  - MT5_PATH, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
  - USE_META_API = False（Docker + mt5linux 方式を使う）
- Xvfb は起動済み（サーバー再起動までは維持される）。
- 現在の戦略パラメーター（最適化済み）: ZSCORE_ENTRY=1.2, ZSCORE_EXIT=0.3, MAX_POSITIONS=10

## アンティグラビティへの引き継ぎ指示
このファイルを読み込んだ後、「Docker + Ubuntu コンテナで MT5 環境を構築するための
Dockerfile を作って」と指示してください。
