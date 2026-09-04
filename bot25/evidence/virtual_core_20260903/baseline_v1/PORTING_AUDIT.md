# Bot25 V23 porting audit

## 2026-08-28 passive evidence and reconciliation hardening

- Preserved a prior non-recoverable sync block when a later read-only failure
  is recoverable; the transient reason is warning-deduplicated.
- Added exact state-lot versus broker-volume comparison to owned-position sync.
- Added an execution-independent frontier observer, causal state tagger,
  restart-deduplicated 1/5/15/30/60/120 minute markouts, and separate atomic
  observer state.
- Added flush/fsync to trade and passive CSV appends and a recoverable
  `schema_rollover` audit row after incompatible headers are archived.
- Preserved V23 thresholds, entry/add/close ordering, lot, magic, comment,
  state identity, live/shadow flags, bridge, and IPC names.

## Preserved from the frozen candidate

- XAUUSD, completed Bid M5, ATR14, EMA200, strict radius-2 pivots.
- 0.10 ATR break buffer, 0.50 ATR frontier add, six tickets per side, 3:1 cap.
- V23 delta only: productive close >=0.10 USD, drought >120 minutes, and block
  only a prospective minority-side frontier add.
- Continuous bilateral seed, best-price core retention, profitable satellites
  closed newest first, EMA retouch/opposite-break release, 12-hour episodes.
- Base shadow cost proxy: 0.030 adverse price on entry and close.

## Unified with bot23 operations

- Dedicated magic/comment and bridge IPC namespace.
- Configured account login/server check without logging either value.
- Hedging and trade-permission preflight before real orders.
- Atomic versioned state, pre-request OPEN reservation, restart reconciliation,
  exact owned-position confirmation, pending CLOSE retry, market-closed defer.
- Retired bot25 state is archived and replaced only after bridge/account/symbol
  preflight, successful empty bot25-scoped position and order queries, and a
  content-hash compare-and-swap check. Unknown inventory and changed/corrupt or
  foreign state remain fail-closed.
- Exact man_231 state-v5 is a compatible predecessor. It becomes V23 state-v6
  only after read-only owned inventory reconciliation and a state-file CAS;
  existing magic, comments, tickets, episode and pending-close state remain.
- Strict CSV header, causal timestamps, one decision receipt per processed bar,
  diagnostic coalescing, rotating application and container logs.
- Position-ticket / position-ID / deal-ID separation, two-phase close evidence,
  complete-position broker cost aggregation, and deal-ID log deduplication.
- Manual-action alert dedupe/rate limit with environment-only webhook values.
- Explicitly authorized live mode plus a separate environment acknowledgement.

## Intentionally not copied from bot23

- Four-lane allocator, ZA signal IDs, `reverse_d60`, daily realized-loss logic,
  and lane readiness are strategy semantics, not operational infrastructure.
- Bot23's ZA-specific counterfactual markout observer is not treated as a
  man_231 execution dependency. Bot25's M5 receipts and episode/inventory fields
  are the canonical forward observation record.

## Current boundary

Source, bridge contract, Compose wiring, no-order tests, and both live gates are
prepared. Compilation/attachment of the EA and service recreation must still be
confirmed on the CentOS runtime before the port is described as active.
