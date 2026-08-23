# bot29 / S29

USTECの`PV2C531_CORRECTED_DIAGNOSTIC_R1`をlive forwardする固定移植。

- 完成M1のmidpoint Closeで`ret25 > 0`かつ`absret_activity_corr60 >= 0.5213354405981323`
- activityはMT5 HISTのtick volume。次の初回tickがdecisionから45秒以内ならLONG、1ポジションのみ
- 決済期限はdecision時刻から60分。Long entryはAsk、exitはBid
- current modeは`live_trading_enabled=true`、`shadow_forward_enabled=false`
- magic `200029`、bridge `BotBridge_s29`、IPC suffix `s29`

実`live_config.py`と`startup.ini`はGit対象外で個別配置する。deploy、restart、EA attachment、real-order testは未検証。
