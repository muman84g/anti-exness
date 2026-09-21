# bot0 dashboard

bot0 is a separate, read-only dashboard service. Version 3 contains only the
bot23 adapter. It reads `s23_params.json` and `s23_trades.csv` from read-only
mounts and exposes GET-only JSON plus a small HTML view.

The close contract is one row per unique non-empty `deal_id` from
`position_close_confirmed`. A broker `deal_time_utc` column or the same field in
`note` is required for period attribution; the recorded row timestamp is never
used as a fallback. Duplicate identities are reported. Conflicting, malformed,
missing-time, missing-profit, and missing-id rows are quarantined and excluded.

Metrics and curves are split by `live` and `shadow`, then by strategy and signal.
The dashboard never presents a combined live/shadow PnL. Account currency must be
declared in params and each accepted close row must carry the same currency;
otherwise PnL and PF are null with an explicit blocked status. Deal counts and
win rates remain visible as audit counts. Open position count and MTM are null
because no read-only current position plus Bid/Ask snapshot is part of this
service.

The service cannot prove bot liveness from a file mtime. It reports source
file age and event age separately and leaves runtime liveness as `unknown`.
An absent explicit config-generation field is reported as `unknown`; a
candidate id is not treated as a generation.

The collector reads params and trades as one snapshot. A failed refresh retains
the immutable last-good snapshot and marks the response stale with the source
error. It never replaces a good snapshot with an empty-success value.

Build and run from the repository root with the compose service:

```bash
docker compose build bot0-dashboard
docker compose up -d bot0-dashboard
```

The default bind is `127.0.0.1:8230`. Put a separately authenticated reverse
proxy or a private VPN boundary in front of it if remote access is needed.
