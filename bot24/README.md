# Bot24 Visual No-Adverse C

Close-ledger re-audit (2026-09-04): replay re-syncs readable evidence before
consuming state; preflight rejects incomplete tails and duplicate/conflicting
deal ownership. Core and v206 derived close-state changes share one rollback
boundary with deferred helper saves and one complete atomic commit. Optional
empty CSV fields normalize identically on replay. No trading parameter changed.

Live/shadow port of frozen XAUUSD M1 candidate
`visual_no_adverse_c:target16`.

Bot24's execution contract is explicitly strategy-specific: confirmed-M1
entry/add and confirmed-M1 basket exit evaluation, with execution at the first
available quote after the newly confirmed bar. The 5-second process poll keeps
broker synchronization and monitoring alive but must not trigger another exit
decision within the same confirmed M1 bar. This restores parity with the
historical M1 evidence and the observed live log cadence. Deployment remains
blocked until the running host source/state are reconciled with this local
candidate.

New-entry time admission is routed by `time_regime_wrapper.py`. The current
topology deliberately reproduces the existing UTC 13:00-18:00 session and its
single `visual_no_adverse_c_target16` signal adapter. The wrapper is not allowed
to own positions, exits, orders, lots, SL/TP, state, or persistence. Existing
baskets therefore retain their confirmed-M1 target/stop/max-hold lifecycle
after the entry regime ends. Any mismatch between enabled bot24 strategy IDs
and routed strategy IDs fails at startup.

- Magic/comment: `200024` / `s24_no_adverse`
- Bridge/IPC: `BotBridge_s24`, `cmd_s24.txt`, `res_s24.txt`
- State/logs: `state/s24_bot_state.json`, `logs/s24_bot.log`, `logs/s24_trades.csv`
- Mode: `live_trading_enabled=true`, `shadow_forward_enabled=false`

## Independent v206 lane

Bot24 also contains the frozen `man_237_v206 / path_monotonic_center_approach`
strategy as its own independently managed lane 206. It uses magic `240206`, exact
comment `s24_v206`, 0.01 lot and one-position capacity. A same-time core and
v206 signal may therefore create two intentional, independently owned entries;
they are not deduplicated across strategies. v206 uses broker-side fixed SL,
actual-fill 1R TP, a 30-minute timeout beginning at broker-confirmed fill time,
fresh broker-quote-time evaluation, and a 5-minute cooldown. Before OPEN it
refreshes quote and symbol metadata, persists the full signal/ownership/lot
receipt, and re-queries exact positions and orders after every result. An
unresolved OPEN or CLOSE is not automatically resent. Disabling v206 blocks
only new entries; existing `240206 / s24_v206` inventory remains monitored.
Its state, repair path and close path are separate from the existing
`200024 / s24_no_adverse` basket.

## Passive runner lane

The live core mapping above is unchanged. A second lane is enabled only as a
local counterfactual observer (`runner_shadow.execution_mode=shadow`): maximum
2 positions, favorable add at 0.85 ATR30, basket target USD 32, basket stop USD
48, maximum hold 120 minutes, and cooldown 3 minutes. It never calls the
executor and cannot place or close broker orders.

The passive lane writes independent evidence when the runner is actually
started:

- `logs/s24_shadow_runner_trades.csv`: stateful runner entries, exits, PnL and stop reasons
- `logs/s24_shadow_opportunities.csv`: confirmed signal and routing outcome
- `logs/s24_shadow_markouts.csv`: executable Bid/Ask markouts at 1/5/15/30/60 minutes
- `logs/s24_shadow_state_tags.csv`: causal wick/range/activity/path/inventory descriptors
- `state/s24_shadow_observer_state.json`: restart-safe pending markout state

Signal-stop reasons distinguish session, spread, unavailable features, volume,
impulse, breakout, stale, cooldown, capacity, opposite-side inventory and add
threshold. The mandatory `logs/s24_trades.csv` uses the bot24 29-column
execution-evidence schema documented below; passive CSV files remain separate.

