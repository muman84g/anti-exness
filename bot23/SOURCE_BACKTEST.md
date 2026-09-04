# Source Backtest

## 2026-09-04 CLOSE claim recovery v33

Operational safety correction only: an EA recovered CLOSE claim cannot call
the trade handler again. Return `CLOSE_RESULT_UNRESOLVED` for owned position/deal
reconciliation, preserving the Python submission marker across restart.
The bridge protocol requirement is `2026-09-04-s23-close-claim-v33`; this does not
change signal parameters, lane counts, lot, stop/target or the backtest mapping.
The rebuilt EX5 and matched Python/params must be updated together. No CentOS
deployment or integrated Forward replay is implied by local verification.

## 2026-09-04 IPC recovery execution boundary

This local operational correction does not change the selected signals, lane
limits, TP/SL, timing parameters, or backtest mapping. Definite pre-submission
IPC failures are recoverable after owned-inventory reconciliation. ZA may
re-evaluate its latest confirmed bar after an all-lanes-noop transient final
guard failure; it does not backfill historical ticks or assume the price of the
original missed entry. Time-varying policy is checked at the new attempt.

Ambiguous OPEN remains consumed, and pending/filled orders cannot be retried by
this release path. The response/claim completion hand-off waits within the
existing IPC response budget without discarding a confirmed response. These
changes remove infrastructure-only skips; exact tick backtest fill parity is
still limited by live polling, broker quotes, spread and execution latency.
Local failure-injection tests are not an integrated Forward PnL rerun or runtime
deployment proof.

## 2026-09-04 close-ledger replay durability

Existing confirmed-deal replay now re-establishes file and parent-directory
durability before state progression. This changes only crash recovery safety;
signal, fill, exit, lot and timing parameters are unchanged. Local no-order
evidence is separate from broker/runtime or deployment verification.
The repeated audit adds strict CSV quote parsing: physical newline termination
alone does not prove that a quoted close row is complete.

## 2026-09-04 Q01 variance-ratio release integration

- Signal/policy: `Q01_variance_ratio_release` / `q01_k4_w48_t135_b12_hold30_cap1_v001`
- Frozen params hash: `fdec1cecc71305877f280d3225fd17093f92a42708597202ba0bfad4eafacf67`
- Frozen definition: `C:/Users/muuma/Documents/Codex/2026-09-02/bot/work/bot25_q01_lane_preimplementation_frozen_20260904.json`
- Source math: completed Bid M5; `c.diff(4).shift(1).rolling(48).var() / (4 * c.diff().shift(1).rolling(48).var())`; threshold 1.35; prior 12-bar High/Low breakout; 110-M5-bar warm-up and positive ATR20 gate.
- Entry/lifecycle: first fresh quote after completed signal M5, maximum delay 7 minutes, raw spread at most 0.30, one 0.01-lot position, confirmed-fill plus 30 minutes, confirmed-close plus 5-minute cooldown. The installed local config keeps the independent `q01_live_trading_enabled=false` gate closed, so Q01 signal decisions cannot submit real orders until a separately authorized candidate changes that gate.
- Feed gap: a quote interval over 300 seconds closes at the first arrival quote. Feed-gap and fixed-hold exits do not defer for wide spread. Exact market-closed no-fill retains a durable close intent and retries from fresh broker quote time.
- Ownership: independent lane 22, magic 230044, comment `s23_q01_l1`; Q01 does not reuse any existing basket or signal identity.
- Evidence status: fixed DEV/Leakcheck/Forward tick evidence is inherited from the frozen research package. This local port verifies implementation parity and lifecycle safety; it does not create a fresh holdout or live-runtime result.
- Local candidate: `bot23-integrated-session-vwap-on-t0530-edge-on-q01-v008`; bridge `2026-09-04-s23-legacy-query-v32`.
- Runtime boundary: no CentOS/MT5 placement, restart, attachment, account access, state repair, or order execution was performed.

## 2026-08-31 t0530 edge-break best integration

- Signal: `t0530_edge_break_fade`
- Frozen live policy: `ny0530_edge_break_fade_w15_h15_cap4_v001`
- Params hash: `27d51f6243e74a56e2ad10428f1a1f46e58f2f89a31bd96db4fb7025301d6163`
- Evidence label: DEV-selected / known leakcheck; historical forward is decision-ineligible and did not pass the promotion gate.
- Local implementation authorization does not promote the research evidence to fresh holdout, forward, or live evidence.
- Full DEV tick reconstruction: research mid 139 events, Bid 139 events, implementation 139 events; exact event-time and direction match, including live continuity/OHLC guards.
- Runtime boundary: the later local candidate is `bot23-integrated-session-vwap-on-t0530-edge-on-q01-v008`; Q01, t0530 edge, and session-VWAP are locally enabled; no CentOS/MT5 placement, restart, attachment, account access, or order execution was performed.

