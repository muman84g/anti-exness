# bot29 / S29

取引対象USTECの`PV2C531_CORRECTED_DIAGNOSTIC_R1`をlive forwardする固定移植。XAUUSDは取引しない。

- 完成M1のmidpoint Closeで`ret25 > 0`かつ`absret_activity_corr60 >= 0.5213354405981323`
- activityはMT5 HISTのtick volume。次の初回tickがdecisionから45秒以内ならLONG、1ポジションのみ
- 決済期限はdecision時刻から60分。期限後のfresh quoteでspreadが260 points以内なら即closeし、一度wideなら3回連続安定まで待機、30分でtimeout close
- 固定UTC時刻やDST表は使わず、fresh broker quoteとmarket-closed retcodeで平日・週末・休日の再開を共通判定
- current modeは`live_trading_enabled=true`、`shadow_forward_enabled=false`
- symbol / broker minimum lot / configured lotは`USTEC` / `0.05` / `0.05`
- magic `200029`、bridge `BotBridge_s29`、IPC suffix `s29`

実`live_config.py`と`startup.ini`はGit対象外で個別配置する。deploy、restart、EA attachment、real-order testは未検証。
