# Project Handover: Exness Live Bot

## 現在の進捗状況
- **メタトレーダー5 (MT5)**: Windows環境での動作確認済み。
- **MetaApi Bridge**: CentOS等のLinux環境への移行準備として、抽象化レイヤーの作成とMetaApi用ブリッジの実装を開始。
- **Git管理**: ローカルリポジトリを初期化し、`.gitignore` を設定しました。

## 次のステップ (別PCで作業を再開する場合の指示)
1. このプロジェクトフォルダを `git pull` またはコピーで取得してください。
2. `pip install -r requirements.txt` (あれば) で環境を構築してください。
3. `config.py` や `live_config.py` (除外対象) の設定内容を確認・復元してください。
4. アンティグラビティのチャットを開き、この `handover.md` を読み込ませて「作業を再開して」と指示してください。

## 注意事項
- `live_config.py` などの機密情報は `.gitignore` で除外されています。別PCに手動でコピーするか、環境変数を設定し直す必要があります。
