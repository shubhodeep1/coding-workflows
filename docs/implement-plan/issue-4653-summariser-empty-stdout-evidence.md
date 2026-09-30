# Implement-Plan Log — Name the summariser's empty-stdout failure in reviewer heal evidence

- Plan: docs/completed/issue-4653-summariser-empty-stdout-evidence-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4653
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main (was ai/issue-4605, whose PR #4607 closed unmerged; rebuilt on main 2026-09-28, operator answer Q1: D)
- Project branch: claude/implement-plan-issue-4653-summariser-empty-stdout-evidence-main   Final PR: #4685 draft (supersedes #4657, closed unmerged 2026-09-28)
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR (the number is in the completion 1/1 stage report)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_011RQxHtFb2paXDZ6p1MrPYf (reused)   safety net and hand-back in the validation 1/3 read-result stage report
- Last updated: 2026-09-28
- Last note: validation 1/3 passed (run 36367059872, 10/10 tests, project branch head f10bc78); plan moved to docs/completed/ in the completion PR; next: final-merge 1/1.

## Phases
1. [x] Phase 1 — summariser empty-stdout evidence and log upload   — PR #4658 merged 2026-09-27 (into the superseded branch claude/implement-plan-issue-4653-summariser-empty-stdout-evidence; its diff re-applied on main 2026-09-28); review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-27: CONFORMANT — no fixes (pre-validation; security skipped; audited on the superseded branch based on ai/issue-4605)
- Re-check on main — 2026-09-28: the same checks on the rebuilt branch: `pytest tests/test_workflow_failure_heal.py tests/test_review_autofix_failure_log_upload.py tests/test_workflow_file_size_limit.py tests/test_changelog_fragment_contract.py` 141 passed; `tests/test_review_autofix_review_pipeline_contract.py` 136 passed; `ruff check --select E,F --ignore E501` pass; `yamllint -s review_autofix.yml` pass; `review_autofix.yml` 451,634 bytes. The new tests fail against main's unmodified `workflow_failure_heal.py` / `review_autofix.yml` (2 + 1 failures) and pass with the fix. Not a new conformance run (the code is unchanged); the cap count stays at 1.

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue; re-verified with .claude/scripts/security_pass_skip.py on 2026-09-28: skip true)

## Validation
- Cycle 1 — run 36367059872 2026-09-28 (target_ref: claude/implement-plan-issue-4653-summariser-empty-stdout-evidence-main, authorized SHA f10bc78): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 297s). No fix PR.

## Completion
- Completion PR (claude/implement-plan-issue-4653-summariser-empty-stdout-evidence-complete) open 2026-09-28 — doc moved to docs/completed/issue-4653-summariser-empty-stdout-evidence-plan.md
- Final PR #4685 draft (into main; #4657 into ai/issue-4605 closed as superseded 2026-09-28)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which branch should the fix build on? — Picked: A — `ai/issue-4605`, the issue's `Target branch:` (the failing PR #4607's head). Alternatives: B — the default branch. Why: `/implement-issue-claude` step 3 builds on the branch the issue names; the affected files are identical on both branches. Applied in: no code change. Status: changed to B (2026-09-28, operator answer Q1: D on #4653; applied on claude/implement-plan-issue-4653-summariser-empty-stdout-evidence-main)
- AD-2 [plan, 2026-09-27] How far should the fix go? — Picked: A — recognise the empty-stdout line in the evidence and upload the summariser logs with the failure-log artifact. Alternatives: B — evidence parser only; C — also change the summariser's model or retry behaviour. Why: the issue asks to inspect the uploaded summariser log, which the artifact never carried, and forbids a retry fix; C guesses at an unidentified cause (§8). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What should the evidence say for an empty-stdout attempt? — Picked: A — `summariser_exit rc=0` plus one `summariser_empty_stdout prefix=<prefix>` line, no attempt counts. Alternatives: B — copy the raw diagnostic line with its attempt number; C — only `summariser_exit rc=0`. Why: A keeps the fingerprint stable and names the failure mode; B changes the fingerprint per attempt count; C loses the distinction from a clean exit. Applied in: phase 1 PR. Status: pending review
- AD-4 [phase 1/1 — review round 1, 2026-09-27] How should the reviewers' prefix-class finding (widen `[A-Za-z0-9_.-]{1,40}` to the plan's `[^)]*`) be handled? — Picked: A — keep the bounded class, document why, and add a test that every prefix `summarize_reviewer_consensus.sh` accepts matches it. Alternatives: B — widen to `[^)]{1,40}` as suggested; C — reject with no change. Why: the prefix is copied verbatim into the fingerprint and heal report and the same log holds untrusted model stderr, so §1 favours the bounded class; the test turns the reviewers' silent-miss risk into a CI failure; C needs a dedicated-bot verdict this session cannot post. Applied in: PR #4658. Status: pending review
- AD-5 [conformance 1/3, 2026-09-27] The parser does not record `attempt N timed out after …s.` lines, so when a final summariser attempt times out, `summariser_exit` shows the previous recorded code (`0` after an empty-stdout attempt, `226` after `exited rc=226`) rather than the script's `last_rc` (137). This limitation predates the project and also applies to `exited rc=` lines. Fix it here? — Picked: A — leave as is; record it for review. Alternatives: B — map a timeout line to rc=137; C — clear `summariser_exit` on a timeout. Why: the plan scopes the evidence change to empty stdout (§5), and B/C change the fingerprint of the timeout failure class, which the plan's non-goals keep stable. Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] When a regex copies a captured field from a log into heal evidence, bound its character class and pin it with a test to the producer's accepted values, rather than widening it to `[^)]*`: the same log can carry untrusted model stderr. (files: scripts/workflow_failure_heal.py, scripts/summarize_reviewer_consensus.sh)
- [source:plan-deviation] An issue-mode project whose `Target branch:` is another pull request's head is stranded when that pull request closes unmerged; check the base PR's state before building on it, and rebuild on the default branch when it is closed. (files: .claude/commands/implement-issue-claude.md)

