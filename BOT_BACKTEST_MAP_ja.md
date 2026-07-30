# Bot / Backtest Map

Last updated: 2026-07-26

This file is the central entry point for bot-to-backtest mapping. Check this before relying on scattered README, HANDOFF, or SOURCE_BACKTEST notes.

## Runtime Status

Runtime status is intentionally not authoritative in this file. Verify current compose configuration, containers/processes, logs, and the user's latest instruction before any live-affecting work. Status notes in the table are historical context only.

## Mapping

| bot | service/container | local folder | source backtest | source detail | status / note |
| --- | --- | --- | --- | --- | --- |
| bot11 / S11 | `exness-bot-11` | `bot11` | not consolidated | `s11_params.json` indicates USTECm -> US500m lead-lag mean reversion. | Historical 2026-07-08 note said running. Create `SOURCE_BACKTEST.md` before changing strategy behavior. |
| bot18 / S18 | `exness-bot-18` | `bot18` | `backtest33_bot18\live_bot_backtest` | Fixed live/backtest entry. Current source is `candidates\event_filter_template\live_bot18_v2_staging`; old or non-current material is under `backtest33_bot18\legacy\backtest24_original`, `legacy\backtest32_cross_asset_dev`, and `legacy\backtest34_bot18`. | Historical 2026-07-08 note said running. Compose should mount `bot18` and attach `BotBridge_s18`; verify the real runtime before any live-affecting action. |
| bot18_v2GBPUSDm / S18 derivative | not assigned | `bot18_v2GBPUSDm` | `backtest33_bot18\legacy\backtest32_cross_asset_dev` plus the frozen `bot18_v2` GBPUSD CatBoost artifact | Physical `GBPUSDm` uses the frozen GBPUSD policy candidate through an explicit `policy_symbol=GBPUSD` alias. See `SOURCE_BACKTEST.md`. | Shadow-only folder; no compose service assigned. GBPUSDm has only M1 close dev diagnostics, not a completed CatBoost full-policy or tick validation. |
| bot19 / S19 | `exness-bot-19` | `bot19` | historical D10 source now at `backtest33_bot18\legacy\backtest34_bot18` | `SOURCE_BACKTEST.md`: run `20260705_2200_pilot_dev_tick_d10_e75_fs_m80_b2k`, candidate `baseline_recovery_loweff_cap10_short_deep8_extreme6_e75_fs_m80_b2k`, variant `value_d10_tp1`. `backtest34_bot19` now holds later S19 GBPUSDm diagnostics only. | Historical 2026-07-08 note said running. Uses server-side pending stop. |
| bot20 / S20 | `exness-bot-20` | `bot20` | `backtest43` | `SOURCE_BACKTEST.md` exists. | Historical 2026-07-08 note said not running; verify current state. |
| bot21 / S21 | `exness-bot-21` | `bot21` | `backtest67_1_bot21` | `SOURCE_BACKTEST.md`: Ehlers top3 current implementation for `US500_137_1h`, `AUDUSD_021_1h`, `USDJPY_035_1h`. | Shadow-first folder with compose service defined. No deployment, bridge attachment, restart, or live switch authorized yet. |
| bot22 / S22 | `exness-bot-22` | `bot22` | `backtest108_1` | `SOURCE_BACKTEST.md`: Bollinger squeeze-breakout pullback `man_024_v002 / EURUSD_005_1h`, params hash `f97149f97d028e98`. | Live-order params enabled by explicit user instruction on 2026-07-26. `clean_reusable_eval=false`; forward/live operation only. Service deployment, bridge attachment, or restart are separate runtime actions. |
| bot23 / S23 | not assigned | `bot23` | `chisiki_x_bot_ideas_20260706` | `SOURCE_BACKTEST.md`: Chisiki/ReactVol fixed4 XAUUSD M1 candidates: `visual_loss_abort_g`, `visual_no_adverse_c`, `h14_18_h120_tp12_dd40_vol105_impulseonly_all_all`, `visual_break_reverse_a`. | Shadow-first folder created 2026-07-30. No compose service, deploy, restart, bridge attachment, settings change, or live switch included. |

## Rules

- Update this table before or during any bot mapping change.
- If the target bot lacks `SOURCE_BACKTEST.md`, create it before touching strategy behavior.
- Do not infer a live target from a backtest folder name. Cross-check README, HANDOFF, SOURCE_BACKTEST, docker-compose, and the user's latest instruction.
- For each live bot, prefer one fixed parent folder named `backtestNN_botXX`; put current live-aligned source under `live_bot_backtest/` and non-current material under the parent `legacy/`.
- `live_config.py` is local sensitive config. It may be edited only when the user explicitly authorizes live config changes, and its contents must not be printed, staged, committed, pushed, or uploaded.
- Do not edit login/account fields unless the user explicitly asks for those exact fields: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, account IDs, and login/bootstrap initialization.
