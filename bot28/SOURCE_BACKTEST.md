# Source Backtest

- Bot: `bot28` / S28
- Live strategy ID: `PV2C560_DEVQ80_H75_FORWARD_R1`
- Source folder: `backtest/bot関連backtest/0_bot28実装PV2C560_OOS_001/`
- Parent candidate: `PV2C560_CORRECTED_DIAGNOSTIC_R1`
- Forward candidate: `PV2C560_DEVQ80_H75_FORWARD_R1`
- Frozen feature: rolling60 corr(`logret1^2`, lag1(`logret1^2`)), `min_periods=30`
- Gate: `ret25 > 0` and feature `>= 0.1507958826882894`
- Live symbol contract: 注文対象は`USTEC`のみ。`XAUUSD`は非対象。Exness表示のUSTEC最小lot `0.05`に合わせ、live設定lotも`0.05`
- Close execution: `LIVE_BOT_CLOSE_BASELINE_ja.md`準拠。USTEC 260-point guard、wide後3 stable polls、30分timeout、market-closed 60秒retry。固定UTC/DST session tableと週末precloseは不採用
- Execution: completed midpoint M1、decision後45秒以内の初回tick、LONG Ask entry / Bid exit、decision基準hold 75分
- DEV: 494 trades、base `1297.385 bps`、stress `1146.863 bps`、PF `1.274`、MDD `316.730 bps`、前後半 `581.671 / 715.714 bps`
- Observed leakcheck fixed diagnostic: 133 trades、base `684.909 bps`、stress `643.791 bps`、PF `1.569`、MDD `187.121 bps`、前後半 `421.534 / 263.375 bps`

提案はDEVだけで固定後、既観測leakcheckへ一回固定再生した。`selection_eligible=false`、forward-onlyであり、既観測結果をclean reusable PASSへ読み替えない。
