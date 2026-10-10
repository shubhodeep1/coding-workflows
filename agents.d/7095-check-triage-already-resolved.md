<!-- agents: section="Workflow architecture" -->
Check-triage already-resolved gate (#7095): clarify's `Decide clarify route`
step runs `scripts/check_triage_resolution.py gate` for a trusted
`ai:check-triage` issue (after the security-dependency hold). It closes the
issue (evidence comment with `<!-- ai:check-triage-resolved:v1 issue= pr= sha= -->`,
`ai:closed`, closed as completed; step output `check_triage_resolved=true`,
`gate=check_triage_resolved`) only when GitHub shows the linked PR merged and
the same check, or for workflow-run triage every failed job name, succeeded on
a default-branch commit that contains the merge commit. Unverified evidence
keeps the normal path and posts the failing job's traceback once
(`<!-- ai:check-triage-traceback:v1 issue= job= -->`); both markers count only
from the GH_PAT account. A write failure after verification exits 3 and fails
the step. Kill switch `CHECK_TRIAGE_RESOLVED_GATE_ENABLED` (default `true`).

<!-- agents: section="Stable log prefixes (contractual)" -->
- `CHECK_TRIAGE_RESOLVED` (`scripts/check_triage_resolution.py`: `issue= pr= outcome=not_triage|disabled|already_resolved|resolved|unverified|write_failed reason= evidence_sha= traceback_jobs=`)
LOG_PREFIX.name=CHECK_TRIAGE_RESOLVED
