# Bot25 V24 virtual-core change audit

## 2026-09-05 L05 additive exit update

- Added frozen `L05` as an exit-only overlay; V24 entry, virtual core,
  frontier, cap/ratio, native release, drought, expiry, and feed-gap behavior
  remain unchanged.
- L05 requires completed-M5 causal order: post-entry opposite native pivot
  break, later reclaim, then later re-loss. It selects only tickets still
  losing after the configured adverse-close allowance.
- Selected tickets use the existing reservation, ownership check, broker close,
  reconciliation, accounting, retry, and dedupe path with
  `reason=loss_policy_L05`.
- State is version 8. Exact version-7 V24 state is upgraded only after exact
  broker inventory proof and CAS; positions and transitional core mapping are
  preserved and policy trackers start empty to prevent retrospective close.
- Local implementation and tests only. No CentOS deployment, restart, MT5
  command, state replacement, Git commit, or push was performed in this update.

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

## L05 re-audit correction (2026-09-05)

- Current state-v8 now rejects an L05 tracker or per-ticket L05 watermark when
  the common activation identity is absent, and rejects either when the
  processed-M5 watermark is absent. This is a fail-closed corruption guard;
  valid strategy behavior is unchanged.
- Direct state-v5/state-v6 migration now initializes the activation and every
  migrated ticket from one identical `last_processed_m5_bar` watermark and
  clears both trackers. This makes the already-supported direct legacy upgrade
  non-retroactive in the same way as the state-v7 upgrade.
- The downloaded state-v7 sample with two open positions was loaded read-only:
  both ticket watermarks and activation resolve to `2026-09-04T12:35:00Z`,
  trackers remain empty, the upgraded shape validates, and the source file is
  unchanged.
- A randomized differential check covering 250 books and 20,000 completed M5
  observations matched the frozen L05 state machine exactly.

## Full runtime-boundary re-audit correction (2026-09-05)

- Extended the candidate boundary to the bridge, executor, data fetcher, safety,
  passive evidence helpers, Compose mapping, and all non-secret bot25 runtime
  files; the earlier L05 snapshot covered only the strategy-centered subset.
- Current state-v8 now rejects malformed pending-OPEN recovery identity before
  preflight: nonpositive known position IDs, invalid clean-confirmation counts,
  or missing/invalid causal identity cannot reach reconciliation.
- Current state-v8 also rejects malformed pending productive-close accounting,
  including duplicate/nonpositive IDs, confirmed IDs outside the target set,
  nonfinite accumulated profit, and invalid deal timestamps.
- These checks only harden invalid-state rejection. V24 entry, L05 selection,
  position ownership, retained-position restart, and close behavior are unchanged.

## Current boundary

The local source and shadow tests may be prepared, but CentOS deployment,
Compose recreation, EA attachment, state migration, and real orders remain
explicitly unauthorized.

## Full lifecycle re-audit correction v5 (2026-09-05)

- Current state-v8 now rejects duplicate position tickets/comments and an
  episode ID whose numeric suffix disagrees with `episode_sequence`.
- OPEN comment allocation advances past any still-owned comment if a recovered
  sequence counter lags, so a restart cannot reuse a live order identity.
- A persisted pending OPEN must now retain the exact production causal identity:
  frontier-add side/reason, completed-M5 signal, decision/quote timestamp,
  opportunity ID, allowed signal age, and the complete known-position ID set.
- Restart adoption additionally requires the matching broker position open time
  to fall from the reservation instant through 60 seconds after it. A same-comment
  position from before the reservation, or one created later by another path, is
  left unresolved and entries remain blocked.
- Pending close metadata without a close-requested ticket, and productive-close
  targets that do not match the live close-request lifecycle, are rejected before
  reconciliation. This prevents stale accounting state from being consumed.
- These changes harden identity and restart recovery only. V24 virtual seed count,
  frontier-add conditions, 6-per-side/3:1 limits, L05 selection, lot, bridge
  contract, and configured live/shadow flags are unchanged.

## Full lifecycle re-audit correction v6 (2026-09-05)

- Current state-v8 now validates the top-level save timestamp and the complete
  sync-block lifecycle. A cleared block cannot retain stale reason/details or
  recovery evidence, and flat-clear evidence must belong to the active
  recoverable block. Changing recoverability for the same reason resets that
  evidence.
- Direction/counter and price fields reject JSON booleans instead of accepting
  Python's implicit `bool`-as-number conversion. Persisted entry UTC must agree
  with its broker epoch second, and restart matching now requires the exact
  broker open price as well as ticket, identifier, time, side, lot, and comment.
- The canonical comment's `L`/`S` marker must agree with BUY/SELL in current
  state, pending OPEN state, Python broker ownership, and EA OPEN/POSITION/CLOSE
  policy checks. The corresponding bridge contract is
  `2026-09-05-s25-v24-atomic-v10`; it supersedes v8/v9 for a future deployment.
- The five-second poll interval is part of the frozen live configuration
  contract. None of these corrections alters V24 episode creation, virtual
  cores, frontier-add conditions, limits, L05 selection, lot, or exit rules.

## Full lifecycle re-audit correction v7 (2026-09-05)

- The Python executor independently rejects an OPEN/CLOSE comment whose `L`/`S`
  marker disagrees with the requested BUY/SELL type. Boolean numeric arguments
  are rejected instead of being converted to `0`/`1`.
- OPEN confirmation requires the request-correlated success price and broker
  position-open millisecond timestamp to match the uniquely re-queried
  position. The EA's post-OPEN confirmation also verifies position type,
  canonical comment direction, and exact 0.01 volume before publishing success.
- Frozen configuration validation requires actual JSON booleans for live/shadow
  mode and rejects boolean aliases in numeric strategy/config fields.
