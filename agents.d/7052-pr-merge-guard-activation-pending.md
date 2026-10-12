<!-- agents: section="Workflow architecture" -->
Pending operator step for #6831 (activation gaps after PR #6647), recorded by
#7052: the fix needs a change to both `pr_merge_status_guard.py` copies
(`.claude/hooks/` and `workflow-templates/.claude/hooks/`) and possibly to
`CLAUDE.md`. Pipeline editors cannot write `.claude/**` and the isolated
workspace hides `CLAUDE.md`, so the unblock judge chose `operator_step` and
only a trusted operator session lands the change; this note does not
authorize any editor to touch the hooks. Until then the change stays off
behind the placeholder `PR_MERGE_GUARD_ACTIVATION_UNSET_OPERATOR_STEP`.
Nothing reads it, and the guard keeps its current behaviour.
`tests/test_claude_template_live_parity.py` and
`tests/test_pr_merge_status_guard.py` stay enforced; nothing is bypassed, and
nothing about #6831 is reported as validated. When a person lands the change,
both copies are edited together and kept byte-identical, and the same change
replaces this note with a description of the shipped behaviour and retires
the placeholder, as #6755 was.
