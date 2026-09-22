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
and a batch read-before/read-after identity barrier. Optional evaluation and
metadata paths can be supplied with `BOT23_EVALUATION_PATH` and
`BOT23_METADATA_PATH`; all supplied paths participate in the same barrier, so
an atomic replacement or cross-file race fails closed. A failed refresh retains
the immutable last-good snapshot and marks the response stale with the source
error; the failed attempt is throttled by the collector TTL. Source hashes,
file identity, and rotation coverage are included in the response. The API
also exposes each optional source under `sources.optional` using a safe label,
basename, file identity, and hash. These optional files are identity-only
inputs; they are not parsed or included in accounting. It never replaces a
good snapshot with an empty-success value. Monetary metrics and equity curves
share one usability contract: only `live`/`shadow` rows with matching account
currency and explicit profit units produce cumulative monetary values;
`unknown` execution rows remain null and blocked.

For direct Windows browser access, the Compose service publishes
`0.0.0.0:8230:8230`; open `http://<CentOS-IP>:8230/` and use the Basic auth
identity in the external JSON file. The password file must stay outside Git.
Create it on CentOS, for example:

```bash
sudo install -d -o 65532 -g 65532 -m 700 /etc/exness-bot
sudo tee /etc/exness-bot/bot0-auth.json >/dev/null <<'EOF'
{"username":"bot0","password":"<set-on-CentOS>"}
EOF
sudo chown 65532:65532 /etc/exness-bot/bot0-auth.json
sudo chmod 400 /etc/exness-bot/bot0-auth.json
printf 'BOT0_AUTH_FILE_HOST=/etc/exness-bot/bot0-auth.json\n' >> .env
```

The file is mounted read-only and the container process runs as UID/GID
`65532:65532`, so the ownership and mode above let the process read the file
while keeping it private on CentOS. Keep `.env` untracked. Create the auth file
before building or starting the service, then run from the repository root:

```bash
docker compose build exness-bot-0
docker compose up -d exness-bot-0
```

The endpoint uses HTTP Basic auth over plain HTTP. Credentials and dashboard
data are therefore visible to anyone who can observe the network path; restrict
port 8230 with the CentOS firewall or place HTTPS/reverse-proxy protection in
front of it for untrusted networks. For the requested direct access, the
firewall rule is:

```bash
sudo firewall-cmd --permanent --add-port=8230/tcp
sudo firewall-cmd --reload
```

`.env` is protected by the repository `.gitignore`; never add the auth JSON or
its password to tracked files.

`python -m unittest discover -s bot0 -v` runs fixtures for the frozen
contracts, including the real exported ledger count when it is available.
