# bot27 / S27

取引対象USTECの`PV2C859_DEVQ825_H90_FORWARD_R1`を移植したlive forward bot。XAUUSDは取引しない。

- strategy: `ret25 > 0`かつ`absret_std_ratio30_120 <= 0.6693523825105777`
- feature source: 完了M1のBid close
- execution: signal bar終了後30秒以内の最初の5秒pollでmarket LONG
- exit: broker確認済みactual entry時刻から90分後。期限後のfresh quoteでspreadが260 points以内なら即closeし、一度wideなら3回連続安定まで待機、30分でtimeout close
- session: 固定UTC時刻やDST表は使わず、fresh broker quoteとmarket-closed retcodeで平日・週末・休日の再開を共通判定
- symbol / broker minimum lot / configured lot: `USTEC` / `0.05` / `0.05`
- magic / comment: `200027` / `s27_pv2c859`
- bridge / state / logs: `BotBridge_s27` / `state/s27_bot_state.json` / `logs/s27_bot.log`, `logs/s27_trades.csv`
- current mode: `live_trading_enabled=true`、`shadow_forward_enabled=false`

C4566のvolatility dwell早期exitはobserved leakcheckの反転悪化を理由に無効化した。旧stateに`exit_policy_state`が残っていても評価せず、既存ポジションもactual-entry基準90分exitへ戻る。

```powershell
Get-ChildItem bot27\*.py | ForEach-Object { py -m py_compile $_.FullName }
py bot27\live_s27_bot.py --self-test
py bot27\porting_parity_test.py
```

`c4566_exit_policy.py`とC4566証跡は非稼働の履歴として保存する。`live_config.py`と`startup.ini`はローカル/CentOS個別管理でGit対象外。コード更新だけでは稼働中serviceへ反映されない。
