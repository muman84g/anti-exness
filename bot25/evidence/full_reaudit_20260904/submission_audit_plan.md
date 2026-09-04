# Submission-boundary audit

Local bot25 only. High risk; serial, no delegation. No deployment, orders,
secrets, state reset or live activation. Preserve fixed V24 trading rules.
Baseline is evidence_work_state_repeat_v2.json; prior corrections preserved.
Acceptance: no OPEN after admission quote ages out during reservation; confirmed
position must match reserved direction/comment/lot and returned ticket/identifier.
Audit angles: order call path, delayed submission, ownership/restart regression.
One focused correction batch, estimated 15-30 minutes, then full no-order suite.
Source evidence: _open_position checks entry policy before reservation I/O but
does not check host quote freshness at actual executor call; returned-ticket
matching checks generic ownership, not reservation side/comment.
