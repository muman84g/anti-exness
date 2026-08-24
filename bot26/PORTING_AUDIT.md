# Bot26 Porting Audit

- identity: bot26 / S26 / magic 200026 / `s26_pv2c520`
- strategy parameters: `PV2C520_C4535_CONT1_WINDOW60_H75_FORWARD_R2`、threshold `0.8284896671815759`、continuation `+1 bps / 60秒`、actual fill基準hold 75分
- feature parity: source ledgerの通常2件とstale違反2件で`ret25`、`lead5_corr_z`、reference staleを`1e-9`以内で一致
- execution parity: confirmed M1 signal、最初の有効Askをreferenceにcontinuation確認、LONG Ask entry、actual fill基準75分後Bid close、single pending/position lane
- stale behavior: `>120秒`をentry前にfail closed
- safety: exact bridge name、symbol/magic/comment ownership、hedging preflight、OPEN確認、CLOSEDEAL確認、foreign ticket拒否
- reconciliation: `POSITIONS`障害はexit停止、`ORDERS`障害は新規entryのみ停止。exact-owned positionのexitは継続し、完全同期後は保有中でもrecoverable blockをclear
- auditability: usable completed M1ごとに`signal` / `no_signal` / not-evaluatedのdecision receiptを1件保存
- mode: user-authorized live (`live=true`, `shadow=false`)。real order、deploy、restartは未検証

Semantic deletion review: 即時entryとdecision時刻基準exitを廃止し、C4535のcontinuation pendingとactual fill時刻基準exitへ置換した。旧strategy stateは明示的にschema v2へ移行し、保有positionはbroker open timeからexit時刻を再計算する。magic、bridge、state/log名、threshold、stale上限、entry/exit side、所有権確認、live/shadow設定は保持した。pending lane予約、期限切れ、actual-fill hold、旧state移行をself-testで回帰確認する。
