# Project Handover: Exness Live Bot

## 達成したマイルストーン 🎉 (2026/03/16 更新)
- **MT5 (Windows)**: Windows環境での動作確認済み。
- **Docker 移行と環境構築**: CentOSにて Docker (Ubuntu 22.04 + Wine32 + MT5) 環境の構築に成功。
  - **MT5の配置手法**: Windows側から「コピー」することで完全解決。
  - **Pythonブリッジ通信 (mt5linux)**: 構築完了！
    - Wine環境でのインストーラサイレント落ち問題を **埋め込み(embeddable) ZIP版の直接展開** で解決。
    - Wine環境下で、バックグラウンドPythonタスクが落ちる問題を **`pythonw.exe` の使用** と **`PYTHONHASHSEED=0`** で解決。
    - Pythonの浮動小数点計算がWineのC-Runtime不足で落ちる問題（fetestexceptエラー）を、 **`winetricks win10 vcrun2015` の導入** によって完全解決。
  - **通信テスト**: **`rpyc` サーバーが立ち上がり、Linux側のPythonからの接続（accepted）が成功しました！！！！！🚀**

## 現在の状況（残りのエラー） (2026/03/20 更新)
以下の問題を順番にクリアしてきました。
1. **`ModuleNotFoundError: No module named 'metaapi_cloud_sdk'`**: `Dockerfile` に追記して解決済み。
2. **`Could not resolve host: mt5.exness.com`**: コンテナ内のDNS未解決問題を `docker-compose.yml` に Google DNS (`8.8.8.8`) を追加して解決済み。MT5がブローカーに繋がるようになりました。

**🚨 現在直面している課題と最新の対策 🚨**
通信テストは成功・DNSも解決したにもかかわらず、MT5 API(Python側) の初期化で `(-10005, 'IPC timeout')` エラーが発生していました。原因は、Wine環境下での MT5ターミナル と Linux Python(`mt5linux`) 間の Windows Named Pipe (IPC) の接続不良です。

**抜本的なアーキテクチャ変更（実装済み）:**
この問題を根本的に回避するため、ソースコード（`Dockerfile`, `entrypoint.sh`）に以下の「2段階の抜本的対策」を実装した状態で、システムがクラッシュ・中断しました。
1. **MT5ターミナルの `/portable` 起動 (`entrypoint.sh`)**: Windowsレジストリ依存を排除し、Named Pipeを正常に作成させるための対策。
2. **`mt5linux` ブリッジの廃止と Wine Python での直接実行**: Linux側Pythonとのプロセス間通信(IPC)を完全に排除しました。`live_main.py` などのBot本体を、直接 Wine 内の Python 環境で実行する構造に変更し、必要な全依存パッケージ（`pandas`, `lightgbm`, `MetaTrader5` など）を `Dockerfile` でWine側のPythonにインストールするよう構成しました。

## 次のステップ（次回チャット再開時）

---
**【次回プロンプト用テキスト（コピーして貼り付けてください）】**

> お疲れ様！クラッシュから復帰しました。いま開いている `handover.md` を読んで現状の進捗を把握してほしい。
> 前回の作業で、MT5の `(-10005, 'IPC timeout')` エラー対策として、「MT5の/portable起動」および「mt5linuxブリッジを廃止してBotを直接Wine Pythonで実行するアーキテクチャへの変更」がソースコード（Dockerfile, entrypoint.sh）に実装されたところまで進んでいるよ。
> 
> これからその最新のソースコードをCentOSサーバーで再ビルド (`docker compose up -d --build`) してコンテナを起動するから、ログの確認サポートと、新しいアーキテクチャで正しくMT5に接続・トレードが開始できるかどうかの検証を一緒に進めてほしい！

---
