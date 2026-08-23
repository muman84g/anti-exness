# bot27 / S27

USTECの`PV2C859_DEVQ825_H90_FORWARD_R1`を移植したlive forward bot。

- strategy: `ret25 > 0`かつ`absret_std_ratio30_120 <= 0.6693523825105777`
- feature source: 完了M1のBid close
- execution: signal bar終了後30秒以内の最初の5秒pollでmarket LONG
- exit: 実際のentry eventから90分後、最初の5秒pollでmarket close
- lot / magic / comment: `0.01` / `200027` / `s27_pv2c859`
- bridge / state / logs: `BotBridge_s27` / `state/s27_bot_state.json` / `logs/s27_bot.log`, `logs/s27_trades.csv`
- current mode: `live_trading_enabled=true`、`shadow_forward_enabled=false`

DEV固定探索でq82.5・hold 90分を選び、その後の既観測leakcheckは固定診断としてのみ確認した。clean reusable PASSではない。MT5 M1取得と5秒pollはraw tick replayのforward approximationであり、30秒を超えるentryはfail closedする。

```powershell
Get-ChildItem bot27\*.py | ForEach-Object { py -m py_compile $_.FullName }
py bot27\live_s27_bot.py --self-test
py bot27\porting_parity_test.py
```

`live_config.py`と`startup.ini`はローカル/CentOS個別管理でGit対象外。実口座で有効化する前にself-test、service recreate、EA attachment、symbol・magic・IPC確認が必要。
