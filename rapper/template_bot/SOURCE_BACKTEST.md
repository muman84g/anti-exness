# Source Backtest

Fill this file before using the template as botNN.

## Mapping

- Bot: `botNN`
- Service: `exness-bot-NN`
- Strategy ID: `botNN_template_strategy`
- Source backtest folder:
- Candidate/spec:
- Params hash:
- Symbol(s):
- Signal timeframe:
- Execution timeframe:
- History source: `DIRECT_HIST` or `M1_RESAMPLE`

## Fixed Results

- Data boundary: dev / forward / reusable evaluation status
- PnL:
- PF:
- Trades / baskets:
- Max DD:
- Worst window:
- Cost stress:
- Monthly concentration:

## Live Alignment

- EA HIST timestamp basis: UTC unless directly verified otherwise.
- Latest signal bar dropped: yes/no
- Signal available at: bar close only
- Entry due: `signal_bar_time + signal_timeframe`
- Stale guard:
- Price side: long Ask / short Bid, exits by strategy
- Spread / slippage / deviation:
- SL / TP:
- Position/order lifecycle:

## Known Differences

Document every backtest/live difference here before live or shadow deployment.