Both core and runner use `exit_clock=confirmed_m1`. This is a bot24 strategy
contract, not a shared default for other bots.

The runner uses the canonical shared-account safety modules: exact ownership,
hedging-mode preflight, confirmed OPEN/CLOSE reconciliation, foreign exposure
rejection, CSV schema preflight, corrupt-state fail-closed, account login/server
identity checks, broker quote timestamps, broker fill-time lifecycle recovery,
restart-safe unresolved-OPEN tracking, manual reconciliation alerts and staged
high-risk block clearing. An unresolved add blocks every new entry/add while a
fully matched pre-existing basket retains its confirmed-M1 exit monitoring.

```powershell
py -m py_compile *.py
py live_s24_bot.py --self-test
py test_s24_runner_shadow.py
py test_s24_time_regime_wrapper.py
py test_s24_safety_regressions.py
py test_s24_bridge_contract.py
py test_shadow_opportunity_observer.py
py test_shadow_state_tagger.py
py -m unittest discover -v
```

The Compose service and live mode are defined. Compose automatically prepares
the bridge and starts a retrying runner; chart attachment remains manual.
This installation deliberately uses the fixed local credentials in
`live_config.py` and `startup.ini`; Compose does not provide an unused second
credential channel. Keep both files private and synchronized when the account
connection is intentionally changed.

