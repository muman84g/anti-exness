# Bot27 Porting Audit

- identity: bot27 / S27 / magic `200027` / `s27_pv2c859`
- current strategy: `PV2C859_DEVQ825_H90_FORWARD_R1`
- feature parity: `ret25`、`absret_std_ratio30_120`がsource ledgerと`1e-12`以内で一致
- execution: completed Bid M1 signal、LONG Ask entry、broker確認済みactual entry基準90分後Bid close
- retired behavior: C4566 volatility dwell早期exitは無効。旧positionの`exit_policy_state`が残っていても評価しない
- preserved behavior: q82.5 threshold、LONG、lot、magic、bridge、IPC、state/log名、ownership・同期安全機構
- validation: syntax、self-test、source parity PASS
- release: deploy、restart、EA attach、live switch、real orderは未実施

Semantic deletion review: C4566のcontinuous/cumulative dwell early-exitを稼働behaviorから除外した。既存・新規positionともactual-entry基準90分exitを維持する。C4566 moduleと証跡は非稼働履歴として削除しない。
