# Bot / Backtest Map

Last updated: 2026-08-25

This file is the central entry point for bot-to-backtest mapping. Check this before relying on scattered README, HANDOFF, or SOURCE_BACKTEST notes.

Common bot-managed close/session/DST behavior is defined by `LIVE_BOT_CLOSE_BASELINE_ja.md`. bot26-29 use its USTEC 260-point spread guard; bot28-29 inherit the bot27 base runner through compose mounts.

## Runtime Status

Runtime status is intentionally not authoritative in this file. Verify current compose configuration, containers/processes, logs, and the user's latest instruction before any live-affecting work. Status notes in the table are historical context only.

## Mapping

| bot | service/container | local folder | source backtest | source detail | status / note |
| --- | --- | --- | --- | --- | --- |
| bot11 / S11 | `exness-bot-11` | `bot11` | not consolidated | `s11_params.json` indicates USTECm -> US500m lead-lag mean reversion. | Historical 2026-07-08 note said running. Create `SOURCE_BACKTEST.md` before changing strategy behavior. |
| bot18 / S18 | `exness-bot-18` | `bot18` | `backtest33_bot18\live_bot_backtest` | Fixed live/backtest entry. Current source is `candidates\event_filter_template\live_bot18_v2_staging`; old or non-current material is under `backtest33_bot18\legacy\backtest24_original`, `legacy\backtest32_cross_asset_dev`, and `legacy\backtest34_bot18`. | Historical runtime mapping only. 2026-07-20 true-tick recheck rejected current basket: earlier positive M1 close / tick-derived M1 reports are not clean live tick profit proof. Verify runtime before any live-affecting action. |
| bot18_v2GBPUSDm / S18 derivative | not assigned | `bot18_v2GBPUSDm` | `backtest33_bot18\legacy\backtest32_cross_asset_dev` plus the frozen `bot18_v2` GBPUSD CatBoost artifact | Physical `GBPUSDm` uses the frozen GBPUSD policy candidate through an explicit `policy_symbol=GBPUSD` alias. See `SOURCE_BACKTEST.md`. | Shadow-only folder; no compose service assigned. GBPUSDm has only M1 close dev diagnostics, not a completed CatBoost full-policy or tick validation. |
| bot19 / S19 | `exness-bot-19` | `bot19` | `backtest110/candidates/s19-snowball-v2-live-proxy-dev` | `bot19_original_snowball_repaired_v2`: fixed-anchor A-v2 Snowball with one next same-direction add order; inherited multi-position cap 40. | Compose mapping is A-v2 with `live=false` / `shadow=true`; not deployed or started. Dev base was positive but cost stress negative, so status remains rework required. Old D10 files are retained as the base module and legacy evidence. |
| bot20 / S20 | `exness-bot-20` | `bot20` | `bot関連backtest/backtest43_bot20` | `SOURCE_BACKTEST.md`: `large_candle_short_basket_m1` / `confirm_refine_top_45_04`. | Live-order params enabled by explicit user instruction on 2026-07-27. Leak review found no clear future-reference leak, but selection is `holdout_seen / forward_only`: reusable eval was positive with only 4 baskets / 24 positions and is not a formal holdout clear. Service deployment, bridge attachment, restart, and noVNC account state are separate runtime actions. |
| bot21 / S21 | `exness-bot-21` | `bot21` | `bot関連backtest/backtest67_1_bot21` | `SOURCE_BACKTEST.md`: Ehlers top3 lineage。現在有効なのは`AUDUSD_021_1h`のみ。time closeはUTC/米国DST自動判定の週次閉場30分前close（dev +320.9、PF 1.470、MDD 79.4、101取引）。 | Live-order params enabled。通常週次calendarのみ自動、祝日短縮は別途根拠が必要。`CLOSEDEAL` bridge、deploy/restart/service recreationは別runtime操作。 |
| bot22 / S22 | `exness-bot-22` | `bot22` | `bot関連backtest/backtest108_1_bot22` | `SOURCE_BACKTEST.md`: Bollinger squeeze-breakout pullback `man_024_v002 / EURUSD_005_1h`, params hash `f97149f97d028e98`. | Live-order params enabled by explicit user instruction on 2026-07-26. `clean_reusable_eval=false`; forward/live operation only. Service deployment, bridge attachment, or restart are separate runtime actions. |
| bot23 / S23 | `exness-bot-23` | `bot23` | `bot関連backtest/backtest152` | `SOURCE_BACKTEST.md`: forward-only XAUUSD true-tick candidate `ZA_atr20_regime_switch`, final evidence `20260823_bot23_weekly_diagnosis_dev_cycle_v1/rebased_v8`. | ATR30<2.0でz2→1σ pullback、spread/ATR<=0.10、ATR出口3.5/6.5/1.0。close/pending fillはcurrent Bid/Askを5秒poll。Deploy/restart未実施。 |
| bot24 / S24 | `exness-bot-24` | `bot24` | `bot関連backtest/backtest152/archive/bot23_fixed3_opt2_20260814` | `SOURCE_BACKTEST.md`: frozen XAUUSD M1 candidate `visual_no_adverse_c:target16` (`man_028_v002`, hash `54324f...`). | Historical source was consolidated under backtest152; live configuration unchanged. |
| bot25 / S25 | `exness-bot-25` | `bot25` | `bot関連backtest/backtest152/archive/bot23_fixed3_opt2_20260814` | `SOURCE_BACKTEST.md`: frozen XAUUSD M1 candidate `h14_18_h120_tp12_dd40_vol105_impulseonly_all_all:target16` (`man_028_v002`, hash `54324f...`). | Historical source was consolidated under backtest152; live configuration unchanged. |
| bot26 / S26 | `exness-bot-26` | `bot26` | `bot関連backtest/0_bot26実装PV2C520_OOS_001` | `SOURCE_BACKTEST.md`: 注文対象USTEC、参照専用USOIL、XAUUSD非対象。`PV2C520_DEVQ80_H75_FORWARD_R1`、actual fill基準75分hold。USTEC最小/configured lot `0.05`。 | User-authorized live (`live=true`, `shadow=false`)。Observed leakcheck反転悪化により2026-08-24に改善前へ復元。Deploy/restart/real order未実施。 |
| bot27 / S27 | `exness-bot-27` | `bot27` | `backtest/bot関連backtest/0_bot27実装PV2C859_OOS_001` | `SOURCE_BACKTEST.md`: 注文対象USTEC、XAUUSD非対象。`PV2C859_DEVQ825_H90_FORWARD_R1`、actual fill基準90分hold。USTEC最小/configured lot `0.05`。 | User-authorized live (`live=true`, `shadow=false`)。Observed leakcheck反転悪化により2026-08-24に改善前へ復元。Deploy/restart/real order未実施。 |
| bot28 / S28 | `exness-bot-28` | `bot28` | `bot関連backtest/0_bot28実装PV2C560_OOS_001` | `SOURCE_BACKTEST.md`: 注文対象USTEC、XAUUSD非対象。`PV2C560_DEVQ80_H75_FORWARD_R1`、decision基準75分hold。USTEC最小/configured lot `0.05`。 | User-authorized live (`live=true`, `shadow=false`)。DEV固定選択、既観測診断は昇格根拠外。Deploy/restart/real order未検証。 |
| bot29 / S29 | `exness-bot-29` | `bot29` | `bot関連backtest/0_bot29実装PV2C531_OOS_001` | `SOURCE_BACKTEST.md`: 注文対象USTEC、XAUUSD非対象。`PV2C531_CORRECTED_DIAGNOSTIC_R1`、decision基準60分hold。USTEC最小/configured lot `0.05`。 | User-authorized live (`live=true`, `shadow=false`)。Deploy/restart/real order未検証。 |

## Rules

- Update this table before or during any bot mapping change.
- If the target bot lacks `SOURCE_BACKTEST.md`, create it before touching strategy behavior.
- Do not infer a live target from a backtest folder name. Cross-check README, HANDOFF, SOURCE_BACKTEST, docker-compose, and the user's latest instruction.
- For each live bot, prefer one fixed parent folder named `backtestNN_botXX`; put current live-aligned source under `live_bot_backtest/` and non-current material under the parent `legacy/`.
- `live_config.py` is local sensitive config. It may be edited only when the user explicitly authorizes live config changes, and its contents must not be printed, staged, committed, pushed, or uploaded.
- Do not edit login/account fields unless the user explicitly asks for those exact fields: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, account IDs, and login/bootstrap initialization.
