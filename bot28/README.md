# bot28 / S28

USTECの`PV2C560_DEVQ80_H75_FORWARD_R1`をシャドーforwardする固定移植。

- 完成M1のmidpoint Closeで`ret25 > 0`かつ`sqret_ac_l1_w60 >= 0.1507958826882894`
- 次の初回tickがdecisionから45秒以内ならLONG、1ポジションのみ
- 決済期限はdecision時刻から75分。Long entryはAsk、exitはBid
- current modeは`live_trading_enabled=true`、`shadow_forward_enabled=false`
- magic `200028`、bridge `BotBridge_s28`、IPC suffix `s28`

DEV固定探索でhold 75分を選び、その後の既観測leakcheckは固定診断としてのみ確認した。clean reusable PASSではない。実`live_config.py`と`startup.ini`はGit対象外で個別配置する。deploy、restart、EA attachment、real-order testは未検証。
