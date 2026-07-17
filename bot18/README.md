# bot18 / S18 basket replacement

`bot18` is the S18 v2 basket implementation that runs under the existing `exness-bot-18` service.
Keep both the service/container name and the CentOS runtime folder name unchanged: `exness-bot-18` / `bot18`.

## Current runtime settings

- service/container: `exness-bot-18`
- folder: `bot18`
- runner: `live_s18_bot.py`
- params: `s18_params.json`
- live trading: `true`
- shadow forward: `false`
- artifacts: `artifacts/`

## Symbols

- GBPUSD: CatBoost / allow_rate=0.50 / spread_add_points=2.0 / lot=0.02
- EURUSD: LightGBM / allow_rate=0.50 / spread_add_points=2.0 / lot=0.01
- AUDUSD: CatBoost / allow_rate=0.50 / spread_add_points=2.0 / lot=0.01

USDJPY, USDCHF, NZDUSD, CHFJPY, and USDCAD are not part of this fixed basket.

## Lot allocation

Entry threshold / allow_rate are unchanged. Only per-symbol lot is changed. As of 2026-07-17, lots are reduced while live/backtest reproducibility concerns are monitored.

- GBPUSD: `0.02`
- EURUSD: `0.01`
- AUDUSD: `0.01`

The top-level `lot` remains `0.01` as a fallback; live profiles use each profile's `lot`.

## Outputs

- bot log: `logs/s18_bot.log`
- trades: `logs/s18_trades.csv`
- policy decisions: `logs/s18_policy_decisions.csv`
- decision snapshots: `logs/s18_decision_snapshots.csv`
- state: `state/s18_<symbol>_bot_state.json`

`s18_policy_decisions.csv` is throttled to avoid high-volume repeated threshold-block rows. It logs policy passes, policy errors, reason/signature changes, and otherwise one repeated decision per symbol every `policy_decision_log_interval_seconds` seconds.

`s18_decision_snapshots.csv` is for backtest/live reproducibility checks. It logs one detailed row per closed-M1 cycle-start policy decision while flat, including the M1 decision time, live tick Bid/Ask, effective spread, selected H1/M1 features, model output, threshold, and whether the bot blocked, shadow-allowed, or started a cycle. Duplicate polls inside the same M1 decision bar are skipped. Actual market-entry drift from the virtual trigger is recorded in `s18_trades.csv` as `source_entry`, requested/filled entry, and `source_drift_pips`.

## Startup state recovery

On startup, `live_s18_bot.py` reconciles local state with MT5 before normal entry processing.

- It reads the current tick, bot-owned live positions, H1 regime history, and closed-M1 policy features before entering the main loop.
- Bot-owned exposure is identified by profile `symbol` + `magic` plus ticket/comment/SL evidence. This is safe for accounts where bot18 and other bots share the same MT5 account.
- If MT5 still has bot-owned live positions that are missing from local state, the existing sync/adoption path restores them into state before a new cycle can start.
- After reconciliation, startup catch-up reads up to `startup_catchup_max_m1_bars` closed M1 bars from the last processed/cycle-start decision bar and advances local virtual state without sending orders. If a virtual trigger was crossed while the bot was offline and MT5 has no matching bot-owned position, the bot does not create a historical fill. It keeps the current cycle and virtual trigger set, disarms the missed trigger, and waits for a fresh recross before sending a market order.
- If local state is blocked or too old to replay, bot18 confirms bot-owned `POSITIONS` and `ORDERS` are both flat before doing a flat reset. If bot-owned exposure exists or the read-only checks fail, it stays fail-closed with `reconciliation_required`.
- This is not a historical trade replay. If local state is lost and MT5 is flat, missed virtual market-order triggers cannot be reconstructed safely; the bot does not backfill trades. If state is still present, missed triggers are kept but marked unarmed until price returns to the non-trigger side and crosses again.
- Startup reconciliation status is saved in state as `startup_state_reconciled_at_jst` and `startup_state_recovery`.
  Startup catch-up status is saved as `startup_catchup_replayed_at_jst`, `startup_catchup_replay`, and `last_catchup_m1_decision_time_utc`.

## Market order rejection safety

S18 uses local virtual grid orders and sends market `OPEN` commands only after a virtual level is crossed.

