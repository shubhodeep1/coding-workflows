# Implement-Plan Log — Let a spot-fix reissue declare the new files its follow-up must create

- Plan: docs/completed/issue-4665-reissue-new-output-paths-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4665
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: stable
- Project branch: claude/implement-plan-issue-4665-reissue-new-output-paths   Final PR: #4667 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base stable) — the project ends after the final merge into stable
- Waiting on: completion PR (then final PR #4667 into stable)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01TM4SVVtTqLwEfcyXidUoE9 (hand-back and safety net re-armed after each push; ids in the stage report)
- Last updated: 2026-09-30
- Last note: validation 1/3 (run 36674984839, target_ref = the project branch at 56ff2fa) passed 10/10 tests; conformance is CONFORMANT and no validation fix merged after it, so the completion PR moves the plan to docs/completed/.

## Phases
1. [x] Phase 1 — declare and union new output paths (`new_output_paths` in the judge contract, validated union into the spot-fix `files_touched` allowlist, tests, docs, changelog fragment)   — PR #4668 merged 2026-09-28 (merged by the owner after the Q1: A answer on #4665); review rounds: 2; interventions: 0
   - [x] Prompt schema + rules in `prompts/mode-judge-review-blocked.txt` and `prompts/_templates/mode-judge-review-blocked.txt`
   - [x] Validated third union source in `scripts/review_rb_judge.sh` with `REISSUE_FILES_TOUCHED_NEW_OUTPUTS`
   - [x] Tests: `tests/test_review_rb_judge_label_propagation.py`, `tests/test_files_touched_scope_guard.py`, `tests/test_orchestrate_poll_workflow_contract.py`
   - [x] Docs: `README.md`, `agents.md`; fragment `changelog.d/4665-reissue-new-output-paths.md`
   - Done: new tests pass, existing reissue / scope-guard / prompt / contract suites pass, and a reissue without the field is byte-identical.

## Conformance
- Run 1 — 2026-09-28: INCOMPLETE (Correctness FAIL: one EVIDENCE-BASED BLOCKER, `new_output_paths` entries the scope guard rewrites) — fix PR #4686 (pre-security); review rounds: 1; merged 2026-09-28 (41f0000)
- Run 2 — 2026-09-28: CONFORMANT — no fixes (post-fix re-audit at 41f0000; 202 plan-suite tests + 23 related test files pass)

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue)

