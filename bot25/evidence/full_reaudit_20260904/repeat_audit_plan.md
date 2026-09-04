# Repeat local audit

Scope: bot25 local code only; no runtime, secrets, canonical state, deploy or push.
Baseline: evidence_work_state_v2.json candidate manifest; all candidate hashes
matched at this pass start. Risk high. Serial audit, no delegation.
Acceptance: preserve V24 seed=0/frontier-only behavior; no lost or duplicated
confirmed close after ledger/state failure; preserve exact ownership and restart.
Angles: production call path, persistence failure boundaries, release identity.
Estimate: 20-50 minutes; stop for user authority only if runtime work is required.
Finding: _sync_strategy replaces state.positions before operational close CSV
append. An append exception leaves consumed memory available to later saves.
Fix scope: transactional close consumption and deterministic retry evidence.
Unchanged: thresholds, lot, core count, strategy exits, real activation, bridges.
