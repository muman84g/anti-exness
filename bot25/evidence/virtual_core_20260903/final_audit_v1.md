# Final local audit: bot25 V24 virtual bilateral core

Status: local PASS; CentOS/runtime operation NO-GO.

## Candidate identity

- Strategy: `bot25_v24_xauusd_virtual_bilateral_core_v001`
- Params hash: `788e7b076cd49f67cc0f2f87677f350b8ac88bcc13b15bd26cde7589105cca36`
- Source hash: `live_s25_bot.py` = `69406de3f3f61cddcbb7b7c218c8399ddbc6dd96b1b737339fde000c91000d52`
- Params file hash: `s25_params.json` = `9e6c8f7690a320849f877c7f9b3691720f4ea232cfae3fe4451cb0e76117b5f0`

## Audit findings

- Virtual episode start contains no `_open_position` call.
- Preflight enforces one virtual core per side and zero physical seed orders.
- Capacity and ratio use logical counts; broker inventory and MTM remain real-only.
- Release selects all profitable real tickets newest-first.
- Empty-real-inventory episodes retain causal feed-gap and 12-hour reset behavior.
- Non-flat V23 state or broker inventory cannot use the compatible V24 migration.
- Params remain shadow-only.

## Test evidence

- Explicit Python syntax compile: PASS.
- `live_s25_bot.py --self-test`: PASS.
- `test_s25_passive_evidence.py`: PASS.
- Static AST/params invariant check: PASS.
- Evidence work-state v1 validation: PASS.

## DEV evidence

Run: `C:\Users\muuma\Documents\Codex\2026-09-02\bot\outputs\bot25_v24_virtual_core_dev_20260903_v4`.

- Input: 21,258,719 DEV ordered XAUUSD Bid/Ask ticks.
- Base: V23 496.708 / PF 1.0439 / MDD 421.121; V24 568.502 / PF 1.0847 / MDD 414.129.
- Stress: V23 362.859 / PF 1.0319 / MDD 443.513; V24 473.250 / PF 1.0700 / MDD 422.161.
- Frontier entries 649, release events 1084, productive closes 128, and blocked adds 149 match exactly.
- Removed physical seeds: 270. Max real inventory falls from 12 to 10; logical max remains 12.
- Input, runner, V23 equity source, PnL delta, terminal equity, and artifact accounting checks: PASS.
- Equity image visual inspection: PASS.
- Reusable evaluation, Leakcheck, and Forward were not used.

## Residual boundary

No broker commission/swap, real fill delay, or rejection is present in the DEV price replay. No CentOS copy, restart, Compose action, EA operation, broker action, push, or state mutation was performed. Existing live V23 inventory must be flat before any future V24 transition.
