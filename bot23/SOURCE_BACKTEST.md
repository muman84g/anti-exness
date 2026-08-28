# Source Backtest

## Frozen mapping

- Bot: `bot23` / S23 / XAUUSD
- Base family / persisted state strategy ID: `bot23_za_horizontal_inventory_v001`
- Parent idea: `bot23_late_short_30m_action_matrix_v001`
- Adopted idea: `bot23_x_archive_inventory_range_false_break_fade_opt_v001`
- Candidate: `bot23-x-archive-plus-jst0911-plus-jst1113-round-s2p5-d0p05-r0p03-v001`
- Parent candidate: `bot23-long-target-portfolio-rearm-v001`
- Selected spec: `reverse_d60 + long_target_portfolio_rearm_8m + balanced_book_false_break_fade_w15_c2_both + jst0911_stable001_param_15_55_45_v001 + jst1113_round_s2p5_d0p05_r0p03_h60_cap1_v001`
- Morning overlay params hash:
  `c36023031af830bca0c08dd441ff800868909d404813e0a89c51e4fc1f3b086e`
- Midday overlay params hash:
  `526d90e6dc16981ba5e60d31750f1b4862fbe3d9170382ed624fea53ef55fd83`
- Entry-policy params hash:
  `40475d07b84eabc1b1290bee6787113903f374ca90cf2ca271c82b825b313572`
- Routing: `first_consuming_lane_preserve_primary_v1`
- Portfolio-rearm params hash:
  `0f8f3fc3e32c74ce00344b01fbc335d9ac6cfbf4801357e768d87c851229afb4`
- Inventory-range-fade params hash:
  `d02b82730f7f686d97317f96aab26762168c8396f40c21f4787ab8bd4296bab0`
- Research status: `forward_candidate / observed_leakcheck_effect_survives`
- Knowledge root:
  `C:/botter/backtest/検討中/chatgpt案/多重ポジ/利益確保案/bot23_多重ポジ化`
- Manifest: `EVIDENCE_MANIFEST_20260825.json`

## Frozen implementation

- Producer:
  `backtest/idea_registry/recipe_registry/bot23_za_opportunity_producer_v1.py`
  SHA-256 `06809515cb54f903de2dc5620f142b21a56ed50d2046e4aa474a43b3d621329e`
- Compatibility engine:
  `backtest/idea_registry/recipe_registry/bot23_za_compatibility_engine_v1.py`
  SHA-256 `ba4e83eb132e14eadc368038843906c15ed4b15300330e2b641bc2c67ef143f6`
- TradeOps runner:
  `scripts/agent/run_bot23_za_horizontal_tradeops_v1.py`
  SHA-256 `15eb42ef4dd24365c390b648ed396495e6bf231cbed45fc60da190cd2faef9a8`
- Recipe identity:
  `7c799295e591db8557afda41db7db33707a0a510061a4f40ddf378d5f0777c99`
- Params identity:
  `652b224f5dea5da6f514b9ef3a3ba839ffabb937e54b8bb676801853f7b7a996`

The recipe and params identities above are the unchanged four-lane foundation.
The active entry-policy identity is the separate `reverse_d60` hash recorded in
the frozen mapping.

## Strategy contract

The producer derives the original ZA impulse signal from completed M1 data
without reading inventory. The signal uses ret10, ATR30, tick-volume/30 mean,
impulse threshold 0.55 ATR, volume ratio 1.05, confirmed-bar spread at most 300
points, and session 13:00-18:00 UTC.

Each unique opportunity is routed sequentially through four independent ZA
consumers. A lane consumes through pending arm, same-side pending refresh,
entry, or add. Once consumed, later lanes must not observe that opportunity.
Lane 1 therefore preserves primary ZA behavior while later lanes receive only
opportunities that earlier lane state did not consume.

Before routing, raw SHORT opportunities are compared with the completed-M1
close exactly 30 minutes before the first executable live poll. The comparison
uses the current executable Bid. A decline of at least 0.60% changes the
effective side from SHORT to LONG. Raw LONG and smaller-decline SHORT
opportunities are unchanged. Missing or invalid lookback/quote data blocks that
SHORT opportunity rather than falling back to the old behavior.

