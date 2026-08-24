# Source Backtest

- Bot: `bot26` / S26
- Runtime strategy ID: `PV2C520_DEVQ80_H75_FORWARD_R1`
- Source folder: `backtest/bot関連backtest/0_bot26実装PV2C520_OOS_001/`
- Candidate: `PV2C520_DEVQ80_H75_FORWARD_R1`
- Mapping: USTEC LONG、USOIL `lead5_corr_z:high`、threshold `0.8284896671815759`、context stale上限120秒
- Entry: signal decision後の最初の実Askで即時market entry。C4535の+1 bps継続確認pendingは使用しない
- Exit: broker確認済みactual entry時刻から壁時75分後の最初のBid

Observed leakcheckでC4535 v02が現行h75を下回ったため、2026-08-24に改善前へ戻した。比較はDEV正本とentry・exit・PnLが完全一致した固定再生で行った。

| dataset | h75 Base/Stress | C4535 Base/Stress | 判定 |
|---|---:|---:|---|
| dev_tick | `689.164 / 591.078` | `982.121 / 893.029` | C4535優位 |
| observed leakcheck_tick | `593.195 / 568.798` | `476.542 / 453.680` | h75優位 |

C4535は削除せず、`backtest/bot関連backtest/0_bot26実装PV2C520_OOS_001/best案/`の過学習疑いforward-only履歴として保存する。既観測leakcheckでの再調整は行わない。
