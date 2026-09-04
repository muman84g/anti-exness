# Source Backtest

- Bot: `bot25` / S25
- Strategy: `V23` (`man_231` + drought minority-side frontier-add pause)
- Strategy ID: `bot25_v23_xauusd_drought_minority_pause_v001`
- Candidate ID: `combo_014_v001_v001`
- Frozen specification hash: `12dc94c78f5fb6bb01710e40a8f5f199af472f2323ab0f2bb02063fda427ca10`
- Instrument/execution: XAUUSD true ticks; decisions from completed Bid M5 bars
- Parent specification: `backtest/検討中/chatgpt案/多重ポジ/20260827_XAUUSD_break比率wavefront/best/07_man231_core_satellite_固定仕様.md`
- V23 specification: same folder, `best/24_man231_drought_minority_add_pause_固定child.md`
- Direct bot25 comparison: same folder, `best/57_現行best_V23とbot25_man231直接比較.md`
- Parent comparison: same folder, `13_man231_親P0詳細比較.md`
- Post-leak unseen check: same folder, `14_man231_postleak未確認tick結果.md`

## Frozen behavior

- Seed one BUY and one SELL and continuously restore a missing side.
- ATR14 is simple rolling true range; EMA200 uses `adjust=False`.
- A strict radius-2 pivot breaks only beyond pivot +/- 0.10 ATR.
- The active side adds after a 0.50 ATR frontier advance, at most six tickets
  per side and at most a 3:1 active/opposite count ratio.
- After a native productive close of at least 0.10 USD, a 120-minute drought
  blocks only a frontier add whose prospective side count is below the opposite
  side count. No block applies before the first productive close.
- An opposite break or EMA200 retouch releases only profitable satellites on
  the active side, newest first. The best-priced ticket remains the core:
  lowest-entry BUY or highest-entry SELL.
- A 12-hour episode or feed gap over five minutes requests a full close.
  Unlike the historical runner, live cannot use a pre-gap historical price, so
  it closes on the first eligible fresh quote under the shared spread-defer
  baseline.
- Entry spread ceiling is 0.300 XAUUSD price (300 points at point 0.001).
- Shadow proxy applies the Base assumption of 0.030 adverse price slippage at
  both entry and close. Live satellite selection also requires a 0.030 price
  buffer before submitting close, while broker-confirmed fills remain canonical.

## Recorded results

| dataset | Base PnL | Stress PnL |
| --- | ---: | ---: |
| dev tick | +496.708 | +362.859 |
| leakcheck tick | +688.470 | +654.6365 |
| post-leak previously unseen tick | +408.564 | +393.722 |

V23 exceeded the previous bot25 man_231 result in all six dataset/cost cells,
with fewer entries and lower every-tick MTM MDD in each dataset. All three
recorded segments were positive in Base and Stress. These are
backtest/forward-tick observations, not proof of live profitability. Real
operation was explicitly authorized on 2026-08-27. Params and the independent
Compose acknowledgement are enabled, while runtime preflight remains
fail-closed until bridge/account/hedging/ownership checks succeed.

The original dev note recorded `dev_rejected_user_quality_floor`; the later
post-leak note returned it only to deepening and explicitly did not authorize
live use. Bot25 source porting is based on the user's subsequent explicit
instruction, not on a clean reusable-evaluation or live-promotion gate.

Operational hardening does not alter the frozen V23 entry/add/close rules or cost
assumptions. Bot23-equivalent account identity, order reservation/recovery,
close retry, strict causal logging, and log rotation are live safety layers; the
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
