# Bot25 canonical log contract

## Files

- `logs/s25_bot.log`: rotating health and failure log, 10 MiB x 5 backups.
- `logs/s25_trades.csv`: strict event ledger. V24 adds logical inventory and
  virtual-core columns while retaining real `long_positions`/`short_positions`.
- Incompatible prior CSVs are recoverably moved to `logs/old/`; schemas are
  never appended together. The replacement ledger begins with
  `schema_rollover`, naming the archived basename in `note`.
- Every event row, passive opportunity, markout, and state-tag append is
  flushed and fsynced before the file is closed.
- `logs/s25_shadow_opportunities.csv` records a frontier candidate before
  capacity, ratio, V23, and execution gates, followed by its route result.
- `logs/s25_shadow_markouts.csv` records executable Bid/Ask PnL and MFE/MAE at
  1/5/15/30/60/120 minutes whether or not the candidate was consumed.
- `logs/s25_shadow_state_tags.csv` records completed-M5 and current inventory
  state at registration; it never contains future markout values.
- A nonempty passive-observer state from another strategy version is never
  adopted automatically. The observer disables itself without affecting the
  trading path until that passive state is separately archived or reconciled.

## Identity and linkage

- `episode_id` is assigned when the cost-free virtual bilateral core starts.
- `basket_id` is the compatibility alias of `episode_id`.
- `opportunity_id` links a completed-M5 decision to its frontier entry or
  strategic close. Episode-start and startup events use separate deterministic IDs.
- The same frontier `opportunity_id` joins passive registration, route,
  markouts, state tags, `entry`, and V23 `entry_blocked` where applicable.
- `entry_blocked` with `reason=v23_drought_minority_add_pause` records a
  reached frontier add rejected by V23, with both side counts in `note`.
- `productive_close_confirmed` records the broker gross price PnL or shadow-cost-proxy
  amount that starts or rearms the causal 120-minute drought clock.
  Commission, swap, and fee remain included in the separate realized
  `position_close_confirmed.profit`; they do not change the V23 strategy clock.
- `startup_state_migrated` records an exact state-v5/man231 or state-v6/V23 to
  state-v8/V24+L05 upgrade. Flat migration requires empty owned inventory; nonflat
  migration requires an exact state/broker identity, side, lot, symbol, magic,
  comment and lifecycle match. Old contents are never logged.
- `ticket` is the MT5 position ticket. `position_identifier` is the stable MT5
  position ID. `deal_id` is the broker deal ID and is the realized-close
  dedupe key. These values must not be substituted for one another.

## Price and profit

- Live `entry.price`: broker-confirmed position open price.
- Live `entry.quote_time_utc` and `entry.executable_at`: broker position open
  time; the separate `decision_time` remains the strategy decision clock.
- Live `position_close_confirmed.price`: broker close-deal price.
- Shadow prices: executable Bid/Ask plus the frozen adverse cost proxy.
- Live close `gross_profit`, `commission`, `swap`, and `fee` are aggregated over
  every deal belonging to the complete position ID. `profit` is their sum.
- `profit_currency` is read from the connected MT5 account. Shadow uses the
  frozen backtest currency from params.
- `price_basis` and `profit_basis` state the source; derived price-distance
  values are never written as account-currency profit.

## Time contract

- `timestamp_utc`: row-write time.
- `quote_time_utc`: broker quote/deal time used by the event.
- `signal_bar_time`: completed M5 bar start.
- `event_time`: signal event time, except confirmed close where it is deal time.
- `release_time` / `available_time`: signal bar plus five minutes.
- `decision_time`: causal decision or close-request time.
- `executable_at`: actual executable quote/deal time; blank for a bar that was
  not yet available and therefore not executable.

## Required event chain

- Startup: optional `startup_state_retired` or `startup_state_migrated`, then
  `startup_recovery`. The retired-state event
  event exists only after account/symbol preflight, successful empty scoped
  position and order queries, and an unchanged-file check.
- Open: `open_reserved` -> `entry`, or an explicit reject/ambiguous/recovery
  event. A reservation is persisted before the broker request.
- M5: one `m5_decision` per processed bar with `signal`, `no_signal`, or a
  specific `not_evaluated_*` reason.
- L05 loss exits use the ordinary `close_reserved`, `close_requested`, and
  confirmed-close rows with `reason=loss_policy_L05`. The matching
  `m5_decision.note` identifies the completed-bar re-loss and selected-ticket
  count; L05 does not emit `productive_close_confirmed`.
- Close: `close_reserved` -> `close_requested` ->
  `position_close_confirmed`; shadow uses `close` directly after reservation.
- Reconciliation emits realized rows only after all missing state positions in
  that sync pass have valid owned close deals. Partial evidence emits no profit.
- Close rows deduplicate by event/deal ID and must match ownership and accounting.
  Duplicate identities already in a ledger are invalid. Productive-close rows
  carry sorted position IDs in `ticket_set` and deduplicate by episode/ticket set.
- Close rows precede one complete state commit. A failed commit remains retryable;
  the CSV schema and existing columns are unchanged.