- A persisted frontier-add reservation must belong to an active episode, match
  the active wave, and match the processed completed-M5 watermark. OPEN and
  CLOSE reservation lifecycles cannot coexist.
- Persisted post-close wave handoff requires a supported release reason,
  completed-M5 identity, active episode, valid wave transition, and—while close
  requests remain—the same reason/bar as the close reservation.
- These corrections do not alter signal generation, virtual-core accounting,
  add thresholds, position limits, lot, L05 selection, or close selection.

## Full lifecycle re-audit correction v8 (2026-09-05)

- Current state-v8 rejects missing top-level, strategy, position, pending OPEN,
  productive-close, post-close handoff, and close-defer safety fields instead of
  silently inserting defaults after load.
- Real positions require a complete active episode. Active episodes require a
  positive sequence-matched ID and both persisted frontier prices; inactive
  episodes cannot retain an active wave or frontier.
- Explicitly recognized legacy/V24 migrations remain separate from current-state
  validation. After exact broker inventory proof, a missing legacy episode is
  reconstructed from broker entry time and missing frontiers are fixed once to
  the fresh preflight midpoint. The resulting complete current shape is checked
  before the CAS-protected migration commit.
- No signal, allocation, lot, V24 virtual-core, L05, or exit-selection rule was
  changed.

## Full lifecycle re-audit correction v9 (2026-09-05)

- Persisted pending-CLOSE state now accepts only reasons produced by the live
  production paths. L05 and productive exits must retain the exact last
  processed completed-M5 identity and a request timestamp inside the frozen
  signal-delay window; time/feed full closes must not carry an M5 identity.
- Productive-close accounting is restricted to productive close reasons rather
  than any non-empty string. Full-close defer state is restricted to feed-gap
  and 12-hour episode exits and requires an active episode plus causally ordered
  arm/evaluation/wide-spread timestamps.
- Regression coverage includes arbitrary reason injection, missing/stale M5
  identity, delayed fabricated close requests, invalid defer reason, and
  impossible defer time order. No V24/L05 signal, lot, limit, or exit selection
  was changed.

## Full lifecycle re-audit correction v10 (2026-09-05)

- `_save_state` now validates the complete current state immediately before
  every atomic write. The single-commit reconciliation transaction applies the
  same validation before committing and rolls its in-memory references back to
  the prior state if validation fails.
- Regression tests prove malformed runtime state cannot replace the canonical
  file and malformed transactional state neither commits nor remains in memory.
  Older shortcut fixtures were upgraded to complete V24 episode, M5, pending
  OPEN, and pending CLOSE identities so the tests exercise reachable production
  states rather than bypassing the live contract.
- This is persistence-boundary hardening only. Signal generation, position
  selection, V24 accounting, L05 behavior, caps, lot, and configured mode are
  unchanged.

## Full lifecycle re-audit correction v11 (2026-09-05)

- Current-version state now rejects unknown top-level, strategy, position, and
  nested lifecycle fields. A stale retired field can no longer survive merely
  because all required fields are also present.
- Recognized migration input remains separately staged, but the completed state
  must satisfy the exact current schema before commit. No trading rule changed.

## Full lifecycle re-audit correction v12 (2026-09-05)

- Pending CLOSE now validates request, submission, and retry timestamp order.
- A due retry deadline is consumed in memory before the new durable submission
  marker is written, preventing stale-deadline/new-attempt state conflicts.
- Retry eligibility and broker CLOSE behavior are unchanged.

## Full lifecycle re-audit correction v13 (2026-09-05)

- Current-state validation now recomputes V24 logical LONG/SHORT counts,
  including virtual-core substitution, and rejects persisted side-cap or 3:1
  ratio violations before runtime mutation.
- This matches the existing migration and entry-allocation gates. Existing
  positions are not auto-closed; invalid state fails closed.

## Full lifecycle re-audit correction v14 (2026-09-05)

- Current live-mode state now rejects any synthetic shadow inventory at both
  load validation and the common pre-commit persistence boundary.
- If broker reconciliation encounters such inventory in memory, it retains a
  non-recoverable synchronization block and returns failure without replacing
  the last valid canonical state file with the structurally forbidden state.
- Regression coverage proves direct persistence is rejected and the canonical
  file remains unchanged. Valid live inventory, virtual-core accounting,
  signals, exits, lots, and selected L05 behavior are unchanged.

## Full lifecycle re-audit correction v15 (2026-09-05)

- The live-shadow state guard now runs before broker position/order queries, so
  a simultaneous bridge/query failure cannot enter an earlier persistence
  branch with structurally forbidden state.
- The outer strategy loop and its symbol-info/quote-clock failure branches use
  a common guarded runtime save. They retain the in-memory trading block but do
  not repeatedly raise or overwrite the last valid canonical file.
- Main-loop regression tests cover normal sync failure, unavailable symbol
  information, and stale quote handling with forbidden live shadow inventory.
  No valid-state lifecycle or trading semantic changed.

## Full lifecycle re-audit convergence v16 (2026-09-05)

- An end-to-end regression now reproduces the canonical flat state-v5 path:
  compatible staging, broker-flat proof, state-v8 commit, virtual-core episode
  start, and restart. Both runs issue zero physical seed orders and retain
  logical LONG=1 / SHORT=1 after episode start.
- A separate reconciliation regression fixes the intended manual-close rule:
  an exit deal may have magic zero, while ownership remains proven by the
  stored bot25 position identity, symbol, comment, side, lot, and unique MT5
  position identifier. Tightening exit-deal magic to 200025 would incorrectly
  strand manually closed positions.
- No additional production-code defect was found in this convergence pass and
  no trading semantic changed.
