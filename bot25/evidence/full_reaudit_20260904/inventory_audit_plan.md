# Complete inventory audit

Bot25 local high-risk repair, serial, no delegation. Evidence-gated-work applies.
No secrets, real orders, canonical state reset, other bots, remote actions or push.
Baseline evidence_work_state_recovery_v2.json. Preserve fixed V24 parameters.
Acceptance: post-OPEN inventory must contain no same-magic foreign rows and all
preexisting state positions must still match exact ownership; pending adoption
must not consume its identity while unexplained extra inventory exists.
Source cause: new_owned filters foreign records out before success checks;
known position ownership is not checked in the post-OPEN branch; pending adoption
occurs before the outer extra-inventory check. Reproduce, fix, run full regression.
Estimate 15-30 minutes. No deployment/readiness inference from fake-broker tests.
