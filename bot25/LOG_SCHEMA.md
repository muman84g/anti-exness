# Bot25 canonical log contract

## Files

- `logs/s25_bot.log`: rotating health and failure log, 10 MiB x 5 backups.
- `logs/s25_trades.csv`: strict 44-column event ledger.
- Incompatible prior CSVs are recoverably moved to `logs/old/`; schemas are
  never appended together.

## Identity and linkage

- `episode_id` is assigned before the first bilateral seed order.
- `basket_id` is the compatibility alias of `episode_id`.
- `opportunity_id` links a completed-M5 decision to its frontier entry or
  strategic close. Seed and startup events use separate deterministic IDs.
- `ticket` is the MT5 position ticket. `position_identifier` is the stable MT5
  position ID. `deal_id` is the broker deal ID and is the realized-close
  dedupe key. These values must not be substituted for one another.

## Price and profit

- Live `entry.price`: broker-confirmed position open price.
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

- Startup: `startup_recovery`.
- Open: `open_reserved` -> `entry`, or an explicit reject/ambiguous/recovery
  event. A reservation is persisted before the broker request.
- M5: one `m5_decision` per processed bar with `signal`, `no_signal`, or a
  specific `not_evaluated_*` reason.
- Close: `close_reserved` -> `close_requested` ->
  `position_close_confirmed`; shadow uses `close` directly after reservation.
- Reconciliation emits realized rows only after all missing state positions in
  that sync pass have valid owned close deals. Partial evidence emits no profit.
