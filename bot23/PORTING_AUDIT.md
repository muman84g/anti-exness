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

## 2026-08-28 JST11-13 round-level sweep capacity-one overlay adoption

The fixed XAUUSD candidate `round_s2p5_d0p05_r0p03` was added as an
independent JST11:00-13:00 lane. The executable release window is UTC
02:00-04:00 with the end exclusive. Confirmed M1 selects a 2.5-USD grid from
the prior close, requires a 0.05 ATR60 sweep and 0.03 ATR60 reclaim, and admits
only a new raw-side onset. Lane 8 owns magic 230030, comment `s23_md_l1`, lot
0.01, capacity one, and a 60-minute close clock starting from confirmed broker
fill. ZA routing, morning routing, add, pullback, adaptive exit, cooldown,
reverse_d60, and LONG-target rearm were not changed.

State schema remains version 3. An existing compatible state receives only an
empty midday lane plus the frozen midday policy identity; all ZA and JST09-11
baskets, pending actions, reservations, and routing state are preserved. A
non-empty incompatible identity is not adopted. Ownership and startup checks
now require the exact new magic/comment while retaining all prior namespaces.

No semantic behavior or persisted field was intentionally deleted. The
morning fixed-hold implementation was only extracted into a shared helper and
retained through its original wrapper and close reason. The first regression
run exposed two failures from applying `math.ceil` to warm-up NaN values in
the new grid calculation. The calculation was corrected to preserve NaN until
the 60-bar ATR window is valid; no threshold or research rule was changed.

Initial no-order validation passed Python compilation, runner self-test, 84 bot23
regressions, six passive-observer tests, and three state-tagger tests. New
coverage checks exact LONG/SHORT mechanics, onset gating, UTC04:00 exclusion,
actual-fill hold timing, capacity one, state migration, namespace separation,
and rejection of a foreign magic.

Research used ordered Bid ticks and first-eligible-tick execution. Live uses
broker HIST M1 and a later polling quote. The consumed forward sample has only
four trades and is not independent proof. The fixed-hold bridge/close gap found
in final audit was corrected in the follow-up section below.

Compose already mounts the modified runner and params as bot23 read-only
files, so no Compose edit was required. No deployment, container recreation,
service restart, runtime state/log edit, MT5 order, commit, or push was
performed.

### 2026-08-28 final-audit correction

The first final audit found that a failed fixed-hold close could leave
`pending_close_reason` plus a permanent `live_time_close_failed` block, making
later polls unable to retry. It also found no broker quote timestamp, no spread
defer/reopen contract, incomplete close-deal audit columns, no midday passive
observer, and no combined JST09-13 portfolio replay.

The corrected bridge INFO response includes `MqlTick.time_msc`; live preflight
rejects an older bridge that lacks it. Fixed-hold state now persists the last
evaluated quote, wide-spread defer start, stable-poll count, and retry time.
Pre-deadline and duplicate quotes do not count. A normal spread closes
immediately; after a wide quote, three fresh narrow quotes are required, with a
30-minute force limit. Exact retcode 10018 resets spread defer, waits 60 seconds,
and retries without a permanent block. Other ambiguous/failed close outcomes
remain fail-closed.

Confirmed close rows now retain ticket, position identifier, deal ID, entry and
exit price, side, lot, broker deal time, and ticket-level net PnL. The trades CSV
header is checked before bridge connection; an older CSV must be archived before
restart. Separate midday opportunity/markout/state-tag files observe raw signals
and capacity/spread/stale/sync rejection without affecting orders.

The exact fixed JST09-11 and JST11-13 overlays were combined on ordered Bid/Ask
ticks without retuning. Dev Stress: 248 trades, USD +544.814, PF 1.74115,
every-tick MTM MDD USD 80.473, maximum four positions. Observed-leakcheck
diagnostic: 95 trades, USD +258.534, PF 2.09609, MDD USD 44.363, maximum three.
Overlap occurred in 26 DEV episodes / 12.568 hours and 11 observed-leakcheck
episodes / 5.117 hours. Canonical artifacts are under
`evidence/jst0913_combined_v004`; v001-v003 are retained failed-run evidence and
must not be used for selection.

