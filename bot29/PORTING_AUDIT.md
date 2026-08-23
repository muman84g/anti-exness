# Porting Audit

- identity: bot29 / S29 / magic `200029` / `BotBridge_s29` / `s29_*`
- source: `PV2C531_CORRECTED_DIAGNOSTIC_R1`
- signal parity: corrected ledgerの先頭・中央・末尾で`ret25`と`absret_activity_corr60`を`1e-12`以内で一致
- execution parity: completed midpoint M1、decision後45秒gate、LONG Ask entry / Bid exit、decision基準60分、single position
- safety: bot27のownership、hedging preflight、OPEN確認、CLOSEDEAL確認、foreign ticket拒否、fail-closed同期を共通baseから継承
- mode: user-authorized live (`live=true`, `shadow=false`)。deploy、restart、EA attachment、real orderは未検証

Semantic deletion review: 新規botのため既存戦略挙動の削除はない。base側はbot27固有のsuffix/magic/log/state文字列だけを環境化し、bot27既定値とactual-entry holdを明示保持した。bot29は凍結正本どおりdecision-time holdをparamsで分岐し、self-testで期限を保全する。
