# Bot23 ZA four-lane inventory + independent JST09-11 / JST11-13 overlays

`bot23` is the live/shadow port of the fixed
`bot23_late_short_30m_action_matrix_v001:reverse_d60` candidate for XAUUSD.
It preserves the ZA confirmed-M1 producer and four-lane
`first_consuming_lane_preserve_primary_v1` inventory, then transforms only a
late SHORT opportunity: when the executable Bid is at least 0.60% below the
completed-M1 close exactly 30 minutes earlier, the effective side is LONG.

After any LONG basket is closed at its native target, the runner blocks only
new LONG baskets across all four lanes for eight minutes. The live clock starts
from broker close-deal confirmation; existing baskets, LONG adds, and every
SHORT path remain unchanged. Unsubmitted pending LONG entries are cancelled
when the target close is armed and again when it is confirmed.

The adopted overlay freezes the minimum/maximum entry-price range whenever the
portfolio holds equal non-zero LONG and SHORT position counts and the completed
M1 close is inside that range. After one completed-M1 close breaks the range,
two consecutive completed-M1 closes back inside within 15 minutes create one
new-basket opportunity opposite the breakout. ZA has priority on the same bar;
the synthetic opportunity waits for the next completed M1 without a ZA signal.

An independent morning overlay runs only from 00:00 through 01:59 UTC
(JST 09:00-10:59). It implements the frozen provisional candidate
`stable_001-param-15-55-45`: false-break confirmation with direction control
(15-minute hold), price/effort divergence with direction control (55-minute
hold), and M15 compression/M5 edge release in the primary direction (45-minute
hold). Each signal owns one 0.01-lot lane and at most one position, for a
combined morning maximum of three. Holds start from the actual broker fill
time. This overlay does not use ZA routing, pullback, adds, adaptive exits, or
the LONG-target portfolio-rearm gate.

An independent midday overlay runs only for executable releases from 02:00
through 03:59 UTC (JST 11:00-12:59). Its frozen signal is
`round_s2p5_d0p05_r0p03`: using confirmed M1, the prior close selects the
nearest 2.5-USD grid boundary, a bar must sweep that boundary by at least
0.05 ATR60 and reclaim by at least 0.03 ATR60, and only a new raw-side onset
is admitted. One private 0.01-lot lane holds the confirmed broker fill for 60
minutes and has capacity one. It does not enter ZA routing or use ZA pullback,
adds, adaptive exits, cooldown, or LONG-target rearm.

## Structure

- One inventory-free ZA opportunity is created from each confirmed M1 signal.
- The frozen `reverse_d60` policy is applied once before lane routing. Raw LONG
  and non-qualifying SHORT opportunities are unchanged.
- The opportunity is offered to Lane 1, then Lane 2, Lane 3, and Lane 4.
- Routing stops at the first lane that consumes it through pending arm,
  pending refresh, confirmed entry, or confirmed/attempted add.
- The same opportunity is never submitted to more than one lane.
- A LONG target close is a portfolio event: all lane exits are processed before
  any lane may fill or admit a new entry on the same polling cycle.
- Balanced-book range state is evaluated once per completed M1 before that
  poll's exits, matching the frozen ordered-tick replay. A change to unequal
  LONG/SHORT counts invalidates an unconfirmed break.
- The range-fade opportunity bypasses only the low-volatility ZA extreme and
  pullback requirement. Session, spread/ATR, daily-loss, cooldown, capacity,
  portfolio rearm, ownership, reservation, and final execution guards remain.
- Each lane independently owns its basket, pending state, cooldown, frozen
  ATR30, daily realized PnL, tickets, and close lifecycle.
- Each lane permits at most two positions; portfolio capacity is eight.

| Lane | Magic | Comment prefix |
|---:|---:|---|
| 1 | 230023 | `s23_za_l1` |
| 2 | 230024 | `s23_za_l2` |
| 3 | 230025 | `s23_za_l3` |
| 4 | 230026 | `s23_za_l4` |
| AM 1 | 230027 | `s23_am_l1` |
| AM 2 | 230028 | `s23_am_l2` |
| AM 3 | 230029 | `s23_am_l3` |
| MD 1 | 230030 | `s23_md_l1` |

