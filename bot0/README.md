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
Historical attribution uses only exact ledger identity fields and unique entry
joins. Current params and current magic lists never backfill past rows. The main
view contains only strategies and signals whose current `effective_enabled` is
true. The identity check uses the exact current
`(strategy_id, signal_id, signal_variant_id)` pair. A configured missing
variant matches only a ledger row with a missing variant; it never absorbs an
unexpected historical variant. Inactive and
unmapped accepted rows remain visible in the audit section with counts; they
are never remapped to a current strategy or signal.

For `research_entries_v142` lanes 25–30 only, the dashboard strictly parses the
five-field opportunity identity (`symbol|signal_bar_utc|signal_id|variant|side`)
and restores signal/variant only after matching one entry row (including its
`signal_bar_time`) and checking its strategy, lane, magic, physical and MT5 symbols, side, basket,
live flag, ticket, position identity, and entry record time against the close.
Owner magic/comment and any explicit signal/variant fields must agree.
Unknown variants, malformed IDs, repeated entries with the same opportunity,
competing entries on the same ticket/position, and any mismatch stay
unattributed. Failure reasons are included in the API audit. The close's `signal_bar_time` describes the
exit bar and is not compared with the entry opportunity clock. Only the two
source-defined IR variants are accepted on lane 30; the other five lanes require
their signal ID as the variant. Current params only control the existing enabled
pair visibility gate.

For the four NY0530 lanes (18–21), the four-field opportunity identity is
`physical_symbol|signal_bar_utc|t0530_edge_break_fade|LONG/SHORT`, with no
variant. Entry signal/event bars and the release, available, decision, entry,
and broker close clocks must be ordered and match the identity. The strategy,
lane, magic, symbol, MT5 symbol, side, live flag, basket, ticket, and available
position identifiers must agree; conflicting rows on the same ticket or
position block attribution. A recovery witness must be unique and recorded
between the entry and broker close, and must carry the same nonempty basket and
live flag. A position identifier present on only one side must equal the shared
ticket. A broker deal time repeated in both a CSV column and note must agree or
the close is quarantined. Owner magic/comment/deal magic are checked whenever
present. When they are absent, a matching `position_lifecycle_recovered` row
with `confirmed_broker_fill_time_restored` is required and the API marks the raw
series `broker_fill_recovery_witness; broker_owner_unverified`. This supports
raw ledger display only; it does not establish broker ownership or verified
currency PnL.

The 2026-09-23 `research_path_curvature_lane_27` close for deal `40552841` now
appears under `curvature_fade_short` in raw `profit` and its chart at `-9.41`.
The CSV has no explicit profit unit or currency for that deal, so verified
realized currency PnL and PF remain null.

Metrics and curves are split by `live`, `shadow`, and `unknown`, then by
strategy and signal. The dashboard never presents a combined live/shadow PnL.
Verified realized PnL and PF require explicit account currency and a matching
profit unit on every row; otherwise they remain null with an explicit blocked
status. `raw_ledger_metrics` keeps exact `ledger_profit` and `profit` fields
separate and groups each by exact strategy, signal, variant, execution class,
unit, and currency. Raw PF is defined as positive raw values divided by the absolute sum
of negative raw values and is null when there is no negative value. Its
provenance and value coverage are returned with every group.

Each current signal gets self-contained SVG chart series for the request's
`as_of_utc` window. The window starts one calendar month before `as_of_utc`,
clamping the day to the earlier month's last day, and ends at `as_of_utc` with
the half-open interval `[start,end)`. It starts at zero and uses the same
accepted rows and exact raw metric series as `raw_ledger_metrics`, including
strategy, signal, variant, execution class, value field, unit, and currency.
Different series
are never combined. Missing values stay in the coverage denominator, and empty
and stale-empty states are explicit. No CDN or chart library is required.
Open position count and MTM are null because no read-only current position plus
Bid/Ask snapshot is part of this service.

The first dashboard section and additive `/api/summary.overview` key show the
current UTC day, Monday-start UTC week, and UTC calendar month. Their intervals
are half-open and clipped at `as_of_utc`, so the entire 23:59 minute is included
when that day is complete while the next period's exact boundary is excluded.
Optional API `from` and `to` bounds also clip the overview; the API reports the
base and clipped UTC ranges. Curves use visible current-effective live closes
from the accepted snapshot and the broker `deal_time_utc`; equal close times are
ordered by `deal_id`, and the x-axis is proportional to elapsed UTC time. The
curve is a step line: it stays flat between closes, updates at each close, and
extends the final value to the visible period end. Overview totals
use only the ledger `profit` field. They are raw unverified values, never fall
back to `ledger_profit`, and are split by exact unit and currency. The curve
coverage denominator is every visible live close in that period; missing values
and mixed exact groups are labeled incomplete/separate instead of being
presented as complete. The compact per-period signal table uses the same live
rows, field, exact unit/currency groups, and coverage policy as its curve.
If an API refresh fails, the top overview is cleared and displays an unavailable
message instead of leaving the prior successful curve looking current.

For a reproducible chart end, call `GET /api/summary?as_of_utc=2026-02-28T12:00:00Z`.
The optional `from` and `to` parameters still limit the summary source rows;
the chart reports the intersection with its request-relative window.

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
share one usability contract: only non-empty `live`/`shadow` rows with matching
account currency and explicit profit units produce cumulative monetary values;
empty buckets keep realized PnL, PF, and currency null with `blocked_no_values`;
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
contracts. The optional real-export count check runs only when both the
bot23 trade export and params match the recorded 2026-09-23 fixture hashes;
newer live exports are skipped rather than compared with stale counts.
