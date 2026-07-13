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
