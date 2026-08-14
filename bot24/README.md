# Bot24 Visual No-Adverse C

Shadow-first live port of frozen XAUUSD M1 candidate
`visual_no_adverse_c:target16`.

- Magic/comment: `200024` / `s24_no_adverse`
- Bridge/IPC: `BotBridge_s24`, `cmd_s24.txt`, `res_s24.txt`
- State/logs: `state/s24_bot_state.json`, `logs/s24_bot.log`, `logs/s24_trades.csv`
- Mode: `live_trading_enabled=true`, `shadow_forward_enabled=false`

The runner uses the canonical shared-account safety modules: exact ownership,
hedging-mode preflight, confirmed OPEN/CLOSE reconciliation, foreign exposure
rejection, and two-stage high-risk block clearing.

```powershell
py -m py_compile *.py
py live_s24_bot.py --self-test
```

The Compose service and live mode are defined. Compose automatically prepares
the bridge and starts a retrying runner; chart attachment remains manual. See
`../BOT20_25_CENTOS_STARTUP.md` for host-only login files.
