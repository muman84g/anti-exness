# Source Backtest

- Bot: `bot27` / S27
- Runtime strategy ID: `PV2C859_L3_ACTIVITY_VSA_FORWARD_R1`
- Source folder: `backtest/bot関連backtest/0_bot27実装PV2C859_OOS_001/`
- Candidate: `PV2C859_DEVQ825_H90_FORWARD_R1`
- Mapping: USTEC LONG、`ret25_sign_long`、`absret_std_ratio30_120:low`、threshold `0.6693523825105777`
- Live symbol contract: 注文対象は`USTEC`のみ。`XAUUSD`は非対象。Exness表示のUSTEC最小lot `0.05`に合わせ、live設定lotも`0.05`
- Close execution: `LIVE_BOT_CLOSE_BASELINE_ja.md`準拠。USTEC 260-point guard、wide後3 stable polls、30分timeout、market-closed 60秒retry。固定UTC/DST session tableと週末precloseは不採用
- Entry: signal decision後30秒以内の最初の実Askでmarket entry
- Inventory extension (2026-08-27): raw signal・entry時計・C4560 exitを変えず、first-freeの2独立ポジションに拡張。凍結DEVは526→1,008 trades、Base PF 1.4279→1.4363、MTM MDD 283.30→606.60bps。observed leakcheckは142→275 trades、Base PF 2.2843→2.3323、MTM MDD 174.70→333.96bps
- Rejected lane-2 entry forward (2026-08-27): lane 2だけthreshold `0.69`に緩和する案はtrade数と総PnLが小幅増加したが、DEV・observedともPFが低下するため不採用。時間帯限定の緩和も後付け仮説のため不採用
- Selected lane-2 exit forward (2026-08-27): entryは両laneとも現行thresholdに統一。lane 1はC4560 `floor=+4bps / continuous=20m / hard hold=90m`を固定し、lane 2だけ`floor=+2bps / continuous=20m / hard hold=90m`。現行2-lane比でDEVは1,008→1,026 trades、Stress PnL 3,015.34→3,026.97bps、PF 1.3900→1.3924、MTM MDD 634.33→627.03bps。observed leakcheckは275→277 trades、Stress PnL 2,168.47→2,168.87bps、PF 2.2655→2.2705、MTM MDD 337.48→337.48bps
- Exit: broker確認済みactual entry時刻から90分後の最初のBid。C4566のvolatility dwell早期exitは使用しない

## 2026-08-28 Short overlay promotion

- Long baseline: 3 lanes。lane 1/3は+4bps連続20分、lane 2は+2bps連続20分。lane 3 entry spread上限は`0.595764962212364bps`
- Selected Short overlay: `Activity 0.75 + VSA 0.75`。Activityは直前高activity/no-progressからの直前安値break、VSAは高activity・上ヒゲ比率0.35・陰線・EMA20上
- Short exit: Ask基準+4bps連続10分、actual entry基準60分hard hold
- Forward tick fixed comparison (`2026-08-14 05:43:00.026Z` - `2026-08-27 15:28:56.547Z`): oldはShort 146件、+232.59bps、PF 1.365、合成MTM DD 459.54bps、日次σ 135.32。new path案は114件、+206.04bps、PF 1.479、合成MTM DD 477.96bps、日次σ 143.40。安定性優先でoldを採用
- Live lot mapping: USTEC最小lot`0.05`では0.75倍lotを直接表現できないため、Long unitを`0.20`、Shortを`0.15`として全体4倍scaleし、相対露出を正確に維持。絶対USD PnL/DDも従来0.05基準より4倍になる
- Live/backtest difference: Short Closeはbridgeの`MidClose`、Open/High/LowはMT5 Bid barを使う。過去tick backtestは全OHLCがBid/Ask midpointのため、髭・安値判定に小さなbroker-bar近似差が残る
- State migration: 旧`PV2C859_C4560_GAP15_FORWARD_R1` stateを互換IDとして受け入れ、既存Long positionのticket/lane/exit stateを維持する
- Runtime action: ローカルファイル編集とno-order testのみ。deploy/restart/live switchingは未実施

Observed leakcheckでC4566 v01がPV2C859 h90をPnL・PF・MDDで下回ったため、2026-08-24に改善前へ戻した。比較はDEV正本とentry・exit・exit reason・PnLが完全一致した固定再生で行った。

| dataset | h90 Base/Stress | C4566 Base/Stress | 判定 |
|---|---:|---:|---|
| dev_tick | `1507.463 / 1373.737` | `1725.234 / 1544.597` | C4566優位 |
| observed leakcheck_tick | `981.339 / 945.620` | `819.865 / 769.937` | h90優位 |

C4566は削除せず、`backtest/bot関連backtest/0_bot27実装PV2C859_OOS_001/良案_C4566_v01_gap補正/`の過学習疑いforward-only履歴として保存する。既観測leakcheckでの再調整は行わない。
