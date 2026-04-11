# Project Handover: Exness Live Bot (2026-03-30 12:51)

## 🚨 最新状況：アンチ・デバッグ回避（Wine Staging 移行）
現在、**Wine Staging 11.0** への移行ビルドが進行中です。
「A debugger has been found running」というエラーを、Ubuntu 24.04 + Wine Staging + レジストリ偽装（hide_wine.reg）の組み合わせで突破する準備を整えました。

### ビルドの進捗
- [x] OS 基礎パッケージのインストール完了
- [/] Wine Staging 11.0 および依存関係 (496MB) のダウンロード・インストール中
- [ ] コンテナ自動起動・MT5 起動（このあと自動で実行されます）

## 📌 PCに戻った際の手順 (Verification)
ビルドはバックグラウンドで続行され、完了すると自動的に MT5 が立ち上がります。
数十分後、以下の手順で「デバッガ検出エラーが消えたか」を確認してください。

1. **コンテナの生存確認**
   ```powershell
   docker compose ps
   ```
   → SERVICE `exness-bot` の STATUS が `Up` になっていればOKです。

2. **MT5 の画面を確認（重要！）**
   エラーダイアログが消えているか、画像で確認します。
   ```powershell
   docker compose exec exness-bot import -window root -display :99 /app/screen.png
   ```
   → 右クリック等でプロジェクトフォルダ内の `screen.png` を開いてください。
   MT5 のログイン画面やチャートが見えれば、**「アンチ・デバッグ回避成功」**です！

3. **通信ブリッジの確認**
   ```powershell
   docker compose exec exness-bot python3 /app/test_local_connectivity.py
   ```
   → `TCP server started on port 5555` と表示されれば待機成功です。

---
## 【次回チャット再開時のアクション】
PCに戻られたら、以下のメッセージをコピーして私に送ってください。

> お疲れ様！`handover.md` の通りに `docker compose ps` と `screen.png` を確認したよ。
> ビルドの結果（エラーが消えたかどうか）を画像で見て、次のステップ（疎通テスト）に進めよう。