The local `2026-09-02-s24-core-atomic-v13` bridge requires atomic account,
hedging-mode, permission, inventory and exact ownership guards for both the
existing core `OPEN`/`CLOSE` path and v206 `OPEN_R1`/`CLOSE_R1`. New core
positions receive a durable opportunity-derived comment under the
`s24_no_adverse:` namespace; pre-existing exact `s24_no_adverse` ownership is
retained for close compatibility. Core close submission markers are persisted
per ticket and prevent duplicate CLOSE after a timeout or visibility lag.
Atomic account/mode/ownership/policy rejections prove that `CTrade` was not
reached, so only that submission marker is cleared. A durable non-recoverable
block still requires reconciliation and prevents retry on the next poll. A malformed
single-strategy state container is preserved under a quarantine key and isolated
to a blocked default lane. If that rejected snapshot contains an owned basket or
an in-flight OPEN/CLOSE receipt, process preflight fails closed instead of
silently replacing active lifecycle evidence with an empty lane. v206 retains
the same rejected snapshot inside its migration-gated state. Corrupt top-level
bot identity still fails the whole preflight closed. Frozen bridge,
time-routing, safety, symbol and strategy execution settings are checked at
process construction so an edited parameter file cannot silently change the
scored contract. Poll and status intervals are also frozen and validated before
the first broker poll; malformed, non-finite, zero or negative values cannot
cause a post-decision crash or a busy loop.
The no-order shadow runner is validated independently: malformed passive state
is copied to `quarantined_shadow_runner_states` and only that shadow lane is
reset, so it cannot throw before or erase an independently owned core basket.
Malformed quarantine containers and malformed core basket types fail closed
without escaping the startup state checker.
The top-level execution switches and every passive-shadow enable switch must be
JSON booleans; strings such as `"false"` are rejected. Live and core-shadow
entry cannot both be enabled. With both `live_trading_enabled=false` and
`shadow_forward_enabled=false`, bot24 is reconciliation-only: it records and
consumes a new signal but cannot create a simulated basket.
Passive observer/tagger artifact names are confined to their configured local
log/state directories. Observer horizons, retention, contract size and lot are
strict positive values, and non-finite or inverted OHLC history disables the
affected passive evidence component rather than emitting corrupt evidence.
Turning live entry off is not permission to forget broker inventory. If a broker-backed core basket already exists, bot24
continues exact read-only reconciliation, persists any due close intent, and
defers the broker CLOSE behind `live_disabled_with_owned_inventory`; it never
converts that basket to a simulated flat state. Re-enabling live execution after
an exact owned/order-clean sync clears only this recoverable block. Persisted
simulated core baskets remain explicitly `shadow=true` and cannot mix with a
broker-backed basket during a mode transition.
Bridge CAPS, configured account login/server, hedging mode and broker quote
timestamp are preflight requirements even while live entry is disabled. The
runner still reads durable core/v206 ownership in that mode, so a shadow-only
switch cannot authorize reconciliation against an unverified terminal.
The same quote timestamp contract is enforced on every runtime INFO response.
Missing, invalid, stale, future or non-monotonic quote clocks stop history,
shadow and entry/exit decision advancement, while exact core/v206 inventory and
CLOSEDEAL reconciliation continues without quote-time actions. After an exact
owned sync, broker open price and open time replace stale local lifecycle values;
v206 also rebuilds its timeout from that broker time, and core restores its
latest-add reference from the newest owned fill. A malformed active core
peak-PnL accumulator is normalized to `None` and conservatively rebuilt from the
next executable quote instead of quarantining otherwise identifiable inventory.
Core ownership matching requires both the executable position ticket and the
lifecycle position identifier. The same pair is revalidated for every existing
row immediately before OPEN, and both ticket and identifier uniqueness are
checked again after the broker response before any new fill is adopted.
Core comment ownership is syntax-bounded: only exact legacy
`s24_no_adverse` or the current `s24_no_adverse:<10 lowercase hex>` namespace
is accepted. Empty, non-hex, wrong-length, nested-delimiter, or mere-prefix
comments are foreign inventory and block reconciliation and new entry instead
of being adopted. Python ownership, executor OPEN/CLOSE policy, and the MQL5
bridge apply the same boundary.
All numeric fields on execution-bearing MQL5 commands are lexically validated
before conversion. Empty, signed, exponent, non-decimal, or trailing-junk text
is rejected before OPEN, CLOSE, R1 repair, or any trade API call.
The request-envelope expiry is also validated as unsigned integer text before
conversion, so a malformed expiry never reaches command dispatch.
Query commands use exact arity and strict numeric fields. `INFO` and `HIST`
are pinned to XAUUSD, `HIST` is pinned to M1, and inventory queries accept only
the core or v206 magic, preventing altered payloads from supplying another
instrument's quote or bar stream to the bot.
Exact OPEN rejections with MT5 retcode 10018, 10026 or 10027 are cleared as
definitive no-fill only when both order and deal receipts are zero and complete
post-command position/order inventory proves no delta. Clearing that submission
receipt does not consume an otherwise valid core signal or v206 opportunity.
It is retained for a retry only on a new broker quote at least 60 seconds later;
polling the same quote cannot resubmit. Consecutive atomic permission rejections
(10026/10027) are counted per lane and the third rejection creates a durable
non-recoverable block and manual alert. Other atomic OPEN guards, including
account/mode/ownership/policy/symbol/margin and bridge inventory-query guards,
clear only the proven no-fill submission receipt and immediately create a
durable non-recoverable block and manual alert; they cannot silently wait for a
later signal. Any execution-bearing or malformed
response retains the unresolved OPEN block. The independent v206 lane also
validates the complete post-OPEN namespace before adopting a returned fill.
Earlier V14 state gains empty retry/count fields without changing owned
inventory. Partial retry identity, a retry outside the original signal validity
window, or malformed counters fail closed instead of becoming executable.
Persisted core/v206/shadow timestamps must remain ISO timestamp strings, and
boolean or malformed financial/ownership fields cannot pass through numeric
coercion. Invalid active lifecycle state is preserved and blocks startup;
invalid passive shadow state remains isolated to its no-order quarantine.
The standalone opportunity observer, state tagger and shadow-runner CSV are
also passive boundaries. Malformed observer state, opportunity/markout CSV or
incompatible passive CSV headers are preserved in place, recorded as runtime
initialization errors and disable only the affected no-order component. The
observer validates the complete pending/completed state shape before it may
reconcile any CSV, rejects boolean financial values, and writes strict JSON
without non-finite numbers. Existing passive CSV rows are checked for exact
width at initialization and again before every append, so a file corrupted
after startup is not extended. The
disabled observer uses inert defaults and does not reopen the rejected state or
CSV during fallback construction. A later observer, tagger or shadow-runner
write failure disables only that component for the process while core/v206
management continues. The execution `s24_trades.csv` remains a mandatory
preflight schema and is never downgraded to this passive behavior.
Unexpected v206 poll or quote-less reconciliation exceptions are contained at
the lane boundary. A structurally valid submission/close receipt is retained
because it may represent an already-issued broker command; malformed partial
lane state is quarantined and replaced by the last valid snapshot before the
durable exception block is saved. The no-order shadow runner has no external
execution receipt, so a poll exception restores its complete pre-poll lane
snapshot before that passive component is disabled. Core processing never
persists a half-mutated passive lane as a side effect of its later save.
The bridge command lock serializes individual IPC calls but cannot protect
shared in-memory and durable state across two Python runners. Bot24 therefore
holds `state/s24_runner.lock` with an OS advisory lock for the complete live
runner lifetime, before runtime construction or broker preflight. A second
runner fails startup without reading or writing bot state or issuing commands.
The file may remain after a crash, but ownership is released by the OS when the
process exits; no stale-PID deletion or state repair is required. `--self-test`
does not acquire the live namespace lock and redirects every state read and
write to a temporary directory, so validation cannot migrate or normalize the
configured live state file. The safety regression suite applies the same
per-test state isolation.
Core and v206 durable-state validation rejects mixed-side or overlapping OPEN
lifecycle containers, CLOSE receipts without their persisted close intent, and
malformed confirmed-close receipt identity instead of normalizing them into an
apparently executable state.
Core writes one durable `strategy_decision` receipt per usable completed bar,
including signal, no-signal and routing-not-evaluated outcomes.
Current-generation core baskets require the originating signal bar on every
position. A legacy active basket without that evidence remains close-manageable
but cannot add or open again; the strict marker is restored when it becomes flat.
After a confirmed core or v206 close, the lane persists broker close time,
closed side, close reason, and the latest consumed signal bar. A completed-M1
signal that was already executable at or before that close cannot reopen the
same lane in the same direction after restart; an opposite-side signal remains
eligible under the frozen reverse/routing rules. Partial legacy close identity
is quarantined fail-closed instead of being guessed.
Every command is atomically published with a unique request ID and UTC expiry,
claimed before execution by the EA, and accepted by Python only when the
response carries the matching request ID. This prevents a delayed response or
an unconsumed command from being mistaken for the current request.
An empty command-file remnant produced by bridge v7 is removed under the IPC
lock during the v8 transition; a non-empty command is never overwritten.
On both Windows and CentOS the inter-process lock is an OS advisory lock, so a
killed Python process releases ownership automatically.  The inert lock file
may remain and is never treated as proof that a process still owns the bridge.
Unused legacy `PENDING`, `MODIFY`, and `CANCEL` commands are not advertised and
always return `ERR|UNSUPPORTED_COMMAND`.

