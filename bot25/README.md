# Bot25 man_231 XAUUSD bilateral book

Shadow-first port of the fixed `man_231` candidate. It continuously maintains
both BUY and SELL inventory, follows completed-M5 pivot breaks with an active
side, adds at a 0.50 ATR frontier, and releases profitable non-core satellites
while preserving the best-priced core ticket on each side.

- Magic/comment: `200025` / `s25_m231`
- Bridge/IPC: `BotBridge_s25`, `cmd_s25.txt`, `res_s25.txt`
- State/logs: `state/s25_bot_state.json`, `logs/s25_bot.log`, `logs/s25_trades.csv`
- Mode: `live_trading_enabled=true`, `shadow_forward_enabled=false`
- Real-order double gate: params must enable live and environment variable
  `BOT25_ENABLE_REAL_TRADING=MAN231_LIVE_ACK` must also be present.

The runner uses the bot23 operational baseline: exact magic/comment ownership,
configured-account identity and hedging-mode preflight, confirmed OPEN/CLOSE
reconciliation, foreign exposure rejection, and two-stage high-risk block
clearing. An OPEN reservation is saved before the broker request, so a restart
cannot blindly duplicate an ambiguous entry. Pending CLOSE requests are
reconciled and retried after a bounded wait instead of remaining stuck.

## Logs

- `s25_bot.log` rotates at 10 MiB with five backups. It contains health,
  preflight, recovery, status, and failure messages but never credentials or a
  webhook URL.
- `s25_trades.csv` has a strict header. An old/incompatible file is moved to
  `logs/old/` with a timestamp before a new canonical header is written. It
  records broker quote time, completed-M5
  signal/event/release/availability/decision/executable times, episode ID,
  magic, ownership identifiers, inventory counts, active wave, reason, and
  live/shadow mode.
- Exactly one `m5_decision` receipt is retained for every newly processed M5
  bar, including warmup, stale, no-add, blocked, entry, and close-request paths.
  Repeating diagnostics are summarized at five-minute intervals.
- `position_close_confirmed` uses the broker close deal price and aggregates
  profit, entry/exit commission, swap, and fee for the complete MT5 position
  ID. `profit` is their net account-currency result. Deal ID dedupe prevents a
  restart from recording the same realized result twice.

The schema is intentionally episode/inventory oriented rather than copying
bot23 lane fields. Bot25 has no four-lane allocator, ZA opportunity ID, or
`reverse_d60`; inventing those columns would imply behavior man_231 does not
have. `basket_id` is retained as a compatibility alias of `episode_id`. See
`LOG_SCHEMA.md` and `PORTING_AUDIT.md`.

```powershell
py -m py_compile *.py
py live_s25_bot.py --self-test
```

The source is configured for explicitly authorized real operation. The updated
`BotBridge_s25` reports bridge identity, account identity, quote epoch and
history epochs required by the runner. Both independent real-order gates are
enabled. A recreated service still fails closed until the updated EA is attached
and bridge/account/hedging/ownership preflight succeeds. See
`SOURCE_BACKTEST.md`.
