# 2026-09-04 live configuration and Git delivery audit

## Scope and decision
User authorized live configuration after confirming restart with existing positions.
Canonical bot25 is V24, live_trading_enabled=true, shadow_forward_enabled=false.
Compose defaults BOT25_ENABLE_REAL_TRADING to V24_VIRTUAL_CORE_LIVE_ACK.
An explicit conflicting environment override still blocks startup.
This is configuration authorization, not proof of a running CentOS instance.

## Retained-position contract
Exact state-v5/man231 or state-v6/V23 inventory can be adopted into V24 and restarted with no seed OPEN.
Ownership, ticket/identifier, side, volume and broker time must agree; pending lifecycle actions, ambiguous state or orders block migration.
Existing physical cores substitute for virtual cores until confirmed closure; no double count.
Current V24 restart retains its logical episode. Frontier adds alone create new real positions.
Do not delete, reset or upload local state over server state.

## Verification of this configuration
- bot25: 35 unit/regression tests PASS, including exact non-flat takeover/restart and activation gate.
- bot24: 189 tests PASS.
- bot23: 541 tests run, 540 PASS, 1 skipped.
- All three runner self-tests PASS.
- Isolated clone with dummy account configuration and isolated IPC; no real credentials or bridge used.
- First bot23/24 run lacked dummy startup.ini; corrected test environment only.
- A bot23 rerun incorrectly disabled logging and broke assertLogs tests; restored logging semantics and full suite passed.
- Docker executable unavailable: Compose runtime validation is not claimed. Target service/mount/gate source reviewed.
- No new strategy optimization or profitability validation.
- Original historical manifests remain historical: live mode/self-test/test changes invalidate their exact current-snapshot applicability. They are not relabeled as release proof.

## Current canonical byte hashes (SHA-256)
- live_s25_bot.py: 97883e3dbe399a0d6a53190ee6fcb7a09dd0305eeb3d8a33079a6606914eb03f
- s25_params.json: 06373fab44973dc50701729769243c627a196d11a18b5909a0bf1609aa0bac56
- test_s25_v24_virtual_core.py: 4090fe340ef8ce1ad42ed72710b545ef51bd9265732b92c05e105368086de1e
- docker-compose.yml: b973b9c81dbbb853518344a6aba683c61cf57f281e60351a720b6a2f25c7fa19
- canonical state remains a8d97856495c30ad0370c0e78ccd7e236fd8eca59e8b4f89d13bddfe1ff2c15c.
Git text blobs may normalize line endings; compare like-for-like hashes.

## External boundary
Git target: muman84g/anti-exness main, baseline a749784fd028ac183ee7bc78ca05efb0a3a055fd.
Push only after staged-name/token scan, diff check, exact local-copy comparison and fresh remote-head check.
Do not force push. If remote advances, stop and reconcile.
CentOS placement, restart, EA attach, state mutation and actual orders were not performed.
Remote paths, inventory, process count, account and EA CAPS remain unverified.
Bot25 requires bridge 2026-09-04-s25-v24-atomic-v8; an old bridge must not be treated as ready.
Removed bot25 signal_adapters.py and timeframe_config.py are intentionally retired unused modules, preserved in Git history and prior local archive.