## 2026-08-28 JST09-11 stable_001 15/55/45 overlay adoption

- Added three independent morning lanes to the original bot23 runner without
  changing the ZA four-lane routing, session, position limits, or exit logic.
- Frozen UTC window is 00:00-02:00 (JST 09:00-11:00). Signal/hold pairs are
  false-break direction-control/15m, price-effort direction-control/55m, and
  M15-compression M5-release primary/45m.
- New ownership namespaces are magics 230027-230029 and comments
  `s23_am_l1`-`s23_am_l3`. Each holds at most one 0.01-lot position; total
  morning capacity is three. Fixed holds use actual confirmed fill time.
- State version remains 3. Migration adds only empty morning states and the
  frozen policy identity, preserving existing ZA baskets, pending entries, and
  unresolved broker evidence. A conflicting morning policy fails closed.
- Added deterministic tests for each signal clock/direction, namespace
  isolation, old-state migration, fixed-hold timing/idempotency, and capacity.
- Post-review correction restored the research `completed_pulse` transition
  semantics, separated the early quote/exit clock from the post-HIST ZA and
  morning-entry decision clock, and uses broker-confirmed `open_time` for live
  morning holds. Flat morning lanes no longer query position/order inventory or
  save state on every poll outside UTC00-02.
- Python compilation, runner self-test, and all 76 no-order regressions pass,
  including non-repeating compression, broker-fill timing, clock separation,
  and off-session no-query/no-write coverage.
- No live configuration, startup file, installed state/log, deployment,
  restart, bridge attachment, Git action, or real order was changed or run.

## 2026-08-28 LONG-target portfolio rearm adoption

- Directly ported fixed candidate `bot23-long-target-portfolio-rearm-v001` on
  top of the unchanged reverse_d60 four-lane runner.
- A native LONG target close arms a portfolio guard before broker CLOSE
  submission. New LONG baskets remain blocked through confirmation and for
  eight minutes from the latest confirmed close-deal timestamp.
- Unsubmitted pending LONG entries are cancelled portfolio-wide. Existing
  baskets, LONG adds, pending/new SHORT behavior, ownership identifiers,
  position limits, close thresholds, and all other risk controls are preserved.
- Every lane's reconciliation/exit pass now completes before any lane's pending
  fill or new admission pass, preventing same-poll cross-lane re-entry.
- State version stays at 3. New routing fields migrate in place without editing
  or resetting the installed state file; malformed persisted expiry fails
  closed for new LONG baskets only.
- Python compilation, self-test, and 52 no-order regression tests pass. The new
  tests cover arm/confirmation timing, pending-LONG cancellation, SHORT and
  existing-add preservation, and malformed-state behavior.
- No state/log reset, deployment, service restart, bridge attachment, commit,
  push, or live order was performed.

## 2026-08-28 balanced-inventory range false-break fade adoption

- Ported fixed candidate `bot23-x-archive-inventory-range-fade-opt-v001` on top
  of the unchanged reverse_d60 and eight-minute portfolio-rearm parent.
- The range is the minimum/maximum entry price of an equal non-zero LONG/SHORT
  book. Completed-M1 breakout, 15-minute window, two consecutive boundary
  re-entry closes, opposite-break direction, and both-side coverage match the
  frozen backtest parameters hash `d02b8273...4296bab0`.
- Raw ZA has same-bar priority. One synthetic opportunity may remain pending
  until a no-ZA completed M1; durable take occurs before lane routing.
- The synthetic path bypasses only the low-volatility ZA extreme/pullback arm.
  Existing spread/ATR, session, daily loss, cooldown, capacity, rearm,
  reservation, ownership, and broker confirmation safeguards remain active.
- State version remains 3. The new routing state migrates inactive without
  clearing baskets, unresolved OPEN evidence, or existing ZA pending entries.
- Python compilation, runner self-test, 60 bot23 no-order regressions, six
  observer regressions, and three state-tagger regressions pass. Boundaries
  cover upper/lower breaks, ZA priority, 15-minute expiry, imbalance
  invalidation, direct low-volatility admission, and restart migration.
- No live configuration, startup file, installed state, logs, deployment,
  restart, bridge, commit, push, or real order was changed or executed.

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

