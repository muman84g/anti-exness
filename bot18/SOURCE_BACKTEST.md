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
- Legacy related sources: `backtest24_bot18`, `backtest34_bot18_small_range_filter`
- Strategy family: S18 v2 three-symbol ML event-filter basket
- Symbols: GBPUSD, EURUSD, AUDUSD

## Deployment note

The CentOS deployment keeps the existing `bot18` directory name. Do not create a separate `bot18_v2` runtime directory for `exness-bot-18`.

Preserve the existing CentOS `bot18/live_config.py` unless the user explicitly authorizes live-config changes. Code/artifact updates should not overwrite credentials or account settings.

## Runtime logging

`s18_policy_decisions.csv` is diagnostic only. It should not log every repeated threshold-block decision indefinitely; the live runner throttles repeated policy decisions while preserving passes, policy errors, and reason/signature changes.

## Live lot allocation

As of 2026-07-15, the live per-symbol profile lots are GBPUSD `0.09`, EURUSD `0.07`, and AUDUSD `0.07`. Entry thresholds, policy artifacts, and allow-rate settings are unchanged.
