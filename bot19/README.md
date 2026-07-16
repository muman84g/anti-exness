# bot19 / S19 GBPUSD D10 Pending Stop

> Mapping audit: 現在のコードと設定は下記D10候補に一致するが、元handoffでは同候補が「未昇格」「forward-readyではない」とされている。ユーザーが意図したbot19採用候補との対応は未確認であり、現source mappingを採用根拠として扱わない。

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

## 現D10実装のGBPUSD / GBPUSDm比較

- GBPUSD物理tickの元backtestは現行runnerでも `+135.40 USD / PF 1.1431` を完全再現。
- GBPUSDm物理tickをmedian spread 10 + 2 = gate 12 pointsで再生すると `-72.91 USD / PF 0.9366`。
- この結果は現フォルダのD10実装だけの比較。ユーザーが意図したbot19採用候補の比較ではない可能性がある。
- 詳細: `SOURCE_BACKTEST.md` と `backtest34/S19_GBPUSDM_PHYSICAL_TICK_COMPARISON_20260711.md`。

`live_s18_bot.py` と `s18_params.json` はコピー元由来の残ファイルです。S19 の対象は `live_s19_bot.py` / `s19_params.json` です。

## 重要

`BotBridge_s19.mq5` は bot19 フォルダ内に置いた bridge 雛形です。2026-07-06 時点で、同じ内容の `BotBridge_s19.mq5` / `BotBridge_s19.ex5` を実MT5データフォルダの `MQL5/Experts` 配下にも配置済みです。

bridge を変更した場合は、更新後の `BotBridge_s19.mq5` を MT5 側 `MQL5/Experts` 配下へ配置し、MetaEditor で再コンパイルしてください。フォルダ内の `.mq5` を直しただけでは、MT5 が読み込む `.ex5` は更新されません。

MT5 側 bridge は `CAPS` で `BotBridge_s19` を返し、少なくとも `ECHO` / `INFO` / `HIST` / `PENDING` / `ORDERS` / `POSITIONS` / `POSITION` / `MODIFY` / `CANCEL` / `CLOSE` を返せる必要があります。S19 runner は起動時に `CAPS` / `INFO` / `HIST` / `POSITIONS` / `ORDERS` をpreflightし、失敗時は起動を止めます。

MT5側bridgeはPython側の `ea_bridge.py` と同じ `cmd_s19.txt` / `res_s19.txt` を使う必要があります。`BotBridge_s19.mq5` はこの名前に合わせています。Python側は `ea_bridge_s19.lock` で複数botプロセス間の同時アクセスを直列化します。
Windowsでは `BotBridge_s19` が置かれた MT5 terminal data folder の `MQL5/Files` を優先検出します。環境差がある場合は `EA_BRIDGE_FILES_DIR` または `MT5_FILES_DIR` で明示してください。
`BotBridge_s19.ex5` はbot19用の実行bridgeとしてgit管理対象です。CentOS側は `git pull` で `.mq5` と `.ex5` の両方を更新できます。MT5が別の `MQL5/Experts` 配下を見ている場合だけ、その実配置先へコピーしてください。

server-side pending stop を使うため、発注応答が未確認になった場合は `pending_open` / `reconciliation_required` を state に残し、新規entryを止めます。MT5 上のbot19建玉が state に無い場合でも、現在の `virtual_orders`、`pending_open`、または保存済みの pending-grid repair 履歴から ticket/comment/direction/SL/entry が一意に一致する場合だけ state に自動採用します。一意に照合できない場合は `reconciliation_required` のまま停止します。

同一口座で複数botを動かす前提で、bot19の建玉・pending注文は `symbol` + `magic` で絞って扱います。新しいstateの建玉・virtual orderには `symbol` / `magic` を保存します。missing state ticket を server-side SL とみなす前、または `CLOSE` / `MODIFY` / pending `CANCEL` のようなticket単体操作の前には、ticketがbot19所有であることを symbol/magic/comment 証拠で確認します。bot18など別magicのticketがstateへ混入した場合は、close/cancel/adopt/SL扱いせず新規entryをブロックします。

未約定・建玉なしのcycle中は、S19 pending grid は `Buy Stop` 2本 + `Sell Stop` 2本を正常形とします。MT5側で1本だけcancelされるなどして2:2が崩れた場合、runnerは残りのS19 pendingを全cancelし、spread/regimeがentry可能なら同じ `grid_anchor` で2:2を再発注します。spread/regimeがNGなら `grid_anchor` を維持して再発注待ちにします。cancelまたは再発注に失敗した場合は `reconciliation_required` で停止します。

MT5 がbot19の建玉・未約定注文ともに完全flatを返し、state側に未解決の `pending_open` / pending-grid repair の残骸だけがある場合は、その stale block を自動解除して通常のflat-cycle待機に戻します。

If stale pending tickets remain in `state/s19_gbpusd_bot_state.json`, use `reset_state_if_flat.py` only after stopping bot19. It refuses to reset unless MT5 reports no bot19 positions and no bot19 pending orders.

## 出力

- bot log: `logs/s19_bot.log`
- trades: `logs/s19_trades.csv`
- policy decisions: `logs/s19_policy_decisions.csv`
- state: `state/s19_<symbol>_bot_state.json`

## 確認コマンド

```powershell
& 'C:\Users\muuma\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' .\live_s19_bot.py --self-test
& 'C:\Users\muuma\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' .\live_s19_bot.py --policy-self-test
python .\reset_state_if_flat.py --self-test
```

## S19 IPC isolation

bot19 must not share the default MT5 file IPC lane with bot18.

- Python default command file: `cmd_s19.txt`
- Python default response file: `res_s19.txt`
- Python default lock file: `ea_bridge_s19.lock`
- MT5 EA input `InpCommandFile`: `cmd_s19.txt`
- MT5 EA input `InpResponseFile`: `res_s19.txt`

After pulling this change, reload or recompile `BotBridge_s19.mq5` on the MT5 side.
If an older `BotBridge_s19.ex5` is still attached, manually set the EA inputs above before starting `exness-bot-19`.
The startup log should show `2026-07-06-pending-stop-v3-dedicated-ipc` in the CAPS preflight.