After any LONG basket closes at its native target, a fixed portfolio-wide
eight-minute clock blocks new LONG baskets only. Existing LONG baskets and adds
continue, and all SHORT behavior is unchanged. In live execution the guard arms
before CLOSE submission and the eight-minute interval begins only after all
owned close deals are broker-confirmed. All lane exit checks precede admission
checks during each poll.

When the total non-zero LONG and SHORT position counts are equal, the overlay
freezes the minimum and maximum entry prices of all held positions after a
completed M1 closes inside that range. One completed close outside starts a
15-minute return window. Two consecutive completed closes back at/inside the
broken boundary create one opportunity opposite the original breakout. Both
breakout sides are enabled. Unequal inventory before confirmation invalidates
the break. Only one synthetic opportunity may wait; a raw ZA signal has
priority and leaves it pending for the next completed M1 without ZA.

Synthetic range-fade opportunities skip only the low-volatility ZA extreme and
pullback arm. They retain the executable spread/ATR gate, blocked hour, daily
loss, cooldown, lane capacity, portfolio LONG rearm, durable reservation,
ownership, and final broker-execution checks.

Per lane: maximum two positions, 0.01 lot, add 0.65 ATR, add-profit guard 0.30,
14 UTC new-basket block, confirmed realized daily limit -27 USD, cooldown eight
minutes, failure-to-progress after ten minutes below the frozen peak, and
maximum hold 70 minutes. ATR30 below 2.0 uses z2 to 1-sigma pullback and
target/stop/peak multiples 3.5/6.5/1.0.

The independent morning overlay is restricted to executable releases in
00:00-02:00 UTC. Lane 5 is the direction-controlled JST09 range false-break
confirmation and holds 15 minutes; lane 6 is direction-controlled price/effort
divergence and holds 55 minutes; lane 7 is the primary M15-compression/M5-edge
release and holds 45 minutes. Each lane is 0.01 lot, one position, no add, no
cooldown, and fixed-time close from actual fill. The combined morning cap is
three. Completed-bar availability is causal; live current spread/staleness and
broker confirmation remain additional safety gates.

The independent midday overlay is restricted to executable releases in
02:00-04:00 UTC, with the end exclusive. Lane 8 detects only the onset of a
confirmed-M1 2.5-USD round-level sweep/reclaim using ATR60 depth 0.05 and
reclaim 0.03. LONG has deterministic priority only in the degenerate case in
which both raw conditions hold. The lane is 0.01 lot, capacity one, no add and
no cooldown, and closes 60 minutes from the confirmed broker fill.

### JST09-11 stable_001 provisional evidence

Dev Stress produced 146 trades, USD 345.0425, PF 1.78453, every-tick MTM MDD
USD 75.623, DD/PnL 0.21917, and all 11 weeks positive. The original stable_001
leakcheck was USD +79.333. A later fixed, non-ranking 15/55/45 replay on the
already-observed leakcheck produced 55 Stress trades, USD +102.1015, PF
1.666455, and MTM DD USD 64.7335. The parameterization has only one complete
forward day: four Stress trades, USD +28.446, with DD/PnL 0.585. It is therefore
a fixed provisional implementation candidate, not independently proven forward
evidence; the forward period must not be used for further tuning.

### JST11-13 round-level sweep fixed evidence

The fixed `round_s2p5_d0p05_r0p03` candidate uses confirmed M1, a 2.5-USD
round-price grid, minimum sweep depth 0.05 ATR60, reclaim 0.03 ATR60, a
60-minute fill-based hold, one 0.01-lot lane, and capacity one.

| Dataset/scenario | Trades | PnL USD | PF | Every-tick MTM MDD USD |
|---|---:|---:|---:|---:|
| Dev Stress | 102 | 199.7715 | 1.676534 | 88.591 |
| Observed leakcheck Stress | 40 | 156.4325 | 2.892275 | 27.583 |
| Consumed forward Stress | 4 | 17.583 | 3.110804 | 15.756 |

Canonical runs:

