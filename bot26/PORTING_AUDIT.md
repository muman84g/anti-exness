# Bot26 Porting Audit

- identity: bot26 / S26 / magic `200026` / `s26_pv2c520`
- current strategy: `PV2C520_DEVQ80_H75_FORWARD_R1`
- feature parity: `ret25`、`lead5_corr_z`、context staleがsource ledgerと`1e-9`以内で一致
- execution: completed midpoint M1 signal、LONG Ask entry、broker確認済みactual entry基準75分後Bid close
- retired behavior: C4535 `+1 bps / 60秒` continuation pendingは無効。旧pendingはstate移行時またはrun時に取り消す
- preserved behavior: threshold、context stale上限、LONG、lot、magic、bridge、IPC、state/log名、ownership・同期安全機構
- validation: syntax、self-test、source parity PASS
- release: deploy、restart、EA attach、live switch、real orderは未実施

Semantic deletion review: continuation confirmationとpending lane予約を稼働behaviorから除外した。既存positionのactual-entry基準75分exitは維持し、C4535 stateは安全にh75 identityへ移行する。
