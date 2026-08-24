# Source Backtest

- Bot: `bot27` / S27
- Runtime strategy ID: `PV2C859_DEVQ825_H90_FORWARD_R1`（既存state identity互換のため維持）
- Candidate revision: `PV2C859_C4566_V01_FORWARD_R1`
- Source folder: `backtest/bot関連backtest/0_bot27実装PV2C859_OOS_001/良案_C4566_v01_gap補正/`
- Parent candidate: `PV2C859_CORRECTED_DIAGNOSTIC_R1`
- Entry parent: `PV2C859_DEVQ825_H90_FORWARD_R1`
- Frozen mapping: USTEC LONG、`ret25_sign_long`、`absret_std_ratio30_120:low`、threshold `0.6693523825105777`、actual-entry基準hard hold 90分
- C4566 exit: vol30 q2帯 `(1.6831322559641573, 2.4177661654559843]` は10分grace + `+4bps`連続20分。外側は5分grace + `+4bps`累積10分。quote gap `>15秒`は時計へ算入しない
- Corrected DEV tick: 591 trades、base `1725.234 bps`、stress `1544.597 bps`、PF `1.458`、MDD `177.348 bps`、validation前後半 `687.835 / 306.355 bps`

C4566はDEV tickだけで固定したforward-only候補。holdout/leakcheckを探索・順位付けに使っておらず、clean reusable PASSへ読み替えない。C4564の無tick gap会計不具合をlive移植パリティで検出し、同条件のgap会計だけを補正した。
