# Bot25 V23 XAUUSD bilateral book

Live-configured port of the fixed `V23` child of `man_231`. It continuously maintains
both BUY and SELL inventory, follows completed-M5 pivot breaks with an active
side, adds at a 0.50 ATR frontier, and releases profitable non-core satellites
while preserving the best-priced core ticket on each side.

V23 adds one rule only: after a broker-confirmed gross-price or shadow productive close of
at least 0.10 USD, if no further productive close occurs for more than 120
minutes, a frontier add is blocked only when its prospective side currently has
fewer tickets than the opposite side. Seed, equal/dominant-side adds, the first
productive close, all closes, and episode expiry remain man_231-compatible.

- Magic/comment: `200025` / `s25_m231`
- Bridge/IPC: `BotBridge_s25`, `cmd_s25.txt`, `res_s25.txt`
- State/logs: `state/s25_bot_state.json`, `logs/s25_bot.log`, `logs/s25_trades.csv`
- Passive evidence: `logs/s25_shadow_opportunities.csv`,
  `logs/s25_shadow_markouts.csv`, `logs/s25_shadow_state_tags.csv`, and
  `state/s25_shadow_observer_state.json`
- Mode: `live_trading_enabled=true`, `shadow_forward_enabled=false`
- Real-order double gate: params must enable live and environment variable
  `BOT25_ENABLE_REAL_TRADING=MAN231_LIVE_ACK` must also be present. The token is
  intentionally retained for deployment compatibility; it is not the strategy identity.

The runner uses the bot23 operational baseline: exact magic/comment ownership,
configured-account identity and hedging-mode preflight, confirmed OPEN/CLOSE
reconciliation, foreign exposure rejection, and two-stage high-risk block
clearing. An OPEN reservation is saved before the broker request, so a restart
cannot blindly duplicate an ambiguous entry. Pending CLOSE requests are
reconciled and retried after a bounded wait instead of remaining stuck.
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

The exact state-v5 man_231 identity is upgraded to V23 state-v6 only after a
read-only owned-position/order reconciliation. Existing `s25_m231` comments,
magic, episode IDs, tickets, pending closes, and inventory are preserved;
`last_productive_close_utc` starts empty, matching V23's pre-first-close rule.
If the state file belongs to any other older bot25 strategy, startup does not adopt or
overwrite it blindly. After bridge, symbol, account, hedging, and permission
preflight, the runner independently queries bot25-scoped positions and orders.
Only two successful empty results permit the unchanged old file to be atomically
moved to `state/old/` and a fresh man_231 state to be created. Query failure,
any scoped inventory, a corrupt/foreign state, or a file changed during
preflight remains fail-closed. Do not hand-edit or replace state identity fields.

## Logs

- `s25_bot.log` rotates at 10 MiB with five backups. It contains health,
  preflight, recovery, status, and failure messages but never credentials or a
  webhook URL.
- `s25_trades.csv` has a strict header. An old/incompatible file is moved to
  `logs/old/` with a timestamp before a new canonical header is written. It
  records broker quote time, completed-M5
  signal/event/release/availability/decision/executable times, episode ID,
  magic, ownership identifiers, inventory counts, active wave, reason, and
  live/shadow mode.
- Every CSV append is flushed and fsynced. A new canonical trade CSV begins
  with `schema_rollover` when an incompatible predecessor was archived.
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

`shadow_forward_enabled=false` remains the execution mode. The separately
named passive observer is evidence-only and remains active during live orders.

```powershell
py -m py_compile *.py
py live_s25_bot.py --self-test
py test_s25_passive_evidence.py
```

The source is configured for explicitly authorized real operation. The updated
`BotBridge_s25` reports bridge identity, account identity, quote epoch and
history epochs required by the runner. Both independent real-order gates are
enabled. A recreated service still fails closed until the updated EA is attached
and bridge/account/hedging/ownership preflight succeeds. See
`SOURCE_BACKTEST.md`.
