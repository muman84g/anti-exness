# Bot23 Chisiki/ReactVol Fixed4

`bot23` is a shadow-first live wrapper for four fixed XAUUSD dev candidates:

- `visual_loss_abort_g`
- `visual_no_adverse_c`
- `h14_18_h120_tp12_dd40_vol105_impulseonly_all_all`
- `visual_break_reverse_a`

The runner is copied from the `bot/rapper/template_bot` safety pattern and keeps
each candidate isolated by magic, comment prefix, basket state, cooldown, reverse
guard, and sync block.

## Safety Defaults

- `live_trading_enabled=false`
- `shadow_forward_enabled=true`
- EA HIST timestamps treated as UTC
- latest M1 bar dropped
- per-strategy `POSITIONS` / `ORDERS` clean sync
- recoverable sync block clear only after positions and orders are confirmed empty
- immediate state save after clean clear
- no same-strategy same-bar re-entry after a basket close, except the explicit
  one-time stop-reverse rule for `visual_break_reverse_a`

## Files

- Runner: `live_s23_bot.py`
- Params: `s23_params.json`
- State: `state/s23_bot_state.json`
- Log: `logs/s23_bot.log`
- Trades: `logs/s23_trades.csv`
- IPC default: `cmd_s23.txt` / `res_s23.txt`

## No-Order Checks

```powershell
py -m py_compile live_s23_bot.py live_safety.py live_data_fetcher.py live_executor.py ea_bridge.py
py live_s23_bot.py --self-test
```

Read-only bridge checks, when an EA is attached:

```text
CAPS
INFO|XAUUSD
HIST|XAUUSD|1|5
POSITIONS|XAUUSD|230001
ORDERS|XAUUSD|230001
```

Repeat `POSITIONS` / `ORDERS` for magics `230002`, `230003`, and `230004`.
