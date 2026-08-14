# Bot25 ReactVol H14-18

Shadow-first live port of frozen XAUUSD M1 candidate
`h14_18_h120_tp12_dd40_vol105_impulseonly_all_all:target16`.

- Magic/comment: `200025` / `s25_reactvol`
- Bridge/IPC: `BotBridge_s25`, `cmd_s25.txt`, `res_s25.txt`
- State/logs: `state/s25_bot_state.json`, `logs/s25_bot.log`, `logs/s25_trades.csv`
- Mode: `live_trading_enabled=true`, `shadow_forward_enabled=false`

The runner uses the canonical shared-account safety modules: exact ownership,
hedging-mode preflight, confirmed OPEN/CLOSE reconciliation, foreign exposure
rejection, and two-stage high-risk block clearing.

```powershell
py -m py_compile *.py
py live_s25_bot.py --self-test
```

The Compose service and live mode are defined. Compose automatically prepares
the bridge and starts a retrying runner; chart attachment remains manual. See
`../BOT20_25_CENTOS_STARTUP.md` for host-only login files.