- Dev: `C:/botter/backtest/output/backtest213/candidates/xau-jst1113-round5-parameter-shortlist-v001/runs/20260828_fixed_dev_reproduction_v001`
- Leakcheck: `C:/botter/backtest/output/backtest213/candidates/xau-jst1113-round5-parameter-shortlist-v001/runs/20260828_fixed_leakcheck_v001`
- Forward: `C:/botter/backtest/output/backtest213/candidates/xau-jst1113-round5-remaining-fixed-forward-v001/runs/20260828_consumed_forward_final_v001`

The forward file was already consumed during selection and contains only four
trades. It is reported for transparency, not treated as independent forward
proof and not available for retuning.

### JST09-13 combined portfolio-risk evidence

The exact fixed morning and midday trade streams were replayed together on the
same ordered Bid/Ask ticks. This is an overlap/MDD audit, not another search.

| Dataset/scenario | Trades | PnL USD | PF | Every-tick MTM MDD USD | Max positions |
|---|---:|---:|---:|---:|---:|
| Dev Stress | 248 | 544.8140 | 1.741148 | 80.473 | 4 |
| Observed leakcheck Stress | 95 | 258.5340 | 2.096087 | 44.363 | 3 |

Morning and midday inventory overlapped in 26 DEV episodes totaling 12.568
hours and 11 observed-leakcheck episodes totaling 5.117 hours. The audit is
stored under `C:/botter/bot/bot23/evidence/jst0913_combined_v004`. Failed
pre-PnL/summary attempts v001-v003 are retained explicitly and are not evidence.

## Evidence

| Dataset/scenario | ZA trades | Four-lane trades | PnL USD | PF | Every-tick MTM MDD USD |
|---|---:|---:|---:|---:|---:|
| Dev Base | 518 | 1,518 | 681.347 | 1.138702 | 251.330 |
| Dev Stress | 517 | 1,519 | 624.294 | 1.128014 | 248.715 |
| Observed leakcheck Base | 127 | 385 | 181.076 | 1.157717 | 153.836 |
| Observed leakcheck Stress | 122 | 372 | 224.514 | 1.208000 | 193.440 |

### reverse_d60 fixed evidence

| Dataset/scenario | Current four-lane trades | reverse_d60 trades | reverse_d60 PnL USD | PF | Every-tick MTM MDD USD |
|---|---:|---:|---:|---:|---:|
| Dev Base | 1,518 | 1,529 | 908.314 | 1.188445 | 228.926 |
| Dev Stress | 1,519 | 1,520 | 862.938 | 1.180786 | 246.086 |
| Observed leakcheck Base | 385 | 391 | 218.173 | 1.190972 | 153.836 |
| Observed leakcheck Stress | 372 | 378 | 261.099 | 1.243088 | 193.440 |

The observed leakcheck improvement versus current four-lane was USD +37.097
Base and USD +36.585 Stress, with six additional trades in each scenario. The
20 qualifying leakcheck opportunities are a limited sample. The result cannot
be used to retune the 0.60% threshold.

The audits passed causal clock ordering, stale-publication rejection,
deterministic rematerialization, Bid/Ask and sign reconciliation, prefix paths,
cost/PF/MDD reconciliation, zero duplicate opportunity ownership, and zero
terminal positions. The observed leakcheck is not a clean reusable holdout.

### LONG target portfolio rearm fixed evidence

| Dataset/scenario | Parent trades | Rearm trades | Parent PnL USD | Rearm PnL USD | Rearm PF | Rearm MDD USD |
|---|---:|---:|---:|---:|---:|---:|
| Dev Base | 1,529 | 1,484 | 908.314 | 986.008 | 1.2118 | 199.475 |
| Dev Stress | 1,520 | 1,470 | 862.938 | 993.680 | 1.2200 | 246.086 |
| Forward 2026-08-26 | 32 | 31 | 136.588 | 154.612 | — | — |
| Forward 2026-08-27 | 29 | 27 | 17.285 | 30.219 | — | — |

The forward days were used to derive/evaluate the idea and are not independent
forward evidence. The adoption is therefore `forward_only` and must be judged
from future live observations without retuning the fixed eight minutes.

### Inventory range false-break fade fixed evidence

