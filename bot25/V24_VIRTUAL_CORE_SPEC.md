# Bot25 V24 virtual bilateral core fixed specification

Parent identity: `bot25_v23_xauusd_drought_minority_pause_v001`
Parent hash: `12dc94c78f5fb6bb01710e40a8f5f199af472f2323ab0f2bb02063fda427ca10`
Candidate identity: `bot25_v24_xauusd_virtual_bilateral_core_v001`
Candidate parameter hash: `788e7b076cd49f67cc0f2f87677f350b8ac88bcc13b15bd26cde7589105cca36`

The hash is SHA-256 of this canonical UTF-8 JSON object:

```json
{"change":"physical_bilateral_seeds_to_virtual_core","drought_minutes":120,"episode_minutes":720,"frontier_add_atr":0.5,"max_active_to_opposite_ratio":3,"max_logical_positions_per_side":6,"parent_hash":"12dc94c78f5fb6bb01710e40a8f5f199af472f2323ab0f2bb02063fda427ca10","physical_seed_orders":0,"release":"all_profitable_real_tickets_lifo","virtual_core_per_side":1}
```

## Fixed delta

- An eligible episode starts with logical counts LONG=1 and SHORT=1, but with
  zero broker positions and zero orders.
- Virtual cores are never valued and can neither gain nor lose. They incur no
  spread, slippage, commission, fee, or swap.
- Every frontier add remains a real 0.01-lot broker position at the frozen V23
  0.50 ATR threshold and cost rules.
- Capacity and the 3:1 gate use `real_count + 1` on each side. The logical side
  maximum remains six, so the real side maximum is five.
- Opposite-break and EMA200-retouch releases close every profitable real ticket
  on the active side, newest first. There is no protected physical ticket.
- Productive-close drought, pivot/EMA features, spread gates, feed-gap handling,
  12-hour expiry, ownership, close confirmation, and retry rules are unchanged.
- A virtual-only episode still expires or resets after a feed gap.

## Migration and release boundary

- V23 state-v6 may become V24 state-v7 only when stored positions, pending open,
  pending close, broker-owned positions, and broker-owned orders are all empty.
- Non-flat V23 inventory is left untouched and startup fails closed.
- The checked-in V24 params are shadow-only. Deployment and real activation
  require a separate explicit decision.
