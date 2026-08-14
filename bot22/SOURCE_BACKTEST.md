# Source Backtest

Implemented candidate from `C:\botter\backtest\output\backtest108_1_bot22`:

| Symbol | Spec | Params hash | Params | Dev PnL | Dev PF | Dev MDD | Dev Trades |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| EURUSD | `EURUSD_005_1h` | `f97149f97d028e98` | bb=20, std=2.0, width_lb=80, squeeze_q=0.35, pullback=8, SL=43, TP=48, hold=18 | 1205.4 | 1.378 | 207.0 | 255 |

Additional dev-only alignment audits:

- Causal M1 replay: PnL 1283.2, PF 1.433, MDD 191.5, Trades 261.
- 1.5x spread + 0.5 pip slippage: PnL 889.4, PF 1.284, MDD 245.8.
- 2.0x spread + 1.0 pip slippage: PnL 502.9, PF 1.151, MDD 302.0.
- Executed signal mismatches: 0.
- Same-minute SL/TP collisions: 0.
- H1 source provenance: 24,898 H1 bars checked; OHLC/spread mismatches 0; `available_at` mismatches 0.
- H1 anchor shift audit: 0 minute offset was best; shifted anchors were materially worse.
- Live decision replay against `live_s22_bot.py`: 2,480 decisions checked, mismatches 0.

Observed reusable replay:

- Source: `holdout_data/EURUSDm_M5.csv`, 2026-01-01 to 2026-05-29 JST.
- Result: PnL 189.3, PF 2.302, MDD 48.3, Trades 21.
- Status: `clean_reusable_eval=false`. This candidate was selected after observed reusable top-5 comparison, so it must not be reported as a clean reusable pass.

Backtest/live mapping:

- Backtest signal: completed H1 bar, entry on next H1 open.
- Live signal: completed H1 bar fetched from MT5; latest possibly incomplete H1 bar is dropped.
- MT5 `HIST` bar timestamps were directly checked on CentOS via read-only EA IPC
  on 2026-07-30 and are treated as UTC before indicator calculation and
  stale-signal checks.
- Live entry: market order on the first runner cycle after the signal is detected. The default stale-signal guard skips entries more than 5 minutes after the intended next-H1 entry time.
- Backtest execution evidence: H1 resampled OHLC headline plus causal M1 replay diagnostics. It is not true tick execution.
- Live execution: MT5 Bid/Ask market order, server SL/TP when real trading is enabled, bot-managed time close.
- Long entry uses Ask, short entry uses Bid. Long exit is Bid, short exit is Ask.
- MQL bridge execution must confirm `ResultRetcode()` and deal/order evidence. Python records live active state only after a symbol/magic/comment/side `POSITIONS` re-query uniquely confirms the position.
- Default market deviation is `max_deviation_points=20`.
- Real trading preflight requires a hedging account (`require_hedging_account=true`); netting/exchange modes are rejected for shared-account ownership safety.
- Default mode is live-order enabled by explicit user instruction on 2026-07-26. Service deployment, bridge attachment, or restart are still separate runtime actions.

Known differences and cautions:

- Broker symbol names may need `mt5_symbol` edits if the live account uses suffixes such as `EURUSDm`.
- Time-close uses actual live entry time plus `max_hold_bars` hours, not the historical intended entry timestamp.
- The stale-signal guard uses UTC after HIST timestamp normalization. Do not
  assume broker-local time unless a fresh read-only `HIST` check proves the EA
  changed its timestamp basis.
- Real-order mode uses server-side SL/TP; bot-managed time close still depends on runner cycles and bridge availability.
- The operational lot is `0.01`; the backtest reference lot was `0.1`.
- Entry-shift sensitivity is high: an impossible -60 minute entry was much stronger in diagnostics. This is not usable evidence and reinforces strict confirmed-H1-after-close entry.
- The observed reusable replay was already seen and must not be used for parameter changes or candidate re-ranking.
