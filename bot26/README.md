# Bot26 PV2C520 C4535 Forward Bot

USTECをLONGし、USOILのcross-symbol regimeを使う`PV2C520_C4535_CONT1_WINDOW60_H75_FORWARD_R2`のlive forward実装。

- strategy: `ret25 > 0`かつ`lead5_corr_z >= 0.8284896671815759`
- context: USOIL、backward causal 1-bar shift
- fail closed: context staleが120秒を超える場合はentryしない
- signal: 完了M1、midpoint close
- execution: signal decision時の最初の有効Askを基準に、60秒以内にAskが+1 bpsへ到達した最初の5秒pollでmarket LONG
- pending lane: confirmation待ち中は単一position laneを予約し、後続signalを評価しない
- exit: broker確認済みactual entry時刻から75分後、最初の5秒pollでmarket close
- lot: `0.01`
- magic/comment: `200026` / `s26_pv2c520`
- bridge/IPC: `BotBridge_s26`, `cmd_s26.txt`, `res_s26.txt`
- state/log: `state/s26_bot_state.json`, `logs/s26_bot.log`, `logs/s26_trades.csv`
- current mode: `live_trading_enabled=true`、`shadow_forward_enabled=false`

M1 closeはbridgeが返すBid closeとbar spreadからmidpointを作る。`MidClose`が欠落した場合はsignalを計算せずfail closedする。

採用正本は`backtest/bot関連backtest/0_bot26実装PV2C520_OOS_001/best案/`のC4535 v02。元lineageがholdout_seenのためforward-onlyとして扱い、再利用評価のPASSへ読み替えない。

```powershell
Get-ChildItem bot26\*.py | ForEach-Object { py -m py_compile $_.FullName; if ($LASTEXITCODE -ne 0) { throw "py_compile failed: $($_.Name)" } }
py bot26\live_s26_bot.py --self-test
py bot26\porting_parity_test.py
```

`live_config.py`と`startup.ini`はローカル/CentOS個別管理でGit対象外。実口座で有効化する前にself-test、service recreate、EA attachment、symbol・magic・IPC確認が必要。
