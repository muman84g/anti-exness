# Bot25 pending-recovery audit

Local-only corrective audit complete. Runtime deployment remains NO-GO.
Evidence-gated-work controls scope, identity and causal correction. High risk,
serial bounded pass; no delegation. AC_LOCAL_SAFETY, AC_V24_BEHAVIOR and
AC_CONVERGENCE pass for the declared local scope.

Reproduced before repair: first clean inventory, failed POSITIONS, next clean
inventory retained an old count; failed ORDERS was worse, being passed as an
empty list and clearing pending OPEN immediately on the second observation.
The new regression failed in both subcases before correction.

Correction: unsafe/interrupted observations reset the pending confirmation count
even if a stronger block reason is retained. Pending reconciliation requires
explicit ORDERS availability. Failed ORDERS never counts as empty. No intent,
existing position or unresolved broker result is deleted by the failure branch.

After repair: 29 unittest cases PASS, including both new failure subcases and
clean two-observation recovery. V24, passive and live-safety self-tests PASS;
Compose parse PASS. Previous seed-zero, frontier-only, ownership, restart,
submission-clock and close-transaction tests remain passing. Final source review
found no additional issue in the pending recovery paths covered by this pass.
This does not prove all possible runtime defects absent.

Changed: live_s25_bot.py, test_s25_execution_boundary.py, README.md.
No thresholds, lots, exit rules, EA, deployment files or state schema removed.
Prior source hashes: evidence_work_state_submission_v2.json. Current hashes:
evidence_work_state_pending_v2.json. This release branch references immutable
baseline v1; historical reports and failed branches remain unmodified.

Canonical hashes match the previously recorded state:
lifecycle a8d97856495c30ad0370c0e78ccd7e236fd8eca59e8b4f89d13bddfe1ff2c15c;
observer df189bff4fc97c6dca53c4c9048e19d09f8b7e26b703007ef19aa30d1d8a8a77.
No secrets, canonical-state reset/migration, broker orders/closures, CentOS write,
restart, real activation, bridge attachment or push. Actual deployed source and
broker inventory were not inspected. Do not overwrite a nonflat server state.
