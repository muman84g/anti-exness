# bot19 Source Backtest

## Mapping audit status

- 現在の `live_s19_bot.py` / `s19_params.json` は本書のD10/TP1候補と整合する。
- ただし `backtest34/HANDOFF_20260705.md` では、この候補は `数値上トップ、未昇格`、後続判断でも `forward-readyではない` と記録されている。
- backtest34および全campaignのmanifest付きGBPUSD物理tick成果物を再検索したが、現D10実装に対応する最高値は +135.40 USDで、別の高成績source runは確認できなかった。
- よって本書は「現在置かれているD10実装の由来」は示すが、「ユーザーが意図したbot19採用候補」を証明しない。bot19への昇格判断またはbot番号mappingが不整合の可能性がある。

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

## 2026-07-11 GBPUSDm診断

`GBPUSDm_M1.csv` をOHLC/OLHCの4点synthetic tickへ変換し、固定S19候補をdev-only診断した結果はいずれも大幅マイナスでした。ただし同じ方法のGBPUSD対照も既知の実tick結果を再現せず、取引数を約11〜13倍に過剰生成しました。

このM1 synthetic診断は無効対照であり、現在の採否には使いません。詳細はbacktest34の `S19_GBPUSDM_M1_DIAGNOSTIC_20260711.md` を参照してください。

## 2026-07-11 GBPUSDm physical tick比較

- `GBPUSDm_tick.csv` を取得後、元と実質同一期間の物理tickで固定候補を再生。
- GBPUSDは現行runnerでも `+135.40 USD / PF 1.1431 / 1013 trades / MDD 113.46` を完全再現。
- spread gateは通常spread + 2 pointsで比較: GBPUSD median 7 / gate 9、GBPUSDm median 10 / gate 12。
- GBPUSDm gate 12: `-72.91 USD / PF 0.9366 / 1160 trades / MDD 173.77`。
- GBPUSDmはauto TP数がほぼ同じだがSLが148件増え、固定候補の期待値を再現しなかった。
- 結論: 現D10実装についてGBPUSD元結果は正しく、GBPUSDmでは再現しない。ただし意図したbot19候補とのmappingが未確認なので、これをbot19全体の採否結論にはしない。詳細はbacktest34の `S19_GBPUSDM_PHYSICAL_TICK_COMPARISON_20260711.md`。
