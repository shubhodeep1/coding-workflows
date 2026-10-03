# Implement-Plan Log — Bind the env-requeue watchdog's control markers to the watchdog's own identity

- Plan: docs/completed/issue-5135-bind-watchdog-markers-to-author-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5135
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4938-environment-blocker-self-heal
- Project branch: claude/implement-plan-issue-5135-bind-watchdog-markers-to-author   Final PR: #5158 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4938-environment-blocker-self-heal)
- Waiting on: completion PR (this PR, into the project branch)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Vsm4aMQkgH9rtSvqrWytab (project checker)
- Last updated: 2026-09-30
- Last note: validation cycle 1 passed (run 36723476692, project head 9bce148); completion PR opened to move the plan to docs/completed/. Next: final-merge 1/1 marks final PR #5158 ready.

## Phases
1. [x] Phase 1 — bind the watchdog's control markers (`ai:claude-env-requeue:v1`, `ai:claude-env-requeue-exhausted:v1`) to the watchdog's own login   — PR #5176 merged 2026-09-29 (merge 9bce148); review rounds: 0; interventions: 0
   - `scripts/claude_issue_route.py`: `watchdog_login` in `env_requeue_decision` / `env_requeue_plan`, `--watchdog-login` on `env-requeue-plan`
   - `scripts/claude_issue_queue_watchdog.sh`: resolve the login with `gh api user`, fail closed with `env_requeue_skipped reason=watchdog_login_unknown`
   - `tests/test_claude_issue_route.py`: forged-marker, case-insensitive, fail-closed, CLI, and watchdog-script tests
   - `README.md`, `agents.md`, `changelog.d/5135-watchdog-marker-author.md`
   - done: every plan Goal holds; `pytest tests/test_claude_issue_route.py` passes; `bash -n` passes on the watchdog script

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — no fixes (pre-validation; security skipped)

## Security pass
- Skipped (ai:security: automation-produced issue; verified by `.claude/scripts/security_pass_skip.py`)

## Validation
- Cycle 1 — run 36723476692 2026-09-30 (target_ref: project branch, head 9bce148): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 259s); no fixes

## Completion
- Completion PR: this PR (`claude/implement-plan-issue-5135-bind-watchdog-markers-to-author-complete` into the project branch) — doc moved to docs/completed/issue-5135-bind-watchdog-markers-to-author-plan.md
- Merged PRs: phase 1 #5176
- Final PR #5158 draft (into claude/implement-plan-issue-4938-environment-blocker-self-heal)

## Activation
- n/a (base claude/implement-plan-issue-4938-environment-blocker-self-heal)

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which identity authenticates the watchdog's control markers? — Picked: A — the account behind the watchdog's `GH_PAT`, resolved at runtime with one `gh api user` call and passed as `--watchdog-login`; markers count only from that login. Alternatives: B — a new dedicated GitHub App identity with its own installation secret; C — an HMAC signature over each marker keyed by a new repository secret. Why: B and C need new secrets or app installation (§23.C administration, never auto-decided), and A closes the reported collaborator path with the pattern `review_autofix.yml` already uses. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What happens when the watchdog's login cannot be resolved? — Picked: A — fail closed: skip the env-requeue decision step for that run with `warn env_requeue_skipped reason=watchdog_login_unknown`, while the closed-target cleanup still runs. Alternatives: B — fall back to today's any-trusted-author rule; C — treat every marker as absent and continue. Why: B reopens the finding, and C would re-queue without a cap. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should each marker's `Run:` link also be verified as a watchdog workflow run (dispatch provenance)? — Picked: A — no; the author binding is the provenance check. Alternatives: B — one `actions/runs/<id>` read per marker, checking the workflow path and that the comment falls inside the run's time span. Why: only the `GH_PAT` account can pass the author check, and that account can already send the dispatch itself, so B narrows no attacker class while adding per-marker API calls (§15) and a new failure mode (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Does the binding cover only the exhausted marker named in the finding, or both control markers? — Picked: A — both `ai:claude-env-requeue:v1` and `ai:claude-env-requeue-exhausted:v1`. Alternatives: B — the exhausted marker only. Why: a forged re-queue marker also tampers with the cap and the stale wait, and the finding's recommendation covers all control markers. Applied in: phase 1 PR. Status: pending review
- AD-5 [phase 1/1, 2026-09-29] The base branch's CI `lint` job fails on 24 pre-existing ruff E101 hits (mixed tab/space hanging indents) in the two docstrings this phase rewrites (`env_requeue_decision`, `env_requeue_plan`). Fix them here? — Picked: A — yes, convert those docstring hanging indents to tabs in this phase. Alternatives: B — leave them, so the phase PR ships with a red `lint` check and a review round hands it back; C — leave them to the #4938 project. Why: the lines sit inside docstrings this phase already edits, the change is whitespace-only, and `main` is clean, so a red required check would only block this PR's auto-merge. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] In tab-indented Python, keep docstring hanging indents tab-only; ruff E101 flags a tab followed by spaces, and the CI `lint` job runs `ruff check --select E,F` over `scripts/*.py`. (files: scripts/claude_issue_route.py)

## Notes
- Issue mode: started by the Claude issue dispatcher routine; permission mode auto.
- The session's GitHub MCP tools were not available; `gh` was installed by re-running `.claude/hooks/session-start.sh` (the repo was attached after session start), and GitHub writes use `gh api` routine calls.
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5135#issuecomment-5890525455
- 2026-09-29: the first checker stalled after one check-in, and the conformance stage was started by the safety net.
- 2026-09-30: validation 1/3 read-result stage: issue base branch not merged (its PR #5000 is an open draft), project branch already up to date with it.
