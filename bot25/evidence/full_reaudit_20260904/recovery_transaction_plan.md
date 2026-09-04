# Recovery transaction audit

Scope bot25 local only, high risk, serial, no delegation. No secrets, canonical
state reset, real orders, remote changes or push. Evidence-gated-work applies.
Baseline evidence_work_state_pending_v2.json; preserve all earlier repairs.
Acceptance: pending recovery cannot lose its ledger/state identity after a write
failure, including replacement already visible; retries emit one recovery row.
Source evidence: _reconcile_pending_open mutates positions and clears pending
before _trade_row/_save_state. Apply existing reconciliation transaction to both
adoption and proven-flat consumption; keep prior order/ownership gates intact.
Estimate 15-30 minutes. Test failure injection, restart, multiple inventory and
all existing regression tests. No strategy or schema redesign.
