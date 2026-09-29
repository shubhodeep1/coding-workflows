# Implement-Plan Log — Parse issue branch lines in linear time in issue_body_integration_branch

- Plan: docs/plans/issue-4956-linear-issue-branch-parser-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#4956   Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
- Project branch: claude/implement-plan-issue-4956-linear-issue-branch-parser   Final PR: #5001 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5005 (review round 4)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01SLVShEKcrHxQinLPTfHkTx (project checker, reused)   safety net and hand-back: see the round 3 stage report
- Last updated: 2026-09-29
- Last note: review round 3 (session_014LGGF95s21LL7YHoGwDQGL): all findings rejected (INTEGRATION_BRANCH_LINE_RE matches the lone label line "Integration branch: `" with group ' ', so the line-bounded reference also returns ""; the test already pins that body and passes). The step 2 sync brought #4957's issue_body_orchestrator_project_branch into the project branch, which conflicted with the phase PR's test module; resolved by a [claude-merge-resolve] merge keeping both sides, and that push starts review round 4.

## Phases
1. [ ] Phase 1 — linear-time parser for `issue_body_integration_branch` (`scripts/gh_helpers.sh`, its test, changelog fragment)   — PR #5005 open (waiting); review rounds: 3; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verdict recorded in the plan's Notes).

## Validation

## Completion
- Final PR #5001 draft (into claude/implement-plan-issue-4813-close-sweep-target-branch-merges)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which copies of the branch-line grammar does this issue fix? — Picked: A — only `issue_body_integration_branch` in `scripts/gh_helpers.sh` (the flagged location, both of its patterns). Alternatives: B — also `scripts/orchestrate_lib.py` and `scripts/resolve_integration_ref.sh`. Why: §5; the other copies are on `main`, outside this project's diff, and route orchestrator planning, so changing them belongs in its own issue. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as whitespace inside a line? — Picked: A — every character `\s` matched before except `"\n"` (tabs, `\r`, Unicode spaces), so single-line results stay identical to the Python parser. Alternatives: B — space and tab only, as the issue words it, which would stop matching values followed by `\r` or other whitespace that parse today. Why: §1 without breaking backward compatibility; the issue's goal (no newline spanning) is met either way. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How to make the parse linear? — Picked: A — a line-by-line string parser with no regex backtracking. Alternatives: B — the same regexes applied per line (still cubic within a line: 29.7 s for 2,000 spaces); C — regexes plus a line-length cap (changes results for long lines, still quadratic up to the cap); D — possessive quantifiers (Python 3.11+ only). Why: the only option that is linear for every input with no change in results for single-line forms. Applied in: phase 1 PR. Status: pending review
- AD-4 [phase 1/1 — review round 1, 2026-09-29] Reviewers (3 of 6) flagged that an empty or whitespace-only label value returns "" and ends the search before a later valid line; the diagnosis is wrong (extract_integration_branch returns "" for the same bodies) but no test covered it. How to answer? — Picked: A — reject the behaviour change, add the reviewers' bodies to the Python-parity test and a comment in the parser, pushed as the round's `[claude-autofix]` commit. Alternatives: B — reject with no code change and a dedicated-bot verdict (no bot credential in this session, so the PR would stay blocked); C — skip empty values and keep searching (breaks parity with orchestrate_lib.py and resolve_integration_ref.sh, against the plan's goal). Why: §5 and the plan's parity goal; the test locks the agreement the reviewers doubted. Applied in: PR #5005. Status: pending review

## Lessons
- [source:intervention] When a string parser replaces a regex for performance, keep the regex's empty-value matches and add the edge bodies reviewers doubt to the parity test against the reference parser, so the intentional "" result is documented by a test, not only by a comment. (files: scripts/gh_helpers.sh, tests/test_gh_helpers_issue_body_integration_branch.py)
- [source:plan-deviation] A regex fix that only stops `\s*` from spanning newlines is not enough when a lazy group is followed by adjacent optional whitespace runs: that backtracks cubically inside one line, so parse the value with string operations and test a long single whitespace run, not only many blank lines. (files: scripts/gh_helpers.sh)
- [source:plan-deviation] A timing test for a search regex must use a body with no match: a match right after the blank lines returns on the first attempt and hides the quadratic scan. (files: tests/test_gh_helpers_issue_body_integration_branch.py)
- [source:intervention] State a regex replacement's contract as "the original regexes applied to each line on its own" and check every comment claim about the whole-body regex against bodies whose next line has no backtick: under re.MULTILINE a `\s*` after the label can take the next line as the value, so a "returns an empty string" claim holds only for some bodies. (files: scripts/gh_helpers.sh, tests/test_gh_helpers_issue_body_integration_branch.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine in session session_019VGvihqo4vEMB83gP6DWSc (Auto mode).
- This session had no `mcp__github__*` tools; GitHub reads and routine writes went through `gh api` REST (the repo's SessionStart hook installed `gh`, since the repo was attached mid-session).
- 2026-09-29 round 2 stage: synced the project branch with its base (clean merge of #4997, pushed as fe69e3c).
