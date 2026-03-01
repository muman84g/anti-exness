# MetaApi セットアップガイド (CentOS用)

CentOSでExness Botを動作させるために、MetaApiのセットアップが必要です。以下の手順に従ってください。

## 1. MetaApi アカウントの作成
1. [MetaApi公式サイト](https://metaapi.cloud/) にアクセスし、アカウントを作成します。
2. 「API Tokens」メニューから、**Personal Access Token** を取得します。

## 2. MetaTrader アカウントの登録
1. MetaApiのダニッシュボードで「Add Account」をクリックします。
2. 以下の情報を入力して Exness の MT5 口座を連携させます：
   - **Account Name**: 任意（例: Exness Demo）
   - **Login**: MT5のログインID
   - **Password**: MT5のパスワード
   - **Server**: Exnessのサーバー名（例: Exness-MT5Trial6）
   - **Platform**: MetaTrader 5

## 3. 設定ファイルの更新
`live_config.py` を開き、以下の項目を入力します。

```python
META_API_TOKEN = "取得したトークン"
META_API_ACCOUNT_ID = "アカウント一覧に表示される Account ID"
USE_META_API = True  # CentOSで動かす場合は True にする
```

## 4. CentOSでのライブラリインストール
サーバー上で以下のコマンドを実行して必要なライブラリをインストールします。

```bash
pip install metaapi-cloud-sdk pandas pytz scikit-learn lightgbm yfinance
```

## 5. 接続テスト
作成した `check_env.py` はWindows専用のチェックが含まれるため、MetaApi用のテストスクリプトを作成しました。
`python test_metaapi.py` を実行して接続を確認してください。