## 2026-08-29 NY 05:30-08:30 session-VWAP fixed candidate

- Policy: `ny0530_0830_session_vwap_extension_fade_q90_20d_atr60_h15_cap5_v001`
- Params SHA-256: `b47b8d7d26094681fe559f6daf9c7e2bb1f4cd610527b0a69c5426c20a7a2a65`
- Runtime input: broker Bid OHLC and M1 TickVolume; confirmed M1 only
- Clock: completed-M1 available time (UTC event time plus one minute) converted to
  `America/New_York`; 05:30 inclusive, 08:30 exclusive
- Signal: daily session VWAP extension / ATR60 (60-bar window, minimum 30 bars), absolute-Z Q90 over 20 calendar days,
  new threshold-onset with side-change; fade positive Z SHORT and negative Z LONG
- Lifecycle: five private 0.01-lot lanes, capacity five, confirmed-fill plus 15 minutes
- Live no-fill recovery: exact trade-permission rejects `10026/10027` retain the same
  signal for a bounded cooldown retry; ambiguous OPEN results are not resent. Exact
  market-closed close retcode `10018` retains the owned position and retries after 60 seconds.
- Restart identity: the original retry opportunity is lane-persisted. A crash-after-fill
  position is adopted only from one exact owned symbol/magic/comment/side/lot match whose open time falls in the persisted submission window;
  the persisted submission start must itself fall between the canonical signal release and expiry.
  Persisted source, signal/event/release/available timestamps, direction, opportunity ID and canonical expiry must also be mutually consistent;
  retry expiry and stale-signal admission use the later of host UTC and broker UTC quote time, so either clock can prove expiry;
  submission/fill matching and trade-permission cooldown start use the broker UTC quote clock.
  Signals available by the broker-confirmed same-direction close-deal time are not reused by another private lane.
  Initial OPEN confirmation applies the same side/lot/price/submission-window identity checks.
  CLOSEDEAL requires a valid deal id, execution price, finite account-currency result and a broker timestamp no earlier than entry;
  later valid close evidence clears only close-related reconciliation blocks, not unrelated policy or ownership blocks.
  POSITIONS and ORDERS use distinct bridge record schemas; malformed or non-finite INFO, OPEN, POSITION, ORDER and CLOSE
  payloads are rejected at the IPC boundary rather than interpreted as broker confirmation. Fixed ACCOUNT, INFO, CAPS, position,
  and order schemas require exact field counts; delimiter-bearing or extended records cannot be truncated into owned inventory evidence.
  Before a live OPEN, lot min/max/step and configured digits/point must match broker INFO; this entry-only guard does not
  suppress owned-position exits or shadow/DEV evaluation. A missing live broker quote timestamp is not replaced by host time,
  and a broker quote whose UTC clock differs from host submission UTC by more than `max_signal_delay_minutes` is not used for OPEN.
  Durable unsent or failed CLOSE intent is re-armed only after exact owned-position and empty-order reconciliation, while a
  successfully submitted `close_requested` position waits for disappearance plus CLOSEDEAL and is not blindly resent.
   Mixed multi-position CLOSE results preserve successful ticket requests and retry only failed/unsent tickets. CLOSEDEAL
   position id and symbol plus persisted opening ownership must match; exit-deal magic is not ownership evidence because it
   identifies the closing order/actor and may differ for manual closure. Active hedging ticket plus identifier must both match state.
   Bridge v6 aggregates every OUT/OUT_BY deal for one position identifier and returns weighted exit price, total account-currency
   result, and total exit volume. State is cleared only after a direct ticket query proves absence and aggregate exit volume matches
   the original persisted lot. Cross-day multi-ticket results are applied in broker deal-time order. Preflight rejects older bridge versions.
   Direct absence accepts only the pinned bridge's exact `ERR|POSITION_NOT_FOUND`; numeric trade retcodes and legacy spellings are
   query failures, not absence evidence. Bridge v26 retains the exact terminal record count for POSITIONS/ORDERS and converts any
   enumeration select failure into a query error, so a truncated or partial inventory response cannot prove flatness. It also enforces
   the frozen bot23 symbol/lot/deviation/magic/comment allowlist, broker symbol mode, USD value contract, and margin headroom at OPEN execution and atomically rechecks
   symbol/magic/comment/position identifier immediately before every CLOSE broker call. Position open time includes broker milliseconds so fixed-hold deadlines do not truncate fills to seconds. Mutating IPC is request-correlated, deadline-bound, atomically published, durably claimed, and restricted to the used OPEN/CLOSE surface under single-consumer and single-runner locks. Command draining deletes the command file rather than leaving a zero-length busy slot. Expiry is reported as definitely unpublished only after command disappearance is verified, so a failed deletion plus a backward clock adjustment cannot revive a request whose caller already cleared its receipt. A claim-write/readback failure is likewise definitive only after both command and claim disappearance are verified; otherwise the caller times out with its durable receipt retained for inventory reconciliation. The per-lane daily realized-loss accumulator advances monotonically by UTC date, so a late
   prior-day deal or older evaluation clock cannot erase newer-day loss. Malformed daily-risk state blocks new baskets while allowing
   exact owned-basket close reconciliation to complete.
   Every live lane persists an exact pending-OPEN receipt before submission. Restart recovery adopts only one newly observed position
   whose symbol, magic, comment, side, lot, broker-millisecond fill time, and receipt window all match; the same rule covers a new basket
   and one additional ticket in an existing basket.
   A durable daily date later than the evaluation UTC date cannot permit a basket: an already-reached loss limit remains active,
   otherwise the future date is invalid state. Persisted position open epoch must be positive, and every live lane requires a valid
   broker position open time before it can monitor and later reconcile that close lifecycle.
   Malformed durable OPEN-retry or ZA cooldown timestamps fail closed for new exposure. A non-finite persisted ZA basket peak is
   conservatively reset from current executable PnL so the fixed failure-to-progress exit cannot be disabled by damaged state.
   State shape validation includes routing and all 17 lane dictionaries. Basket sequence is validated before OPEN; broker entry
   price is restored during exact owned sync; malformed frozen ATR uses fixed exits. Invalid fixed-hold defer state is reset before
   a due close, malformed virtual trend state is invalidated without entry, and malformed session close identity blocks signal reuse.
  Every bot-managed 10018 close uses a fresh-broker-quote 60-second cooldown without a reconciliation block. ZA FTP/max-hold
  and trend max-hold elapsed time use broker quote UTC rather than host poll UTC. Exact owned sync restores broker open time
  for ZA and fixed-hold positions before elapsed lifecycle checks.
