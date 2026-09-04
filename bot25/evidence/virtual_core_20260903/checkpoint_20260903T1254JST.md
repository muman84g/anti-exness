# Safe checkpoint

- Local candidate: `bot25_v24_xauusd_virtual_bilateral_core_v001`
- Candidate hash: `788e7b076cd49f67cc0f2f87677f350b8ac88bcc13b15bd26cde7589105cca36`
- `live_s25_bot.py`: `025ba4f9ce4a501bb35c02395339997d06a760d9a9623229c1cdf6d7a2f6b472`
- `s25_params.json`: `9e6c8f7690a320849f877c7f9b3691720f4ea232cfae3fe4451cb0e76117b5f0`
- Python compile: PASS
- V24 self-test: PASS
- Passive evidence test: PASS
- DEV fixed comparison: complete in `outputs/bot25_v24_virtual_core_dev_20260903_v2`
- Base: PnL 568.502, PF 1.0847, every-tick MTM MDD 414.129, 649 real entries.
- Stress: PnL 473.250, PF 1.0700, every-tick MTM MDD 422.161, 649 real entries.
- V23 path parity: frontier entries 649, release events 1084, productive waves 128,
  blocked adds 149 all unchanged.
- Removed physical seed entries: 270.
- Operation status: NO-GO. Params are shadow-only; no deploy, restart, push,
  broker action, Leakcheck, or Forward was performed.
- Failed run v1 is retained and marked invalid; v2 is the completed result.
