# Source Backtest

- Bot: `bot25` / S25
- Strategy: local `V24` virtual-core child of V23 with frozen `L05` exit overlay
- Strategy ID: `bot25_v24_xauusd_virtual_bilateral_core_v001`
- Candidate ID: `bot25_exit20_l05_v3_live_v001`
- Frozen candidate-parameter hash: `ad0f8775c62303b037aa89e900d971900c6ead2e640f70b3dfec989c30d1d88c`
- Base V24 hash: `788e7b076cd49f67cc0f2f87677f350b8ac88bcc13b15bd26cde7589105cca36`
- Parent V23 hash: `12dc94c78f5fb6bb01710e40a8f5f199af472f2323ab0f2bb02063fda427ca10`
- Instrument/execution: XAUUSD true ticks; decisions from completed Bid M5 bars
- Parent specification: `backtest/検討中/chatgpt案/多重ポジ/20260827_XAUUSD_break比率wavefront/best/07_man231_core_satellite_固定仕様.md`
- V23 specification: same folder, `best/24_man231_drought_minority_add_pause_固定child.md`
- Direct bot25 comparison: same folder, `best/57_現行best_V23とbot25_man231直接比較.md`
- Parent comparison: same folder, `13_man231_親P0詳細比較.md`
- Post-leak unseen check: same folder, `14_man231_postleak未確認tick結果.md`

## V24 behavior delta

- Start each eligible episode with one logical LONG core and one logical SHORT
  core. They are state only: no broker tickets, PnL, spread, slippage,
  commission, or swap.
- Frontier additions remain real broker positions. Six-per-side and 3:1 use
  logical counts, leaving room for at most five real positions per side.
- ATR14 is simple rolling true range; EMA200 uses `adjust=False`.
- A strict radius-2 pivot breaks only beyond pivot +/- 0.10 ATR.
- The active side adds after a 0.50 ATR frontier advance, at most six tickets
  per side and at most a 3:1 active/opposite count ratio.
- After a native productive close of at least 0.10 USD, a 120-minute drought
  blocks only a frontier add whose prospective side count is below the opposite
  side count. No block applies before the first productive close.
- An opposite break or EMA200 retouch releases every profitable real ticket on
  the active side, newest first. The protected core is virtual.
- A 12-hour episode or feed gap over five minutes requests a full close.
  Unlike the historical runner, live cannot use a pre-gap historical price, so
  it closes on the first eligible fresh quote under the shared spread-defer
  baseline.
- Entry spread ceiling is 0.300 XAUUSD price (300 points at point 0.001).
- Shadow proxy applies the Base assumption of 0.030 adverse price slippage at
  both entry and close. Live satellite selection also requires a 0.030 price
  buffer before submitting close, while broker-confirmed fills remain canonical.

## L05 adopted exit overlay

L05 does not replace V24. For each real ticket, only an opposite native pivot
break at or after its entry M5 is eligible. A later completed M5 close must first
reclaim the broken pivot and another later completed M5 close must lose it
again. At that final event only losing tickets are passed to the normal
fill-confirmed close controller with `reason=loss_policy_L05`. Same-bar
break/reclaim/re-loss, profitable tickets, pre-entry breaks, and retrospective
evaluation after deployment are excluded.

The frozen OOS audit is
`C:\Users\muuma\Downloads\codex-temp\s25_m1_close_dev_20260904\runner_audit_20260905\BOT25_L05_OOS_AUDIT_20260905_ja.md`.
Leakcheck Base/Stress were respectively `+720.940 / PF 1.6456 / DD 229.276`
and `+672.586 / PF 1.5886 / DD 231.297`. Forward Base/Stress were
`+698.705 / PF 1.9585 / DD 279.556` and
`+668.327 / PF 1.8675 / DD 302.852`. The user accepted the lower trade count
and lower OOS PnL in exchange for the improved PF, DD, and PnL/DD on 2026-09-05.

## Parent V23 recorded results

| dataset | Base PnL | Stress PnL |
| --- | ---: | ---: |
| dev tick | +496.708 | +362.859 |
| leakcheck tick | +688.470 | +654.6365 |
| post-leak previously unseen tick | +408.564 | +393.722 |

V23 exceeded the previous bot25 man_231 result in all six dataset/cost cells,
with fewer entries and lower every-tick MTM MDD in each dataset. All three
recorded segments were positive in Base and Stress. These are
backtest/forward-tick observations, not proof of live profitability. Real
operation of the historical V23 was explicitly authorized on 2026-08-27.
That historical authorization did not enable the V24 child. The user separately
authorized V24 live configuration on 2026-09-04; runtime preflight remains fail-closed.

The original dev note recorded `dev_rejected_user_quality_floor`; the later
post-leak note returned it only to deepening and explicitly did not authorize
live use. Bot25 source porting is based on the user's subsequent explicit
instruction, not on a clean reusable-evaluation or live-promotion gate.

