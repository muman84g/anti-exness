# Source Backtest

- Bot: `bot25` / S25
- Strategy: `man_231` best-price core + profitable satellite release
- Strategy ID: `bot25_man231_xauusd_bilateral_core_satellite_v001`
- Frozen params hash: `589b6e4924505aa177c9cf5620334ac462f6553c77393ac19577e0dc5094ed61`
- Instrument/execution: XAUUSD true ticks; decisions from completed Bid M5 bars
- Canonical specification: `backtest/検討中/chatgpt案/多重ポジ/20260827_XAUUSD_break比率wavefront/07_man231_core_satellite_固定仕様.md`
- Parent comparison: same folder, `13_man231_親P0詳細比較.md`
- Post-leak unseen check: same folder, `14_man231_postleak未確認tick結果.md`

## Frozen behavior

- Seed one BUY and one SELL and continuously restore a missing side.
- ATR14 is simple rolling true range; EMA200 uses `adjust=False`.
- A strict radius-2 pivot breaks only beyond pivot +/- 0.10 ATR.
- The active side adds after a 0.50 ATR frontier advance, at most six tickets
  per side and at most a 3:1 active/opposite count ratio.
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
| dev tick | +493.692 | +356.241 |
| leakcheck tick | +658.071 | +623.4515 |
| post-leak previously unseen tick | +371.886 | +356.754 |

All three recorded segments were positive in Base and Stress. These are
backtest/forward-tick observations, not proof of live profitability. Real
operation was explicitly authorized on 2026-08-27. Params and the independent
Compose acknowledgement are enabled, while runtime preflight remains
fail-closed until bridge/account/hedging/ownership checks succeed.

The original dev note recorded `dev_rejected_user_quality_floor`; the later
post-leak note returned it only to deepening and explicitly did not authorize
live use. Bot25 source porting is based on the user's subsequent explicit
instruction, not on a clean reusable-evaluation or live-promotion gate.

Operational hardening does not alter the frozen entry/add/close rules or cost
assumptions. Bot23-equivalent account identity, order reservation/recovery,
close retry, strict causal logging, and log rotation are live safety layers; the
semantic preservation/deletion audit is in `PORTING_AUDIT.md`.

Live realized PnL is broker-accounting truth: the bridge aggregates all deals
for the MT5 position ID so entry/exit commission, swap, and fee are included,
while the logged close price remains the final broker close-deal price. This is
more complete than treating only the exit deal's commission as round-trip cost.
