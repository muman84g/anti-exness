# Source Backtest

- source: `C:\botter\backtest\bot関連backtest\0_bot29実装PV2C531_OOS_001`
- candidate: `PV2C531_CORRECTED_DIAGNOSTIC_R1`
- source classification: `OBSERVED_CORRECTED_DIAGNOSTIC_PASS`
- selection eligible: `false`（観測済みreusableデータの修正診断。forward-only）
- frozen feature: rolling60 corr(`abs(logret1)`, `TickVolume`), `min_periods=30`
- gate: `ret25 > 0` and feature `>= 0.5213354405981323`
- live symbol contract: 注文対象は`USTEC`のみ。`XAUUSD`は非対象。Exness表示のUSTEC最小lot `0.05`に合わせ、live設定lotも`0.05`
- close execution: `LIVE_BOT_CLOSE_BASELINE_ja.md`準拠。USTEC 260-point guard、wide後3 stable polls、30分timeout、market-closed 60秒retry。固定UTC/DST session tableと週末precloseは不採用
- execution: completed midpoint M1、decision後45秒以内の初回tick、LONG Ask entry / Bid exit
- exit: decision時刻から壁時計60分後の初回tick。同時刻close/reentryは禁止
- corrected replay: 141 trades、base `+507.532935 bps`、PF `1.384074`、MDD `163.547208 bps`; stress `+468.483141 bps`、PF `1.349938`

Live HISTの`Volume`はMT5 barのtick volumeとして凍結generatorの`TickVolume`へ対応させる。5秒pollのため、raw replayの最初のtickそのものではなく、45秒gate内で観測した現在Askでshadow/live entryする近似差がある。