Time-based core and v206 exits use the same frozen close trigger but defer a
first close attempt above 300 spread points. After a wide quote, three distinct
fresh quotes at or below the cap are required; the spread guard is forcibly
released after 30 minutes. A market-closed response preserves ownership and is
retried only after a fresh broker quote at least 60 seconds later.
When a disappeared owned position has no CLOSEDEAL in its narrow persisted
open-time window, both core and v206 retry once with the bridge's bounded wider
history before retaining a no-deal reconciliation block.
If M1 history is temporarily unavailable, core entry and new exit decisions
remain blocked, while exact inventory reconciliation continues. Only a close
reason already persisted from an earlier confirmed M1 decision may be retried
from a fresh broker quote; the outage cannot originate a new TP/SL/max-hold
decision.
If the INFO quote itself is temporarily unavailable, both core and v206 still
run quote-less exact position/order/CLOSEDEAL reconciliation without advancing
any time-based lifecycle. A recoverable quote block is added only after that
sync and cannot replace an existing non-recoverable ambiguity. A quarantined
v206 snapshot containing a basket or in-flight OPEN/CLOSE receipt cannot pass
the migration gate on flat confirmations alone.

The same bridge emits HIST bar timestamps as explicit Unix seconds interpreted
as UTC. Formatted broker-server wall time is no longer accepted by the bot24
parser, so signal and stale checks do not depend on an assumed server timezone.