## Selected Git changes before this report
```text
M	BOT_BACKTEST_MAP_ja.md
M	bot23/PORTING_AUDIT.md
A	bot23/PRE_EU30_ADOPTION_AUDIT_20260828.json
A	bot23/PRE_EU30_POST_ADOPTION_AUDIT_20260829.json
M	bot23/README.md
M	bot23/SOURCE_BACKTEST.md
M	bot23/live_s23_bot.py
M	bot23/s23_params.json
A	bot23/test_s23_close_durability.py
M	bot23/test_s23_regressions.py
M	bot24/README.md
M	bot24/SOURCE_BACKTEST.md
M	bot24/live_s24_bot.py
M	bot24/test_s24_bridge_contract.py
A	bot24/test_s24_close_durability.py
M	bot24/test_s24_safety_regressions.py
M	bot24/v206_live_lane.py
M	bot25/BotBridge_s25.mq5
M	bot25/LOG_SCHEMA.md
M	bot25/PORTING_AUDIT.md
M	bot25/README.md
M	bot25/SOURCE_BACKTEST.md
M	bot25/V24_VIRTUAL_CORE_SPEC.md
M	bot25/ea_bridge.py
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_inventory_v2.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_inventory_v2.validation.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_pending_v2.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_pending_v2.validation.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_recovery_v2.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_recovery_v2.validation.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_repeat_v2.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_repeat_v2.validation.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_submission_v2.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_submission_v2.validation.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_v1.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_v1.validation.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_v2.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_v2.validation.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_v3.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_v3.validation.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_wave_v2.json
A	bot25/evidence/full_reaudit_20260904/evidence_work_state_wave_v2.validation.json
A	bot25/evidence/full_reaudit_20260904/final_audit_v1.md
A	bot25/evidence/full_reaudit_20260904/final_audit_v2.md
A	bot25/evidence/full_reaudit_20260904/inventory_audit_final.md
A	bot25/evidence/full_reaudit_20260904/inventory_audit_plan.md
A	bot25/evidence/full_reaudit_20260904/no_change_reaudit_20260904.md
A	bot25/evidence/full_reaudit_20260904/pending_audit_final.md
A	bot25/evidence/full_reaudit_20260904/pending_audit_plan.md
A	bot25/evidence/full_reaudit_20260904/recovery_transaction_final.md
A	bot25/evidence/full_reaudit_20260904/recovery_transaction_plan.md
A	bot25/evidence/full_reaudit_20260904/repeat_audit_plan.md
A	bot25/evidence/full_reaudit_20260904/submission_audit_final.md
A	bot25/evidence/full_reaudit_20260904/submission_audit_plan.md
A	bot25/evidence/full_reaudit_20260904/wave_handoff_final.md
A	bot25/evidence/full_reaudit_20260904/wave_handoff_plan.md
A	bot25/evidence/virtual_core_20260903/RUNTIME_SEED_INCIDENT_AUDIT_20260904.md
A	bot25/evidence/virtual_core_20260903/evidence_work_state_v3.json
M	bot25/live_data_fetcher.py
M	bot25/live_executor.py
M	bot25/live_s25_bot.py
M	bot25/passive_evidence_io.py
M	bot25/s25_params.json
M	bot25/shadow_opportunity_observer.py
D	bot25/signal_adapters.py
A	bot25/test_s25_execution_boundary.py
A	bot25/test_s25_v24_virtual_core.py
D	bot25/timeframe_config.py
M	docker-compose.yml
```
This report itself is also included. Historical bot23 evidence snapshots not needed by runtime remain local.

## CentOS handoff (not executed)
Preserve current state and positions; confirm correct account, exact ownership and no unresolved migration before activation.
```bash
cd /home/muu/python_program/anti-exness
git pull --ff-only origin main
```
If local changes block pull, stop and back up/reconcile them; do not reset or overwrite state.
Ensure bot23, bot24 and bot25 live_config.py and startup.ini are regular files for the intended account. They are excluded from Git; do not replace existing correct files unnecessarily.
Compile/attach the required EA source versions. A git pull alone does not prove running containers use the update.
After separately authorizing target activation, run for each NN=23,24,25 (replace NN):
```bash
sudo docker compose run --rm --no-deps exness-bot-NN python3 /app/botNN/live_sNN_bot.py --self-test
sudo docker compose up -d --no-deps --force-recreate exness-bot-NN
sudo docker compose logs --tail=80 exness-bot-NN
```
Recreation can submit real orders. Confirm loaded params/live mode, host/container hashes, one runner, bridge CAPS, ownership and seed0 after recreation. If seed orders recur, stop bot25 only; no repeated restarts.
