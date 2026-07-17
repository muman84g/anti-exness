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

Cycle-start policy evaluation is keyed by the latest closed M1 decision time,
so the live runner evaluates at most once per closed M1 bar while flat, matching
the backtest exporter event cadence. Live still is not tick-identical to the
backtest: the regime result is cached for `regime_refresh_seconds`, and the
actual tick used for the first eligible live poll after an M1 close can differ
from the historical M1 close price/spread row used by the exporter.

For reproducibility checks, live writes `logs/s18_decision_snapshots.csv`.
This CSV records one detailed row per non-duplicate closed-M1 cycle-start
policy decision, including `m1_decision_time_utc`, live tick Bid/Ask,
effective spread, H1/M1 feature values, model probability, threshold, reason,
and whether the decision blocked, shadow-allowed, or started a cycle. Compare
this file against backtest event rows before changing thresholds or features.

## Live price calculation

S18 live execution uses local virtual grid levels as trigger/accounting state. When a virtual level is crossed and a market `OPEN` is sent, the executable request is rebuilt from the current tick on every send/retry: LONG uses current Ask and SHORT uses current Bid, with server SL one grid distance from that current market entry. This avoids carrying an obsolete virtual-level SL into a later fill after Algo Trading rejection, timeout, or broker rejection.

After a server-side or local SL leaves a symbol flat, live execution keeps the current cycle and remaining virtual grid. It does not start a fresh cycle simply because the symbol is flat. Virtual orders now carry an `armed` flag: if an order is created or restored while price is already on the trigger side, it is not eligible to fill until price returns to the non-trigger side and then crosses again. This preserves the snowball cycle concept while preventing same-level `SL -> immediate re-entry` churn. If the symbol is flat and an old breakout trigger is crossed only after a large market drift, live execution suppresses that stale fill by disarming the order until a fresh recross instead of clearing/reanchoring.

The source exporter `export_bot18_cycle_events.py` has the same `trigger_rearm_on_recross=True` rule. In the default `price_path_mode="close"` diagnostic, this means an order can fill only after the previous evaluated close has returned to the non-trigger side and the current evaluated close crosses the trigger again. Tick-exact live fills can still differ from M1-close diagnostics because live sends market orders from the current Bid/Ask when a trigger is crossed.

Recoverable live-only entry blocks, such as transient position-sync, SL-close, autoTP-close, SL-repair, max-count, or recovered untracked-position blocks, are cleared only after a later clean sync proves MT5 live tickets and bot state tickets are aligned. Ambiguous exposure and unresolved reconciliation remain fail-closed.

## Startup state recovery difference

On live startup, bot18 performs a read-only MT5 sync before normal entry processing: current tick, bot-owned positions/orders, H1 regime history, and closed-M1 policy features are fetched, and any uniquely identifiable bot-owned live positions are restored into state. This reduces restart/state-loss drift for an already-open cycle.

Startup catch-up then reads closed M1 history from the last processed/cycle-start decision bar and advances local virtual state without sending orders. Because bot18 uses local virtual triggers that send market `OPEN` only while the runner is alive, catch-up never creates a historical position for a missed offline trigger. If a trigger was crossed while MT5 has no matching bot-owned position, the trigger is kept but disarmed until a fresh recross. If replay history is insufficient or local state is otherwise unrecoverable, bot18 confirms bot-owned MT5 positions and orders are both flat before resetting local state; otherwise it remains fail-closed with `reconciliation_required`.

This recovery is intentionally not a historical trade replay. If local state is lost while MT5 is flat, the live bot cannot safely infer missed virtual trigger fills from later prices. It does not invent historical entries; with recoverable state it waits for fresh recross, and with unrecoverable flat state it resets only after bot-owned MT5 exposure is confirmed absent.

## Live lot allocation

As of 2026-07-17, the live per-symbol profile lots are reduced to GBPUSD `0.02`, EURUSD `0.01`, and AUDUSD `0.01` while live/backtest reproducibility concerns are monitored. Entry thresholds, policy artifacts, and allow-rate settings are unchanged. The top-level fallback `lot` remains `0.01`; live profiles use each profile's `lot`.
