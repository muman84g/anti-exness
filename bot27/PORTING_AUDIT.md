# Bot27 Porting Audit

- identity: bot27 / S27 / magic `200027` / `s27_pv2c859`
- current strategy: `PV2C859_DEVQ825_H90_FORWARD_R1`
- feature parity: `ret25`、`absret_std_ratio30_120`がsource ledgerと`1e-12`以内で一致
- execution: completed Bid M1 signal、LONG Ask entry、broker確認済みactual entry基準90分後Bid close
- retired behavior: C4566 volatility dwell早期exitは無効。旧positionの`exit_policy_state`が残っていても評価しない
- preserved behavior: q82.5 threshold、LONG、magic、bridge、IPC、state/log名、ownership・同期安全機構
- runtime correction 2026-08-25: targetをUSTECと明記。broker最小lotに合わせ`0.01`から`0.05`へ修正し、bot27共通baseを使うbot28/29を含め発注前lot contractをfail-close検証

## 2026-08-28 Long 3lane + Short overlay

- fixed winner: Forward tickで`Activity 0.75 + VSA 0.75`がpath案よりPnL、MTM DD、日次σで優位
- live mapping: lane 1-3 Long、lane 4 Activity Short、lane 5 VSA Short。Long 0.20 / Short 0.15で0.75相対露出を正確に維持
- preserved safety: magic/comment ownership、hedging preflight、OPEN後再照合、CLOSEDEAL確認、foreign ticket拒否、fresh quote/spread defer、market-closed retry、state fail-closedを維持
- state migration: 旧strategy IDを明示的な互換IDとしてのみ受理し、既存lane 1/2 Long position stateを保持
- known mismatch: ShortのCloseはMidClose、Open/High/LowはMT5 Bid bar。全midpoint OHLCのhistorical tick backtestと小差が残る
- semantic deletion review: 既存Long signal・lane 1/2 exit・ownership/reconciliation・close/session処理は削除なし。試作した3/4 signal間引きはForward DD劣化のため採用前に除去し、lot比率の正確なscaleへ置換
- external actions: deploy、restart、bridge attachment、real orderは未実施
- validation: syntax、self-test、source parity PASS
- release: deploy、restart、EA attach、live switch、real orderは未実施

Semantic deletion review: C4566のcontinuous/cumulative dwell early-exitを稼働behaviorから除外した。既存・新規positionともactual-entry基準90分exitを維持する。C4566 moduleと証跡は非稼働履歴として削除しない。
