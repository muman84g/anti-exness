# bot25 physical seed recurrence audit — 2026-09-04

## Decision

The recurrence was caused by the deployed bot25 path remaining on the physical-seed V23/man231 implementation while the V24 runtime candidate existed only in the local canonical tree. The Git deployment source still called `_ensure_bilateral_seed()`, which opened one real LONG and one real SHORT at episode start.

The local V24 runner now supports a guarded non-flat takeover. Exact state-v5/man231 or state-v6/V23 positions may continue without replacement seeds only when state and broker identity, side, lot, magic, and comment match, broker orders and pending lifecycle are empty, cap/ratio remain valid, and state CAS succeeds. Ambiguous inventory remains **NO-GO** and the old state is left byte-for-byte unchanged.

## Evidence

- User-reported CentOS path: `/home/muu/python_program/anti-exness/bot25/live_s25_bot.py`
- User-reported CentOS sizes: `live_s25_bot.py=113649`, `s25_params.json=2320`
- Git `origin/main` before correction preparation:
  - strategy: `bot25_man231_xauusd_bilateral_core_satellite_v001`
  - `live_s25_bot.py` SHA-256 over Git blob: `58d1612c7a75ed60bd9d81d5933795a237a83142105d7fe54f42792703645fc6`
  - `s25_params.json` SHA-256 over Git blob: `91e57ee5c21ea4e3f7c97d1dbea998207c01f58a8043046d0bf318b47430d718`
  - production calls: `_ensure_bilateral_seed()` -> `_open_position(... LONG ...)` and `_open_position(... SHORT ...)`
- Local canonical V24 design baseline: 8/8 files match immutable `evidence_work_state_v2.json` before the operational takeover delta.
- Local canonical V24:
  - strategy: `bot25_v24_xauusd_virtual_bilateral_core_v001`
  - `live_s25_bot.py` before non-flat takeover support: `69406de3f3f61cddcbb7b7c218c8399ddbc6dd96b1b737339fde000c91000d52`
  - `live_s25_bot.py` after non-flat takeover and push-preflight hardening: `4b4b7ad3e74fa86f5149be21d654067e1ec5bb15349257f6c8197ad689941b59`
  - `s25_params.json`: `9e6c8f7690a320849f877c7f9b3691720f4ea232cfae3fe4451cb0e76117b5f0`
  - `live_trading_enabled=false`
  - `shadow_forward_enabled=true`
  - `physical_seed_orders=0`
  - activation acknowledgement: `V24_VIRTUAL_CORE_LIVE_ACK`
- Static production-call inspection found `_open_position()` only inside `_process_m5_event()` after frontier, capacity, ratio, drought and execution gates. `_ensure_virtual_bilateral_core()` contains no broker-open call.
- One best-price migrated physical position per side temporarily substitutes for the virtual core. Logical counts are not doubled, and the virtual side activates automatically after the existing exit path closes that physical core.
- Shadow-only canary verifies an exact state/broker inventory match without reconciliation; it cannot add, close, simulate, or mutate the canonical position lifecycle.

## Local changes

- `C:\botter\bot\docker-compose.yml`
  - removed the literal legacy `MAN231_LIVE_ACK`
  - bot25 activation now defaults empty
  - before SHA-256, reconstructed from the exact two replaced lines: `35eb7cbfac0e08e68f5bf262ecb503325eccf88370badb7a7b8930ce9836fab5`
  - local canonical full-file SHA-256: `4996ad16aeff1fb305d188a101d52a6fc0f3a883463743db6492fff30050b6cc`
  - selected push-candidate full-file SHA-256 on origin `a749784`: `e3d3a4ca4cfaa50b0262d02f7f7062831cf3ec91b1618f996c28eb8ecc8725d0`
  - unrelated local Compose changes are excluded from the Git candidate
- `C:\botter\bot\bot25\test_s25_v24_virtual_core.py`
  - new and extended regression test
  - SHA-256: `f0a8ed4b548ffc95b7e82fc3c5178fe604f8ab144238eb8e7e291e5eb20c0f83`
- Documentation updated: `README.md`, `PORTING_AUDIT.md`, `SOURCE_BACKTEST.md`, and `V24_VIRTUAL_CORE_SPEC.md`.
- `evidence_work_state_v3.json` supersedes v2 for the operational non-flat takeover change; v2 remains the immutable design baseline.

No bot23 or Q01 file was edited.

## Validation

- Python compile: PASS
- Existing V24 self-test: PASS
- Passive evidence test: PASS
- New production-boundary regression: PASS
  - initial episode: broker opens `0`, real `0/0`, logical `1/1`
  - restart: broker opens `0`, same episode restored, logical `1/1`
  - qualifying frontier add: broker opens exactly `1`, real `1/0`, logical `2/1`
  - no `bilateral_seed` trade row
  - exact state-v5 non-flat inventory: migrated with broker opens `0`
  - migrated physical core: no virtual-core double count
  - broker-confirmed migrated-core close: existing sync/exit evidence path retained and only the closed side becomes virtual
  - restart after non-flat migration: broker opens `0`
  - shadow canary with real inventory: matching and mismatching checks leave the position lifecycle byte-for-byte unchanged
  - pending open, broker lot mismatch, pending order, duplicate broker ID, malformed value, or extra strategy key: migration refused and old state bytes preserved
- Compose parse: PASS
- Canonical state SHA-256 before and after validation: `a8d97856495c30ad0370c0e78ccd7e236fd8eca59e8b4f89d13bdDFE1FF2C15C` (unchanged; hexadecimal case is not significant)

## External actions

- CentOS file placement: not performed
- CentOS file modification: not performed
- bot25 restart: not performed
- state migration/reset/overwrite: not performed
- broker close: not performed
- real trading re-enabled: not performed
- Git push: not performed

The actual CentOS file SHA-256, process count, mounts, bridge CAPS and open-position magic/comment/open times were not available in the user-provided directory listing and remain unverified.
