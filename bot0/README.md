# bot0 dashboard

bot0 is a separate, read-only dashboard service. Version 4 contains the bot23
ledger adapter and frozen production gate contract. It reads `s23_params.json`
and `s23_trades.csv` from read-only mounts and exposes GET-only JSON plus a
small HTML view.

The close contract is one row per unique semantic `deal_id` from
`position_close_confirmed`. Semantic identity excludes `timestamp_utc`, so a
repeated durable row with a later recording timestamp is deduplicated. A broker
`deal_time_utc` column or semicolon key-value field in `note` is required for
period attribution; the recorded row timestamp is never a fallback. An empty
close `opportunity_id` is joined only when exactly one entry with the same
position identifier or ticket has an opportunity. The current exported bot23
ledger audits 525 closes: 494 direct and 31 unique entry joins. Ambiguous joins
remain visible and are never silently assigned.

Signal identity accepts row columns, semicolon key-value notes, and JSON notes.
Historical attribution uses only ledger row fields and entry joins. Current
params and current magic lists never backfill past rows. Metrics and curves are
split by `live` and `shadow`, then by strategy and signal. The dashboard never
presents a combined live/shadow PnL. Raw `ledger_profit` is shown separately
with `unconfirmed` units. Realized PnL and PF require explicit account currency
and an explicit matching profit unit on every row; otherwise they are null with
an explicit blocked status. Deal counts and win rates remain visible as audit
counts. Open position count and MTM are null because no read-only current
position plus Bid/Ask snapshot is part of this service.

The service cannot prove bot liveness from a file mtime. Runtime liveness is
`unknown`. The configured live gate is exposed as `configured_live_enabled` and
fails closed when either it or root `enabled` is absent. An absent explicit
config-generation field is reported as `unknown`; a candidate id is not treated
as a generation.

The collector reads params and trades as one snapshot using strict CSV parsing
and read-before/read-after file identity. A failed refresh retains the immutable
last-good snapshot and marks the response stale with the source error. Source
hashes, file identity, and rotation coverage are included in the response. It
never replaces a good snapshot with an empty-success value.

Build and run from the repository root with the compose service:

```bash
docker compose build bot0-dashboard
docker compose up -d bot0-dashboard
```

The default bind is `127.0.0.1:8230`. Put a separately authenticated reverse
proxy or a private VPN boundary in front of it if remote access is needed.

`python -m unittest discover -s bot0 -v` runs fixtures for the eleven frozen
contracts, including the real exported ledger count when it is available.
