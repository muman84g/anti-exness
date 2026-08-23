# Bot27 Porting Audit

- identity: bot27 / S27 / magic 200027 / `s27_pv2c859`
- strategy parameters: PV2C859 corrected freezeと一致
- feature parity: corrected source ledgerの3件で`ret25`と`absret_std_ratio30_120`を`1e-12`以内で一致
- execution parity: completed Bid M1 signal、LONG Ask entry、actual entry eventから60分後のBid close、single position
- live approximation: 5秒poll、signal delay 30秒超をfail closed
- safety: exact bridge name、symbol/magic/comment ownership、hedging preflight、OPEN確認、CLOSEDEAL確認、foreign ticket拒否
- reconciliation: `POSITIONS`障害はexit停止、`ORDERS`障害は新規entryのみ停止。exact-owned positionのexitは継続し、完全同期後は保有中でもrecoverable blockをclear
- auditability: usable completed M1ごとに`signal` / `no_signal` / not-evaluatedのdecision receiptを1件保存
- mode: user-authorized live (`live=true`, `shadow=false`)。real order、deploy、restartは未検証

Semantic deletion review: bot27固有のPV2C421 `ret30/rv_ratio/midpoint-close`と未使用PV2C520 dispatchを削除した。bot番号、magic、service、bridge、IPC、state/log名、LONG、single-position、ownership・同期安全機構は維持した。hold時計はPV2C859正本に合わせ、signal bar終了基準からactual entry event基準へ変更しself-testで保全する。
