# Source Backtest

## Frozen mapping

- Bot: `bot23` / S23
- Strategy: `visual_loss_abort_g_failure_to_progress`
- Registry lineage: `man_028_v003`
- Campaign: `bot23_loss_abort_structural_20260814`
- Candidate: `loss_abort_structural_exit_v1`
- Frozen parameter hash: `e1b7ac7a0f6fa87d2fafed38ea7beb38ba7ecffd675fce800fd9a98f9e8c3a1b`
- Symbol/timeframe: XAUUSD, tick-derived confirmed M1 bars, next-cycle execution
- Status: `forward_only`; reusable evaluation data was already observed and was
  not used to alter this live specification.

## Frozen strategy parameters

- UTC session 13:00-18:00, impulse bars 8, impulse ATR 0.55
- add ATR 0.45, maximum positions 8, volume minimum 1.05
- basket target USD 10, basket stop USD 18, maximum hold 70 bars
- cooldown 8 bars
- failure-to-progress: from held bar 10, close when lifetime peak basket PnL is
  below USD 3
- no reverse-on-fail

## Evidence

- Dev base: PnL 483.874, PF 1.24331, max DD 109.621, 724 closed tickets,
  403 initial entries.
- Dev cost stress: PnL 366.905, PF 1.18163, max DD 108.493, 720 closed
  tickets, 400 initial entries.
- Observed reusable period (descriptive only): PnL 138.95, PF 1.40378,
  max DD 86.323, 180 closed tickets.

Primary artifacts are under
`backtest/検討中/bot23/bot23_loss_abort_structural_20260814/`.

## Live mapping notes

The live runner stores the lifetime peak of broker-reported basket PnL and uses
elapsed confirmed M1 bar time, not poll counts. Existing entry ownership is
tracked by symbol, magic, comment, ticket, and position identifier. The runner
starts shadow-only. Before any future first start, an old fixed4 S23 state file
must not be reused; identity mismatch intentionally fails closed.