- Adoption state (2026-09-03): user authorized local bot23 integration after the
  fixed parent was compared across DEV, known/reused Leakcheck, and retrospective
  Forward. Current local config has `session_vwap_enabled=true`; no deployment,
  restart, bridge attachment, account access, or live order was performed.
- Updated portfolio evidence:
  `backtest/output/backtest235/BOT23_SESSION_VWAP_FORWARD_PORTFOLIO_REPORT_20260903_ja.md`.
  The Forward label remains retrospective/known rather than a fresh independent
  holdout; no post-result parameter tuning was performed.
- Evaluation identity: all 22 lanes retain explicit `spec_id` and `signal_id`.
  A separate `s23_signal_evaluation.csv` records group/lane/spec/signal/variant,
  raw/effective direction, transform, opportunity, ticket/deal, and confirmed PnL.
  ZA variants are separately labeled as primary, late-SHORT reverse-LONG, and
  inventory-range false-break fade. Post-adoption audit additionally splits
  every shadow/DEV basket close into position-level evaluation rows so mixed ZA
  variants retain exact individual PnL; legacy positions lacking an opportunity
  identity are quarantined as `za_unattributed_legacy`, not assigned to primary.
  Live realized outcomes remain broker-confirmed `position_close_confirmed`
  rows; close-request rows must not be summed as realized PnL.
  Passive evaluation schema/construction/write failures are isolated from the
  owned-position lifecycle and disable that passive sink after the first
  visible failure. A bad evaluation header disables that output for the process
  while the operational trade ledger remains a startup gate. Broker-confirmed
  close rows are deduplicated by deal/lane/position identity, and close state
  plus daily realized PnL advance only after operational close evidence exists.
  Operational and passive ledgers revalidate a same-path replacement header;
  startup rejects malformed row widths, duplicate/conflicting confirmed deals,
  and unterminated partial-write tails. Appended rows are flushed and fsynced.
  After those close rows exist, realized-PnL, rearm/recovery, basket-clear,
  sync-block, and final state-save processing is rollback-guarded; any exception
  restores the complete pre-consumption in-memory state for an idempotent retry.
  Helper-level state writes are deferred inside this transaction and only the
  complete final state is committed, closing the process/power-loss window that
  exception-only rollback cannot cover.
  Each derived operational/passive row is independently keyed by the deterministic
  broker-close transaction deal ID, allowing restart to skip durable counterparts
  while repairing a missing passive row.