## 2026-08-23 ZA ATR20 regime-switch adoption

- Adopted fixed forward-only candidate `ZA_atr20_regime_switch` from
  `backtest152/runs/20260823_bot23_weekly_diagnosis_dev_cycle_v1/rebased_v8`.
- Added the ATR30 `< 2.0` Bollinger z2 / 1-sigma / 10-minute pullback entry,
  executable spread/ATR `<= 0.10`, and frozen ATR30 exit multiples
  target/stop/FTP `3.5/6.5/1.0`.
- Corrected live signal volume ratio to the frozen tick-volume / 30-bar mean
  definition and added Bollinger 20-bar population-standard-deviation features.
- Preserved state version/strategy identity, ownership checks, shared-account
  isolation, current-quote close monitoring, fixed high-volatility exits,
  inventory limits, daily/session gates, and live/shadow enable flags.
- Added regression checks for candidate parameters, pullback arming/fill,
  frozen adaptive thresholds, same-M1 adaptive target close, and the existing
  shared-account and recovery safety suite.
- Added the mounted standard manual-action alert helper. Definitive MT5
  `10026/10027` rejects use a 30-second retry and alert after three consecutive
  rejects; non-recoverable reconciliation alerts deduplicate by signature.
- Existing baskets lacking a historical frozen ATR retain fixed exits rather
  than receiving an invented ATR value.
- No deployment, service restart, bridge attachment, push, or live switch was
  performed.

## 2026-08-24 independent re-audit correction

The six independent-audit findings were corrected as one safety batch. Entry
submission now has layered fail-closed guards, malformed pending state is
recoverable without an order attempt, the high-volatility pending transition
matches the frozen backtest branch, state-identity refusal alerts once per
unresolved signature, and the helper test identifies bot23. A dedicated
no-order regression suite covers each finding plus preservation of the
low-volatility spread gate. No strategy parameter, ownership identifier,
runtime state, deployment file, service, or sensitive configuration was
changed.

## Clean-room rebuild release audit (2026-08-24)

- Candidate construction did not copy the installed bot23 runner. Frozen
  backtest sources plus the bot24 scaffold were used, and the old runner was
  introduced only after construction as a behavioral oracle.
- Two correction cycles closed all observed findings. The final artifact
  passed 18 differential probes, the full inherited oracle self-test, nine
  persistent regressions, eight adversarial probes, six malformed-state
  probes, self-test, compose validation, and exact package hash checks.
- Semantic deletion review: scaffold-only ATR90 output and an unsupported
  per-deal logging branch were intentionally removed; no trading rule,
  persisted field, ownership guard, position lifecycle, or recovery state was
  removed. Those preserved behaviors are covered by the oracle, regression,
  adversarial, and differential suites.
- Installed source SHA-256:
  `480240298c27188bd6008ac4d2859c52e4746ef997e1717fbeb435f334dca840`.
  Full evidence is in
  `backtest152/runs/20260824_bot23_cleanroom_rebuild_v1`.

## 2026-08-25 owned-inventory recovery correction

Production logs showed that a transient `symbol_info_failed` block could not
clear while an owned basket remained open. `_sync_strategy` returned before it
could reach the complete-owned-inventory recovery branch, so target, stop,
failure-to-progress, and max-hold monitoring remained paused after the quote
feed recovered.

The sync order now preserves fail-closed entry behavior while allowing a full
read-only ownership reconciliation to complete. A recoverable block clears
only when positions and orders are both available and every persisted live
position matches its ticket/identifier, side, symbol, magic, and comment.
Non-recoverable blocks remain blocked. `run_once` also uses the central block
setter so a transient query failure cannot overwrite a pre-existing
non-recoverable block.

The no-order regression suite now covers complete-owned recovery,
non-recoverable preservation (including protection from a later transient
failure), and the end-to-end transition from one failed symbol-info poll to
resumed open-basket monitoring. All 13 regressions pass.
No strategy parameter, runtime state, deployment file, service, or MT5 order
was changed. Installed runner SHA-256:
`61bac85592f1d5968f6e53ccb5a6cd1a8ef8013f500bfb7b06ebc1911b5174d0`.

