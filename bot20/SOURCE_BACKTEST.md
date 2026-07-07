# Source Backtest

Implemented candidate:

- `C:\botter\backtest\output\backtest43_bot20_gold_regime_crossasset_ideas`
- Strategy group: `large_candle_short_basket_m1`
- Variant: `confirm_refine_top_45_04`

Reference results from the dev run:

- Profit: `+1464.20 USD`
- PF: `2.154`
- Baskets: `98`
- Positions: `464`
- Max DD: `189.36`

Stress summary:

- Worst stress Profit: `+1254.09`
- Worst stress PF: `1.967`
- Worst stress Max DD: `194.12`
- DevWeak: `0`

Holdout reusable eval:

- `C:\botter\backtest\output\backtest43_bot20_gold_regime_crossasset_ideas\candidates\large_candle_short_basket_m1\runs\20260707_2137_reusable_eval_holdout_confirm_refine_top_once`
- Profit: `+581.28`
- PF: `6.606`
- Baskets: `4`
- Positions: `24`
- Max DD: `81.94`
- Status: positive but `too_few_baskets`, so not a formal holdout clear.

Live implementation differences:

- Uses market SELL entries after the 45-minute M1 confirmation condition is observed, not historical next-M1-open fills.
- Uses MT5 reported floating PnL for live basket DD/profit-only add.
- Default config is shadow-forward until `live_trading_enabled` is changed.
- Signal hours assume MT5 bar timestamps match the UTC convention used in backtest. If the broker server clock is offset, adjust `signal_session_hours_utc`.
