<!-- agents: section="Workflow architecture" -->
`PUSH_DESTINATION_GUARD_UNSET_OPERATOR_STEP` is an unset placeholder for a
pending operator step: no workflow, script or test reads it, and setting it
changes nothing. It comes from unblock round 1 on #7220 (stop `blocked`). The
`claude/*` push allow rules in `.claude/settings.json` still let a branch
deletion or a refspec that writes the default branch through without a
prompt (`git push origin claude/x +main`, `git push origin claude/x :release`),
and a repository whose default branch is not `main` / `master`, or whose
default branch cannot be resolved, gets no ask at all. The fix is an edit to
the live `.claude/hooks/pr_merge_status_guard.py`, which pipeline editors
cannot write, and CI requires that file to stay byte-identical to
`workflow-templates/.claude/hooks/pr_merge_status_guard.py`
(`tests/test_claude_template_live_parity.py`). The step: in a trusted
interactive checkout, an operator edits both hook copies identically so that
`git push` asks for confirmation for every branch deletion (`--delete`, `-d`,
a combined short flag containing `d`, and a colon deletion such as
`:branch`), on any remote including the session's own `claude/*` branches;
for any refspec whose destination is the resolved default branch, including
`+`-prefixed refspecs, `refs/heads/<default>` destinations, a `HEAD` refspec
that resolves to it, and pushes with several refspecs; and, when the default
branch resolves neither locally (`symbolic-ref origin/HEAD`) nor remotely
(`ls-remote --symref`), for any destination outside `claude/*`. Targets:
`_push_targets` (deletions currently return no target and `+` is stripped
silently) and the default-branch skip in `_evaluate_bash` (commits on the
default branch stay skipped). Reuse the existing confirmation-reason path;
`CLAUDE_PR_MERGE_GUARD=off` keeps disabling only the merged-PR check, and the
new asks ship on with no extra switch. Add focused cases to
`tests/test_pr_merge_status_guard.py` (which already covers both copies),
invert `test_deletion_on_origin_does_not_check_merged_pr` to expect `ask`
while still making no PR query, and update the §21.E limitation note in
`CLAUDE.md` and `workflow-templates/CLAUDE.md` in the same commit. Do not
change the `.claude/settings.json` allow/ask rules, do not add the hook to
`.github/ai/claude_template_divergence.json`, and do not gate or disable the
existing hook behaviour. Once both hook copies with the new checks are on
`main`, remove this paragraph and retire the placeholder as
`WRAPPED_GIT_WRITE_GUARD_UNSET_OPERATOR_STEP` was retired.