## 2026-08-25 ZA four-lane horizontal inventory source switch

The directly installed bot23 source now implements canonical candidate
`bot23-za-horizontal-inventory-v001` from the frozen knowledge root
`backtest/検討中/chatgpt案/多重ポジ/利益確保案/bot23_多重ポジ化`.
One inventory-free confirmed-M1 ZA opportunity is offered sequentially to four
independent lanes.  Routing stops on the first pending arm, pending refresh,
pending fill on the signal tick, entry attempt, or add attempt.  This preserves
the frozen first-consuming rule and prevents the same opportunity from reaching
more than one lane.

Each lane retains the frozen ZA controls and has its own basket, maximum two
positions, 0.01 lot, frozen ATR, pending state, cooldown, daily realized-loss
limit, close lifecycle, magic, and comment namespace.  Ownership is fixed to
bot23-private magics 230023-230026 and comments `s23_za_l1`-`s23_za_l4`; aggregate capacity is
eight positions.  State identity is version 3.

The live translation adds durable route and OPEN reservations.  Ambiguous OPEN
results, restart with an unresolved OPEN reservation, live/state lot mismatch,
and foreign inventory fail closed.  Legacy version-2 state is refused without
overwriting the old state file.  A crash may lose one opportunity but cannot
fall through to a later lane and duplicate it.

Python compilation, 23 no-order regressions, exact ownership/parameter checks,
and `docker compose config --quiet` pass.  The regressions include same-M1
deduplication, primary-first routing, pending-arm consumption, pending-fill
same-tick consumption, ambiguous-open fall-through prevention, restart
reservation recovery, and volume reconciliation.  No service restart, state
reset, log reset, EA attachment, or MT5 order was performed.

Installed identities:

- runner SHA-256: `460aaaa26c5791773b246aa3784a86988b55e97339328ad36e5f6f86a1a921a8`
- params SHA-256: `4179ddabfe72c581b79a1ebb63e93ab4f2a22b9a27c788bb39d90c0fa7ad99c3`
- regression SHA-256: `8a34e8d66896fb0f84d1839c23f7bb8a4d0c49f51bbccc074b4840644a0b47fa`

## 2026-08-25 bot23 four-lane ownership correction

The initial four-lane live translation incorrectly reused sequential magics
`200023-200026`.  Live account history and the installed bot24-bot26 contracts
prove that `200024`, `200025`, and `200026` belong to those separate bots.
Bot23 lanes are therefore isolated under previously unused magics
`230023-230026`; comment prefixes remain `s23_za_l1`-`s23_za_l4`.

This changes only live ownership/reconciliation identifiers.  It does not
change the frozen opportunity producer, routing, position sizing, entry/close
rules, research recipe identity, or research params identity.  JSON validation,
Python compilation, compose validation, self-test, and all 23 no-order
regressions pass.  No service restart, state/log reset, bridge attachment, or
order submission was performed.

Corrected installed identities:

- runner SHA-256: `0d7dca11576b6a615f8645d8c9e4e442558d71cbff8703e770845824141be9b3`
- live params SHA-256: `6e3160a00cbb072f7b3bc215faf09d15cd2e0d827559115e25ed6de59af732f5`
- executor SHA-256: `8d2b5e8234ad7c2dca3f70d210cd08ba44e5273b2f7c95f2d831a8b890425bcf`
- bridge source SHA-256: `0365dd3a30041c6cf07044d9f3aaac891d52f986096beaf85a344acd20e71847`
- regression SHA-256: `ff6b54ab4abb1d1205af4885af98b6b2acea432ef5fef31b560a26b64a13608d`

The same cutover audit also corrected delayed close-confirmation accounting:
confirmed realized PnL is assigned to the broker deal's UTC day, not the later
poll/restart day on which that deal is observed.  This preserves the intended
daily realized-loss gate across UTC midnight.

Production evidence also showed an open basket during an extended
`symbol_info_failed` interval.  The strategy still cannot calculate or submit a
basket exit without executable Bid/Ask data, and adding broker SL/TP would alter
the frozen strategy.  The runner now emits a rate-limited manual-action alert
whenever this outage occurs with owned inventory open; clean recovery continues
to resume automatic monitoring after the bridge quote returns.

