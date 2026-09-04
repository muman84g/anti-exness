# Bot25 V24 XAUUSD virtual bilateral book

Local shadow candidate derived from the fixed `V23` child of `man_231`. It keeps
one cost-free logical core on each side for capacity and ratio accounting, but
does not send bilateral seed orders. Broker positions are opened only by a
0.50 ATR frontier add. On a release, every profitable real ticket on the active
side is eligible newest-first; the protected core is the virtual ticket.

V23 adds one rule only: after a broker-confirmed gross-price or shadow productive close of
at least 0.10 USD, if no further productive close occurs for more than 120
minutes, a frontier add is blocked only when its prospective side currently has
fewer logical tickets than the opposite side. Equal/dominant-side adds, the first
productive close, and episode expiry retain the V23 thresholds.

- Magic/comment: `200025` / `s25_m231`
- Bridge/IPC: `BotBridge_s25`, `cmd_s25.txt`, `claim_s25.txt`, `res_s25.txt`
- State/logs: `state/s25_bot_state.json`, `logs/s25_bot.log`, `logs/s25_trades.csv`
- Passive evidence: `logs/s25_shadow_opportunities.csv`,
  `logs/s25_shadow_markouts.csv`, `logs/s25_shadow_state_tags.csv`, and
  `state/s25_shadow_observer_state.json`
- Mode: `live_trading_enabled=true`, `shadow_forward_enabled=false` (user-authorized 2026-09-04; remote activation unverified).
- Compose supplies `V24_VIRTUAL_CORE_LIVE_ACK` by default; an explicitly different environment value remains fail-closed. Existing positions require exact broker/state reconciliation and bridge v8. Never replace non-flat server state with a local empty state.
- Real-order double gate: params must enable live and environment variable
  `BOT25_ENABLE_REAL_TRADING=V24_VIRTUAL_CORE_LIVE_ACK` must also be present.
  Both configuration gates are enabled by the 2026-09-04 user authorization;
  broker/account/ownership preflight still determines whether startup is allowed.

The runner uses the bot23 operational baseline: exact magic/comment ownership,
configured-account identity and hedging-mode preflight, confirmed OPEN/CLOSE
reconciliation, foreign exposure rejection, and two-stage high-risk block
clearing. Every bridge request has a unique response-correlated ID and durable
EA-side claim. An OPEN reservation is saved before the broker request, so a
restart cannot blindly duplicate an ambiguous entry. A CLOSE submission marker
is saved before the broker request; a definitive no-fill may retry, while an
ambiguous submission is never replayed and remains blocked for reconciliation.
Real position state and entry evidence use the broker position open timestamp;
the poll/decision clock is never substituted when broker time is unavailable.
An existing non-recoverable ownership/reconciliation block cannot be replaced
by a later transient read failure. Position reconciliation also requires the
persisted lot to equal the broker volume; an identity/side match alone is not
accepted.

The passive observer runs beside live trading and never routes or submits an
order. It registers every reached frontier before capacity, ratio, V23, spread,
pending-open, retry, or sync gates are applied; the resulting route is recorded
as consumed or unconsumed. Executable-side markouts at 1/5/15/30/60/120 minutes
include spread, MFE, MAE, and the route reason. The causal state tagger stores
only the completed M5 bar, current quote, V23 state, inventory counts/MTM,
episode age, and productive-close age available at registration. Observer or
tagger failures are logged once per signature and do not change trading.

Exact bot25 state-v5/man231 and state-v6/V23 predecessors can upgrade to V24
state-v7 with existing positions only when stored and broker position IDs,
ticket, open time, side, lot, magic, and comment all match, broker orders are empty, no open/close
reservation exists, and the state file passes an unchanged-content check. One
best-price legacy position per side temporarily represents that side's core, so
the virtual core is not double-counted. When that position closes through the
existing exit path, the side becomes virtual automatically. Any ambiguity stays
fail-closed. Other retired bot25 identities are still replaceable only after
confirmed flat inventory. Do not hand-edit or replace state identity fields.
While migrated real inventory exists and V24 is shadow-only, the runner verifies
an exact read-only state/broker ownership match and logs status, but does not
reconcile, add, close, or otherwise mutate the canonical position lifecycle.
Existing-position management resumes only in explicitly activated live mode.

