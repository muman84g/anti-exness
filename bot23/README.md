# Bot23 Loss-Abort Failure-to-Progress

`bot23` is the shadow-first live port of the single frozen XAUUSD M1 candidate
`visual_loss_abort_g_failure_to_progress`, revised by the forward-only
`M_block14_loss27` dev true-tick candidate.

The entry, add, basket target/stop, maximum hold, cooldown, session, and volume
entry signal remains the loss-abort specification. Adds use 0.65 ATR30, stop at
two positions, and are forbidden once pre-add basket PnL reaches USD 3. New
baskets are blocked during 14:00-14:59 UTC and after the bot's confirmed daily
realized PnL reaches -USD 27. TP, SL, failure-to-progress, and max-hold are
checked from the current Bid/Ask every five-second poll; entry/add decisions
remain confirmed-M1-only.

## Safety defaults

- `live_trading_enabled=true`; `shadow_forward_enabled=false`.
- One unique namespace: magic `200023`, comment prefix `s23_loss_abort`.
- Exact bridge identity and required command-capability preflight.
- Live mode requires an MT5 hedging account.
- OPEN is accepted only after ticket/position-identifier ownership confirmation.
- CLOSE remains pending until the matching position close deal is confirmed.
- High-risk sync blocks clear only after the related ticket is absent and two
  consecutive clean flat confirmations are observed.
- Foreign positions/orders with the same magic fail closed.
- The daily loss budget uses confirmed bot-owned close deals and resets at the
  UTC day boundary; it does not use account-wide PnL.

## Files and checks

- Runner: `live_s23_bot.py`
- Parameters: `s23_params.json`
- State: `state/s23_bot_state.json`
- Log/trades: `logs/s23_bot.log`, `logs/s23_trades.csv`
- IPC defaults: `cmd_s23.txt`, `res_s23.txt`, `heartbeat_s23.txt`

```powershell
py -m py_compile *.py
py live_s23_bot.py --self-test
```

The files are live-enabled, but no deployment, EA attachment, or restart is
performed by folder creation. Compose automatically prepares the bridge and
starts a retrying runner; chart attachment remains manual. Place
`startup.ini` and `live_config.py` as described in `../BOT20_25_CENTOS_STARTUP.md`.
