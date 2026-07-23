# Source Backtest

Implemented candidates from `C:\botter\backtest\output\backtest67_1`:

| Symbol | Spec | Params | Dev PnL | Dev PF | Dev MDD | Dev Trades |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| US500 | `US500_137_1h` | period=31, cycle_atr=0.18, SL=24, TP=32, hold=16 | 924.76 | 1.326 | 216.00 | 253 |
| AUDUSD | `AUDUSD_021_1h` | period=24, cycle_atr=0.14, SL=32, TP=32, hold=12 | 699.60 | 1.331 | 209.20 | 252 |
| USDJPY | `USDJPY_035_1h` | period=30, cycle_atr=0.14, SL=32, TP=64, hold=12 | 1866.60 | 1.410 | 303.10 | 292 |

Leak and execution audits:

- Dev exact prefix-vs-extended audit: PASS for all three.
- Dev M1 sequential comparison stayed positive:
  - US500: 911.03 PnL, PF 1.337, MDD 272.02, 255 trades
  - AUDUSD: 750.60 PnL, PF 1.365, MDD 242.60, 256 trades
  - USDJPY: 1744.90 PnL, PF 1.397, MDD 331.50, 291 trades
- Observed reusable tick replay, clean_reusable_eval=false:
  - US500: PnL 200.42, PF 2.547, MDD 57.59, Trades 17
  - AUDUSD: PnL 55.70, PF 1.797, MDD 49.50, Trades 14
  - USDJPY: PnL 33.80, PF 1.618, MDD 51.70, Trades 10
  - All are inconclusive because each has fewer than 80 trades.

Backtest/live mapping:

- Backtest signal: completed H1 bar, entry on next H1 open.
- Live signal: completed H1 bar fetched from MT5; latest possibly incomplete H1 bar is dropped.
- MT5 `HIST` bar timestamps are treated as broker server time, currently `Europe/Athens`, and converted to UTC before indicator calculation and stale-signal checks.
- Live entry: market order on the first runner cycle after the signal is detected. The default stale-signal guard skips entries more than 10 minutes after the intended next-H1 entry time.
- Backtest execution evidence: H1 resampled OHLC headline plus M1/tick replay diagnostics.
- Live execution: MT5 Bid/Ask market order, server SL/TP when real trading is enabled, bot-managed time close.
- Long entry uses Ask, short entry uses Bid. Long exit is Bid, short exit is Ask.
- MQL bridge execution must confirm `ResultRetcode()` and deal/order evidence. Python records live active state only after a symbol/magic/comment/side `POSITIONS` re-query uniquely confirms the position.
- Default market deviation is `max_deviation_points=20`.
- Real trading preflight requires a hedging account (`require_hedging_account=true`); netting/exchange modes are rejected for shared-account ownership safety.
- Default mode is shadow-forward. Real trading, service deployment, bridge attachment, or restart were not authorized by this source note.

Known differences and cautions:

- Broker symbol names may need `mt5_symbol` edits if the live account uses suffixes.
- Time-close uses actual live entry time plus `max_hold_bars` hours, not the historical intended entry timestamp.
- The stale-signal guard uses UTC after broker-timezone conversion. If the broker server timezone differs from `Europe/Athens`, update `broker_timezone` before running.
- Shadow mode checks SL/TP on runner polling snapshots, so it is not a substitute for server-side live SL/TP behavior.
- The observed reusable replay was already seen and must not be used for parameter changes or candidate re-ranking.
