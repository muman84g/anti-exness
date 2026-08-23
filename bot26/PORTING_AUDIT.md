# Bot26 Porting Audit

- identity: bot26 / S26 / magic 200026 / `s26_pv2c520`
- strategy parameters: `PV2C520_DEVQ80_H75_FORWARD_R1`、threshold `0.8284896671815759`、hold 75分
- feature parity: source ledgerの通常2件とstale違反2件で`ret25`、`lead5_corr_z`、reference staleを`1e-9`以内で一致
- execution parity: confirmed M1 signal、LONG Ask entry、decision基準75分後Bid close、single position
- stale behavior: `>120秒`をentry前にfail closed
- safety: exact bridge name、symbol/magic/comment ownership、hedging preflight、OPEN確認、CLOSEDEAL確認、foreign ticket拒否
- reconciliation: `POSITIONS`障害はexit停止、`ORDERS`障害は新規entryのみ停止。exact-owned positionのexitは継続し、完全同期後は保有中でもrecoverable blockをclear
- auditability: usable completed M1ごとに`signal` / `no_signal` / not-evaluatedのdecision receiptを1件保存
- mode: user-authorized live (`live=true`, `shadow=false`)。real order、deploy、restartは未検証

Semantic deletion review: 旧h60 identityをh75 forward identityへ置換した。magic、bridge、state/log名、threshold、stale上限、entry/exit side、所有権確認は保持し、75分holdはself-testとparity testで保全した。
