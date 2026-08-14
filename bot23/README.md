# Bot23 Loss-Abort Failure-to-Progress

`bot23` is the shadow-first live port of the single frozen XAUUSD M1 candidate
`visual_loss_abort_g_failure_to_progress`.

The entry, add, basket target/stop, maximum hold, cooldown, session, and volume
rules remain the frozen loss-abort specification. The structural change closes
a basket from bar 10 onward when its lifetime peak basket PnL has never reached
USD 3. This check runs after the USD 10 target and USD 18 stop checks.

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
