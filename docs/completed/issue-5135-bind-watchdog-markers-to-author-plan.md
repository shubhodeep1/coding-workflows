# Bind the env-requeue watchdog's control markers to the watchdog's own identity

Source issue: shubhodeep1/coding-workflows#5135 (https://github.com/shubhodeep1/coding-workflows/issues/5135)
Base branch: claude/implement-plan-issue-4938-environment-blocker-self-heal
Security pass: skip (ai:security: automation-produced issue)

## Summary

The hourly Claude issue queue watchdog trusts any owner, member, or collaborator comment that starts with its re-queue or exhausted marker, so a collaborator can forge `<!-- ai:claude-env-requeue-exhausted:v1 … -->` and permanently stop the re-queue and its Telegram alert for an environment-blocked issue (security finding `forged-watchdog-exhaustion-marker`, medium). This plan makes those two control markers count only when they were posted by the account the watchdog itself writes them with, and treats every other copy as data.

## Context

- Issue #4938 (project branch `claude/implement-plan-issue-4938-environment-blocker-self-heal`) added `env-requeue` mode to `scripts/claude_issue_queue_watchdog.sh`, driven by `.github/workflows/claude-issue-queue-watchdog.yml` with `GH_TOKEN: ${{ secrets.GH_PAT }}`. It posts `<!-- ai:claude-env-requeue:v1 blocker=<id> reason=<r> -->` after each re-queue and `<!-- ai:claude-env-requeue-exhausted:v1 … -->` after the cap, both with that `GH_PAT`.
- `env_requeue_decision` in `scripts/claude_issue_route.py` (lines ~1021–1134) filters comments with `is_trusted_issue_author` (OWNER / MEMBER / COLLABORATOR users, or `github-actions[bot]`), then counts every trusted comment that starts with either marker. Line 1116 sets `alerted = True` for any trusted exhausted marker, and `alerted` short-circuits to `skip` / `exhausted_alerted` forever, however old. A forged re-queue marker likewise counts toward the cap (an early alert) or holds the issue in `requeued_waiting`.
- The security audit filed this as #5135 (`STRIDE: Tampering`, confidence 9/10), recommending that control markers be verified against the watchdog's identity and that other commenters' markers be treated as data.
- Precedent: `.github/workflows/review_autofix.yml` (the `gate_marker_author_login` step, lines ~686–790) already resolves the marker author with one `gh api user --jq .login` call using the same token that posts the markers, and fails closed (`marker_author_unavailable`) when it cannot.
- Sibling findings on the same file stay out of scope: #5136 (comment-flood read cap at line ~1230) and #5063 (blocker-marker provenance in the pickup's archive path).

## Goals

- A re-queue or exhausted marker counts in `env_requeue_decision` only when the comment's author login equals the watchdog's login (case-insensitive) **and** the author is still trusted under `is_trusted_issue_author`.
- A collaborator's forged exhausted marker no longer suppresses the re-queue or the alert; a collaborator's forged re-queue marker no longer counts toward the cap or the stale wait.
- The watchdog resolves its login from the token it posts with (`gh api user --jq .login`, one REST call per env-requeue run) and passes it to `env-requeue-plan --watchdog-login <login>`.
- When the login cannot be resolved or is malformed, the env-requeue decision step does nothing that run (no dispatch, no marker, no alert) and logs `warn env_requeue_skipped reason=watchdog_login_unknown`; the closed-target cleanup in the same step still runs.
- `env_requeue_plan` and `env_requeue_decision` fail closed when called without a login (no reads / `skip` with reason `watchdog_login_unknown`), so no caller can get the old any-collaborator trust by omission.

## Non-goals

