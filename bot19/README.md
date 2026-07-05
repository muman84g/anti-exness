# bot19 / S19 GBPUSD D10 Pending Stop

`bot19` は、`backtest34_bot18_small_range_filter` の D10 / TP1 / e75 / fs_m80_b2k 候補を live bot へ写した検証用フォルダです。

## 現在の稼働設定

- folder: `bot19`
- runner: `live_s19_bot.py`
- params: `s19_params.json`
- live trading: `true`
- shadow forward: `false`
- symbol: `GBPUSD`
- entry: MT5 server-side `Buy Stop` / `Sell Stop`
- source memo: `SOURCE_BACKTEST.md`

`live_s18_bot.py` と `s18_params.json` はコピー元由来の残ファイルです。S19 の対象は `live_s19_bot.py` / `s19_params.json` です。

## 重要

`BotBridge_s19.mq5` は bot19 フォルダ内に置いた bridge 雛形です。2026-07-06 時点で、同じ内容の `BotBridge_s19.mq5` / `BotBridge_s19.ex5` を実MT5データフォルダの `MQL5/Experts` 配下にも配置済みです。

bridge を変更した場合は、更新後の `BotBridge_s19.mq5` を MT5 側 `MQL5/Experts` 配下へ配置し、MetaEditor で再コンパイルしてください。フォルダ内の `.mq5` を直しただけでは、MT5 が読み込む `.ex5` は更新されません。

MT5 側 bridge は少なくとも `ECHO` / `INFO` / `HIST` / `PENDING` / `ORDERS` / `POSITIONS` / `POSITION` / `MODIFY` / `CANCEL` / `CLOSE` を返せる必要があります。S19 runner は起動時に `INFO` / `HIST` / `POSITIONS` / `ORDERS` をpreflightし、失敗時は起動を止めます。

MT5側bridgeはPython側の `ea_bridge.py` と同じ `cmd.txt` / `res.txt` を使う必要があります。`BotBridge_s19.mq5` はこの名前に合わせています。
Windowsでは `BotBridge_s19` が置かれた MT5 terminal data folder の `MQL5/Files` を優先検出します。環境差がある場合は `EA_BRIDGE_FILES_DIR` または `MT5_FILES_DIR` で明示してください。
`.ex5` は `.gitignore` 対象なので、pushだけではCentOS側に配置されません。CentOSでは `BotBridge_s19.mq5` をMetaEditorでcompileするか、別途 `BotBridge_s19.ex5` を配置してください。

server-side pending stop を使うため、発注応答が未確認になった場合は `pending_open` / `reconciliation_required` を state に残し、新規entryを止めます。手動解除前に MT5 上の建玉・未約定注文・ticket・comment が state と一致することを確認してください。

## 出力

- bot log: `logs/s19_bot.log`
- trades: `logs/s19_trades.csv`
- policy decisions: `logs/s19_policy_decisions.csv`
- state: `state/s19_<symbol>_bot_state.json`

## 確認コマンド

```powershell
& 'C:\Users\muuma\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' .\live_s19_bot.py --self-test
& 'C:\Users\muuma\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' .\live_s19_bot.py --policy-self-test
```
