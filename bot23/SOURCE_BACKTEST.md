# Source Backtest

## Mapping

- Bot: `bot23`
- Service: not added / not deployed
- Strategy ID: `bot23_chisiki_reactvol_fixed4`
- Source campaign: `backtest/output/chisiki_x_bot_ideas_20260706`
- Source run: `runs/20260730_1722_chisiki_prebot_fixed4`
- Idea: `man_028`
- Symbol: `XAUUSD`
- Signal/execution timeframe: M1 confirmed bars, next-cycle market execution
- Data boundary: dev_data fixed candidates only; holdout_data not used for this porting step

## Fixed Candidates

- `visual_loss_abort_g`: PnL 472.19, PF 1.269, DD 183.55, 495 trades, stress 317.44/PF 1.175.
- `visual_no_adverse_c`: PnL 346.69, PF 1.292, DD 152.24, 252 trades, stress 384.40/PF 1.341.
- `h14_18_h120_tp12_dd40_vol105_impulseonly_all_all`: PnL 365.32, PF 2.389, DD 80.60, 82 trades, stress 342.34/PF 2.297.
- `visual_break_reverse_a`: PnL 210.13, PF 1.181, DD 191.02, 305 trades, stress 173.35/PF 1.147. This is the weakest PF candidate and should be monitored first.
- Combined dev result: PnL 1394.33, PF 1.320, DD 340.47, MDD/PnL 0.24, 1134 trades.

## Pre-Bot Audit

- Dev tick/BidAsk replay: PASS in source run.
- Prefix/price audit: `prefix_mismatches=0`, `price_bad=0` in source run.
- Cost stress: all four remained PnL positive in source run.
- Timezone: source timestamps UTC; live EA HIST is normalized as UTC.
- Entry/close order: live closes existing strategy basket before considering a new entry for that same strategy.
- Same-bar re-entry guard: after a basket close, the same strategy skips a new entry on the same signal bar except the explicit `visual_break_reverse_a` stop-reverse action.

## Live Alignment

- Initial mode: `live_trading_enabled=false`, `shadow_forward_enabled=true`.
- Magic/comment isolation:
  - `230001` / `s23_loss_abort_g`
  - `230002` / `s23_no_adverse_c`
  - `230003` / `s23_reactvol_h14_18`
  - `230004` / `s23_break_reverse_a`
- Price side: long entry uses Ask, short entry uses Bid; basket PnL uses Bid for long exits and Ask for short exits.
- Position/order sync: scoped by symbol and per-strategy magic; unknown live exposure blocks new entries fail-closed.
- State isolation: each strategy has its own basket, cooldown, reverse guard, last add price, sync block, and close timestamp.

## Known Differences

- BT used tick-derived M1 Bid/Ask. EA HIST provides OHLC only, so bot23 live feature bars use HIST OHLC and current `INFO` Bid/Ask for spread guard and current basket PnL.
- The `h14_18...` candidate is mapped to the same fixed entry/exit params, but not a byte-for-byte copy of the historical ReactVol research runner.
- No Docker service, deploy, restart, bridge attach, settings change, or live switch is included in this commit.
