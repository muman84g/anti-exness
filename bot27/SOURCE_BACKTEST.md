# Source Backtest

- Bot: `bot27` / S27
- Runtime strategy ID: `PV2C859_DEVQ825_H90_FORWARD_R1`
- Source folder: `backtest/bot関連backtest/0_bot27実装PV2C859_OOS_001/`
- Candidate: `PV2C859_DEVQ825_H90_FORWARD_R1`
- Mapping: USTEC LONG、`ret25_sign_long`、`absret_std_ratio30_120:low`、threshold `0.6693523825105777`
- Entry: signal decision後30秒以内の最初の実Askでmarket entry
- Exit: broker確認済みactual entry時刻から90分後の最初のBid。C4566のvolatility dwell早期exitは使用しない

Observed leakcheckでC4566 v01がPV2C859 h90をPnL・PF・MDDで下回ったため、2026-08-24に改善前へ戻した。比較はDEV正本とentry・exit・exit reason・PnLが完全一致した固定再生で行った。

| dataset | h90 Base/Stress | C4566 Base/Stress | 判定 |
|---|---:|---:|---|
| dev_tick | `1507.463 / 1373.737` | `1725.234 / 1544.597` | C4566優位 |
| observed leakcheck_tick | `981.339 / 945.620` | `819.865 / 769.937` | h90優位 |

C4566は削除せず、`backtest/bot関連backtest/0_bot27実装PV2C859_OOS_001/良案_C4566_v01_gap補正/`の過学習疑いforward-only履歴として保存する。既観測leakcheckでの再調整は行わない。