All ZA lanes use 0.01 lot, session 13:00-18:00 UTC, the 14 UTC new-basket
block, USD -27 confirmed daily realized loss limit per lane, 0.65 ATR add,
30% add-profit guard, 10-minute failure-to-progress, 70-minute maximum hold,
and eight-minute cooldown. The ATR30 `<2.0` ZA pullback and adaptive exits are
unchanged.

Morning signals are evaluated only from completed bars. M15/M5 values become
available after their source bar completes; the final M5 edge is released to
M1 at M5 completion. Live tick `Volume` is the activity proxy used by the
price/effort signal. Current executable spread and stale-signal checks remain
live safety gates, so exact trade counts can differ from the research replay.

The midday lane is independent of the three morning lanes. Because a morning
position can remain open after 02:00 UTC, the transition can temporarily hold
up to four overlay positions; together with ZA's eight-position limit, the
configured portfolio ceiling is twelve. The midday close clock starts at the
confirmed broker fill, not at the signal-bar timestamp.

## DST-aware entry-admission clock

Future post-13:00 JST overlays use independent `Europe/London` and
`America/New_York` new-entry admission clocks instead of hard-coded month
ranges. On dates when both markets are in the same DST regime, the schedule is:

- JST 13:00-15:30 (16:30)
- JST 15:30 (16:30)-20:30 (21:30)
- JST 20:30 (21:30)-05:30 (06:30)

Boundaries are half-open, so an instant belongs to at most one block. The clock
is configured with `routing_enabled: false`: it is available for research and
future signal implementations but does not alter the existing JST09-13 or ZA
order routes.

Each configured boundary names its own reference clock. The European start is
governed by `Europe/London`; the US start and overnight end are governed by
`America/New_York`. This remains correct during the March and autumn weeks in
which the two markets change DST on different dates.

London governs the 15:30 (16:30) European boundary. New York independently
governs the 20:30 (21:30) and 05:30 (06:30) US boundaries. During the March and
October/November weeks when their DST regimes differ, the clock therefore does
not incorrectly shift all three boundaries from one market's calendar.

This calendar is entry-only. Once a broker fill is confirmed, the owning lane
calculates its close deadline as confirmed fill UTC plus elapsed hold minutes.
Position synchronization, target/stop, and scheduled-close monitoring continue
across admission-block boundaries and DST changes without reclassification.
For fixed-hold lanes, a missing broker `open_time` is never replaced by the
poll time. The owned position is retained in state with entry blocked, and a
later exact owned-position sync restores the broker time automatically when it
becomes available.

## Safety and recovery

- Broker inventory is accepted only after ticket/identifier, side, volume,
  symbol, magic, and comment match persisted lane state.
- A durable opportunity reservation is saved before routing and an OPEN
  reservation is saved before broker submission. A crash may discard one
  opportunity, but must not duplicate it across lanes.
- Transient symbol/position/order failures block new entry. Complete owned
  synchronization clears a recoverable block and resumes basket monitoring.
- If pending-order visibility alone is unavailable, new entries remain blocked,
  while fully reconciled owned market positions continue target/stop/time
  monitoring and may still be closed.
- A symbol-info outage while inventory is open emits a manual-action alert;
  broker-side SL/TP remains unset, so no automated basket exit can run until an
  executable Bid/Ask quote and the bridge recover.
- Non-recoverable ownership or reconciliation blocks require manual action and
  cannot be overwritten by a later transient failure.
- Broker fill confirmation is required before inventory is added to state;
  close-deal confirmation is required before realized PnL or flat state is
  recorded.
- Fixed-hold exits use the broker quote timestamp returned by the bridge. A
  quote from before the deadline, a duplicate quote, or a missing timestamp
  never advances the close state. A normal spread closes immediately; after a
  wide spread, three fresh narrow quotes are required, with a 30-minute force
  limit. Exact MT5 retcode 10018 waits 60 seconds and retries without creating
  a permanent reconciliation block.
- While a LONG target close is awaiting confirmation, new LONG baskets fail
  closed. The fixed eight-minute rearm then starts from the latest confirmed
  close-deal timestamp, survives restart in routing state, and blocks no SHORT.
- Range, breakout, confirmation, pending synthetic side, and per-M1 dedup state
  are persisted under `routing`. First-start migration initializes only this
  overlay as inactive and preserves existing baskets and ZA pending entries.
