# Pending recovery audit

High-risk bot25 local repair; no other bot, secrets, canonical state, runtime,
deployment, orders, restart or push. Preserve V24 seed zero and frontier rules.
Baseline: evidence_work_state_submission_v2.json. Serial bounded audit, no agents.
Acceptance: pending OPEN cannot clear on non-consecutive clean observations;
an intervening failed query must reset confirmation count without removing intent.
Review: state shape, pending recovery, IPC completion and full regression.
Estimate 15-30 minutes; no remote authority inferred. Evidence-gated-work applies.
Source finding: _sync_strategy returns on failed query without resetting the
pending_open.flat_confirmation_count consumed by _reconcile_pending_open.
