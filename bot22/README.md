# Bot22 / S22 EURUSD H1 Bollinger Squeeze Pullback

S22 is the shadow-first implementation of the fixed backtest108_1 candidate:

- `man_024_v002 / EURUSD_005_1h`
- Params hash: `f97149f97d028e98`

## Strategy

- Symbol: `EURUSD`
- Timeframe: H1 confirmed bars. MT5 bar timestamps are interpreted as broker server time (`Europe/Athens`) and converted to UTC before signal/stale checks.
- Signal: Bollinger squeeze-breakout pullback
  - Bollinger period: 20
  - Std multiplier: 2.0
  - Squeeze width lookback: 80
  - Squeeze quantile: 0.35
  - Pullback window: 8
  - Long: a recent squeeze breakout above the upper band, then pullback through the middle band and bullish close back above the middle band
  - Short: a recent squeeze breakout below the lower band, then pullback through the middle band and bearish close back below the middle band
- Entry: market order after the H1 signal bar is confirmed
- Exit:
  - Server SL/TP when live trading is enabled
  - Bot time close after `max_hold_bars=18` hours from actual entry time

## Files

- Runner: `live_s22_bot.py`
- Params: `s22_params.json`
- State: `state/s22_bot_state.json`
- Log: `logs/s22_bot.log`
- Trades CSV: `logs/s22_trades.csv`
- Bridge source: `BotBridge_s22.mq5`

## Live Switch

Default params are intentionally shadow-forward:

```json
"live_trading_enabled": false,
"shadow_forward_enabled": true
```

Real order placement requires an explicit change to `s22_params.json` and a separate deploy/restart authorization.

Live trading also requires a hedging account. The runner rejects netting/exchange account modes because shared-account ownership cannot be isolated safely by magic/comment there.

## Execution Safety

- EA trade calls verify `ResultRetcode()` and deal/order evidence; `CTrade` boolean success alone is not accepted.
- Python re-queries bot-owned `POSITIONS` after live `OPEN` before writing active state.
- Ticket drift is adopted only when one symbol/magic/comment/side match exists.
- Transient position/order sync failures block entries only until the next clean sync; ambiguous ownership remains blocked.
- Market deviation is `max_deviation_points=20` by default.
- Manual-action alerts use `BOT_MANUAL_ALERT_WEBHOOK_URL` or `DISCORD_WEBHOOK_URL` from the environment only.

## Bridge

Attach/compile `BotBridge_s22.mq5` in MT5 Expert Advisors.

Required EA inputs:

```text
InpCommandFile=cmd_s22.txt
InpResponseFile=res_s22.txt
```

The Python side uses the same files via:

```text
EA_BRIDGE_COMMAND_FILE=cmd_s22.txt
EA_BRIDGE_RESPONSE_FILE=res_s22.txt
EA_BRIDGE_HEARTBEAT_FILE=heartbeat_s22.txt
EA_BRIDGE_LOCK_FILE=ea_bridge_s22.lock
```

## Commands

Local/container self-test:

```bash
python3 /app/bot22/live_s22_bot.py --self-test
```

Docker compose no-order self-test:

```bash
sudo docker compose run --rm --no-deps exness-bot-22 python3 /app/bot22/live_s22_bot.py --self-test
```

For the compose command, `bot22/live_config.py` must exist on the host because it is bind-mounted. Use a local sensitive config file and do not commit it.

One-cycle preflight/run:

```bash
python3 /app/bot22/live_s22_bot.py --once
```

Normal shadow run:

```bash
python3 /app/bot22/live_s22_bot.py
```