- Bridge ACCOUNT metadata must match the configured MT5 login and server. An
  older compiled bridge without account identity, or a terminal logged into a
  different account/server, is rejected before live operation.

## Logging policy

- `s23_trades.csv` preserves economic actions, ownership/reconciliation state
  transitions, causal timestamps, tickets, position identifiers, deal IDs,
  entry/exit prices, lane/basket identity, and ticket-level confirmed net PnL.
- Repeated operational `entry_skip` diagnostics write once on transition and
  then one `diagnostic_repeat_summary` every five minutes with
  `repeat_count` and `repeat_window_seconds`. A reason change flushes the prior
  summary immediately.
- `s23_bot.log` writes status every five minutes and rotates at 10 MiB, keeping
  five backups.
- The runner refuses an old or incompatible trades-CSV header. Archive/reset
  the legacy CSV before the version-3 first start; it never silently appends a
  new row shape under an old header.

### Passive forward opportunity observer

- `shadow_opportunity_observer.py` has no bridge, executor, or order dependency.
  Its return values are never used by entry, add, close, or routing decisions.
- Every confirmed ZA opportunity is registered before policy/stale rejection or
  lane routing. The observer records raw/effective side, reverse_d60 disposition,
  executable spread, ATR30, ret10, volume ratio, lane occupancy/pending state,
  readiness, route result, and consumed lane.
- `logs/s23_shadow_markouts.csv` records executable-side PnL and MFE/MAE after
  1, 5, 15, 30, and 60 minutes. LONG uses registration Ask to later Bid; SHORT
  uses registration Bid to later Ask. A policy-blocked signal is labeled using
  its raw side with `raw_fallback_policy_blocked` rather than being presented as
  an executed strategy decision.
- `logs/s23_shadow_opportunities.csv` and
  `state/s23_shadow_observer_state.json` are separate from `s23_trades.csv` and
  `s23_bot_state.json`. Pending horizons survive restart and CSV identities are
  reconciled to suppress duplicate registrations, route rows, and markouts.
- Observer initialization/write failures are logged and contained; they do not
  block or alter live trading. The evidence is diagnostic only and is not a
  live gate or automatic parameter-selection input.
- MFE/MAE resolution is the live poll cadence (currently five seconds), not
  ordered every-tick resolution. Quote gaps and process downtime remain visible
  through `observation_delay_seconds` and `quote_samples`.
- The JST11-13 source has the same passive evidence in separate files:
  `s23_midday_shadow_opportunities.csv`, `s23_midday_shadow_markouts.csv`,
  `s23_midday_shadow_state_tags.csv`, and
  `s23_midday_shadow_observer_state.json`. Capacity, spread, stale, and sync
  rejections remain observable even when no order is sent.
- The local `exness-bot-23` Compose service mounts the observer module read-only.
  This definition change alone does not alter the running container; collection
  begins only after the updated files are deployed and that service is recreated.

### Passive forward state tags

- `shadow_state_tagger.py` adds causal market/inventory descriptors to each raw
  ZA opportunity and writes `logs/s23_shadow_state_tags.csv`.
- The file joins to `s23_shadow_opportunities.csv` and
  `s23_shadow_markouts.csv` by `opportunity_id`; future markouts are deliberately
  not written into the tag row.
- Tags use the completed signal M1 and bars available before it: prior-20 range
  position, high/low sweep and rejection, candle body/wicks, ATR-normalized
  returns and range, activity, path efficiency, and current inventory balance.
- The activity percentile is computed from earlier completed bars only. The
  current bar is never included in its own reference distribution.
- The tagger has no bridge, executor, or order dependency. Its return value is
  ignored, failures are contained, and no tag is read by entry, routing, add, or
  close logic. It therefore observes the existing forward strategy without
  changing orders.
- `opportunity_id` is loaded from the CSV at startup, preventing duplicate tag
  rows after a restart. A mismatched existing header disables tagging through
  the contained error path instead of altering trading.

## Canonical evidence

- Knowledge root:
  `C:/botter/backtest/検討中/chatgpt案/多重ポジ/利益確保案/bot23_多重ポジ化`
- Dev run:
  `C:/botter/backtest/output/backtest153/candidates/bot23-za-horizontal-inventory-v001/runs/20260825_tradeops_full_dev_v002`
