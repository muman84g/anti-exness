# Bot25 complete inventory audit

Local repair complete; runtime deployment/activation remains NO-GO. High-risk
serial audit using evidence-gated-work; no delegation. Acceptance AC_LOCAL_SAFETY,
AC_V24_BEHAVIOR and AC_CONVERGENCE passed for the local covered paths.

Before repair, three reproduced failures: OPEN returned success despite an extra
same-magic foreign-comment position; OPEN returned success despite prior position
volume drift; pending recovery consumed its reservation before noticing a second
unexplained position. These were failures of whole-inventory proof, not strategy.
Fix: post-OPEN checks every returned position's namespace and every prior saved
position's exact ownership. Pending adoption requires one unknown position total
as well as one reservation match. Anomaly preserves pending/state and blocks new
orders, without closing or adopting unexplained inventory.

32 unittest cases PASS, including the three reproduced anomaly subcases. V24,
passive and live-safety self-tests PASS; default Compose parse PASS. Earlier
clock, close transaction, recovery transaction, seed-zero, frontier-only and
restart tests remain passing. EA/params unchanged; no MQL rebuild claimed.
Final review of post-OPEN, rejection and restart recovery paths found no further
issue in the covered scope. No claim of universal or live-runtime defect absence.

Changed: live_s25_bot.py, test_s25_execution_boundary.py, README.md. No strategy
threshold/lot/exit/state field or file removed. Before hashes are in
evidence_work_state_recovery_v2.json; after hashes in
evidence_work_state_inventory_v2.json, a branch from immutable baseline v1.
Old evidence and corrections remain preserved. Estimate 15-30 minutes; serial
local checks only. No other bots, secrets, canonical state reset/migration,
broker orders/close, CentOS placement, restart, EA attach, live activation or push.

Canonical state hashes match prior evidence:
lifecycle a8d97856495c30ad0370c0e78ccd7e236fd8eca59e8b4f89d13bddfe1ff2c15c;
observer df189bff4fc97c6dca53c4c9048e19d09f8b7e26b703007ef19aa30d1d8a8a77.
Remote running source, processes and inventory remain unverified. Do not upload
local flat state over server-owned positions; separate runtime proof is required.