Confirmed core OPEN ownership state is atomically persisted before its
mandatory execution CSV is written. Broker-confirmed core/v206 CLOSEDEAL rows
use the opposite safe ordering: the immutable realized-PnL ledger is durably
written before the owned basket is consumed. A ledger failure retains the
basket for retry; a state-save failure restores it unless the complete new
state is already visible. Deal-based exact replay is idempotent, while a
conflicting replay fails closed. Full-close reconciliation also preserves every
originating entry signal-bar identity before clearing the basket, preventing a
completed signal from being reused after restart.
The same state-first rule applies when atomic inventory proof establishes that
an OPEN was not filled, and when a CLOSE is retryably rejected as market-closed
or trade-permission-disabled.  Pending submission receipts are cleared and the
retry/block decision is durable before its audit row, so a CSV failure cannot
strand an already-resolved command receipt.
Execution CSV preflight and every append validate every existing record, not
only the header. Truncated rows, surplus columns and strict CSV parse failures
stop core preflight or the next append; the original file is left unchanged for
manual archive/recovery. Once a path has been validated or created by the
process, its later disappearance also fails closed instead of creating a fresh
history. This catches runtime deletion or replacement after startup validation.
Full-width rows are also checked as execution evidence: UTC timestamp, event,
strategy, lane, magic and symbol identities, live flag, causal event/release/
available/decision/executable clocks, separate ticket/position/deal identities,
optional signal timestamp, side and numeric fields must be structurally valid.
Core rows identify lane 1 / magic 200024 and v206 rows identify lane 206 / magic
240206. The row about to be
appended is checked before the file is opened, so an internally malformed event
cannot be written and discovered only on the following poll.
After a mandatory execution row is written, the file is flushed and fsynced
before append returns. First creation also syncs the parent directory on
platforms that support directory fsync. A durability failure propagates to the
runner; it is not reported as a successful audit append. Passive shadow CSVs
retain their separate best-effort isolation boundary.
An absent execution path is created exclusively; if another actor creates it
after the absence check, the runner leaves that file untouched and fails
closed. Existing CSV validation and append use the same open descriptor. A
disappearance, path/inode replacement, or size/mtime change during validation
is rejected instead of recreating or appending to unverified evidence.
An absent execution CSV may be created with its canonical header on the first
row. An already-existing zero-byte file is instead treated as lost/corrupt
evidence and fails closed; it is never silently reinitialized as a new history.
The process log uses size-based rotation at 10 MiB with five backups. Repeating
data/synchronization diagnostics are coalesced and summarized every 300 seconds,
preventing a persistent outage from growing `s24_bot.log` without bound while
retaining the first event and repeat count.

The bridge compiles with `OPEN_R1`,
`REPAIR_R1`, and `CLOSE_R1` at 0 errors / 0 warnings. Its response envelope,
record counts, exact position/order shapes, broker quote/fill clocks and
aggregated CLOSEDEAL exit volume are required by the Python parser. Partial,
extra-field, non-finite, old-version and ambiguous-absence responses are
rejected. It has not been attached or deployed; a running older bridge is
intentionally rejected by preflight.
