# Bot27 Porting Audit

- identity: bot27 / S27 / magic 200027 / `s27_pv2c859`
- strategy parameters: PV2C859 entry freeze + C4566_v01 exit freezeと一致
- feature parity: source ledgerの3件で`ret25`、`absret_std_ratio30_120`、`vol30_bps`を`1e-12`以内で一致
- execution parity: completed Bid M1 signal、LONG Ask entry、quote timestamp基準のC4566 exit、未達時はactual entry eventから90分後の最初の新tickでBid close、single position
- tick parity: corrected DEV 591 tradesすべてでentry後のexit tick時刻・理由が一致
- live approximation: 5秒poll。ただしMT5 tick timestampで重複quoteを除外し、15秒超の無tick gapを利益滞在時間へ算入しない。signal delay 30秒超はfail closed
- safety: exact bridge name、symbol/magic/comment ownership、hedging preflight、OPEN確認、CLOSEDEAL確認、foreign ticket拒否
- reconciliation: `POSITIONS`障害はexit停止、`ORDERS`障害は新規entryのみ停止。exact-owned positionのexitは継続し、完全同期後は保有中でもrecoverable blockをclear
- auditability: usable completed M1ごとに`signal` / `no_signal` / not-evaluatedのdecision receiptを1件保存
- transition: C4566 stateを持たない既存positionは従来の90分hard holdを維持し、新規entryだけC4566を適用
- mode: user-authorized live (`live=true`, `shadow=false`)。今回のdeploy、restart、real orderは未実施

Semantic deletion review: fixed-hold-onlyの新規position behaviorをC4566 exitへ置換した。既存positionの90分hold、bot番号、magic、service、bridge、IPC、state/log名、LONG、single-position、ownership・同期安全機構は維持した。