Operational hardening does not alter the frozen V23 entry/add/close rules or cost
assumptions. Bot23-equivalent account identity, order reservation/recovery,
definitive-no-fill-only close retry, strict causal logging, and log rotation are live safety layers; the
semantic preservation/deletion audit is in `PORTING_AUDIT.md`.

The passive observer/tagger and reconciliation hardening added on 2026-08-28
also leave the frozen candidate unchanged. They add counterfactual frontier
evidence, durable CSV writes, retention of non-recoverable sync blocks, and
exact state-lot versus broker-volume matching. Observed markouts are forward
diagnostics only and are not a new selection or tuning source in this port.

Live realized PnL is broker-accounting truth: the bridge aggregates all deals
for the MT5 position ID so entry/exit commission, swap, and fee are included,
while the logged close price remains the final broker close-deal price. This is
more complete than treating only the exit deal's commission as round-trip cost.

## V24 evaluation and operation boundary

The V24 comparison is fixed to the single structural change above and uses only
`C:\botter\backtest\dev_data\XAUUSD_tick_dev.csv` for selection evidence.
Leakcheck and Forward are not used to tune this child. Results are persisted in
the current Codex task output. DEV Base changed from `+496.708 / PF 1.0439 /
MTM MDD 421.121` to `+568.502 / PF 1.0847 / MTM MDD 414.129`; Stress changed
from `+362.859 / PF 1.0319 / MTM MDD 443.513` to `+473.250 / PF 1.0700 /
MTM MDD 422.161`. All 649 frontier entries, 1,084 release events, 128 productive
close waves, and 149 blocked adds matched the V23 control. The removed 270
physical seed entries explain the real-entry change from 919 to 649.

V24 is configured live by user authorization on 2026-09-04. Remote deployment,
restart, and actual runtime readiness remain unverified and were not performed. Exact
state-v5/man231 or state-v6/V23 positions can be retained during a future switch
only when state and broker ownership match completely, orders and pending
lifecycle actions are absent, logical cap/ratio remain valid, and unchanged-file
CAS succeeds. One best-price existing ticket per side substitutes for that
side's virtual core until the existing exit path closes it. Ambiguous non-flat
inventory remains fail-closed and no replacement seed is created.
During shadow-only canary, migrated real inventory is verified for an exact
state/broker match without reconciliation, and all strategy entry/exit mutation
is held until explicit live activation.
State-v7 V24 upgrades to state-v8 L05 only after the same exact broker/state,
empty-order, no-pending-action and CAS checks. The upgrade preserves every
position and the existing legacy-core mapping while starting L05 at a new
non-retroactive watermark.

The 2026-09-05 bridge-v9 and state-shape corrections are live-boundary guards,
not a new research candidate. They enforce agreement among BUY/SELL, the
canonical comment marker, persisted entry time/price, and broker inventory, and
reject malformed recovery state. They do not change the frozen V24 or L05
signal, allocation, or exit semantics and therefore do not create a new
backtest-selection result.

The subsequent bridge-v10/executor correction extends the same live-only
boundary: OPEN success must match the re-queried position's side, volume, price,
and millisecond open time, while persisted OPEN and post-close handoff state must
retain the active episode/M5/wave lifecycle that produced it. Strict JSON type
checks reject boolean aliases in numeric configuration. No V24/L05 candidate
parameter or backtest decision path changed.

The later state-v8 completeness correction is also live-only. It rejects
incomplete current episode/inventory and missing durable safety fields before
runtime mutation. Recognized older states may be completed only inside the
broker-verified migration path; a missing legacy frontier is fixed once from the
fresh preflight midpoint, matching the prior first-evaluation fallback without
changing any selected V24/L05 signal or backtest parameter.

The v9 pending-CLOSE/defer validation is likewise a live restart-safety guard.
It authenticates persisted close intent against the finite production reason
set and, for M5 exits, the already selected completed bar and decision window.
It does not add a signal, change close selection, or alter any DEV, Leakcheck,
or Forward result.

The v10 pre-commit state validation is also live-only. It changes when an
invalid internal state is rejected—from the following restart to immediately
before persistence—but does not change any valid signal, entry, close, or
backtest path.

The v11 exact-field schema check is another persistence-only guard and has no
effect on valid V24/L05 replay semantics or research results.

The v12 CLOSE retry timestamp normalization is live-only and does not change
the selected exit or any backtest result.

The v13 persisted logical-cap/ratio check mirrors the already selected
allocator constraints and does not alter valid replay results.

The v14 live-shadow inventory rejection is a runtime state-integrity guard. It
does not alter virtual-core substitution, valid broker inventory, any selected
entry or exit, or DEV, Leakcheck, and Forward replay results.

The v15 query-order and outer-loop persistence guards complete that same
live-only failure boundary. They do not change a valid V24/L05 replay path or
any research result.
