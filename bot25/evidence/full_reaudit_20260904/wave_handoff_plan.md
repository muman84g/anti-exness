# Wave handoff crash audit

Local bot25 repair, high risk, serial, no delegation. Evidence-gated-work scope:
no secrets, canonical state reset, remote action, orders, restart or push.
Baseline evidence_work_state_inventory_v2.json. Preserve V24 strategy rules.
Source finding: _release_active_side stores pending_post_close_action only after
_close_positions returns. A process failure after broker close but before return
loses the intended new wave. Persist the intended action with validated close
reservation before the first broker mutation. Ownership rejection must not arm it.
Acceptance: crash/restart preserves wave handoff, seed zero and prior close guards;
test exact ownership rejection, normal completion and all existing regressions.
