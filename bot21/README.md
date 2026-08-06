# Bot21 / S21 Ehlers Top3 Multi-Symbol

S21 is the live-order implementation of the backtest67_1_bot21 Ehlers 1h candidates:

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
- Entry: market order after the H1 signal bar is confirmed, in the direction opposite to the Ehlers signal
- Cycle concurrency: confirmed signal bars may open new normal positions while older positions remain active; normal and reversal positions together are capped by `max_active_positions` per symbol
- Exit:
  - Server SL/TP when live trading is enabled
  - Bot time close after `max_hold_bars` hours from actual entry time
  - A confirmed MT5 TP deal on a normal position queues one opposite-side reversal position with the same configured SL/TP distances
  - SL, time, manual, unknown, and reversal-position closes do not create another reversal

## Files

- Runner: `live_s21_bot.py`
- Params: `s21_params.json`
- State: `state/s21_bot_state.json`
- Log: `logs/s21_bot.log`
- Trades CSV: `logs/s21_trades.csv`
- Bridge source: `BotBridge_s21.mq5`

## Live Switch

Current params are live-order enabled by explicit user instruction on 2026-07-27:

```json
"live_trading_enabled": true,
"shadow_forward_enabled": false
```

Service deployment, bridge attachment, or restart are separate runtime actions.

Live trading also requires a hedging account. The runner rejects netting/exchange account modes because shared-account ownership cannot be isolated safely by magic/comment there.

## Execution Safety

- EA trade calls verify `ResultRetcode()` and deal/order evidence; `CTrade` boolean success alone is not accepted.
- Python re-queries bot-owned `POSITIONS` after live `OPEN` before writing active state.
- Server-side TP is accepted only from the matching MT5 close deal (`position identifier`, symbol, and magic); current price is not used to infer TP.
- Reversal comments retain the origin ticket so restart recovery cannot create the same reversal twice.
- Ticket drift is adopted only when one symbol/magic/comment/side match exists.
- Transient position/order sync failures block entries only until the next clean sync; ambiguous ownership remains blocked.
- Market deviation is `max_deviation_points=20` by default.
- Manual-action alerts use `BOT_MANUAL_ALERT_WEBHOOK_URL` or `DISCORD_WEBHOOK_URL` from the environment only.

## Bridge

Attach/compile `BotBridge_s21.mq5` in MT5 Expert Advisors.
The current runner requires bridge capability `CLOSEDEAL`; an older compiled `BotBridge_s21.ex5` is rejected by preflight.

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

Normal live-order run:

```bash
python3 /app/bot21/live_s21_bot.py
```

After pulling an update on the CentOS host, compile/attach the updated bridge in MT5 and recreate only this service before live use:

```bash
sudo docker compose run --rm --no-deps exness-bot-21 python3 /app/bot21/live_s21_bot.py --self-test
sudo docker compose up -d --no-deps --force-recreate exness-bot-21
sudo docker compose logs --tail=100 exness-bot-21
```

Do not treat `git pull` alone as deployment. Confirm `S21 preflight ok.` in the recreated service logs; a stale bridge must fail at the CAPS check instead of placing orders.