- Observed leakcheck run:
  `C:/botter/backtest/output/backtest153/candidates/bot23-za-horizontal-inventory-v001/runs/20260825_observed_leakcheck_tick_v001`
- reverse_d60 dev run:
  `C:/botter/backtest/output/backtest157/candidates/bot23-late-short-30m-action-matrix-v001/runs/20260826_0230_reverse_refinement_v001`
- reverse_d60 observed leakcheck run:
  `C:/botter/backtest/output/backtest157/candidates/bot23-late-short-30m-action-matrix-v001/runs/20260826_0226_observed_leakcheck_replay_v001`
- LONG target portfolio-rearm candidate:
  `C:/botter/backtest/output/backtest208/candidates/bot23-long-target-portfolio-rearm-v001`
- Inventory range false-break fade candidate:
  `C:/botter/backtest/output/backtest213/candidates/bot23-x-archive-inventory-range-fade-opt-v001`
- JST09-11 stable_001 research:
  provisional fixed implementation `stable_001-param-15-55-45`; Dev Stress
  146 trades / USD 345.0425 / PF 1.78453 / MTM DD USD 75.623 / 11 of 11
  positive weeks. The original stable_001 leakcheck was USD +79.333. The
  parameterized forward check contains only one complete day (4 trades,
  USD +28.446 Stress), so it is not independent proof and must not be retuned
  from live outcomes.
- JST11-13 round-level sweep research:
  fixed `round_s2p5_d0p05_r0p03`, 60-minute hold, capacity one. Dev Stress
  produced 102 trades / USD +199.7715 / PF 1.676534 / MTM DD USD 88.591;
  observed leakcheck Stress produced 40 / USD +156.4325 / PF 2.892275 / DD
  USD 27.583. The already-consumed forward file produced only 4 trades / USD
  +17.583 / PF 3.110804 / DD USD 15.756 and is not independent proof.
- JST09-13 combined fixed-risk audit:
  `evidence/jst0913_combined_v004`. On the exact fixed overlays, Dev Stress was
  248 trades / USD +544.814 / PF 1.74115 / every-tick MTM DD USD 80.473 /
  maximum four positions. The observed leakcheck diagnostic was 95 trades /
  USD +258.534 / PF 2.09609 / MTM DD USD 44.363 / maximum three positions.
  Morning/midday inventory overlapped for 26 DEV episodes (12.568 hours) and
  11 observed-leakcheck episodes (5.117 hours); the result is a portfolio-risk
  audit only and was not used to retune either block.

reverse_d60 Dev Base produced 1,529 trades, USD 908.314, PF 1.188445,
and every-tick MTM MDD USD 228.926. Its observed leakcheck Base produced 391
trades, USD 218.173, PF 1.190972, and MDD USD 153.836. Stress produced 378
trades, USD 261.099, PF 1.243088, and MDD USD 193.440. This remains a
forward-only candidate: the leakcheck period is already observed, and live
profit and five-second-poll equivalence are not proven.

The fixed eight-minute portfolio rearm improved reverse_d60 Dev Base from USD
908.314 / PF 1.1884 / MDD 228.926 / 1,529 trades to USD 986.008 / PF 1.2118 /
MDD 199.475 / 1,484 trades. Dev Stress improved from USD 862.938 / PF 1.1808 /
MDD 246.086 / 1,520 trades to USD 993.680 / PF 1.2200 / MDD 246.086 / 1,470
trades. The two already-observed forward days also improved, so this remains a
forward-only adoption rather than independent forward proof.

The fixed range-fade overlay improved that parent on Dev Base from 1,484 trades
/ USD 986.008 / PF 1.2118 / MDD 199.475 to 1,490 trades / USD 1,079.596 / PF
1.2343 / MDD 199.475. Dev Stress improved from 1,470 / USD 993.680 / PF 1.2200
/ MDD 246.086 to 1,473 / USD 1,066.683 / PF 1.2379 / MDD 246.086. Observed
leakcheck Base added one trade and USD 10.076; Stress was unchanged. Because
leakcheck was already observed and the event count is sparse, this remains
`forward_only` and must not be retuned from future live outcomes.

## Start prerequisites

