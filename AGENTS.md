# AGENTS.md

## Scope

- Live-bot-specific Codex guidance goes here.
- Keep this file as a short router and safety checklist. Put longer procedures in `C:\botter\docs\agent\*.md` or the relevant skill.

## Core Safety

- Treat order execution, credentials, account settings, broker connectivity, deployment, and restarts as high risk.
- Do not change strategy parameters, live execution behavior, deployment files, or running service state unless explicitly requested.
- Reconfirm current running targets before live-affecting work. As of the user's 2026-07-08 note, only `bot11`, `bot18`, and `bot19` are running.
- Keep real trading disabled by default for newly ported or changed bots unless the user explicitly requests live trading.
- Do not restart, deploy, attach EA bridges, or flip live switches unless explicitly requested.

## Live Config And Login

- `live_config.py` may be changed only with explicit user authorization for live config changes.
- Never print, stage, commit, push, upload, or paste `live_config.py` contents or diffs.
- Do not edit login/account credential fields unless the user explicitly asks for those exact fields.
- Preserve `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, account IDs, and login/bootstrap initialization because the user may manually switch accounts.
- Do not edit `.env` or credentials unless explicitly authorized.

## Bot Mapping

- Use `C:\botter\bot\BOT_BACKTEST_MAP_ja.md` as the central bot/backtest mapping entry before relying on scattered README, HANDOFF, or SOURCE_BACKTEST notes.
- Update `BOT_BACKTEST_MAP_ja.md` when adding or changing a bot/backtest mapping.
- If an active bot lacks `SOURCE_BACKTEST.md`, create it before changing that bot's strategy behavior.
- Do not infer a live target from a long backtest folder name such as `backtestNN_botXX`; cross-check README, SOURCE_BACKTEST, docker-compose, and the user's latest instruction.

## Naming And Paths

- Preserve bot number naming unless the user asks to rename it: `botNN`, `sNN`, `live_sNN_bot.py`, `sNN_params.json`, `sNN_bot_state.json`, `sNN_bot.log`, and `sNN_trades.csv`.
- Preserve existing paths, compose service names, volume mounts, state paths, log paths, and bridge file names unless the user explicitly asks for a path change.
- For a new bot, use the user-specified bot number. Do not invent a bot number, service name, directory name, or file naming style.
- Keep runtime files under the target bot directory. Do not leave scratch, Downloads, or temporary absolute paths in production bot code or docs.
- Match bridge naming to the bot number: `BotBridge_sNN.mq5`, EA expert name `BotBridge_sNN`, compose mounts, README instructions, and IPC files such as `cmd_sNN.txt`, `res_sNN.txt`, `heartbeat_sNN.txt`, and `ea_bridge_sNN.lock`.

## Porting Workflow

- Use `C:\botter\.agents\skills\live-bot-porting\SKILL.md` when turning a fixed backtest candidate into `botNN` code.
- Inspect the existing target bot directory before editing it.
- Keep edits scoped to the target bot and explicitly required shared files.
- Map backtest entry, exit, add, DD, time, spread, slippage, lot, and timeframe assumptions into live code or document any mismatch in `SOURCE_BACKTEST.md`.
- Verify with no-order checks first: syntax check, import check, self-test, or safe dry run.

## Runtime Safety

- Preserve fail-closed behavior for order failures, position sync failures, state save failures, pending-open uncertainty, and reconciliation.
- A temporary MT5/EA Bridge position-list failure may block new entries for the cycle. Clear that block only after a clean later sync confirms state tickets and MT5 positions match.
- Do not automatically clear serious state problems such as unmanaged live positions, missing state tickets on MT5, failed SL/TP repair, open failure, unresolved `pending_open`, or `reconciliation_required`.
- If manual state repair is required, stop the target bot first and verify ticket, symbol, direction, lot, SL, and TP against MT5 or EA Bridge before editing state.

## Trade CSV Logging

- Live bots should log trade events to `logs/sNN_trades.csv` in addition to `logs/sNN_bot.log`.
- Include at least ENTRY, normal EXIT, failed close/order events, server-side SL detection, manual close detection, and sync-related close evidence when supported by the bot.
- For a first-time bot add, commit the generated target bot folder as-is except `live_config.py` and direct `live_config` derivatives such as `__pycache__/live_config*.pyc`.

## GitHub Push Workflow

- Commit and push only when the user explicitly requests it.
- Before committing, verify staged files with `git status --short` and `git diff --cached --name-only`.
- Never stage or commit `live_config.py`, direct `live_config` derivatives such as `__pycache__/live_config*.pyc`, `.env`, secrets, tokens, or account credentials.
- When pushing a new bot, stage only the target bot source/docs, required compose or AGENTS changes, and necessary operational notes.
- On first-time bot push, include the generated bot folder contents except the sensitive exclusions above.

## Maintenance Hygiene

- Do not only append new instructions. When a rule is obsolete, duplicated, contradicted, or unreadable, remove or replace it in the same edit.
- Keep AGENTS files concise. Move detailed procedures to `docs/agent` or skill references.
- Use UTF-8 for Markdown files. If text appears garbled, verify with Python `Path.read_text(encoding="utf-8")`; if the file itself contains mojibake, replace the corrupted block instead of preserving it.
