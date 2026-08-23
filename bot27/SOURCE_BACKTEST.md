# Source Backtest

- Bot: `bot27` / S27
- Live strategy ID: `PV2C859_DEVQ825_H90_FORWARD_R1`
- Source folder: `backtest/検討中/chatgpt案/0_bot27実装PV2C859_OOS_001/`
- Parent candidate: `PV2C859_CORRECTED_DIAGNOSTIC_R1`
- Forward candidate: `PV2C859_DEVQ825_H90_FORWARD_R1`
- Frozen mapping: USTEC LONG、`ret25_sign_long`、`absret_std_ratio30_120:low`、threshold `0.6693523825105777`、actual-entry基準hold 90分
- DEV: 437 trades、base `1507.463 bps`、stress `1373.737 bps`、PF `1.404`、MDD `288.966 bps`、前後半 `732.255 / 775.207 bps`
- Observed leakcheck fixed diagnostic: 121 trades、base `981.339 bps`、stress `945.620 bps`、PF `2.109`、MDD `130.108 bps`、前後半 `438.681 / 542.658 bps`

提案はDEVだけで固定後、既観測leakcheckへ一回固定再生した。`selection_eligible=false`、forward-onlyであり、既観測結果をclean reusable PASSへ読み替えない。