Finally, the ACCOUNT bridge contract now includes account login and server.
Live preflight compares both with the configured MT5 identity and refuses an
older bridge or a differently logged-in terminal. This closes a real
environment ambiguity observed during the cutover audit without changing any
entry, inventory, or close rule.

## 2026-08-26 open-inventory orders-unavailable correction

The refreshed production evidence showed that the basket opened at 13:24 UTC
was still present at 14:56 UTC.  After the earlier symbol-info outage, the block
changed to `orders_unavailable`; the runner still returned before monitoring
the already-owned market position.  The corrected sync path keeps all new
entries fail-closed while order visibility is unavailable, but permits
target/stop/failure-to-progress/max-hold monitoring and close submission after
the live position set has been fully reconciled to state.  It cannot clear the
recoverable block or open again until both position and order queries are clean.

## 2026-08-26 forward logging normalization

The legacy evidence contained 2,016 trade rows, of which 1,627 were repetitive
`entry_skip` diagnostics.  The version-3 runner retains every economic and
state-transition event, but coalesces unchanged operational skips into a
five-minute `diagnostic_repeat_summary` carrying suppressed count and duration.
Reason transitions flush immediately.  Bot status logging is reduced from one
to five minutes and the text log rotates at 10 MiB with five backups.  CSV
schema is checked before append so legacy headers cannot be silently mixed with
the version-3 audit format.

## 2026-08-26 reverse_d60 entry-policy adoption

The fixed forward-only candidate
`bot23_late_short_30m_action_matrix_v001:reverse_d60` was added before the
existing first-consuming four-lane router. A raw ZA SHORT becomes effective
LONG only when the current executable Bid is at least 0.60% below the
completed-M1 close exactly 30 minutes earlier. Raw LONG and non-qualifying
SHORT opportunities are unchanged; missing lookback or quote evidence blocks
the SHORT rather than falling back to the prior entry behavior.

The four-lane strategy family ID, state schema version, magics, comments,
position limits, lot, adds, exits, close confirmation, reconciliation, alert,
and deployment contracts are unchanged. Candidate and lane spec IDs now bind
the reverse_d60 policy and params hash
`40475d07b84eabc1b1290bee6787113903f374ca90cf2ca271c82b825b313572`.
At first startup, old unsubmitted local pending entries are cleared while open
baskets and unresolved OPEN evidence are preserved.

No-order verification passed JSON parsing, Python compilation, runner
self-test, all 40 regressions, and Compose service/config validation. New
regressions cover current-Bid rather than signal-close comparison, exact
30-completed-M1 indexing, threshold non-match, insufficient-history fail-close,
effective-side routing, and state migration preservation. Semantic deletion
review found one intentional removal only: old-policy unsubmitted pending
entries at cutover. No active basket, pending OPEN, ownership, close, recovery,
or alert behavior was removed.

Installed identities:

- runner SHA-256: `ee4a2f81f5c22a1b3723234b6ef067f618810c5883bf33a6854fd11e5ab28f21`
- live params SHA-256: `88b8b80e2b17e086fdc377388f5d1784d019b1a1cfd814f9f562bd5230a45aba`
- regression SHA-256: `810a5e9c8a15524a3640bcd9836c0edec1e2821bbee41d3787f0accf66510d16`

## 2026-08-26 passive opportunity observer

The local bot23 source now contains a passive forward opportunity observer.
It receives only quotes and opportunity/route metadata already available in
`live_s23_bot.py`; it imports no bridge, executor, or live configuration and
has no broker-order method. Observer return values are ignored by the trading
path, and observer exceptions are contained and signature-deduplicated.

Dedicated runtime artifacts:

- `logs/s23_shadow_opportunities.csv`
- `logs/s23_shadow_markouts.csv`
- `state/s23_shadow_observer_state.json`

The existing `s23_trades.csv` schema and version-3 strategy state remain
unchanged. Markouts use executable Bid/Ask at the five-second live poll cadence,
not ordered every-tick extrema. Local no-order evidence: six observer tests,
44 bot23 regressions, runner self-test, and Python compilation all passed.

Installed identities:

