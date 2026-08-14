# Bot23 porting audit

Audit date: 2026-08-14

## Result

Bot23 was rebuilt as a single-strategy runner after the canonical template was
corrected. The canonical and bot23 copies of `live_executor.py` and
`live_safety.py` are byte-identical. Strategy-specific runners are intentionally
not byte-identical across bot20-23, but the shared-account ownership invariants
applicable to bot23 are present.

## Cross-bot isolation

- bot20: magic `200020`, S20 bridge/state/log/IPC namespace
- bot21: magic `200021`, S21 bridge/state/log/IPC namespace
- bot22: magic `200022`, S22 bridge/state/log/IPC namespace
- bot23: magic `200023`, comment prefix `s23_loss_abort`, S23
  bridge/state/log/IPC namespace

Bot23 filters and validates symbol, magic, comment, ticket, and position
identifier. Same-symbol exposure belonging to another bot is neither adopted nor
closed. Live mode also requires hedging account mode, preventing netting-account
position merging from invalidating ticket ownership.

## Canonical corrections applied before rebuild

- Exact bridge identity and required capability preflight.
- Hedging-account preflight for live shared-account operation.
- Broker-position confirmation after OPEN; uncertain success fails closed.
- Position-ticket/identifier ownership validation before CLOSE.
- Close-deal reconciliation before local state is cleared.
- Two consecutive proven-flat confirmations for automatic clearing of specified
  high-risk OPEN/CLOSE blocks.

## Strategy parity

The live runner uses the frozen session, impulse bucket, ATR, add, maximum
position, target, stop, maximum-hold, cooldown, volume, and
failure-to-progress parameters documented in `SOURCE_BACKTEST.md`. The original
backtest deliberately maps impulse lengths above six to `ret10`; bot23 preserves
that behavior for the frozen `impulse_bars=8` candidate.

## Verification

- All Python files compile in bot20, bot21, bot22, bot23, and the canonical
  template folder.
- Self-tests pass for all four bots and the canonical template.
- Bot23 self-tests cover wrong magic, foreign same-magic comment, hedging-mode
  rejection, confirmed/unconfirmed OPEN, two-stage flat clearing, close
  reconciliation, partial basket reconciliation, and failure-to-progress timing.
- Agent/document layout validation passes.

No deployment, service restart, bridge attachment, push, or live-mode switch was
performed.