## Notes
- Issue mode: start-up checks auto-decided (CLAUDE.md §28.A). Session mode: auto.
- 2026-09-27: root cause of the inconclusive heal located in run 36317817104's job log: 10/10 `summariser (pass1): attempt N produced empty stdout` lines, unrecognised by reviewer_failure_evidence; summariser_pass1.log was never in the failure-log artifact.
- 2026-09-27: two tests in tests/test_workflow_failure_heal.py fail on the base branch ai/issue-4605 without this change and pass on main (test_fingerprint_cap_block_labels_comments_and_reports: PR 4259 vs 4255; test_reviewers_failed_names_the_failure_before_the_editor_flags: review_autofix_step_iteration_summary.sh now requires RUNNER_TEMP). They come from PR #4607's own changes and are out of scope here. 2026-09-28: on main both pass, as do the five tests/test_review_autofix_review_pipeline_contract.py tests that failed on ai/issue-4605.
- 2026-09-27: review_autofix.yml is 468,516 bytes on ai/issue-4605 (§27 guard 480,000); this change adds about 200 bytes. On main it is 451,634 bytes with the change.
- 2026-09-27 (validation 1/3): blocked. Validation could not authorize a final PR into ai/issue-4605 (`validate.yml@main` accepts only an open PR into main; the `source_issue` input of PR #4600 is not on main). Asked Q1 on #4653.
- 2026-09-28: operator answer Q1: D (not a listed option): PR #4607 (head ai/issue-4605) closed unmerged on 2026-09-27, so the base will never merge. Rebuild on main: new project branch claude/implement-plan-issue-4653-summariser-empty-stdout-evidence-main from origin/main, the project diff (`git diff origin/ai/issue-4605 origin/claude/implement-plan-issue-4653-summariser-empty-stdout-evidence`, 8 files, +263/−9) applied cleanly with `git apply --3way`; final PR #4657 (into ai/issue-4605) closed as superseded and a new draft final PR opened into main, carrying `Fixes #4653`; continue at validation 1/3 with the new branch as `target_ref`. The superseded branch claude/implement-plan-issue-4653-summariser-empty-stdout-evidence is left as it is (not deleted, not force-pushed); its copy of this log stops at phase 1 and is stale.
- 2026-09-28: the project branch name no longer equals `claude/implement-plan-<slug>`: every later stage reads the `Project branch:` line of this log and of its `— resume.` block, never the derived name.
- 2026-09-28 (validation 1/3 — read result): synced the project branch with main (clean merge of docs/analysis-only commits, ee078d3, after the validated head f10bc78); validation_status.json from artifact ai-validation-36367059872-1: status=pass. No validation-fix PR, so no conformance re-run (cap count stays at 1).
