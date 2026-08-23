# Bot26 PV2C520 Forward Bot

USTECをLONGし、USOILのcross-symbol regimeを使う`PV2C520_DEVQ80_H75_FORWARD_R1`のlive forward実装。

- strategy: `ret25 > 0`かつ`lead5_corr_z >= 0.8284896671815759`
- context: USOIL、backward causal 1-bar shift
- fail closed: context staleが120秒を超える場合はentryしない
- signal: 完了M1、midpoint close
- execution: signal bar終了後30秒以内の最初の5秒pollでmarket LONG
- exit: signal bar終了から75分後、最初の5秒pollでmarket close
- lot: `0.01`
- magic/comment: `200026` / `s26_pv2c520`
- bridge/IPC: `BotBridge_s26`, `cmd_s26.txt`, `res_s26.txt`
- state/log: `state/s26_bot_state.json`, `logs/s26_bot.log`, `logs/s26_trades.csv`
- current mode: `live_trading_enabled=true`、`shadow_forward_enabled=false`

M1 closeはbridgeが返すBid closeとbar spreadからmidpointを作る。`MidClose`が欠落した場合はsignalを計算せずfail closedする。

評価はsource folderの`evaluate_live_aligned_current_vs_h75.py`を正本とする。旧canonical ledgerは60本bar-clockだったため、現行botの壁時計75分評価とは区別する。

```powershell
py -m py_compile bot26\*.py
py bot26\live_s26_bot.py --self-test
py bot26\porting_parity_test.py
```

`live_config.py`と`startup.ini`はローカル/CentOS個別管理でGit対象外。実口座で有効化する前にself-test、service recreate、EA attachment、symbol・magic・IPC確認が必要。
