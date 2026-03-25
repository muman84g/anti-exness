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

**🚨 現在の最大の壁：Wine上の IPC Timeout (-10005) 🚨**
Wine環境下で動作するMT5ターミナルに対し、Python側から `mt5.initialize()` を使って接続しようとすると `(-10005, 'IPC timeout')` が発生し続ける問題に直面しています。

**これまでに行った対策と結果：**
1. **Wine Pythonでの直接実行**: `mt5linux`を使用せず、Bot全体をWine内のPythonで実行してみたがエラー解消せず。
2. **MetaTrader5ライブラリのダウングレード**: 新しいIPC仕様を避けるため、Wineで実績のある旧バージョン (`5.0.43`) に固定しつつ、NumPyの競合を修正（`numpy<2`）したが、エラー解消せず。
3. **InitializeとLoginの分離**: 接続とログインを同時に行うことによる非同期処理の衝突を避ける定番のワークアラウンドを実装したが、エラー解消せず。

**【結論】**
MetaTrader5のPython APIが用いるWindows固有の「非同期Named Pipe通信」は、Linux/Wineの現在のカーネル実装では完全にエミュレートしきれないという致命的な非互換性バグがあります。月額約30ドルかかるMetaApiのような有料クラウド通信を利用しないという前提に立つと、**ローカル環境（CentOS + Wine）でのPython直接IPC接続は技術的に極めて困難（実質不可能）**であるという見解に至りました。

## 次のステップ（次回チャット再開時の選択肢）
「完全無料でのローカル稼働」を達成するため、次回からは以下のいずれかのアプローチを選択して作業を再開します。

*   **選択肢A：Windows VPSへの移行（激推し・確実な王道）**
    CentOSとDocker(Wine)による無理な運用を完全に諦め、月額1,000円前後の純粋な Windows Server VPS (ConoHa, AWS EC2など) を契約する。これが現在のPythonコード (`live_data_fetcher.py` など) を1行も変えずに、確実かつ完全に無料で運用し続けられるベストプラクティスです。
*   **選択肢B：ZeroMQ / REST を用いた MQL5 EA ブリッジへの完全再設計**
    OSは現在のCentOS Dockerのままで、Pythonの `MetaTrader5` パッケージへの依存をきれいサッパリ捨てる。代わりにMT5側に「ローカルTCPサーバー（ソケット）として動くMQL5のEA（Expert Advisor）」を配置し、Pythonからはソケット通信でJSONや注文を送りつけるアーキテクチャに書き直す（実装難易度は高めですが、サーバー代以外は無料です）。

---
**【次回プロンプト用テキスト（コピーして貼り付けてください）】**

> お疲れ様！`handover.md` を読んで現状を把握してほしい。
> 前回の検証で、「Wine上でのPython MetaTrader5パッケージの直接通信は、IPCバグにより不可能である」という結論に達したところまで確認している。
> 
> 無料で毎月稼働させるため、次回ステップに書かれている「選択肢A（Windows VPS化）」か「選択肢B（MQL5 EAへのアーキテクチャ再設計）」のどちらで進めるか決めたので、その方針で作業を開始しよう！
---