- History admission: 20-calendar-day timestamp coverage plus latest completed M1,
  contiguous ATR60 tail and contiguous current NY-session M1; malformed or incomplete
  pages are not partially admitted and trigger retained-cache rebackfill.

The pre-implementation HIST reproduction and combined bot23 DEV reconciliation are
recorded in
`backtest/output/backtest227/candidates/xau-ny0530-0830-structural-screen-v001/runs/20260829_bot23_current_plus_session_vwap_hist_dev_v3/PREIMPLEMENTATION_RECONCILIATION_ja.md`.
The candidate added 177 DEV trades. Combined Stress was USD +1,980.059 through
+1,999.088 with PF 1.333-1.337 and full-period MDD unchanged at USD 246.086.
This is DEV evidence, not permission to enable or deploy.

## 2026-08-29 EU開始帯 dual rebuild（非採用・売買未配線）

Cycle27のlive移植前監査でraw tick入力差が見つかったため、次の2 artifactを別identityで生成した。

1. `raw_tick_shadow_collector.py` / `s23_raw_tick_shadow_v1`
   - MT5 `CopyTicks`を使う読取専用・追記専用collector
   - bot23売買runnerから独立し、初期設定disabled
   - strict config、exact CSV cursor identity、finite tick values、page metadata/count/windowをfail-closed検証
   - 実環境取得はまだなく、local fake bridgeのrestart/same-millisecond試験のみ
2. `backtest/output/backtest226/candidates/xau-eu-nypre-ohlcv-rebuild-v001`
   - raw imbalance、quote efficiency、spread shapeを使わない別candidate
   - signal入力はBid OHLCとTickVolumeのみ
   - entry/exit/MTMはordered Bid/Ask tick
   - fixed v3: `ohlcv_absorb_a90_e20_plus_first_vwap_follow`、60分hold、capacity 8、0.01 lot

固定v3結果（Base / Stress）は以下。

| dataset | trades | PnL USD | PF | every-tick MDD USD |
|---|---:|---:|---:|---:|
| dev | 73 | +221.913 / +212.301 | 2.083 / 2.015 | 77.427 / 79.911 |
| observed leakcheck | 22 | +38.006 / +35.254 | 1.699 / 1.637 | 33.465 / 33.961 |
| observed forward | 3 | +19.497 / +19.104 | 2.479 / 2.435 | 34.162 / 34.162 |

3期間とも黒字だがforwardは3件しかなく、forward MDD/PnLも1を超える。扱いは`forward_only observation candidate`で、bot23 entry/close/stateへは配線しない。時刻は北山朝也氏資料に合わせ、event/release/ingested/available/cutoffを分離した。既存Cycle27 recipe・resultは上書きしていない。

## Frozen mapping

- Bot: `bot23` / S23 / XAUUSD
- Base family / persisted state strategy ID: `bot23_za_horizontal_inventory_v001`
- Parent idea: `bot23_late_short_30m_action_matrix_v001`
- Adopted idea: `bot23_x_archive_inventory_range_false_break_fade_opt_v001`
- Candidate: `bot23-x-archive-plus-jst0911-plus-jst1113-plus-jst1300-pre-eu30-plus-reverse-stop-trend-v001`
- Parent candidate: `bot23-long-target-portfolio-rearm-v001`
- Selected spec: `reverse_d60 + long_target_portfolio_rearm_8m + balanced_book_false_break_fade_w15_c2_both + jst0911_stable001_param_15_55_45_v001 + jst1113_round_s2p5_d0p05_r0p03_h60_cap1_v001 + jst1300_pre_eu30_squeeze45_double60_rsi45_cap3_dst_v001 + reverse_long_stop_m1_bull_multishort_n2_tp1_sl0p5_v001`
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
- Trend-recovery params hash:
  `a29187af7e67075ef2e4eb0c39cb3cd09bbfb2a6ee7b23e4cd51bbe370c000e9`
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

For every ZA new basket, the blocked-UTC-hour and daily-realized-loss/state
gate is evaluated again at the actual deferred pullback fill and at the final
market `OPEN` boundary. A locally pending ZA entry is cleared without an order
when either gate becomes active after signal creation. This ZA-specific guard
does not change the independent admission contracts of the session overlays.

A persisted ZA pullback may survive restart only when its positive target/ATR,
opportunity ID, signal/event time, one-minute release time, and bounded expiry
form one canonical identity and the current evaluation clock is at or after
that release. A pending identity observed before its own release is treated as
contradictory clock state and cleared without exposure. Incomplete,
contradictory, or overlong pending state is cleared before it can create a
basket. An exact market-closed `10018`
OPEN reject with a complete zero-owned-position reconciliation is definitive
no-fill, not an ambiguous action: the local reservation is cleared and entry
uses a 60-second bounded retry clock. Session-VWAP starts that clock from the
current admission attempt rather than a stale broker quote timestamp.

