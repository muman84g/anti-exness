# bot18 Source Backtest

## Runtime mapping

- service/container: `exness-bot-18`
- production folder: `bot18`
- runner: `live_s18_bot.py`
- params: `s18_params.json`
- bridge: `BotBridge_s18`
- IPC files: `cmd_s18.txt` / `res_s18.txt` / `heartbeat_s18.txt` / `ea_bridge_s18.lock`

## Source

- Main known source: `C:\botter\backtest\output\backtest33_lightGBM_template_audit_bot18v2`
- Current live artifacts match `candidates\event_filter_template\live_bot18_v2_staging`
  / `bot18_v2_basket_forward_20260704`.
- `live_bot18_v2_aud055_staging` is a separate AUDUSD allow-rate 0.55 forward
  calibration and is not the current `bot18` artifact set.
- Legacy related sources: `backtest24_bot18`, `backtest34_bot18_small_range_filter`
- Strategy family: S18 v2 three-symbol ML event-filter basket
- Symbols: GBPUSD, EURUSD, AUDUSD

## Deployment note

The CentOS deployment keeps the existing `bot18` directory name. Do not create a separate `bot18_v2` runtime directory for `exness-bot-18`.

Preserve the existing CentOS `bot18/live_config.py` unless the user explicitly authorizes live-config changes. Code/artifact updates should not overwrite credentials or account settings.

## Runtime logging

`s18_policy_decisions.csv` is diagnostic only. It should not log every repeated threshold-block decision indefinitely; the live runner throttles repeated policy decisions while preserving passes, policy errors, and reason/signature changes.

## Live feature timing

S18 live policy features use closed bars only. H1 history is fetched through
`CopyRates(..., 0, bars)`, so the latest forming H1 row is dropped before
regime features are built. `h1_signal_age_minutes` is the current decision
time minus the latest closed H1 label, matching the backtest exporter rule
`decision_time_utc - h1_signal_time`. M1 policy features also drop the latest
forming M1 row before calculating ATR/range/return/volume.

Live still is not tick-identical to the backtest: the regime result is cached
for `regime_refresh_seconds`, and cycle starts are evaluated on live polling
ticks rather than only at historical M1 close rows.

## Live price calculation

S18 live execution uses local virtual grid levels as trigger/accounting state. When a virtual level is crossed and a market `OPEN` is sent, the executable request is rebuilt from the current tick on every send/retry: LONG uses current Ask and SHORT uses current Bid, with server SL one grid distance from that current market entry. This avoids carrying an obsolete virtual-level SL into a later fill after Algo Trading rejection, timeout, or broker rejection.

After a server-side or local SL leaves a symbol flat, live execution clears the old virtual breakout triggers and waits `post_sl_reanchor_cooldown_seconds` before starting a fresh cycle from the current Bid. This is an intentional live safety behavior so a stopped-out breakout is not immediately re-entered from the same stale trigger set. If the symbol is flat and an old breakout trigger is crossed only after a large market drift, live execution also clears/reanchors when the drift exceeds `max_flat_breakout_entry_drift_pips`; it does not chase a stale flat trigger with a late market order.

Recoverable live-only entry blocks, such as transient position-sync, SL-close, autoTP-close, SL-repair, max-count, or recovered untracked-position blocks, are cleared only after a later clean sync proves MT5 live tickets and bot state tickets are aligned. Ambiguous exposure and unresolved reconciliation remain fail-closed.

## Live lot allocation

As of 2026-07-15, the live per-symbol profile lots are GBPUSD `0.09`, EURUSD `0.07`, and AUDUSD `0.07`. Entry thresholds, policy artifacts, and allow-rate settings are unchanged.
