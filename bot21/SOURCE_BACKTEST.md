# Source Backtest

Implemented candidates from `C:\botter\backtest\output\backtest67_1_bot21`:

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

- The live normal-entry direction is intentionally inverted from the frozen backtest signal direction.
- A normal position closed by a confirmed MT5 TP deal creates at most one opposite-side reversal position using its own fill and the same configured SL/TP distances.
- Multiple confirmed signal cycles may coexist; normal and reversal positions share the `max_active_positions` cap, and reversal opens do not modify normal-signal consumption state.
- These inversion, reversal, and concurrent-cycle rules are live adaptations, not behavior claimed for the cited source backtest.
- Backtest signal: completed H1 bar, entry on next H1 open.
- Live signal: completed H1 bar fetched from MT5; latest possibly incomplete H1 bar is dropped.
- MT5 `HIST` bar timestamps were directly checked on CentOS via read-only EA IPC
  on 2026-07-30 and are treated as UTC before indicator calculation and
  stale-signal checks.
- Live entry: market order on the first runner cycle after the signal is detected. The default stale-signal guard skips entries more than 10 minutes after the intended next-H1 entry time.
- Backtest execution evidence: H1 resampled OHLC headline plus M1/tick replay diagnostics.
- Live execution: MT5 Bid/Ask market order, server SL/TP when real trading is enabled, bot-managed time close.
- Long entry uses Ask, short entry uses Bid. Long exit is Bid, short exit is Ask.
- MQL bridge execution must confirm `ResultRetcode()` and deal/order evidence. Python records live active state only after a symbol/magic/comment/side `POSITIONS` re-query uniquely confirms the position.
- Default market deviation is `max_deviation_points=20`.
- Real trading preflight requires a hedging account (`require_hedging_account=true`); netting/exchange modes are rejected for shared-account ownership safety.
- Live-order params were enabled by explicit user instruction on 2026-07-27. Service deployment, bridge attachment, or restart are still separate runtime actions.

Known differences and cautions:

- Broker symbol names may need `mt5_symbol` edits if the live account uses suffixes.
- Time-close uses actual live entry time plus `max_hold_bars` hours, not the historical intended entry timestamp.
- The stale-signal guard uses UTC after HIST timestamp normalization. Do not
  assume broker-local time unless a fresh read-only `HIST` check proves the EA
  changed its timestamp basis.
- Shadow mode checks SL/TP on runner polling snapshots, so it is not a substitute for server-side live SL/TP behavior.
- The observed reusable replay was already seen and must not be used for parameter changes or candidate re-ranking.