## Logs

- `s25_bot.log` rotates at 10 MiB with five backups. It contains health,
  preflight, recovery, status, and failure messages but never credentials or a
  webhook URL.
- `s25_trades.csv` has a strict header. An old/incompatible file is moved to
  `logs/old/` with a timestamp before a new canonical header is written. It
  records broker quote time, completed-M5
  signal/event/release/availability/decision/executable times, episode ID,
  magic, ownership identifiers, real and logical inventory counts, virtual-core
  flags, active wave, reason, and live/shadow mode.
- Every CSV append is flushed and fsynced. A new canonical trade CSV begins
  with `schema_rollover` when an incompatible predecessor was archived.
  File replacement is detected on every append; a replacement with malformed
  row widths or an unterminated final row is rejected instead of appended to.
- Exactly one `m5_decision` receipt is retained for every newly processed M5
  bar, including warmup, stale, no-add, blocked, entry, and close-request paths.
  V23 vetoes also emit `entry_blocked` with
  `reason=v23_drought_minority_add_pause`; productive closures emit
  `productive_close_confirmed`.
  Repeating diagnostics are summarized at five-minute intervals.
- A successful retired-state transition records `startup_state_retired` before
  the normal `startup_recovery` row. The row includes only the archive basename
  and a short content-hash prefix, not old state contents.
- `position_close_confirmed` uses the broker close deal price and aggregates
  profit, entry/exit commission, swap, and fee for the complete MT5 position
  ID. `profit` is their net account-currency result. Deal ID dedupe prevents a
  restart from recording the same realized result twice.

The schema is intentionally episode/inventory oriented rather than copying
bot23 lane fields. Bot25 has no four-lane allocator, ZA opportunity ID, or
`reverse_d60`; inventing those columns would imply behavior man_231 does not
have. `basket_id` is retained as a compatibility alias of `episode_id`. See
`LOG_SCHEMA.md` and `PORTING_AUDIT.md`.

`shadow_forward_enabled=false` is the current local execution mode. The
separately named passive observer remains evidence-only.

```powershell
Get-ChildItem -File *.py | ForEach-Object { py -m py_compile $_.FullName }
py live_s25_bot.py --self-test
py test_s25_passive_evidence.py
py -m unittest -v test_s25_v24_virtual_core.py
py -m unittest -v test_s25_execution_boundary.py
```

This V24 source is not authorized for CentOS deployment or real operation. A
future switch requires an exact state/broker ownership match, zero owned orders
and pending lifecycle actions, an explicit deployment decision, and both
real-order gates to be deliberately enabled. See `SOURCE_BACKTEST.md`.

Confirmed close consumption is transactional: operational rows are durable before
one final state commit. Failed writes restore the prior state unless the exact
completed state is already durable. Retry deduplicates close and productive-close
rows; ownership/accounting conflicts fail closed. Stale quotes permit read-only
deal reconciliation but never advance entry or quote-timed exits.

Live OPEN rechecks quote freshness after reservation persistence/logging and
before calling the executor. Unsubmitted expired reservations are cancelled.
Returned-ticket confirmation also requires the reserved side, comment and lot;
generic bot ownership alone is not a match for an individual reservation.

Pending OPEN flat recovery requires consecutive complete position/order queries.
A failed/unsafe observation resets the confirmation count, preserves pending
intent, and cannot count an unavailable order inventory as empty.

Pending adoption and confirmed-flat consumption use the same single-commit
transaction as close reconciliation. Recovery rows have stable position/comment
identities so retries after a failed state save do not duplicate the ledger.

Post-OPEN confirmation verifies the entire returned owned namespace, including
exact preexisting position ownership. Foreign same-magic rows or prior-position
drift retain pending intent and block entries. Pending adoption requires exactly
one unexplained position and an exact reservation match, not merely one matching
candidate among several unexplained positions.

Release-driven wave handoff is persisted with the validated close reservation
before any broker CLOSE. Restart therefore preserves the intended next wave if
the process stops after a close submission. Ownership rejection does not arm it.
