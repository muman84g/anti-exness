# Source Backtest

## Frozen mapping

- Bot: `bot23` / S23
- Strategy: `visual_loss_abort_g_failure_to_progress`
- Registry lineage: `man_028_v004_v001` (parent `man_028_v004`, holdout-seen)
- Campaign: `backtest152`
- Candidate: `M_block14_loss27`
- Run: `20260815_regime_controls_forward_dev_v8`
- Symbol/timeframe: XAUUSD, confirmed-M1 signals with ordered Bid/Ask every-tick execution
- Status: `forward_only`; reusable evaluation data was already observed and was
  not used to alter this live specification.

## Frozen strategy parameters

- UTC session 13:00-18:00, impulse bars 8, impulse ATR 0.55
- add ATR 0.65, maximum positions 2, volume minimum 1.05
- no add when pre-add basket PnL is at least 30% of target (USD 3)
- block new baskets during 14:00-14:59 UTC
- after confirmed bot-owned daily realized PnL reaches -USD 27, block new baskets until the next UTC day
- basket target USD 10, basket stop USD 18, maximum hold 70 bars
- cooldown 8 bars
- failure-to-progress: from held bar 10, close when lifetime peak basket PnL is
  below USD 3
- no reverse-on-fail

## Evidence

- Dev base: PnL 459.535, PF 1.25125, every-tick MTM DD 111.320,
  691 tickets, 526 baskets.
- Dev cost stress: PnL 376.448, PF 1.20509, every-tick MTM DD 114.249,
  679 tickets, 513 baskets.
- Monthly dev PnL: Apr 61.220, May 180.050, Jun 195.530, Jul 22.735 USD.
- Holdout was not reused. This revision remains forward-only.

Primary artifacts are under
`backtest/output/backtest152/runs/20260815_regime_controls_forward_dev_v8/`.

## Live mapping notes

The live runner stores the lifetime peak of current Bid/Ask basket PnL and uses
UTC elapsed time. Close branches run every five-second poll while entry/add
signals remain confirmed-M1-only. Existing entry ownership is
tracked by symbol, magic, comment, ticket, and position identifier. The runner
records daily realized PnL only from confirmed bot-owned close deals.
