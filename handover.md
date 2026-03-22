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

**🚨 現在直面しているエラー 🚨**
通信テストは成功・DNSも解決したにもかかわらず、MT5 API(Python側) の初期化でタイムアウトします。
```text
[ERROR] Failed to connect to MT5. Exiting.
MT5 initialize failed: (-10005, 'IPC timeout')
```

**原因分析と対策:**
- Wineで稼働する MT5ターミナル と Linux上のPython(`mt5linux`) を繋ぐ「Named Pipe（IPC）」の接続不良です。
- **Wine 6.0.3 (Ubuntu 22.04標準) の古い仕様が、新しいバージョンのMT5のマルチプロセス通信に対応しきれていない**（kernel32.dll名前付きパイプの不具合）ことが原因と考えられます。
- 対策として `Dockerfile` を修正し、「Ubuntuの標準Wineを使用しつつ、問題になりやすい `rpcss` (RPCサブシステム) をバイパスして起動する (`WINEDLLOVERRIDES="rpcss="'`)」設定を施しました。WineHQリポジトリは依存関係のバグが多いため使用を中止しています。

## 次のステップ（再起動後、次回チャットでやること）
PC再起動後、新しく開いたAntigravityのチャット画面で、この `handover.md` を読み込ませて以下を依頼してください。

---
**【次回プロンプト用テキスト（コピーして貼り付けてください）】**

> おはよう！PC再起動したので、いま開いている `handover.md` を読んで現状を把握してほしい。
> 前回の通信で、`metaapi_cloud_sdk`の追加とDNS解決の設定は完了し、現在はMT5の `(-10005, 'IPC timeout')` エラーの解決に取り組んでいるところだよ。
> 
> 前回最後に、WineのIPCタイムアウト対策として、`Dockerfile` で Wine 8.0 へのダウングレードや `WINEDLLOVERRIDES="rpcss="` などの設定を行ったところまで進んでいる。
> これからその（最後の修正をした）ソースコードをGitでpushして、CentOSサーバーで再ビルド(`docker compose up -d --build`)するから、起動後のログ確認のサポートと、もしまたIPCエラーが出た時の次のデバッグ・回避策（例えば mt5linux ではなく別のブリッジを使うなど）を一緒に考えてほしい！

---