- The local virtual order's `entry` / `stop_loss` values identify the crossed trigger and grid accounting level. They are not reused as the executable market request after a delay. On every market `OPEN` send or retry, the bot rebuilds the request from the current tick: LONG uses current Ask with SL one grid distance below it, and SHORT uses current Bid with SL one grid distance above it. This keeps the bot autonomous after Algo Trading rejection, timeout, or broker rejection without carrying an obsolete far-away SL into a later market fill.
- If an `OPEN` command returns a known definitive broker rejection such as invalid stops (`ERR|10016`), the bot immediately checks live positions by magic/symbol/direction/comment. If a clean position sync finds no matching position, it keeps the local virtual order, logs `ENTRY_FAIL_MARKET`, and waits `market_open_retry_cooldown_seconds` before retrying.
- If an `OPEN` command returns `ERR|10026` or `ERR|10027`, the bot treats it as a definitive no-fill trade-permission rejection, keeps the same local virtual order, and retries after `autotrading_reject_retry_cooldown_seconds`. It does not notify on the first rejection. It sends the manual-action alert only after `autotrading_reject_notify_after_count` consecutive trade-permission rejects.
- If bot-owned live positions are found outside state, the bot recovers them into state when their magic/symbol/comment/SL are sufficient to identify them. If the position sync fails, a position record cannot be parsed, untracked positions cannot be recovered, or multiple matching positions are found after an error response, the bot logs `ENTRY_UNRESOLVED_MARKET` and uses `reconciliation_required` instead of retrying.
- If an `OPEN` command response is ambiguous, such as timeout or no response, the bot checks live positions first. If no matching position can be confirmed, it logs `ENTRY_UNRESOLVED_MARKET` and blocks new entries with `reconciliation_required` so duplicate market orders are not sent blindly. A later clean position sync that proves the bot is flat and the original virtual order still exists clears the stale reconciliation block, keeps the virtual order, and retries after `market_open_retry_cooldown_seconds`.
- If a matching live position is found after an error response, the bot adopts that position into state using the entry direction/comment/source order and logs it as a reconciled `ENTRY`.
- Shared-account isolation is intentional. Position sync is scoped by profile `symbol` + `magic`, and new state positions store `symbol` and `magic`. Before the bot treats a missing state ticket as server-side SL, or sends ticket-only `CLOSE` / `MODIFY`, it verifies that the live ticket belongs to the same bot by symbol, magic, and comment evidence. A same-symbol ticket from another bot remains in state and blocks new entries instead of being closed, modified, adopted, or assumed stopped out.
- If a server-side or local SL leaves the symbol flat, the bot keeps the current cycle and remaining virtual grid. It does not start a fresh cycle simply because the symbol is flat. A same-level order recreated while price is already on the trigger side is stored as `armed=false`; it can fill only after price first returns to the non-trigger side and then crosses again. Untouched higher/lower grid levels can still fill as normal trend continuation triggers.
- If the account is flat and an old breakout trigger is crossed after a large drift, the bot does not chase the stale level. When drift exceeds `max_flat_breakout_entry_drift_pips`, it suppresses that fill by disarming the order until a fresh recross instead of clearing the cycle and reanchoring.
- Temporary new-entry blocks from recoverable states, such as transient position-sync failure, SL close failure, autoTP close failure, SL repair failure, untracked-position recovery, or max-count recovery, are cleared only after a later clean sync proves MT5 live positions and bot state are aligned. Unresolved `reconciliation_required` blocks stay fail-closed.

## Bridge

- Bridge source: `BotBridge_s18.mq5`
- MT5 Expert name: `BotBridge_s18`
- Python command file: `cmd_s18.txt`
- Python response file: `res_s18.txt`
- Python heartbeat file: `heartbeat_s18.txt`
- Python lock file: `ea_bridge_s18.lock`
- MT5 EA input `InpCommandFile`: `cmd_s18.txt`
- MT5 EA input `InpResponseFile`: `res_s18.txt`

`live_s18_bot.py` verifies `CAPS` on startup and stops if the attached bridge is not `BotBridge_s18`.

Read-only bridge/state check:

```powershell
python .\live_s18_bot.py --bridge-state-check
```

This command only sends `ECHO`, `CAPS`, `INFO`, `POSITIONS`, `ORDERS`, and `HIST`. It does not send `OPEN`, `MODIFY`, `CLOSE`, `PENDING`, or `CANCEL`. If `cmd_s18.txt` updates but `res_s18.txt` is never created, Python is writing to the selected MT5 Files folder but the s18 EA is not processing that IPC lane. Check that `BotBridge_s18` is attached/compiled in the same MT5 terminal that owns the selected `MQL5\Files` directory.

## Start / recreate

After code is reflected on CentOS, recreate the existing service name:

```bash
sudo docker compose up -d --no-deps --force-recreate exness-bot-18
```

`docker-compose.yml` mounts `bot18`, not `bot18_v2`.
It also mounts `BotBridge_s18.mq5` into the MT5 `MQL5/Experts/BotBridge_s18.mq5` path.

Do not overwrite the existing CentOS `bot18/live_config.py` unless the user explicitly authorizes live-config changes.

## Verification

```powershell
python .\live_s18_bot.py --self-test
python .\live_s18_bot.py --self-test --policy-self-test
python .\live_s18_bot.py --bridge-state-check
```
