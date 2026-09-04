# Bot25 submission-boundary repeat audit

Local-only repair complete; deployment/activation NOT authorized.
Acceptance AC_LOCAL_SAFETY, AC_V24_BEHAVIOR, AC_CONVERGENCE passed for this scope.
Risk high, direct serial audit, no delegation. Evidence-gated-work governed
scope, causal repair and exact candidate verification.

Findings: reservation I/O can age an initially fresh quote before executor OPEN;
returned-ticket matching did not additionally require reserved side/comment.
Both paths are repaired. No order was sent to a broker: fake-executor tests
inject a 60-second clock delay and mismatched side/comment responses.

28 unittest tests PASS; V24 self-test, passive tests, safety self-test and
Compose parse PASS. Existing seed-zero, frontier add, migration/restart,
transactional close persistence and ambiguous-close tests remain passing.
Post-repair review: reservation cancellation is reachable only before OPEN;
already-submitted unknown outcomes still retain pending identity. A mismatched
fill is not adopted or closed. No further finding in this final scoped pass.

Changed: live_s25_bot.py, test_s25_execution_boundary.py, README.md.
No strategy thresholds, lot, exits, state schema, EA or deployment files changed.
No removed behavior except unsafe stale submission and mismatched adoption.
Before candidate: evidence_work_state_repeat_v2.json; after candidate:
evidence_work_state_submission_v2.json. Old evidence is retained without edits.
This is another release branch from immutable baseline v1, avoiding recursive
validation against mutable historical candidate paths. Not remote proof.

State before/after: lifecycle
a8d97856495c30ad0370c0e78ccd7e236fd8eca59e8b4f89d13bddfe1ff2c15c;
observer df189bff4fc97c6dca53c4c9048e19d09f8b7e26b703007ef19aa30d1d8a8a77.
No canonical state reset/migration, secrets, live positions, CentOS writes,
restart, EA attachment, real activation, or push. Real runtime remains untested.
Do not upload local flat state over server-owned open-position state.