After a `reverse_d60`-origin LONG basket is fully closed at `basket_stop`, one
private recovery episode may start during the originating ZA 13:00-18:00 UTC
session. The stopped basket's ATR30 is frozen. During the next 30 minutes, each
newly completed bullish M1 opens one SHORT in lane 12, capped at two 0.01-lot
tickets. Each ticket uses the native adaptive target (3.5 ATR below ATR30 2.0,
otherwise USD 10) and half the native stop (3.25 ATR or USD 9). Any ticket stop
closes all remaining recovery tickets; individual target and 70-minute maximum
hold close only that ticket. Broker-confirmed close times and completed UTC M1
bars are authoritative; the recovery lane uses magic 230034 and `s23_tr_l1`.

Ordered-tick Dev Base added 9 trades and USD +15.277 (Stress: 10 trades,
USD +7.509) with unchanged combined MDD for max-two, TP 1.0x / SL 0.5x. Frozen
forward 2026-08-16 through 2026-08-28 added six trades and USD +41.934 Base
(USD +41.834 Stress), again with unchanged combined MDD. The leakcheck period
contained zero qualifying reverse-LONG stops, so it provides no independent
confirmation for this deliberately local edge. Canonical run outputs are under
`C:/Users/muuma/Downloads/codex-temp/bot23-stop-flip-multishort-v1/` in
`20260829_dev_tick_v1` and `20260829_forward_20260816_28_frozen_v1`.

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

An unchanged-candidate diagnostic rerun on the extended forward tick file
through 2026-09-02 produced 25 Stress trades / USD -52.561 / PF 0.5107 /
every-tick MTM MDD USD 79.952, with all three sampled weeks negative. This is
diagnostic evidence on an already observed period rather than a reusable
holdout. It rejects continued live entry use of the fixed Midday candidate;
`midday_session_enabled=false` blocks new orders while its frozen identity,
passive shadow evidence, and owned-position exit handling remain intact.

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
- The pre-EU30 lanes use private magics 230031-230033 and comments
  `s23_pe_l1`-`s23_pe_l3`. They are initialized empty and use fixed holds of
  45/60/45 minutes from confirmed broker fills.
- The trend-recovery lane uses private magic 230034 and comment `s23_tr_l1`.
  It is initialized empty with an inactive episode and cannot infer a trigger
  from pre-migration baskets or adopt a foreign position.
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

## JST13:00-pre-EU30 adopted fixed candidate

Runtime clock notation is UTC. The existing partitions are unchanged:
`jst1300_pre_eu30` is UTC 04:00-06:30 in London summer time and 04:00-07:30
in London standard time; `eu_open_to_us_preopen` ends at UTC 11:30 in New York
summer time or 12:30 in New York standard time; `us_to_eu_late` then ends at
UTC 20:30 or 21:30 respectively. London and New York DST are resolved
independently, including their mismatch weeks. This clock controls new-entry
admission only; confirmed-fill lifecycle deadlines remain elapsed UTC.

The adopted three-lane identity is
`jst1330_c4_final10_010_squeeze_double_rsi`: Bollinger squeeze release direction
control at 45 minutes, double-sweep resolution primary at 60 minutes, and RSI
extreme reversal direction control at 45 minutes. Stress results were DEV 120
trades / +142.1600 / PF 1.408080 / every-tick MTM DD 112.4240; observed
leakcheck 47 / +65.3745 / PF 1.476835 / DD 41.1730; and untouched forward 8 /
+26.7110 / PF 5.415771 / DD 12.1720. DEV was 3/3 months and 3/3 lanes positive;
leakcheck was 2/2 months, both time subblocks and both halves positive with 2/3
lanes positive; forward was 3/3 lanes and each JST13/14/15 hour positive.

Before adoption, 50 new DEV-only candidates were frozen into five batches of
ten. All 50 were evaluated unchanged on observed leakcheck; none had positive
Stress PnL, so no challenger qualified for forward. The least-negative result
was -27.1235 with PF 0.956704 and DD 118.324. No failed candidate was repaired,
re-ranked on forward, or forwarded. Live signal parity then compared all three
implemented signals across 4,590 DEV signal/time cells: 158 expected events,
zero mismatches.
- The reverse_d60 source switch was explicitly requested on 2026-08-26. Deployment,
  restart, state reset, log reset, EA attachment, and actual order submission
  remain separate runtime actions.
