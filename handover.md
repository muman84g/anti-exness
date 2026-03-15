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

## 現在の状況（残りのエラー）
Linux側のPythonでBot本体 (`live_main.py`) が起動し、ついにMT5側（Wine側）と通信がつながった直後、以下のエラーで止まっています。

```text
ModuleNotFoundError: No module named 'metaapi_cloud_sdk'
```

これは、単に **Ubuntu側のPythonに `metaapi_cloud_sdk` というライブラリがインストールされていないだけ（`pip install` の記載漏れ）**という、非常に軽微で平和的なエラーです！

## 次のステップ（再起動後、次回チャットでやること）
PCの再起動後、新しく開いたAntigravityのチャット画面で、この `handover.md` を読み込ませて以下を依頼してください。

---
**【次回プロンプト用テキスト（コピーして貼り付けてください）】**

> おはよう！PC再起動したので、いま開いている `handover.md` を読んで現状を把握してほしい。
> 前回の通信で、Wine上のMT5とLinuxのPython間のブリッジ通信をついに確立できたところまで進んでいる！
>
> 最後に残っているのが、Linux側での `ModuleNotFoundError: No module named 'metaapi_cloud_sdk'` というエラーだけ。
> `Dockerfile` の Linux側 Python (`pip3 install`) のリストにこれを追記して、再度コンテナを起動したい。修正と指示をお願い！

---
