# Bot26 PV2C520 h75 Forward Bot

取引対象USTECをLONGし、参照専用USOILのcross-symbol regimeを使う`PV2C520_DEVQ80_H75_FORWARD_R1`のlive forward実装。XAUUSDは取引しない。

- strategy: `ret25 > 0`かつ`lead5_corr_z >= 0.8284896671815759`
- context: USOIL、backward causal 1-bar shift、stale上限120秒
- signal: 完了M1、midpoint close
- entry: signal decision後の最初の5秒pollでmarket LONG
- exit: broker確認済みactual entry時刻から75分後。期限後のfresh quoteでspreadが260 points以内なら即closeし、一度wideなら3回連続安定まで待機、30分でtimeout close
- session: 固定UTC時刻やDST表は使わず、fresh broker quoteとmarket-closed retcodeで平日・週末・休日の再開を共通判定
- symbol / broker minimum lot / configured lot: `USTEC` / `0.05` / `0.05`
- magic / comment: `200026` / `s26_pv2c520`
- bridge / state / logs: `BotBridge_s26` / `state/s26_bot_state.json` / `logs/s26_bot.log`, `logs/s26_trades.csv`
- current mode: `live_trading_enabled=true`、`shadow_forward_enabled=false`

C4535の継続確認pendingはobserved leakcheckの反転悪化を理由に無効化した。旧C4535 stateはstrategy IDをh75へ移行し、未約定pendingを取り消す。既存ポジションのactual-entry基準75分exitは維持する。

```powershell
Get-ChildItem bot26\*.py | ForEach-Object { py -m py_compile $_.FullName; if ($LASTEXITCODE -ne 0) { throw "py_compile failed: $($_.Name)" } }
py bot26\live_s26_bot.py --self-test
py bot26\porting_parity_test.py
```

`live_config.py`と`startup.ini`はローカル/CentOS個別管理でGit対象外。コード更新だけでは稼働中serviceへ反映されない。
