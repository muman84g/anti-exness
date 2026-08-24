# bot27 / S27

USTECの`PV2C859_C4566_V01_FORWARD_R1`を移植したlive forward bot。

- strategy: `ret25 > 0`かつ`absret_std_ratio30_120 <= 0.6693523825105777`
- feature source: 完了M1のBid close
- execution: signal bar終了後30秒以内の最初の5秒pollでmarket LONG
- exit: entry時の`vol30_bps`がq2帯なら、10分grace後に`+4bps`以上の連続20分でclose。外側regimeは5分grace後に`+4bps`以上の累積10分でclose。未達は90分hard hold
- exit clock: MT5 quote timestampだけを使用し、15秒超の無tick gapは滞在時間に算入しない。5秒pollの重複quoteも加算しない
- lot / magic / comment: `0.01` / `200027` / `s27_pv2c859`
- bridge / state / logs: `BotBridge_s27` / `state/s27_bot_state.json` / `logs/s27_bot.log`, `logs/s27_trades.csv`
- current mode: `live_trading_enabled=true`、`shadow_forward_enabled=false`

entryはDEV固定探索のq82.5条件を維持し、exitはDEV tickの`C4566_v01`へ更新した。C4566はC4564監査で判明した閉場・疎tick gap算入を補正したforward-only候補で、clean reusable PASSではない。30秒を超えるentryはfail closedする。

```powershell
Get-ChildItem bot27\*.py | ForEach-Object { py -m py_compile $_.FullName }
py bot27\live_s27_bot.py --self-test
py bot27\porting_parity_test.py
py bot27\c4566_exit_parity_test.py
```

`live_config.py`と`startup.ini`はローカル/CentOS個別管理でGit対象外。実口座で有効化する前にself-test、service recreate、EA attachment、symbol・magic・IPC確認が必要。