- runner SHA-256: `9874ad959afdf4a47c3a5c16bd5e5eff781c011c133c41741cb18384b1038ff3`
- live params SHA-256: `fb9ade2b5a1013e8def88db6a32ad0513f1bbe0069d78c9d1e0315c2319e02d5`
- regression SHA-256: `3804ae183dfff620f9921be260d10b31fb1f2a03ed09f55f9828b6308c303e72`
- observer SHA-256: `f25a003df49e801dce8f9e3a73fa1f5722f001c75a68d714295510628b49eeaa`
- observer regression SHA-256: `5dd72132c92de2aea1f43fe992cf738354529238d2cf503b4605c0c8a4c76f4d`

The local Compose definition now mounts `shadow_opportunity_observer.py` into
the bot23 service as a read-only per-file bind. `docker compose config --quiet`
passes and the resolved target is
`/app/bot23/shadow_opportunity_observer.py` with `read_only: true`. The running
container is unchanged because push, deployment, and service recreation remain
outside the authorized scope; real collection starts only after those steps.

No git commit/push, deployment, service restart, bridge attachment, state/log
reset, or MT5 order was performed. Local Compose SHA-256 after the mount-only
change: `75cd3316287e2a12da0e5584acd15222180b03289a1cd7805036199db1c5f559`.

## 2026-08-28 unresolved add OPEN exit-preservation repair

Production evidence showed lane 1 retaining a known LONG basket while a later
add attempt remained in `pending_open_opportunity_id`. The unconditional
`unresolved_open_action` return in `_sync_strategy` ran before complete basket
ownership reconciliation, so it blocked `_monitor_open_basket` as well as new
admission. The known basket consequently passed its adaptive stop and max-hold
limits without an automated close request.

The sync result now distinguishes exit-monitoring safety from entry admission.
An unresolved OPEN continues to set a non-recoverable new-entry/add block. Only
after every current live position exactly matches persisted ownership and trade
identity may the already-known basket reach target, stop,
failure-to-progress, and max-hold handling. An extra live identifier, foreign
ownership evidence, position mismatch, open order, or unavailable position
query remains fully fail-closed. No reservation is treated as a successful or
failed order merely to restore trading.

When the bot-owned namespace later becomes position/order flat, the unresolved
reservation requires three consecutive clean flat confirmations before it is
cleared. This exceeds the two-confirmation minimum and prevents a single empty
bridge response from reopening admission.

Regression coverage reproduces the production topology, verifies a stop close
through the complete `run_once` route, proves the entry/add block remains set,
proves an unexpected extra ticket remains fully blocked, and proves the
three-confirmation flat recovery. No strategy signal, threshold, lot, state
schema, ownership namespace, bridge command, deployment file, or runtime state
was changed. No deployment, restart, state edit, order, commit, or push was
performed.

## 2026-08-28 entry/lifecycle clock separation correction

The post-13:00 JST admission classifier remains diagnostic-only with
`routing_enabled=false`; existing ZA, JST09-11, and JST11-13 entry routes are
unchanged. Its first implementation used the London DST regime for both the
European and US boundaries. That is wrong during the several weeks each year
when London and New York change clocks on different dates. The corrected clock
uses `Europe/London` for the European boundary and `America/New_York` for both
US boundaries. Explicit 2026 US transition and EU/US mismatch-week regressions
now cover this case.

Fixed-hold lifecycle remains an absolute deadline derived from the broker's
confirmed position open time. A missing live `open_time` no longer falls back
to the decision/poll time. The runner persists the exactly owned position,
blocks further entry, and restores the authoritative broker open time on a
later clean owned-position sync. Existing fixed-hold spread, fresh-quote,
market-closed retry, and position-ownership guards remain in force.

No signal, lot, hold duration, session route, ownership namespace, deployment
file, runtime state, or live service was changed by this correction.

`JST1113_PORTING_EVIDENCE_V3.json` is retained unchanged as historical evidence,
but the canonical evidence-work-state validator rejects its custom schema. It
must not be used as a release manifest. The correction disposition and exact
candidate hashes are recorded in
`CLOCK_SEPARATION_CORRECTION_AUDIT_20260828.json`.
