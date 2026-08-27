# Bot25 man_231 porting audit

## Preserved from the frozen candidate

- XAUUSD, completed Bid M5, ATR14, EMA200, strict radius-2 pivots.
- 0.10 ATR break buffer, 0.50 ATR frontier add, six tickets per side, 3:1 cap.
- Continuous bilateral seed, best-price core retention, profitable satellites
  closed newest first, EMA retouch/opposite-break release, 12-hour episodes.
- Base shadow cost proxy: 0.030 adverse price on entry and close.

## Unified with bot23 operations

- Dedicated magic/comment and bridge IPC namespace.
- Configured account login/server check without logging either value.
- Hedging and trade-permission preflight before real orders.
- Atomic versioned state, pre-request OPEN reservation, restart reconciliation,
  exact owned-position confirmation, pending CLOSE retry, market-closed defer.
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
