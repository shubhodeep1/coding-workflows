<!-- agents: section="Workflow architecture" -->
`MERGE_GUARD_LIVE_PARITY_UNSET_OPERATOR_STEP` is an unset placeholder for a
pending operator step: no workflow, script or test reads it, and setting it
changes nothing. It comes from unblock round 1 on #6832 ("CI failure: CI on
PR #6830"), where `tests/test_claude_template_live_parity.py` reported
`.claude/hooks/pr_merge_status_guard.py` out of step with its
`workflow-templates/.claude/hooks/` twin on PR #6830's head. The two copies
were byte-identical on `main` when this note was written, so the drift may
exist only on that branch. The step: in a trusted checkout, an interactive
operator copies `workflow-templates/.claude/hooks/pr_merge_status_guard.py`
over `.claude/hooks/pr_merge_status_guard.py` byte for byte (template to
live, never the reverse, so the template's fresh PR lookup is kept) and
commits it on the branch that carries the drift: PR #6830's head, or `main`
if the drift has landed there. Do not revert the template, do not add the
hook to `.github/ai/claude_template_divergence.json`, and do not bypass or
relax the parity test, which stays the signal. Pipeline editors cannot write
`.claude/hooks/**` and CI's dry-run sync never prepares it (see "Live
`.claude/` copies and their templates"). Once both copies are byte-identical
on the affected branch, or PR #6830 merges or closes with the two copies
byte-identical on `main`, remove this paragraph and retire the placeholder as
`WRAPPED_GIT_WRITE_GUARD_UNSET_OPERATOR_STEP` was retired.
