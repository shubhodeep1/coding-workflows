<!-- agents: section="Workflow architecture" -->
`PR_MERGE_GUARD_DOC_SYNC_UNSET_OPERATOR_STEP` is an unset placeholder for a
pending operator step: no workflow, script or test reads it, and setting it
changes nothing. It comes from unblock round 2 on #6831 (stop `blocked`).
Fix-up #7052 merged, and the resumed implementation then blocked again on the
`CLAUDE.md` §21.D change alone, which pipeline editors cannot make (the
isolated workspace hides `CLAUDE.md`). The observed gap: both
`pr_merge_status_guard.py` copies already cite §21.D for a rule §21.D does not
state. A push never reads the 300-second TTL cache; it always takes PR state
from a live lookup and reuses only a snapshot from a live lookup in the same
hook run, and a failed push lookup follows §21.C instead of falling back to
cached data. Only a bare commit may still be satisfied from the cache. The
step: in a trusted interactive checkout, an operator first confirms that rule
against both hook copies, then edits §21.D (API-call budget) in `CLAUDE.md`
and `workflow-templates/CLAUDE.md` together, keeping the two byte-identical,
so it states that rule. Do not edit either hook copy for this step, do not add
anything to `.github/ai/claude_template_divergence.json`, and do not bypass or
relax `tests/test_claude_template_live_parity.py` or
`tests/test_pr_merge_status_guard.py`. The guard, CI, releases, tagging,
dispatches and merges are unchanged, and nothing about #6831 is reported as
validated until the operator has made and checked the edit. Once the edit is
on `main`, the same change removes this paragraph and retires the placeholder,
as `WRAPPED_GIT_WRITE_GUARD_UNSET_OPERATOR_STEP` was retired.
