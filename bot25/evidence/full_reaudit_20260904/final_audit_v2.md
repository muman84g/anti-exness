# Bot25 repeat local audit — 2026-09-04

Decision: local repair complete; runtime deployment/activation remains NO-GO.
Work definition and scope remain bot25_full_local_reaudit, AC_LOCAL_SAFETY,
AC_V24_BEHAVIOR, AC_CONVERGENCE. Prior manifest v2 candidate hashes matched at
entry. This pass does not establish actual CentOS process/file/position identity.

## Findings and corrections

1. Close consumption preceded the operational CSV write. A write exception could
   leave an unlogged close removed in memory. The close-consumption phase now
   restores prior state on failure and defers helper saves to one final commit.
   If atomic replacement succeeded before an exception, exact durable-state
   comparison retains that completed state instead of rolling back memory.
2. CSV retry deduplication ignored ownership conflicts; productive-close records
   lacked a stable retry key. Ownership/accounting conflicts and duplicate durable
   identities now fail closed; productive rows use episode plus sorted position IDs.
3. A stale quote prevented even read-only close-deal reconciliation. Reconciliation
   now continues while entries and quote-timed processing remain blocked.
4. A resolved ambiguous close retained its non-recoverable block. It clears only
   when the marker-bearing ticket is resolved in the current pass and no marker
   remains. Missing ORDERS visibility becomes a recoverable entry block, not an
   unguarded clearance. The first added test reproduced the retained-block fault;
   correction and rerun passed. Multi-ticket unresolved remainders stay blocked.
5. Confirmed deals are processed in broker-time order; the last productive-close
   clock therefore cannot move backwards within a reconciliation batch.

## Verification and convergence

- 26 unittest cases PASS, including six added tests and three injected commit
  failure subcases: ledger failure, pre-replace failure, post-replace exception.
- V24 self-test, passive evidence tests, live-safety self-test: PASS.
- Default Docker Compose configuration parse: PASS.
- Seed=0, frontier-only real add, migrated-core restart regressions remain PASS.
- No MQL/EA/IPC/params changes this pass; no new MQL compilation claimed.
- Final source review covers close consumption, in-memory references, single
  commit boundary, late-commit recovery, dedupe, unresolved remainders, and stale
  quote behavior. No additional in-scope finding in this post-correction pass.
- No deleted strategy behavior, files, state fields, thresholds, lot or exit
  policy. Earlier safety work is preserved, not reverted. README compile command
  was corrected for PowerShell wildcard handling.
- Canonical state before/after SHA-256:
  lifecycle a8d97856495c30ad0370c0e78ccd7e236fd8eca59e8b4f89d13bddfe1ff2c15c;
  observer df189bff4fc97c6dca53c4c9048e19d09f8b7e26b703007ef19aa30d1d8a8a77.

Changed candidate files: live_s25_bot.py, test_s25_execution_boundary.py,
README.md, LOG_SCHEMA.md. Before hashes are in manifest v2; after hashes in
evidence_work_state_repeat_v2.json (validated PASS).
The attempted v3 chain failed because historical v2 points at mutable canonical
files. It is retained as failed evidence, not release proof. The validated repeat
branch starts at immutable v1; historical hashes and reports were not rewritten.
New evidence: repeat_audit_plan.md, this report, repeat manifest and validation.
Risk remains high; checks use isolated fake-broker state, not real broker proof.
No secrets displayed or modified. No canonical state migration/reset, position
closure, push, CentOS placement, restart, bridge attachment or live activation.
The exact running CentOS source/candidate and original seed incident root cause
remain unverified remotely. Do not overwrite a server nonflat state with the
local flat state. Separate user approval and fresh runtime evidence are required.

The evidence-gated-work and live-bot-porting skills determined preservation,
transaction failure tests, identity checks and local/runtime separation.
Estimate remained within the bounded repeat audit; no external wait or delegation.