| Dataset/scenario | Parent trades | Candidate trades | Parent PnL USD | Candidate PnL USD | Candidate PF | Candidate MDD USD |
|---|---:|---:|---:|---:|---:|---:|
| Dev Base | 1,484 | 1,490 | 986.008 | 1,079.596 | 1.2343 | 199.475 |
| Dev Stress | 1,470 | 1,473 | 993.680 | 1,066.683 | 1.2379 | 246.086 |
| Observed leakcheck Base | 380 | 381 | 144.490 | 154.566 | 1.1349 | 168.228 |
| Observed leakcheck Stress | 367 | 367 | 158.865 | 158.865 | 1.1482 | 192.107 |

The fixed candidate was selected on Dev before the leakcheck replay. Leakcheck
Base consumed one synthetic opportunity for USD +10.076; Stress armed one but
consumed none and was identical to the parent. This is survival evidence, not
independent holdout proof. The candidate remains `forward_only`.

## Live translation

- ZA lanes use bot23-private magics 230023-230026 and comments
  `s23_za_l1`-`s23_za_l4`. The legacy single-lane magic 200023 remains historical;
  200024-200026 belong to bot24-bot26 and must not be reused by bot23.
- Morning lanes use bot23-private magics 230027-230029 and comments
  `s23_am_l1`-`s23_am_l3`; their state is added empty on first start without
  clearing or rewriting ZA inventory.
- The midday lane uses bot23-private magic 230030 and comment `s23_md_l1`.
  It is initialized empty, owns capacity one, and cannot adopt a foreign magic
  or comment.
- State version 3 persists the global routing reservation, portfolio LONG-rearm
  clock, range-fade state, and independent lane inventory. Missing range-fade
  fields migrate inactive in place without clearing positions or ZA pending
  entries, or inventing a prior trigger.
- The base-family `strategy_id` remains unchanged so open-basket state remains
  compatible. The candidate ID, lane spec IDs, entry-policy ID, and entry-policy
  params hash identify the new behavior. First-start migration clears only old
  unsubmitted pending entries; open baskets and unresolved OPEN evidence remain.
- Startup also queries retired bot23 magic 200023 and refuses cutover unless its
  position and order sets are both available and empty.
- Confirmed-bar spread remains part of the signal; current executable spread is
  used separately for pending-fill and entry guards.
- Live broker fills replace simulated fixed slippage. Actual commissions, swap,
  latency, rejection, and five-second polling can differ from every-tick replay.
- Midday research used ordered Bid ticks to form M1 and the first ordered tick
  after availability. Live uses broker HIST M1 and a later poll quote. The
  updated bridge source includes broker `quote_time_msc`, so fixed-hold close
  waits for a fresh quote after due and preserves market-reopen retry state.
  This equivalence applies only after that MQ5 source is compiled and attached;
  the runner rejects an older INFO response in live preflight.
- A broker submission failure/reservation consumes the opportunity fail-closed
  rather than allowing another lane to risk a duplicate fill.

## Forward-only passive opportunity evidence

The local runner includes `s23_shadow_opportunity_observer_v1`. It is not part
of the backtest recipe, reverse_d60 policy, lane routing, or execution state.
It observes the executable Bid/Ask already fetched by the runner and records
all confirmed ZA opportunities, including policy/stale rejects and all-lane
no-ops, with 1/5/15/30/60-minute executable markouts.

This evidence is intended to discover repeatable states that the current four
lanes do not consume. It must not be used to change the running strategy
automatically. Any definition selected from these forward logs is a new
forward-derived hypothesis and must be frozen before a separate replay. Poll
cadence extrema are not equivalent to ordered-tick MFE/MAE.

The ZA observer uses dedicated files `s23_shadow_opportunities.csv`,
`s23_shadow_markouts.csv`, and `s23_shadow_observer_state.json`. The JST11-13
observer uses separate `s23_midday_shadow_*` files and records signals rejected
by capacity, spread, stale, or sync gates. Neither observer changes the
version-3 bot state or order decisions. Observer failures are
contained and do not affect broker order processing.
- The reverse_d60 source switch was explicitly requested on 2026-08-26. Deployment,
  restart, state reset, log reset, EA attachment, and actual order submission
  remain separate runtime actions.
