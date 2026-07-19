# Bot / Backtest Map

Last updated: 2026-07-18

This file is the central entry point for bot-to-backtest mapping. Check this before relying on scattered README, HANDOFF, or SOURCE_BACKTEST notes.

## Runtime Status

Runtime status is intentionally not authoritative in this file. Verify current compose configuration, containers/processes, logs, and the user's latest instruction before any live-affecting work. Status notes in the table are historical context only.

## Mapping

| bot | service/container | local folder | source backtest | source detail | status / note |
| --- | --- | --- | --- | --- | --- |
| bot11 / S11 | `exness-bot-11` | `bot11` | not consolidated | `s11_params.json` indicates USTECm -> US500m lead-lag mean reversion. | Historical 2026-07-08 note said running. Create `SOURCE_BACKTEST.md` before changing strategy behavior. |
| bot18 / S18 | `exness-bot-18` | `bot18` | `backtest33` / legacy `backtest24`, `backtest34` | `bot18` README describes a 3-symbol ML event filter basket. `backtest33_lightGBM_template_audit_bot18v2` is the main known source. Legacy related folders include `backtest24_bot18` and `backtest34_bot18_small_range_filter`. | Historical 2026-07-08 note said running. Compose should mount `bot18` and attach `BotBridge_s18`; verify the real runtime before any live-affecting action. |
| bot18_v2GBPUSDm / S18 derivative | not assigned | `bot18_v2GBPUSDm` | `backtest32_bot18_v2_cross_asset_dev` plus the frozen `bot18_v2` GBPUSD CatBoost artifact | Physical `GBPUSDm` uses the frozen GBPUSD policy candidate through an explicit `policy_symbol=GBPUSD` alias. See `SOURCE_BACKTEST.md`. | Shadow-only folder; no compose service assigned. GBPUSDm has only M1 close dev diagnostics, not a completed CatBoost full-policy or tick validation. |
| bot19 / S19 | `exness-bot-19` | `bot19` | `backtest34` | `SOURCE_BACKTEST.md`: `backtest34_bot19`, run `20260705_2200_pilot_dev_tick_d10_e75_fs_m80_b2k`, candidate `baseline_recovery_loweff_cap10_short_deep8_extreme6_e75_fs_m80_b2k`, variant `value_d10_tp1`. | Historical 2026-07-08 note said running. Uses server-side pending stop. |
| bot20 / S20 | `exness-bot-20` | `bot20` | `backtest43` | `SOURCE_BACKTEST.md` exists. | Historical 2026-07-08 note said not running; verify current state. |

## Rules

- Update this table before or during any bot mapping change.
- If the target bot lacks `SOURCE_BACKTEST.md`, create it before touching strategy behavior.
- Do not infer a live target from a backtest folder name. Cross-check README, HANDOFF, SOURCE_BACKTEST, docker-compose, and the user's latest instruction.
- `live_config.py` is local sensitive config. It may be edited only when the user explicitly authorizes live config changes, and its contents must not be printed, staged, committed, pushed, or uploaded.
- Do not edit login/account fields unless the user explicitly asks for those exact fields: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, account IDs, and login/bootstrap initialization.
