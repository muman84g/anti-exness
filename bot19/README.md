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

`MetaTrader 5/MQL5/Experts/BotBridge_s19.mq5` が bot19 用 bridge の source of truth です。bot19 service はこの source を起動時に MT5 側 `MQL5/Experts` 配下へ配置してコンパイルします。

bridge を変更した場合は、`MetaTrader 5/MQL5/Experts/BotBridge_s19.mq5` を更新します。bot19 service は起動時にその source を MT5 側 `MQL5/Experts` 配下へ配置し、MetaEditor で自動コンパイルします。

MT5 側 bridge は `CAPS` で `BotBridge_s19` を返し、少なくとも `ECHO` / `INFO` / `HIST` / `PENDING` / `ORDERS` / `POSITIONS` / `POSITION` / `MODIFY` / `CANCEL` / `CLOSE` を返せる必要があります。S19 runner は起動時に `CAPS` / `INFO` / `HIST` / `POSITIONS` / `ORDERS` をpreflightし、失敗時は起動を止めます。

MT5側bridgeはPython側の `ea_bridge.py` と同じ `cmd_s19.txt` / `res_s19.txt` を使う必要があります。`BotBridge_s19.mq5` はこの名前に合わせています。Python側は `ea_bridge_s19.lock` で同時アクセスを直列化します。
Windowsでは `BotBridge_s19` が置かれた MT5 terminal data folder の `MQL5/Files` を優先検出します。環境差がある場合は `EA_BRIDGE_FILES_DIR` または `MT5_FILES_DIR` で明示してください。
`BotBridge_s19.mq5` がgit管理対象です。`.ex5` はコンテナ起動時に MT5 側 `MQL5/Experts` で生成されます。MT5が別の `MQL5/Experts` 配下を見ている場合は、その配置先と compose の bridge source mount を合わせてください。

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

## S19 IPC isolation

bot19 must not share the default MT5 file IPC lane with bot18.

- Python default command file: `cmd_s19.txt`
- Python default response file: `res_s19.txt`
- Python default lock file: `ea_bridge_s19.lock`
- MT5 EA input `InpCommandFile`: `cmd_s19.txt`
- MT5 EA input `InpResponseFile`: `res_s19.txt`

After pulling this change, `docker compose up -d --no-deps --force-recreate exness-bot-19`
copies the selected bridge source into MT5 `MQL5/Experts`, compiles it, and starts MT5 with `BotBridge_s19`.
The generated startup config only selects the expert and chart symbol; it does not rewrite account login, password, or server settings.
If manually attaching from noVNC, choose `BotBridge_s19` for bot19 and confirm the inputs above.
The startup log should show `2026-07-06-pending-stop-v3-dedicated-ipc` in the CAPS preflight.
