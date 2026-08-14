# Template BotNN

This folder is meant to be copied to `C:\botter\bot\botNN` and edited lightly.
It is not a live strategy until the placeholders and signal adapter are fixed.

## Replace First

- Folder name: `template_bot` -> `botNN`
- Runner: `live_sNN_bot.py` -> `live_sNN_bot.py` with real number, for example `live_s23_bot.py`
- Params: `sNN_params.json` -> `sNN_params.json`, for example `s23_params.json`
- `BOT_SUFFIX`, magic, strategy ID, comment prefixes, symbols, lot, point/pip, SL/TP.
- EA files and Docker/README references: `BotBridge_sNN`, `cmd_sNN.txt`, `res_sNN.txt`, `ea_bridge_sNN.lock`.

Keep `live_trading_enabled=false` until live trading is explicitly authorized.

## Timeframe Options

Set `timeframe_profile` in params:

- `signal_timeframe`: `M1`, `M5`, `M15`, or `H1`.
- `execution_timeframe`: usually `M1`; kept explicit for audit.
- `history_source`: `DIRECT_HIST` or `M1_RESAMPLE`.
- `hist_timeframe_name`: MT5 HIST source timeframe.
- `drop_latest_signal_bar`: normally `true`.
- `max_signal_delay_minutes`: stale guard window after `entry_due`.

For derived M5/M15/H1 signals, prefer `M1_RESAMPLE` when backtest did the same.
For direct broker bars, verify `CAPS` / `INFO` / `HIST` read-only first and
record the timestamp basis in `SOURCE_BACKTEST.md`.

## Safety Defaults

`live_safety.py` is wired for:

- UTC HIST normalization.
- UTC `entry_due` stale guard.
- preflight and periodic clean sync.
- target symbol/magic `POSITIONS` and `ORDERS` checks.
- exact bridge identity and required command-capability checks.
- live mode hedging-account requirement for shared-account ticket ownership.
- OPEN confirmation by owned ticket/position identifier before state activation.
- CLOSEDEAL confirmation before locally finalizing a broker-side close.
- recoverable sync block clearing only after both queries succeed and are empty;
  high-risk OPEN/CLOSE uncertainty additionally requires exact ticket absence and
  two consecutive clean flat confirmations.
- immediate state save after clean clear.
- close-after-entry guard: after a symbol closes, same-symbol same-direction
  signals whose bar time was already known at or before the close are consumed
  and skipped until a fresh cycle re-syncs price, positions, orders, and signal.
- optional `[sl ...]` broker residual clear.

Use `True`, `False`, or `None` in `sNN_params.json` for each safety switch.
`None` means not applicable; it should not silently clear or block state.

## Signal Adapter

Edit `signal_adapters.py` or set one of the starter adapters:

- `NONE`: no entries, safe default.
- `EHLERS_CROSS`: bot21-style starter.
- `BOLLINGER_SQUEEZE_PULLBACK`: bot22-style starter.

The return contract is:

```python
{"side": "long" or "short", "bar_time": str(bars.index[-1]), ...}
```

## Required Checks

Before any push/deploy/recreate:

```powershell
py -m py_compile live_sNN_bot.py live_safety.py timeframe_config.py signal_adapters.py live_data_fetcher.py live_executor.py ea_bridge.py
py live_sNN_bot.py --self-test
```

Use the bundled `BotBridge_sNN.mq5` as the bridge source when creating a new
bot. It includes the `ACCOUNT`, `POSITION`, and `CLOSEDEAL` commands required by
the Python safety layer. Do not derive a new bot from an older bridge merely
because its market `OPEN`/`CLOSE` commands are present.

Against a running EA, use read-only IPC only first:

```text
CAPS
INFO|SYMBOL
HIST|SYMBOL|TIMEFRAME|5
POSITIONS|SYMBOL|MAGIC
ORDERS|SYMBOL|MAGIC
```

Do not run `OPEN`, `CLOSE`, `MODIFY`, or `CANCEL` during template validation.
