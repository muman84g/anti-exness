# Bot25 recovery transaction audit

Local repair complete; runtime deployment/activation remains NO-GO.
Scope and acceptance: AC_LOCAL_SAFETY, AC_V24_BEHAVIOR, AC_CONVERGENCE;
bot25 only, high risk, serial, no delegation. Evidence-gated-work governs this
bounded repair and preservation evidence; estimate 15-30 minutes.

Source-confirmed fault: pending OPEN adoption/flat resolution consumed pending
memory before the CSV append and state save. A write failure could leave missing
recovery evidence in memory or duplicate rows after restart/retry.
Fix: extend the existing single-commit reconciliation transaction to both paths.
Preserve in-memory references on rollback, adopt exact completed durable state
after a late commit exception, and deduplicate recovery rows by stable identity.
No broker mutation occurs inside these transactions. Orders-availability,
consecutive-clean-query and ownership guards are preserved.

30 unittest tests PASS; new regression covers adoption and flat resolution,
each with ledger failure, pre-replace failure and post-replace exception.
Each subcase proves retry emits one recovery row, consumes pending correctly,
preserves expected position count and loads the completed state after restart.
V24, passive, live-safety self-tests and Compose parse PASS. Existing close
transaction, migrated-core, seed-zero, frontier-add, ownership, quote freshness
and ambiguity tests remain passing. No MQL rebuild claimed (EA unchanged).
Final inspection of the modified paths found no further issue in this scope;
this is not proof of absence of every possible live defect.

Changed candidate files: live_s25_bot.py, test_s25_execution_boundary.py,
README.md. No file/state field/strategy condition removed. Previous repairs
are extended, not reverted. Prior hashes: evidence_work_state_pending_v2.json;
current hashes: evidence_work_state_recovery_v2.json (immutable-v1 release branch).
Historical branches remain unchanged, including their recorded limitations.

Canonical state hashes match the previously recorded values:
lifecycle a8d97856495c30ad0370c0e78ccd7e236fd8eca59e8b4f89d13bddfe1ff2c15c;
observer df189bff4fc97c6dca53c4c9048e19d09f8b7e26b703007ef19aa30d1d8a8a77.
No secrets, canonical state reset/migration, broker orders or close, CentOS
placement, restart, bridge attach, live activation or push. Actual deployed
source and inventory remain unverified. Do not upload local flat state over
server-owned positions. Remote action requires separate fresh proof/approval.