State schema remains version 3 and the four ZA ownership namespaces are unchanged.
The first start adds empty morning lane states and policy identity while
preserving every existing ZA basket and pending entry. Morning ownership uses
new magics 230027-230029 and does not adopt positions from any other magic.
The first start also adds the empty midday lane and its frozen policy identity
without changing ZA or morning inventory. Midday ownership uses magic 230030
and comment `s23_md_l1`; an incompatible non-empty or foreign identity blocks
that lane rather than being adopted.
The first start adds the portfolio-rearm and range-fade fields under `routing`
while preserving all existing baskets, unresolved OPEN evidence, and pending
ZA entries; it invents neither a historical rearm interval nor a historical
range/breakout.
On the first start with `reverse_d60`, the runner preserves existing baskets and
unresolved OPEN reconciliation state but clears unsubmitted local pending
entries created under the previous entry policy.

An unresolved market `OPEN` remains a hard block on every new entry and add.
It does not suppress target, stop, failure-to-progress, or max-hold exits for an
older basket when every live position exactly matches the persisted ticket,
position identifier, symbol, magic, comment, side, and lot. Any unexpected live
ticket, ownership mismatch, or failed position query still blocks both
admission and automated close. After the lane is broker-flat, an unresolved
OPEN reservation is cleared only after three consecutive clean bot-scoped flat
position/order confirmations.

Before restart, MT5 must be reconciled for the retired bot23 magic 200023 and
active magics 230023-230030, with no unexplained pending orders. The runner independently
checks the retired namespace and refuses cutover if it is non-flat or cannot be
queried. Preserve a compatible `state/s23_bot_state.json`; never reset state
while any lane position or order exists. Because the close audit columns changed,
archive the existing `logs/s23_trades.csv` under an `old` folder before restart
so the new header is created. Startup now checks this before connecting.

`live_trading_enabled=true` and `shadow_forward_enabled=false` remain unchanged.
File replacement does not restart or deploy the running process.

Research/live equivalence still has two limits. Research built M1 from ordered
Bid ticks, while live uses broker HIST M1 OHLC and tick volume and submits on a
later five-second polling quote. The updated bridge now exposes broker quote
time and the fixed-hold path enforces the fresh-quote/spread/reopen contract,
but live exits can still occur several seconds after the first eligible tick.
This remains a forward-audit limitation rather than exact tick-for-tick parity.

## Checks

```powershell
py -m py_compile live_s23_bot.py test_s23_regressions.py
py live_s23_bot.py --self-test
py test_s23_regressions.py
py test_shadow_opportunity_observer.py
py test_shadow_state_tagger.py
py test_eu_session_clock.py
```

Local release-candidate runner SHA-256:
`a5c1774f08e6f9d0fcbc140a2b43d7653a7f09484143c167d8bcb47921150b95`.

Local release-candidate params SHA-256:
`17af7620dde993fb6c363201863930e5fbd9f4cdc49f6610d0a9aaec530a3b88`.
Local release-candidate regression SHA-256:
`050f0eb5349fdeb5e61d7d94832a733b5f10c4dee68810d08775c7e43dd47444`.
Entry-admission clock SHA-256:
`c45e52f49487df60fb3d7dfe0deaea09a1e9bf704df870e22732e4c7a0411abf`.
Position-lifecycle clock SHA-256:
`91e2feea92d986154dfce88171dd154daf6b80f842b42206a1a6d9c79631ee57`.
Clock regression SHA-256:
`14cbbc321f6bb952458cc75ca39a6a95d44ad2c4e62473f4f04431cbcd433089`.
Installed shadow-observer SHA-256:
`f25a003df49e801dce8f9e3a73fa1f5722f001c75a68d714295510628b49eeaa`.
Installed shadow-observer regression SHA-256:
`5dd72132c92de2aea1f43fe992cf738354529238d2cf503b4605c0c8a4c76f4d`.
Installed executor SHA-256:
`310f556570a0e2acd65d74b6a6dd3eb5c42ac1f1aa952fab71e4ac20c1ef12c7`.
Installed bridge-source SHA-256:
`07c85e92c6ab8f4e609339d8989dc7cd7ee576ab3b3f570b0ec54ac5f66ce834`.
All hashes above identify the local, not-yet-deployed release candidate. They do
not replace the canonical research identities in `SOURCE_BACKTEST.md`.
