# Bot / Backtest Map

Last updated: 2026-07-27

This file is the central entry point for bot-to-backtest mapping. Check this before relying on scattered README, HANDOFF, or SOURCE_BACKTEST notes.

## Runtime Status

Runtime status is intentionally not authoritative in this file. Verify current compose configuration, containers/processes, logs, and the user's latest instruction before any live-affecting work. Status notes in the table are historical context only.

## Mapping

| bot | service/container | local folder | source backtest | source detail | status / note |
| --- | --- | --- | --- | --- | --- |
| bot11 / S11 | `exness-bot-11` | `bot11` | not consolidated | `s11_params.json` indicates USTECm -> US500m lead-lag mean reversion. | Historical 2026-07-08 note said running. Create `SOURCE_BACKTEST.md` before changing strategy behavior. |
| bot18 / S18 | `exness-bot-18` | `bot18` | `backtest33_bot18\live_bot_backtest` | Fixed live/backtest entry. Current source is `candidates\event_filter_template\live_bot18_v2_staging`; old or non-current material is under `backtest33_bot18\legacy\backtest24_original`, `legacy\backtest32_cross_asset_dev`, and `legacy\backtest34_bot18`. | Historical runtime mapping only. 2026-07-20 true-tick recheck rejected current basket: earlier positive M1 close / tick-derived M1 reports are not clean live tick profit proof. Verify runtime before any live-affecting action. |
| bot18_v2GBPUSDm / S18 derivative | not assigned | `bot18_v2GBPUSDm` | `backtest33_bot18\legacy\backtest32_cross_asset_dev` plus the frozen `bot18_v2` GBPUSD CatBoost artifact | Physical `GBPUSDm` uses the frozen GBPUSD policy candidate through an explicit `policy_symbol=GBPUSD` alias. See `SOURCE_BACKTEST.md`. | Shadow-only folder; no compose service assigned. GBPUSDm has only M1 close dev diagnostics, not a completed CatBoost full-policy or tick validation. |
| bot19 / S19 | `exness-bot-19` | `bot19` | `backtest110/candidates/s19-snowball-v2-live-proxy-dev` | `bot19_original_snowball_repaired_v2`: fixed-anchor A-v2 Snowball with one next same-direction add order; inherited multi-position cap 40. | Compose mapping is A-v2 with `live=false` / `shadow=true`; not deployed or started. Dev base was positive but cost stress negative, so status remains rework required. Old D10 files are retained as the base module and legacy evidence. |
| bot20 / S20 | `exness-bot-20` | `bot20` | `backtest43` | `SOURCE_BACKTEST.md`: `large_candle_short_basket_m1` / `confirm_refine_top_45_04`. | Live-order params enabled by explicit user instruction on 2026-07-27. Leak review found no clear future-reference leak, but selection is `holdout_seen / forward_only`: reusable eval was positive with only 4 baskets / 24 positions and is not a formal holdout clear. Service deployment, bridge attachment, restart, and noVNC account state are separate runtime actions. |
| bot21 / S21 | `exness-bot-21` | `bot21` | `backtest67_1_bot21` | `SOURCE_BACKTEST.md`: Ehlers top3 current implementation for `US500_137_1h`, `AUDUSD_021_1h`, `USDJPY_035_1h`. | Live-order params enabled. Current runner requires the updated `CLOSEDEAL` bridge; deployment, bridge compilation/attachment, service recreation, and noVNC account state remain separate runtime actions. |
| bot22 / S22 | `exness-bot-22` | `bot22` | `backtest108_1_bot22` | `SOURCE_BACKTEST.md`: Bollinger squeeze-breakout pullback `man_024_v002 / EURUSD_005_1h`, params hash `f97149f97d028e98`. | Live-order params enabled by explicit user instruction on 2026-07-26. `clean_reusable_eval=false`; forward/live operation only. Service deployment, bridge attachment, or restart are separate runtime actions. |
| bot23 / S23 | `exness-bot-23` | `bot23` | `backtest152` | `SOURCE_BACKTEST.md`: forward-only XAUUSD true-tick candidate `M_block14_loss27` (`man_028_v004_v001`), based on `B_guard_30__atr65_cap2`. | Live-enabled test-account candidate; close branches poll current Bid/Ask every 5 seconds, entry/add remain confirmed-M1. Deploy/restart not performed by the port. |
| bot24 / S24 | `exness-bot-24` | `bot24` | `bot23_fixed3_opt2_20260814` | `SOURCE_BACKTEST.md`: frozen XAUUSD M1 candidate `visual_no_adverse_c:target16` (`man_028_v002`, hash `54324f...`). | Live-enabled; per-bot host-only `startup.ini` is mounted to `/app/startup.ini`. Deploy/restart/EA attachment/runner start not performed. |
| bot25 / S25 | `exness-bot-25` | `bot25` | `bot23_fixed3_opt2_20260814` | `SOURCE_BACKTEST.md`: frozen XAUUSD M1 candidate `h14_18_h120_tp12_dd40_vol105_impulseonly_all_all:target16` (`man_028_v002`, hash `54324f...`). | Live-enabled; per-bot host-only `startup.ini` is mounted to `/app/startup.ini`. Deploy/restart/EA attachment/runner start not performed. |

## Rules

- Update this table before or during any bot mapping change.
- If the target bot lacks `SOURCE_BACKTEST.md`, create it before touching strategy behavior.
- Do not infer a live target from a backtest folder name. Cross-check README, HANDOFF, SOURCE_BACKTEST, docker-compose, and the user's latest instruction.
- For each live bot, prefer one fixed parent folder named `backtestNN_botXX`; put current live-aligned source under `live_bot_backtest/` and non-current material under the parent `legacy/`.
- `live_config.py` is local sensitive config. It may be edited only when the user explicitly authorizes live config changes, and its contents must not be printed, staged, committed, pushed, or uploaded.
- Do not edit login/account fields unless the user explicitly asks for those exact fields: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, account IDs, and login/bootstrap initialization.
