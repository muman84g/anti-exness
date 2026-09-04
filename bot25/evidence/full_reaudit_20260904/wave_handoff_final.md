# Bot25 wave handoff crash audit

Local corrective audit complete; runtime deployment/activation remains NO-GO.
High risk, serial, no delegation. Evidence-gated-work governs scope, causal repair
and identity. AC_LOCAL_SAFETY, AC_V24_BEHAVIOR, AC_CONVERGENCE passed locally.

Reproduced failure: _release_active_side stored the next wave only after broker
close submission returned. A failure after close submission lost the intended
transition while preserving close intent. The new restart test failed before
repair, with pending_post_close_action missing.
Fix: pass the intended handoff into _close_positions and persist it after exact
ownership validation, together with close reservation, before any broker command.
Remove the later redundant save. No strategy rule or close selection changed.

34 unittest cases PASS. New tests prove restart after simulated post-close crash
restores the intended wave and that foreign ownership rejection cannot arm a
handoff or send CLOSE. V24, passive, safety self-tests and Compose parse PASS.
Prior seed-zero, frontier-only, transactional persistence, pending recovery,
inventory and quote-clock regressions remain passing. EA unchanged, not rebuilt.
Final review covered the handoff reservation, blocked submission, partial close,
restart reconciliation and shadow immediate-completion connections. No further
finding in this final covered-path pass; no universal live-safety claim.

Changed: live_s25_bot.py, test_s25_execution_boundary.py, README.md.
Before hashes: evidence_work_state_inventory_v2.json. After hashes:
evidence_work_state_wave_v2.json, a branch from immutable baseline v1.
Earlier branches/corrections retained unchanged. No thresholds, lots, exit rules,
state fields or files removed. Only the late redundant handoff write was moved.

Canonical state hashes match prior evidence:
lifecycle a8d97856495c30ad0370c0e78ccd7e236fd8eca59e8b4f89d13bddfe1ff2c15c;
observer df189bff4fc97c6dca53c4c9048e19d09f8b7e26b703007ef19aa30d1d8a8a77.
No secrets, canonical state reset/migration, broker orders/close, other bots,
CentOS deployment, restart, EA attachment, real activation or push. Remote
running source and inventory remain unverified. Do not overwrite server-owned
positions with local flat state. Estimated 15-30 minutes; bounded local pass.
