# Source Backtest

- Bot: `bot25` / S25
- Candidate: `h14_18_h120_tp12_dd40_vol105_impulseonly_all_all:target16`
- Idea/candidate: `man_028_v002` / `bot23_fixed3_trade_preserving_exit_opt_v2`
- Frozen hash: `54324fec28b2d9bffb9e01f73e5181d74e6d6a3d377fb86c1373934d0377be2f`
- Source: `backtest/検討中/bot23/bot23_fixed3_opt2_20260814/`
- Execution: XAUUSD true ticks, signals derived from confirmed M1, next-cycle execution

Frozen live mapping: initial entries UTC 14-18, long-only, ATR30/ATR120 volume
ratio minimum 1.05, H1 and H4 slopes each above 0.60 ATR120, 20-bar impulse
above 0.60 ATR60. Favorable adds may occur in the underlying combo's UTC
13-22 session at 0.80 ATR30 while basket PnL is positive, maximum 4 positions.
Target is USD 16 per position; DD stop is USD 40 times square-root exposure;
trail arms at USD 10 per position with USD 6 times square-root exposure
giveback; maximum hold 120 bars; profitable opposite H1/H4 trend closes.

Dev: PnL 227.898, PF 1.47219, DD 198.603, 99 closed tickets. Reusable
evaluation: PnL 153.018, PF 1.97092, DD 103.0085, 36 closed tickets.
Backtest costs include observed spread and adverse slippage; live execution uses
broker fills and stores broker-confirmed entry/close prices.
