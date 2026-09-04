# Bot25 V24 local full re-audit — 2026-09-04

## Decision

Local candidate: **PASS for local canonical readiness, NO-GO for deployment or real activation**.

The local source is configured `live_trading_enabled=false` and
`shadow_forward_enabled=true`. No CentOS file, service, container, EA, broker
position, order, or Git remote was changed. Runtime approval requires a separate
fresh CentOS inventory/hash/canary audit and explicit user authorization.

## Preserved behavior

- Strategy identity: `bot25_v24_xauusd_virtual_bilateral_core_v001`.
- Episode start emits no broker OPEN. It records logical Long=1 and Short=1.
- `physical_seed_orders=0` remains frozen.
- A real 0.01-lot position is opened only through the frontier-add path.
- Logical side cap 6, ratio 3:1, V23 drought rule, release selection, and
  12-hour/feed-gap lifecycle remain unchanged.
- Exact non-flat state-v5/man231 or state-v6/V23 inventory can migrate only
  after state/broker ticket, stable position ID, broker open time, side, lot,
  symbol, magic, comment, pending-action, order, cap, ratio, and CAS checks pass.

## Corrected findings

1. File IPC could accept incomplete or mismatched response ownership and had no
   durable EA-side mutation claim. The bridge now uses unique request IDs,
   deadline framing, a durable claim, one Python publisher lock, one EA consumer,
   and response-ID correlation. A recovered OPEN/CLOSE claim is never replayed.
2. OPEN and CLOSE lacked a complete atomic account/inventory/ownership contract.
   The Python and MQL boundaries now validate account, server, symbol, magic,
   canonical comment, side, lot, position ID, owned-position count, owned orders,
   symbol admission, margin, and trading permissions.
3. Ambiguous CLOSE could be retried. A per-ticket submission marker is now saved
   before publishing CLOSE. Only a broker-proven no-fill clears it for retry.
4. Shadow startup could ignore bot25 broker positions when state had no real
   positions. It now blocks with `live_positions_without_real_state`; live mode
   also refuses synthetic shadow positions.
5. Real entry state used the poll/decision time instead of the broker position
   open time. Broker time is now mandatory, persisted, and compared during
   ownership/restart reconciliation; no poll-time fallback exists.
6. Current and migrated state validation was incomplete. Identity, counters,
   timestamps, position ticket/ID/open-time/ownership, close markers, legacy-core
   mapping, pending lifecycle, and frozen strategy/operational parameters now
   fail closed before migration or execution.
   A closed transitional core ID is retained as history; restart restores its
   virtual replacement without generating a real seed. This is regression-tested.
7. Position/history/CSV parsing accepted partial or structurally ambiguous data.
   Records now require strict fields, sentinel/count agreement, ordered unique
   bars, finite values, complete-position fee aggregation, replacement-file
   revalidation, and fsync-backed JSON/CSV persistence.
8. Dead pending-order/modify/cancel/tick command implementations were removed
   from the bot25 EA bridge. The advertised bridge surface contains only the
   commands used by V24.

## Validation

- Python unit/regression/boundary/failure suite: 20 tests, PASS.
- Built-in V24 self-test: PASS.
- Passive evidence tests: PASS.
- Shared live-safety self-test: PASS.
- Python compile: PASS.
- Top-level JSON parse: PASS.
- Docker Compose configuration parse: PASS.
- MetaEditor compile of `BotBridge_s25.mq5`: 0 errors, 0 warnings.
- Evidence-work-state v1 validator: PASS.
- Post-correction static audit: no additional in-scope production call path or
  cross-bot namespace defect found.

Expected ERROR/CRITICAL messages printed by the test suite are deliberate
failure-injection assertions and are followed by passing test results.

## Persistence and external-state proof

- Canonical lifecycle state SHA-256 before and after:
  `a8d97856495c30ad0370c0e78ccd7e236fd8eca59e8b4f89d13bddfe1ff2c15c`.
- Canonical passive observer state SHA-256 before and after:
  `df189bff4fc97c6dca53c4c9048e19d09f8b7e26b703007ef19aa30d1d8a8a77`.
- Final bytes of both state files match their pre-audit hashes. An early test
  touched the passive observer state; it was restored byte-for-byte and the
  test isolated. Canonical lifecycle state was never reset or migrated.
- Secret-bearing `live_config.py` and `startup.ini` were neither displayed nor
  copied into evidence.

## Folder hygiene

Unreferenced generic template residue was moved recoverably outside the canonical
bot folder:

- `signal_adapters.py` ->
  `C:/Users/muuma/Downloads/codex-temp/bot25_audit_20260904/retired_signal_adapters.py`
- `timeframe_config.py` ->
  `C:/Users/muuma/Downloads/codex-temp/bot25_audit_20260904/retired_timeframe_config.py`
- generated `__pycache__` ->
  `C:/Users/muuma/Downloads/codex-temp/bot25_audit_20260904/retired___pycache__`

## Runtime NO-GO boundary

Do not upload the local flat state over a CentOS state that represents open
positions. Before any future placement/restart, verify the actual server state
and broker inventory as one transaction: exact deployed hashes, exactly one
runner and one EA consumer, bridge name/version v8, account/server, state
identity, owned positions/orders, and no unresolved OPEN/CLOSE claim. First boot
must remain shadow-only. The canary must prove episode start has broker seed
orders 0 and logical Long/Short 1 each. Real activation additionally requires
`BOT25_ENABLE_REAL_TRADING=V24_VIRTUAL_CORE_LIVE_ACK` and explicit user approval.
