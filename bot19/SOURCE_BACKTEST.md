# bot19 Source Backtest

## 元データ

- 元バックテスト番号: `backtest34`
- 元キャンペーンフォルダ: `C:\botter\backtest\output\backtest34_bot18_small_range_filter`
- 元run: `C:\botter\backtest\output\backtest34_bot18_small_range_filter\runs\20260705_2200_pilot_dev_tick_d10_e75_fs_m80_b2k`
- 参照候補: `baseline_recovery_loweff_cap10_short_deep8_extreme6_e75_fs_m80_b2k`
- variant: `value_d10_tp1`

## 固定した主要パラメータ

- symbol: `GBPUSD`
- distance: `10.0 pips`
- auto TP: `1 level`
- H1 gate: `ER >= 0.30`, `ADX >= 20.0`, `displacement ATR >= 1.50`
- base add distance: `20.0 pips`
- inactive add distance: `23.5 pips` when `gate_false`
- short exposure cap: base `10`, deep `8` at cycle equity `<= -55 USD`, extreme `6` at `<= -75 USD`
- base cap condition: gate false + SL streak `>= 4` + cycle equity `<= -20 USD`
- recovery close: SL streak `>= 6`, realized cycle loss `<= -10 USD`, cycle equity `>= 0.50 USD`
- fail-safe close: exposure block count `>= 2000` and cycle equity `<= -80 USD`
- max entry spread: `9 points`

## backtest34でのdev-only結果

- Trades: `1013`
- PnL: `+135.40 USD`
- PF: `1.1431`
- Max DD: `113.46 USD`
- PnL/trade: `0.1337 USD`
- Max positions: `16`
- Recovery close count: `19`

## 注意

この候補は dev tick で固定した候補です。短期の再利用二次評価では未完了サイクル込みでマイナスだったため、未観測forwardの証明ではありません。

entry latency stress では、サーバー側 pending stop 相当のゼロラグだけがプラスで、ローカル検知後の market entry は大きく崩れました。そのため S19 は `Buy Stop` / `Sell Stop` を前提にしています。
