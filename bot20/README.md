# Bot20 / S20 XAUUSD Large-Candle Short Basket

S20 is the live/shadow implementation of the backtest43 `large_candle_short_basket_m1` `confirm_refine_top_45_04` candidate.

## Strategy

- Symbol: `XAUUSD`
- Direction: short only
- Initial entry: confirmed H1 bearish large candle
  - H1 body >= `2.0 * H1 ATR14`
  - H1 close is in the lower 35% of the candle range
  - signal window is UTC 13:00-16:00
  - after the H1 signal, wait up to 45 minutes for M1 close <= first post-signal M1 open - `0.4 * M1 ATR30`
- Add: profit-only pyramiding
  - basket floating PnL must be positive
  - price must move favorably by `0.8 * M1 ATR30` from the last add
  - one add at most every 5 minutes
  - max positions: 10
- Exit:
  - basket time exit after 4 hours
  - basket DD stop at `3.0 * summed position risk`

## Files

- Runner: `live_s20_bot.py`
- Params: `s20_params.json`
- State: `state/s20_bot_state.json`
- Log: `logs/s20_bot.log`
- Trades CSV: `logs/s20_trades.csv`
- Bridge source: `BotBridge_s20.mq5`

## Live Switch

Default params are intentionally shadow-forward:

```json
"live_trading_enabled": false,
"shadow_forward_enabled": true
```

To place real orders, change them to:

```json
"live_trading_enabled": true,
"shadow_forward_enabled": false
```

Use 0.01 lot first. At `max_positions=10`, max exposure is 0.10 lot.

## Bridge

Attach/compile `BotBridge_s20.mq5` in MT5 Expert Advisors.

Required EA inputs:

```text
InpCommandFile=cmd_s20.txt
InpResponseFile=res_s20.txt
```

The Python side uses the same files via:

```text
EA_BRIDGE_COMMAND_FILE=cmd_s20.txt
EA_BRIDGE_RESPONSE_FILE=res_s20.txt
EA_BRIDGE_HEARTBEAT_FILE=heartbeat_s20.txt
EA_BRIDGE_LOCK_FILE=ea_bridge_s20.lock
```

The runner verifies `CAPS` on startup and stops if the attached bridge is not `BotBridge_s20`.

## Commands

Self-test:

```bash
python3 /app/bot20/live_s20_bot.py --self-test
```

One-cycle preflight/run:

```bash
python3 /app/bot20/live_s20_bot.py --once
```

Normal run:

```bash
python3 /app/bot20/live_s20_bot.py
```
