# Bot21 / S21 Ehlers Top3 Multi-Symbol

S21 is the shadow-first implementation of the backtest67_1 Ehlers 1h candidates:

- `US500_137_1h`
- `AUDUSD_021_1h`
- `USDJPY_035_1h`

## Strategy

- Symbols: `US500`, `AUDUSD`, `USDJPY`
- Timeframe: H1 confirmed bars. MT5 bar timestamps are interpreted as broker server time (`Europe/Athens`) and converted to UTC before signal/stale checks.
- Signal: Ehlers trendline cross
  - Trendline: EMA of H1 close
  - Long: previous close <= previous trendline and current close > trendline
  - Short: previous close >= previous trendline and current close < trendline
  - Cycle filter: `abs(close - trendline) > ATR14 * cycle_atr`
- Entry: market order after the H1 signal bar is confirmed
- Exit:
  - Server SL/TP when live trading is enabled
  - Bot time close after `max_hold_bars` hours from actual entry time

## Files

- Runner: `live_s21_bot.py`
- Params: `s21_params.json`
- State: `state/s21_bot_state.json`
- Log: `logs/s21_bot.log`
- Trades CSV: `logs/s21_trades.csv`
- Bridge source: `BotBridge_s21.mq5`

## Live Switch

Default params are intentionally shadow-forward:

```json
"live_trading_enabled": false,
"shadow_forward_enabled": true
```

Real order placement requires an explicit change to `s21_params.json` and a separate deploy/restart authorization.

Live trading also requires a hedging account. The runner rejects netting/exchange account modes because shared-account ownership cannot be isolated safely by magic/comment there.

## Execution Safety

- EA trade calls verify `ResultRetcode()` and deal/order evidence; `CTrade` boolean success alone is not accepted.
- Python re-queries bot-owned `POSITIONS` after live `OPEN` before writing active state.
- Ticket drift is adopted only when one symbol/magic/comment/side match exists.
- Transient position/order sync failures block entries only until the next clean sync; ambiguous ownership remains blocked.
- Market deviation is `max_deviation_points=20` by default.
- Manual-action alerts use `BOT_MANUAL_ALERT_WEBHOOK_URL` or `DISCORD_WEBHOOK_URL` from the environment only.

## Bridge

Attach/compile `BotBridge_s21.mq5` in MT5 Expert Advisors.

Required EA inputs:

```text
InpCommandFile=cmd_s21.txt
InpResponseFile=res_s21.txt
```

The Python side uses the same files via:

```text
EA_BRIDGE_COMMAND_FILE=cmd_s21.txt
EA_BRIDGE_RESPONSE_FILE=res_s21.txt
EA_BRIDGE_HEARTBEAT_FILE=heartbeat_s21.txt
EA_BRIDGE_LOCK_FILE=ea_bridge_s21.lock
```

## Commands

Local/container self-test:

```bash
python3 /app/bot21/live_s21_bot.py --self-test
```

Docker compose no-order self-test:

```bash
sudo docker compose run --rm --no-deps exness-bot-21 python3 /app/bot21/live_s21_bot.py --self-test
```

For the compose command, `bot21/live_config.py` must exist on the host because it is bind-mounted. Use a local sensitive config file and do not commit it.

One-cycle preflight/run:

```bash
python3 /app/bot21/live_s21_bot.py --once
```

Normal shadow run:

```bash
python3 /app/bot21/live_s21_bot.py
```
