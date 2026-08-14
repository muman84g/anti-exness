# Source Backtest

- Bot: `bot24` / S24
- Candidate: `visual_no_adverse_c:target16`
- Idea/candidate: `man_028_v002` / `bot23_fixed3_trade_preserving_exit_opt_v2`
- Frozen hash: `54324fec28b2d9bffb9e01f73e5181d74e6d6a3d377fb86c1373934d0377be2f`
- Source: `backtest/検討中/bot23/bot23_fixed3_opt2_20260814/`
- Execution: XAUUSD true ticks, signals derived from confirmed M1, next-cycle execution

Frozen live mapping: UTC 13-18; breakout plus 10-bar impulse; impulse ATR
0.60; 30-bar breakout; ATR30/ATR90 volume ratio minimum 1.05; favorable adds
at 0.45 ATR30; maximum 8 positions; basket target USD 16; basket stop USD 48;
maximum hold 120 M1 bars; cooldown 3 bars.

Dev: PnL 326.378, PF 1.20840, DD 294.609, 307 closed tickets. Reusable
evaluation: PnL 115.704, PF 1.34541, DD 114.823, 83 closed tickets.
Backtest costs include observed spread and adverse slippage; live execution uses
broker fills and stores broker-confirmed entry/close prices.
