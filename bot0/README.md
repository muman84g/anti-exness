# bot0 dashboard

bot0 is a separate, read-only dashboard service. Version 1 contains only the
bot23 adapter. It reads `s23_params.json` and `s23_trades.csv` from read-only
mounts and exposes GET-only JSON plus a small HTML view.

The realized PnL contract is one row per unique non-empty `deal_id` from
`position_close_confirmed`. A valid `deal_time_utc` in `note` is used as the
close time; otherwise the audit row timestamp is used. Duplicate identities
are reported and conflicting duplicates are never added twice. MTM is null
because no read-only current Bid/Ask snapshot is part of this service.

The service cannot prove bot liveness from a file mtime. It reports source
file age and event age separately and leaves runtime liveness as `unknown`.
An absent explicit config-generation field is reported as `unknown`; a
candidate id is not treated as a generation.

Build and run from the repository root with the compose service:

```bash
docker compose build bot0-dashboard
docker compose up -d bot0-dashboard
```

The default bind is `127.0.0.1:8230`. Put a separately authenticated reverse
proxy or a private VPN boundary in front of it if remote access is needed.
