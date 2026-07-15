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

- GBPUSD: CatBoost / allow_rate=0.50 / spread_add_points=2.0 / lot=0.09
- EURUSD: LightGBM / allow_rate=0.50 / spread_add_points=2.0 / lot=0.07
- AUDUSD: CatBoost / allow_rate=0.50 / spread_add_points=2.0 / lot=0.07

USDJPY, USDCHF, NZDUSD, CHFJPY, and USDCAD are not part of this fixed basket.

## Lot allocation

Entry threshold / allow_rate are unchanged. Only per-symbol lot is changed based on fixed basket portfolio diagnostics.

- GBPUSD: `0.09`
- EURUSD: `0.07`
- AUDUSD: `0.07`

The top-level `lot` remains `0.01` as a fallback; live profiles use each profile's `lot`.

## Outputs

- bot log: `logs/s18_bot.log`
- trades: `logs/s18_trades.csv`
- policy decisions: `logs/s18_policy_decisions.csv`
- state: `state/s18_<symbol>_bot_state.json`

`s18_policy_decisions.csv` is throttled to avoid high-volume repeated threshold-block rows. It logs policy passes, policy errors, reason/signature changes, and otherwise one repeated decision per symbol every `policy_decision_log_interval_seconds` seconds.

## Market order rejection safety

S18 uses local virtual grid orders and sends market `OPEN` commands only after a virtual level is crossed.

- If an `OPEN` command returns a known definitive broker rejection such as invalid stops (`ERR|10016`), the bot immediately checks live positions by magic/symbol/direction/comment. If a clean position sync finds no matching position, it keeps the local virtual order, logs `ENTRY_FAIL_MARKET`, and waits `market_open_retry_cooldown_seconds` before retrying.
- If an `OPEN` command returns `ERR|10026` or `ERR|10027`, the bot treats it as a definitive no-fill trade-permission rejection, keeps the same local virtual order, and retries after `autotrading_reject_retry_cooldown_seconds`. It does not notify on the first rejection. It sends the manual-action alert only after `autotrading_reject_notify_after_count` consecutive trade-permission rejects.
- If bot-owned live positions are found outside state, the bot recovers them into state when their magic/symbol/comment/SL are sufficient to identify them. If the position sync fails, a position record cannot be parsed, untracked positions cannot be recovered, or multiple matching positions are found after an error response, the bot logs `ENTRY_UNRESOLVED_MARKET` and uses `reconciliation_required` instead of retrying.
- If an `OPEN` command response is ambiguous, such as timeout or no response, the bot checks live positions first. If no matching position can be confirmed, it logs `ENTRY_UNRESOLVED_MARKET` and blocks new entries with `reconciliation_required` so duplicate market orders are not sent blindly.
- If a matching live position is found after an error response, the bot adopts that position into state using the entry direction/comment/source order and logs it as a reconciled `ENTRY`.

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
```