## Validation
- Cycle 1 — run 36378523375 2026-09-28 (target_ref: claude/implement-plan-issue-4665-reissue-new-output-paths): conclusion=failure before validation — step "Authorize explicit validation target": target PR binding is missing or ambiguous (final PR #4667 targets stable, the gate required the default branch). Blocked as Q1 on #4665.
- Cycle 1 (re-dispatch) — run 36674984839 2026-09-30 (target_ref: claude/implement-plan-issue-4665-reissue-new-output-paths, pinned 56ff2fa): conclusion=success, status=pass raw_status=pass — "Runtime validation passed (10/10 tests, 289s)"; no fix issues

## Completion
- Completion PR (this PR) — doc moved to docs/completed/issue-4665-reissue-new-output-paths-plan.md
- Final PR #4667 draft → marked ready at stage final-merge 1/1

## Activation
- n/a: the base is stable, so the change goes live with stable's own lifecycle. The final-merge stage closes #4665 with `ai:merged`.

## Auto-decisions
- AD-1 [plan, 2026-09-27] Should this project also repair #4664 (edit its `files_touched`, remove `ai:scope-blocked`)? — Picked: A — no; leave it to the existing human-gated procedure and say so on the issue. Alternatives: B — edit #4664's body and labels from this session. Why: the issue calls that procedure human-gated, and editing another issue's scope allowlist is outside this code fix. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-27] Where does the judge declare new files? — Picked: A — a top-level `new_output_paths` string array. Alternatives: B — a `new_files` list on each `remaining_issues[]` entry. Why: a changelog fragment or shared fixture belongs to no single finding, and a flat list is simpler to validate. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What happens to an invalid declared path? — Picked: A — skip it, count it, log the reason, keep spot-fix. Alternatives: B — fall back to `redo` like an invalid `remaining_issues[].file`. Why: matches the closed-PR union's skip rule and keeps the preserved baseline; the worst case is today's behaviour. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] May a declared path already exist at the closed head? — Picked: A — no; it must be absent (neither file nor directory), and globs and trailing `/` are rejected. Alternatives: B — allow existing files too. Why: existing files belong in `remaining_issues[]` with line anchors, and absence rules out any existing-directory exemption. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Cap the number of declared paths? — Picked: A — a fixed cap of 10, excess entries skipped as `over_cap`. Alternatives: B — no cap; C — a new repo var. Why: bounds a runaway judge without a new configuration surface (§4, §5). Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-27] Which hand-off author login should the checker use? — Picked: A — shubhodeep1, the GH_PAT account whose workflow comments appear on #4665; verdict bot empty. Alternatives: B — leave both empty (fail closed, review hand-offs then reach the chain only through the §26.H sweep). Why: the review workflow posts hand-offs as the GH_PAT account and review_autofix_sweep.yml defaults to the same login; sibling project issue-4621 uses it. Applied in: no code change. Status: pending review
- AD-7 [conformance 1/3, 2026-09-28] How should `new_output_paths` reject entries that `files_touched_scope_guard.py` would rewrite (trimmed edge whitespace, Unicode line separators)? — Picked: A — keep only printable ASCII (0x21–0x7E at both ends, spaces allowed inside); skip anything else as `invalid_path` without echoing it. Alternatives: B — reject only edge whitespace and the characters `str.splitlines()` splits on, keeping other non-ASCII names; C — change the guard to stop trimming. Why: A closes every normalisation variant at once and a skipped entry only falls back to today's behaviour (AD-3); B must track Python's Unicode whitespace tables, and C changes the guard, a non-goal. Applied in: conformance-1 fix PR. Status: pending review

## Lessons
- [source:intervention] An existence check that gates an allowlist must tell "absent" from "lookup failed": `git cat-file -e <sha>:<path>` exits non-zero for both, so use `git --literal-pathspecs ls-tree --full-tree --name-only <sha> -- <path>` (exit 0 with empty output is the only proof of absence) and skip the entry on any other result. (files: scripts/review_rb_judge.sh)
- [source:conformance] Validate an allowlist entry as the consumer will read it, not as written: `files_touched_scope_guard.py` trims each entry and splits the issue body with `str.splitlines()`, so a padded or Unicode-separator path can turn into an existing directory or an extra entry. Restrict generated entries to printable ASCII with no edge spaces. (files: scripts/review_rb_judge.sh, scripts/files_touched_scope_guard.py)
- [source:intervention] Write invisible or line-breaking characters in test source as `\u` escapes, never raw: a raw U+2028 renders as a line break for any tool that splits with `str.splitlines()`, so reviewers read the wrong code and cite shifted line numbers. (files: tests/test_review_rb_judge_label_propagation.py)
- [source:validation] validate.yml bound an explicit target_ref only to an open PR into the default branch, so an issue-mode project whose final PR targets another base (stable, a parent project branch) could not be validated until the binding accepted that base (#4734 / #4746). Check that a gate's target binding covers every base a project can use before dispatching it. (files: .github/workflows/validate.yml)

## Notes
- Issue mode: base `stable` from the issue's `Target branch:` line; no merged PR has `stable` as its head, so the base did not move. Security pass skipped per the plan header. Activation n/a for a non-default base: the project ends after the final merge, which closes #4665 explicitly with `ai:merged`.
- Permission mode auto (no start-up question needed).
- `.github/workflows/review_autofix.yml` is 505,498 bytes on `stable`, above the §27 480,000-byte guard but under GitHub's 512,000 limit. `stable` predates `tests/test_workflow_file_size_limit.py`, and `main` has already split the file (451,394 bytes), so the next promotion fixes it. This project does not touch the file (§5).
- 2026-09-28: round 2 on PR #4668 had one finding, which was rejected. Convergence needs the dedicated verdict bot, which is not configured, so the project blocked (Q1 on #4665). The owner answered `Q1: A`, merged #4668 into the project branch (squash b6d6666), and commented `/reclarify`. The progress-log lines from the blocked comment were never pushed; this entry records them.
- 2026-09-28: the resumed issue session found checker session_01TM4SVVtTqLwEfcyXidUoE9 idle with no pending check-in and the log's `Status: IN_PROGRESS` lagging the unpushed BLOCKED state. It resumed at conformance 1/3 as the owner asked, instead of reporting `already in progress`. The project branch already contained `origin/stable`, so no sync push was needed.
- 2026-09-28: validation cycle 1 run 36378523375 failed at "Authorize explicit validation target" because the final PR targets stable. The project blocked with Q1 on #4665 (comment 5863627095); the recommended answer was to fix the gate rather than skip validation.
- 2026-09-30: validate.yml gained the stable / project-branch binding (#4734, fixed by #4746). The project branch was synced with stable (56ff2fa), validation 1/3 was re-dispatched as run 36674984839 against the project branch, and it passed.