- Blocker markers (`<!-- ai:claude-blocked:v1 … -->`) and `/reclarify` comments keep today's trust rule. Sessions and humans write them, not the watchdog, and #5063 covers blocker-marker provenance.
- The 1,000-comment read cap (#5136).
- A dedicated GitHub App identity or a signing secret for the watchdog (see AD-1). Both need §23.C administration and stay a human decision.
- Changing marker formats. Markers already posted by the watchdog keep counting, because the same account posted them.

## Constraints

- §1: security first. The fix must close the forgery path for every non-watchdog author and fail closed when the identity is unknown.
- §5: minimal change set, limited to the decision function, its plan / CLI plumbing, the watchdog's one lookup, tests, and docs.
- §6: no rename or removal. `env_requeue_decision` and `env_requeue_plan` gain a trailing keyword parameter `watchdog_login: str = ""`, and `env-requeue-plan` gains `--watchdog-login` (default `""`). New identifiers were checked for collisions (`watchdog_login`, `--watchdog-login`, `watchdog_login_unknown`: no existing use).
- §9: tabs in Python and shell, 2-space YAML (no YAML change planned).
- §15: one new REST read (`GET /user`) per env-requeue run, outside every loop. The existing search and comment reads carry no author identity for the token, and `review_autofix.yml` resolves its marker author the same way.
- §18: no new script or scheduler; the change rides the existing hourly workflow step.
- §19: `Refs #5135` everywhere except the final PR (base is not the default branch, so the final-merge stage closes the issue explicitly).
- §20: one changelog fragment, section `security`.

## Approach

1. `scripts/claude_issue_route.py`
   - Add the keyword parameter `watchdog_login: str = ""` to `env_requeue_decision`. When it is empty after stripping, return `skip` with reason `watchdog_login_unknown` before any other check. In the marker loop, a comment that starts with either control marker counts only when `_user_field(comment, "login").casefold() == watchdog_login.casefold()`. A trusted comment by anyone else that starts with a marker is ignored as data: it is not counted and does not fall through to other checks.
   - Add the same keyword parameter to `env_requeue_plan`. When it is empty, return `{"actions": [], "skipped": [], "errors": ["watchdog login unknown: …"], "searches": 0}` without any read. Otherwise pass it to `env_requeue_decision`.
   - Add `--watchdog-login` (default `""`) to the `env-requeue-plan` subcommand and pass it through in `_cmd_env_requeue_plan`.
   - Update both docstrings (the batching contract, and which markers count).
2. `scripts/claude_issue_queue_watchdog.sh`, `env_requeue()` step 2: before calling `env-requeue-plan`, resolve `watchdog_login` with `gh_retry gh api user --jq .login` (GH_TOKEN is the GH_PAT that posts the markers). Validate it against `^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`. On failure or an invalid value, log `warn env_requeue_skipped reason=watchdog_login_unknown` and return 0 after step 1. Otherwise pass `--watchdog-login "${watchdog_login}"`. Add a §15 comment naming the calls audited.
3. Tests in `tests/test_claude_issue_route.py`: route the existing decision tests through a helper that passes `watchdog_login`. Add forged-marker cases (a collaborator's exhausted marker and re-queue marker are ignored; a watchdog-authored marker still counts; a case-only login difference still counts; an empty login skips). Extend the `env_stubs` gh stub to answer `api user --jq .login`, and add shell tests for the lookup failure and for the flag being passed.
4. Docs: in the README env-requeue paragraph and the agents.md issue-#4938 bullet, say that only markers posted by the watchdog's own account count. Add the changelog fragment.

Alternatives are recorded under Auto-decisions (AD-1 … AD-4).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase for one standalone issue.

1. **Phase 1: bind the watchdog's control markers to its login.** Files: `scripts/claude_issue_route.py`, `scripts/claude_issue_queue_watchdog.sh`, `tests/test_claude_issue_route.py`, `README.md`, `agents.md`, `changelog.d/5135-watchdog-marker-author.md` [new], plus this plan and its progress log. Done when every Goal holds: `pytest tests/test_claude_issue_route.py` passes, the new forged-marker tests fail on the old code and pass on the new, `bash -n` passes on the watchdog script, and the plan output and watchdog logs show `watchdog_login_unknown` on a failed lookup. Rollback: revert the phase PR. The markers are unchanged, so no data migration or cleanup is needed.

## Implementation Steps

Phase 1:
1. `scripts/claude_issue_route.py` `env_requeue_decision` (~1021–1134): add `watchdog_login`, the early `watchdog_login_unknown` skip, and the login check in the marker loop. Update the docstring.
2. `scripts/claude_issue_route.py` `env_requeue_plan` (~1233–1300): add `watchdog_login`, the fail-closed early return, and pass-through. Update the batching contract docstring.
3. `scripts/claude_issue_route.py` CLI (~1796, ~1908): add `--watchdog-login` and pass it through.
4. `scripts/claude_issue_queue_watchdog.sh` `env_requeue()` step 2 and header comment: add the lookup, validation, warn log, and flag.
5. `tests/test_claude_issue_route.py`: add the decision-test helper, forged-marker tests, a plan-level fail-closed test, the CLI flag test, the gh stub `api user` branch, and watchdog shell tests for the flag and a failed lookup.
6. `README.md` (~1343–1356) and `agents.md` (~303–309): add the marker author rule. Add `changelog.d/5135-watchdog-marker-author.md` (`<!-- changelog: security -->`).

## Files & Modules

- `scripts/claude_issue_route.py`
- `scripts/claude_issue_queue_watchdog.sh`
- `tests/test_claude_issue_route.py`
- `README.md`
- `agents.md`
- `changelog.d/5135-watchdog-marker-author.md` [new]
- `docs/plans/issue-5135-bind-watchdog-markers-to-author-plan.md` [new]
- `docs/implement-plan/issue-5135-bind-watchdog-markers-to-author.md` [new]

## Tests

- Unit (`tests/test_claude_issue_route.py`):
  - existing `env_requeue_decision` tests pass unchanged in intent, run with `watchdog_login="shubhodeep1"` (the author the helper uses);
  - a COLLABORATOR `mallory` exhausted marker does not stop the alert (`alert`), and does not stop the re-queue after `/reclarify`;
  - a COLLABORATOR re-queue marker does not count toward the cap or `requeued_waiting`;
  - a marker from `ShubhoDeep1` (case-only difference) counts;
  - an empty `watchdog_login` gives `skip` / `watchdog_login_unknown`;
  - `env_requeue_plan` without a login issues no reads and reports the error;
  - `env-requeue-plan --watchdog-login` is accepted by the CLI.
- Script (stubbed `gh`): the watchdog passes `--watchdog-login shubhodeep1` from `gh api user`; a failing `gh api user` logs `env_requeue_skipped reason=watchdog_login_unknown`, sends no dispatch or marker, and still exits 0; a forged exhausted marker from a collaborator leads to the alert.
- Full suite: `pytest tests/test_claude_issue_route.py` plus the repo's standard `pytest` run for files touched, and `bash -n scripts/claude_issue_queue_watchdog.sh`.

## Risks & Mitigations

- `GET /user` fails for a run (network, token scope) → that run skips the decision step only (fail closed, logged). The next hourly run retries. ACCEPTED: one delayed hour is safer than an uncapped re-queue.
- `GH_PAT` is rotated to a different account → markers posted by the old account stop counting, so each blocked issue can get up to `CLAUDE_ISSUE_ENV_REQUEUE_MAX` more re-queues and one more alert. ACCEPTED: bounded by the cap, and the operator rotating the token sees the alert.
- The watchdog's account is shared with other `GH_PAT` automation and with web sessions the proxy signs as the same user, so anything acting as that account can still write a marker. ACCEPTED: that identity can already send the dispatch and edit labels itself. A dedicated identity needs a GitHub App or secret, a §23.C decision recorded in AD-1 for human review.

## Rollout

Ships with the #4938 project's final PR (this project's base branch). Nothing to migrate: the watchdog already posts every existing marker as the `GH_PAT` account. The watchdog workflow runs only in coding-workflows (`if: github.repository == 'shubhodeep1/coding-workflows'`), so no consumer repo change is needed.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which identity authenticates the watchdog's control markers? — Picked: A — the account behind the watchdog's `GH_PAT`, resolved at runtime with one `gh api user` call and passed as `--watchdog-login`; markers count only from that login. Alternatives: B — a new dedicated GitHub App identity with its own installation secret; C — an HMAC signature over each marker keyed by a new repository secret. Why: B and C need new secrets or app installation (§23.C administration, never auto-decided), and A closes the reported collaborator path with the pattern `review_autofix.yml` already uses. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What happens when the watchdog's login cannot be resolved? — Picked: A — fail closed: skip the env-requeue decision step for that run with `warn env_requeue_skipped reason=watchdog_login_unknown`, while the closed-target cleanup still runs. Alternatives: B — fall back to today's any-trusted-author rule; C — treat every marker as absent and continue. Why: B reopens the finding, and C would re-queue without a cap. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should each marker's `Run:` link also be verified as a watchdog workflow run (dispatch provenance)? — Picked: A — no; the author binding is the provenance check. Alternatives: B — one `actions/runs/<id>` read per marker, checking the workflow path and that the comment falls inside the run's time span. Why: only the `GH_PAT` account can pass the author check, and that account can already send the dispatch itself, so B narrows no attacker class while adding per-marker API calls (§15) and a new failure mode (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Does the binding cover only the exhausted marker named in the finding, or both control markers? — Picked: A — both `ai:claude-env-requeue:v1` and `ai:claude-env-requeue-exhausted:v1`. Alternatives: B — the exhausted marker only. Why: a forged re-queue marker also tampers with the cap and the stale wait, and the finding's recommendation covers all control markers. Applied in: phase 1 PR. Status: pending review

## References

- Issue #5135 (this finding), audit tracker #3576
- Issue #4938 and `docs/plans/issue-4938-environment-blocker-self-heal-plan.md` (the watchdog's env-requeue mode)
- Sibling findings #5136 and #5063 (out of scope)
- `.github/workflows/review_autofix.yml` `gate_marker_author_login` (precedent for resolving the marker author with `gh api user`)
