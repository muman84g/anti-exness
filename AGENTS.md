# AGENTS.md

## Scope

- Inherit `C:\botter\AGENTS.md`.
- Keep this file as the live-bot router and high-risk safety boundary.
- Read `C:\botter\docs\agent\live-bot-safety.md` before any live-bot change.

## Required Sources

- Use `C:\botter\bot\BOT_BACKTEST_MAP_ja.md` as the central bot/backtest mapping entry.
- Verify the target from the bot README, `SOURCE_BACKTEST.md`, compose service/mounts, and the user's latest instruction. Do not infer it from a backtest folder name.
- Use `C:\botter\.agents\skills\live-bot-porting\SKILL.md` when porting or replacing strategy behavior.
- Use `C:\botter\bot\githubへのpush.txt` only when the user explicitly requests commit or push work.

## Authorization Boundary

- Do not change strategy parameters, execution behavior, deployment files, running services, bridges, or live switches unless explicitly requested.
- Treat `live_config.py`, `.env`, credentials, account IDs, and login/bootstrap fields as sensitive. Edit only the exact authorized class of fields and never print or commit secret values.
- Do not restart, deploy, attach an EA bridge, recreate a service, or enable real trading without explicit authorization.
- Reconfirm the currently running target from current evidence before any live-affecting action; dated docs are not runtime truth.

## Mapping And Naming

- Preserve bot number, strategy ID, magic, symbol, service, volume mount, bridge, state, log, and IPC names unless the user asks to change them.
- Update `BOT_BACKTEST_MAP_ja.md`, README, params, runner notes, and `SOURCE_BACKTEST.md` together when their mapping changes.
- Document every unavoidable backtest/live mismatch in `SOURCE_BACKTEST.md`.
- For local virtual-grid bots that execute market orders, treat virtual levels as trigger/state identity. Recalculate actual market entry and SL/TP from the current tick at every send/retry unless the strategy source explicitly requires fixed absolute prices.
- For breakout-style local virtual grids, a stop loss that leaves the symbol flat invalidates the old trigger set. Clear remaining virtual orders, apply cooldown, and reanchor from current market state instead of immediately re-entering from the same trigger. If a flat stale breakout trigger is crossed only after excessive drift, clear/reanchor instead of sending a late market order.

## Verification

- Keep new or changed bots non-live by default unless the user explicitly authorizes real trading.
- Run no-order checks first: syntax, import, self-test, policy/artifact test, or safe dry run.
- For live-bot behavior changes, validate preserved behavior explicitly. Review removed code, state fields, wait states, retries, alerts, and mounts before commit/push; explain every intentional deletion.
- Design live-bot recovery for autonomous operation. Stopping or requiring manual action is a last resort for ambiguous or unsafe states, not the default fix for predictable price drift, stale orders, retryable broker errors, or recoverable state mismatch.
- Keep temporary entry-block recovery reason-aware. Clear `sync_block_new_entries` only after the matching clean-sync recovery condition is proven; do not leave recoverable blocks stale, and do not clear unresolved reconciliation or ambiguous exposure.
- Preserve fail-closed behavior for order, sync, state, pending-open, and reconciliation failures.
- Do not manually repair state while the target bot is running. Verify ticket, symbol, direction, lot, SL, and TP before an authorized repair.
- Report changed files, checks run, and explicitly state whether deploy, restart, or live switching was not performed.
- After any successful GitHub push for live-bot files, report the target-specific CentOS follow-up steps from `C:\botter\bot\githubへのpush.txt`, and state that they were not run unless separately authorized.

## Temporary Work

- Do not create clones, worktrees, backups, or scratch folders under `C:\botter` or `C:\botter\bot`.
- Use `C:\Users\muuma\Downloads\codex-temp\...` and report any created path so it can be cleaned later.
