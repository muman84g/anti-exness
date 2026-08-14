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

## 2026-08-15 forward-only true-tick revision

- Candidate: `M_block14_loss27`, registry `man_028_v004_v001`, run
  `backtest152/runs/20260815_regime_controls_forward_dev_v8`.
- Preserved: bot number, symbol, magic, comment, bridge, state identity,
  ownership checks, target USD 10, stop USD 18, failure-to-progress, 70-minute
  max hold, 8-minute cooldown, and fixed 0.01 lot.
- Changed intentionally: add distance 0.45 to 0.65 ATR30, maximum positions 8
  to 2, no add at or above USD 3 basket PnL, no new basket during 14 UTC, and
  no new basket after confirmed daily realized PnL reaches -USD 27.
- Execution correction: close branches now use current executable Bid/Ask on
  every five-second poll, before the confirmed-M1 new-bar guard. Entry/add
  signals remain confirmed-M1-only. Quote-based close monitoring continues
  when M1 history retrieval is temporarily unavailable.
- Daily accounting is bot-owned only and advances from confirmed CLOSEDEAL net
  PnL in live mode; the state keys are backward-compatible additions under the
  unchanged version-2 identity.
- Residual limitation: five-second File IPC polling can miss a shorter
  intrapoll touch and is not identical to every-tick backtest execution.

## 2026-08-15 manual-close reconciliation repair

- Preserved fail-closed ownership checks and required broker deal confirmation.
- Replaced repeated narrow-window-only lookups with a bounded 30-day fallback
  when MT5 explicitly reports no matching deal.
- Added a regression check proving that a manually closed ticket is removed
  from state without leaving `close_deal_not_confirmed` latched.
