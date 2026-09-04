# Bot25 V24 virtual-core change audit

## 2026-09-03 virtual bilateral core

- Replaced the physical BUY/SELL episode seeds with one logical virtual core
  per side. Virtual cores create no broker order, carry no PnL, and incur no
  spread, slippage, commission, or swap.
- Capacity six and the 3:1 ratio now use logical counts. Therefore each side can
  hold at most five real frontier tickets while retaining the same logical
  maximum of six.
- Release closes every profitable real active-side ticket newest-first. The
  virtual core supplies the former always-present core invariant.
- Feed-gap and 12-hour episode state continue even when the real inventory is
  empty, so a virtual-only episode still resets and rearms causally.
- Exact state-v5/man231 and state-v6/V23 inventory can migrate when stored and
  broker position ticket, stable identity, open time, side, lot, magic, and comment match exactly, orders
  and pending lifecycle actions are absent, and the state file passes CAS.
- One best-price existing position per side is retained as a transitional
  physical core. It suppresses that side's virtual core until the existing exit
  path closes it, preventing double-counting and any replacement seed order.
- A shadow-only canary with migrated real inventory is read-only: it verifies an
  exact state/broker ownership match but does not reconcile, add, close, or
  simulate entries or exits into the canonical state.
- The original audit used shadow-only params. On 2026-09-04 the user authorized
  live params and the V24 Compose acknowledgement for Git delivery. Remote
  deployment/restart and broker runtime validation remain outside this change.

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

## Preserved from the frozen V23 parent

- XAUUSD, completed Bid M5, ATR14, EMA200, strict radius-2 pivots.
- 0.10 ATR break buffer, 0.50 ATR frontier add, six tickets per side, 3:1 cap.
- V23 delta only: productive close >=0.10 USD, drought >120 minutes, and block
  only a prospective minority-side frontier add.
- EMA retouch/opposite-break release and 12-hour episodes.
- Base shadow cost proxy: 0.030 adverse price on entry and close.

## Unified with bot23 operations

- Dedicated magic/comment and bridge IPC namespace.
- Configured account login/server check without logging either value.
- Hedging and trade-permission preflight before real orders.
- Atomic versioned state, pre-request OPEN reservation, restart reconciliation,
  exact owned-position confirmation, and durable per-ticket CLOSE submission.
  Only a proven no-fill can retry; an ambiguous close response cannot be replayed.
- Request-ID-correlated file IPC uses an EA-side durable claim and fails closed
  on stale, mismatched, malformed, or unresolved responses.
- Retired bot25 state is archived and replaced only after bridge/account/symbol
  preflight, successful empty bot25-scoped position and order queries, and a
  content-hash compare-and-swap check. Unknown inventory and changed/corrupt or
  foreign state remain fail-closed.
- Exact man_231 state-v5 and V23 state-v6 are compatible predecessors for V24.
  Non-flat migration requires read-only exact owned-inventory reconciliation,
  zero orders/reservations, ratio/cap compliance, and a state-file CAS.
- Strict CSV header, causal timestamps, one decision receipt per processed bar,
  diagnostic coalescing, rotating application and container logs.
- Position-ticket / position-ID / deal-ID separation, two-phase close evidence,
  complete-position broker cost aggregation, and deal-ID log deduplication.
- Broker position open time is authoritative for real-entry state and evidence;
  a poll timestamp is not used as a fallback.
- Manual-action alert dedupe/rate limit with environment-only webhook values.
- Explicitly authorized live mode plus a separate environment acknowledgement.

## Intentionally not copied from bot23

- Four-lane allocator, ZA signal IDs, `reverse_d60`, daily realized-loss logic,
  and lane readiness are strategy semantics, not operational infrastructure.
- Bot23's ZA-specific counterfactual markout observer is not treated as a
  man_231 execution dependency. Bot25's M5 receipts and episode/inventory fields
  are the canonical forward observation record.

## Current boundary

The local source and shadow tests may be prepared, but CentOS deployment,
Compose recreation, EA attachment, state migration, and real orders remain
explicitly unauthorized.
