# bot28 / S28

取引対象USTECの`PV2C560_DEVQ80_H75_FORWARD_R1`をlive forwardする固定移植。XAUUSDは取引しない。

- 完成M1のmidpoint Closeで`ret25 > 0`かつ`sqret_ac_l1_w60 >= 0.1507958826882894`
- 次の初回tickがdecisionから45秒以内ならLONG、1ポジションのみ
- 決済期限はdecision時刻から75分。期限後のfresh quoteでspreadが260 points以内なら即closeし、一度wideなら3回連続安定まで待機、30分でtimeout close
- 固定UTC時刻やDST表は使わず、fresh broker quoteとmarket-closed retcodeで平日・週末・休日の再開を共通判定
- current modeは`live_trading_enabled=true`、`shadow_forward_enabled=false`
- symbol / broker minimum lot / configured lotは`USTEC` / `0.05` / `0.05`
- magic `200028`、bridge `BotBridge_s28`、IPC suffix `s28`

DEV固定探索でhold 75分を選び、その後の既観測leakcheckは固定診断としてのみ確認した。clean reusable PASSではない。実`live_config.py`と`startup.ini`はGit対象外で個別配置する。deploy、restart、EA attachment、real-order testは未検証。
