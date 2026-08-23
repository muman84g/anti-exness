# Porting Audit

- identity: bot28 / S28 / magic `200028` / `BotBridge_s28` / `s28_*`
- source: `PV2C560_DEVQ80_H75_FORWARD_R1`
- signal parity: corrected ledgerの先頭・中央・末尾で`ret25`と`sqret_ac_l1_w60`を`1e-12`以内で一致
- execution parity: completed midpoint M1、decision後45秒gate、LONG Ask entry / Bid exit、decision基準75分、single position
- safety: bot27のownership、hedging preflight、OPEN確認、CLOSEDEAL確認、foreign ticket拒否、fail-closed同期を共通baseから継承
- mode: user-authorized live (`live=true`, `shadow=false`)。deploy、restart、EA attachment、real orderは未検証

Semantic deletion review: 新規botのため既存戦略挙動の削除はない。base側はbot27固有のsuffix/magic/log/state文字列だけを環境化し、bot27既定値とactual-entry holdを明示保持した。bot28は凍結正本どおりdecision-time holdをparamsで分岐し、self-testで期限を保全する。

2026-08-24 runtime correction: `BotBridge_s28.mq5`を単体コンパイル可能な正本へ展開した。entrypointがMT5 Expertsへコピーしない`_base_bridge.mqh`依存を除去し、bridge名と`cmd_s28.txt` / `res_s28.txt`を保持した。
