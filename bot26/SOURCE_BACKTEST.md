# Source Backtest

- Bot: `bot26` / S26
- Live strategy ID: `PV2C520_DEVQ80_H75_FORWARD_R1`
- Source folder: `backtest/検討中/chatgpt案/0_bot26実装PV2C520_OOS_001/`
- Parent candidate: `PV2C520_OOS_001_STALE_FC_DIAGNOSTIC_R1`
- Forward candidate: `PV2C520_DEVQ80_H75_FORWARD_R1`
- Frozen mapping: USTEC LONG、USOIL `lead5_corr_z:high`、threshold `0.8284896671815759`、context stale上限120秒、decision基準壁時計hold 75分

旧canonical ledgerは60分ではなく60本M1 bar-clockで生成されていた。`run_exact_bar_clock_replay.py`で親ledger 97/97件のsignal・entry・exit・PnLを完全再現し、差の原因を確定した。現行bot26の評価正本は`evaluate_live_aligned_current_vs_h75.py`とし、30秒entry gate、decision基準壁時計hold、actual exit後の再entryで再生する。

- Live-aligned DEV current h60: 367 trades、base `517.473 bps`、stress `404.920 bps`、PF `1.160`、MDD `267.158 bps`、前後半 `67.846 / 449.627 bps`
- Live-aligned DEV proposal h75: 317 trades、base `712.953 bps`、stress `615.302 bps`、PF `1.232`、MDD `266.998 bps`、前後半 `272.745 / 440.208 bps`
- Observed leakcheck current h60 diagnostic: 95 trades、base `488.435 bps`、stress `460.542 bps`、PF `1.644`、MDD `218.091 bps`
- Observed leakcheck proposal h75 diagnostic: 82 trades、base `593.534 bps`、stress `569.206 bps`、PF `2.089`、MDD `149.506 bps`

h75はDEVで固定選択し、既観測leakcheckは変更後の診断確認に限定した。forward-only identityでありclean reusable PASSへ読み替えない。
