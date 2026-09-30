# Require a merged final PR and its project log before a final-merge resume

Source issue: shubhodeep1/coding-workflows#5513 (https://github.com/shubhodeep1/coding-workflows/issues/5513)
Base branch: claude/implement-plan-issue-5222-final-merge-resume-closed-issue
Security pass: skip (ai:security: automation-produced issue)

## Summary

The final-merge resume route added by #5222 (`final_merge_resume` in `scripts/claude_issue_route.py`) decides from comment markup alone. A trusted commenter can post a `<!-- ai:claude-blocked:v1 -->` comment with a `final-merge` stage on any closed issue that carries `ai:claude` and `ai:claude-blocked`, then `/reclarify`, and clarify and the intake start a Claude session. This plan makes the route fail closed. The repository must also attest the stage: the project log on the default branch has to record the `final-merge` stage for that issue, and the final PR it names has to be merged into the default branch after the blocked comment.

## Context

- Security audit finding `forged-final-merge-block` (high, confidence 9/10, `scripts/claude_issue_route.py:425`), filed by `security-audit.yml` against the #5222 project branch (tracker #3576). Recommendation: "Fail closed unless a repository-bound project log records the final-merge stage and its matching final PR is verified merged; do not treat comment markup as stage attestation."
- On this base branch the rule lives in `final_merge_resume(issue, comments)` (`scripts/claude_issue_route.py:382-436`). Two callers use it to authorize a session:
  - clarify, `.github/workflows/clarify.yml` step `Decide clarify route` (lines 511-534): `claude_issue_route.py final-merge-resume --issue-json --comments-json`;
  - the intake, `scripts/claude_issue_intake.sh` step 1b (lines 173-225), through `authorize_target` (`claude_issue_route.py:439-496`) and the `authorize-target` CLI.
  `/implement-issue-claude` step 2 (`.claude/commands/implement-issue-claude.md:16-21`) states the same rule for the session.
- What the repository already records when a project reaches its final merge: `/implement-plan-claude` step 11 sets the log to `Stage: final-merge`, and the final PR carries that log to the default branch. For example, `docs/implement-plan/issue-5119-deflake-stall-guard-observe-test.md` on `main` reads `Source issue: shubhodeep1/coding-workflows#5119 (…)`, `Project branch: claude/implement-plan-issue-5119-deflake-stall-guard-observe-test   Final PR: #5151 draft`, and `Stage: final-merge`. PR #5151 merged into `main` at 2026-09-29T19:01:37Z, after the blocked comment and before the `/reclarify` at 19:01:42Z.
- A log can keep `Stage: final-merge` on the default branch after the project finished: a LIVE verify-activation with no fix PR records `Activation: LIVE` only in its report (step 11a/12). So the log and a merged PR alone do not prove that a *current* blocked comment belongs to that merge. The comment has to be tied to it by time.

## Goals

- `final_merge_resume` is eligible only when every existing comment check passes **and** an attestation verifies all of these:
  - exactly one progress log `docs/implement-plan/issue-<N>-*.md` on the default branch names `Source issue: <repo>#<N>`, has a `Stage:` value starting `final-merge`, a `Project branch:` starting `claude/implement-plan-issue-<N>-`, and a `Final PR: #<F>`;
  - PR `#<F>` is merged, its base is the default branch, and its head is that project branch in the same repository;
  - the latest trusted blocked comment was created before PR `#<F>` merged, and a trusted `/reclarify` was posted after the merge.
- With no attestation supplied, `final_merge_resume` returns `eligible: false` with reason `needs_attestation`. Callers that pass only two arguments therefore fail closed.
- clarify and the intake fetch the attestation only after the comment checks pass. A read failure keeps the closed-issue skip in clarify and rejects with `authorization_read_failed` in the intake.
- `/implement-issue-claude` step 2 (twin) lists the same attestation conditions, so the session's rule matches the route's.
- Tests: the attestation parser and verdict (pure), the time binding, both CLIs, the intake and the clarify step end to end with the `gh` stub, and a regression test that a forged block on a finished project is refused.

## Non-goals

- Changing when or how blocked comments are posted, or adding a stage attribute to the marker (#5222's AD-4 stands).
- Reopening issues, or changing the `Fixes #<N>` contract of the final PR.
- Retrying the attestation reads. A transient failure fails closed, and the human comments `/reclarify` again.
- The other open-issue routes (`trusted_author`, `trusted_reclarify`), which this finding does not cover.

## Constraints

- §1: this is a security fix. Every new check fails closed, and the attestation is read only from the default branch and the PR API, never from comment text.
- §5: the change is limited to the rule, its two callers, the twin's description of it, docs, and tests. There are no new triggers, payload keys, labels, or repository variables.
- §6: existing identifiers are unchanged. `final_merge_resume(issue, comments)` keeps its two positional parameters and gains an optional third. `authorize_target` gains an optional `attestation`, and both output dicts only gain keys. These new names were checked with `grep -rn` over `scripts/ .github/ tests/ .claude/ workflow-templates/ docs/ README.md agents.md` and are unused: `FINAL_MERGE_LOG_DIR`, `FINAL_MERGE_LOG_MAX_CANDIDATES`, `FINAL_MERGE_LOG_SOURCE_RE`, `FINAL_MERGE_LOG_STAGE_RE`, `FINAL_MERGE_LOG_BRANCH_RE`, `FINAL_MERGE_LOG_PR_RE`, `parse_final_merge_log`, `final_merge_attestation`, `fetch_final_merge_attestation`, the reasons `needs_attestation` and `final_merge_unverified`, the output keys `needs_attestation` and `attestation_reason`, the CLI flags `--attestation-json`, `--attestation-repo`, `--attestation-default-branch`, and `--fetch-attestation`, and the clarify env var `FINAL_MERGE_DEFAULT_BRANCH`.
- §9: tabs in Python and shell, 2-space YAML.
- §15: the attestation costs at most 1 default-branch read (skipped when clarify passes `github.event.repository.default_branch`), 1 directory listing, at most 5 log reads (usually 1), and 1 PR read. They happen only after the comment checks pass on a closed `/reclarify` issue that carries both labels. No other path makes a new call.
- §20: security fix, so one `changelog.d/5513-final-merge-resume-attestation.md` fragment (`security`).
- §27: `clarify.yml` stays far under 480,000 bytes (about 80 KB).
- §28.C / protected paths: `.claude/commands/implement-issue-claude.md` is protected. Under the interim twin-first rule, only `workflow-templates/.claude/commands/implement-issue-claude.md` is edited, and the phase PR stops for the `[claude-twin-sync]` copy.

## Approach

All checks stay in `scripts/claude_issue_route.py` (AD-1):

1. `parse_final_merge_log(text)` reads the header lines of a progress log: `Source issue:`, `Stage:`, `Project branch:`, and `Final PR: #<F>`, taking the first occurrence of each.
2. `final_merge_attestation(repo, issue_number, default_branch, logs, pull)` is pure. `logs` is a list of `{"path", "text"}` candidates and `pull` is the PR JSON (or `None`). It returns `{"verified", "reason", "final_pr", "merged_at", "log_path"}`. It picks the one log that names `<repo>#<N>` and fails closed on zero or several (AD-5). It then checks the stage, the branch prefix, and the PR: number, `merged_at`, base, head ref, and head repo.
3. `fetch_final_merge_attestation(repo, issue_number, default_branch="")` makes the reads listed under §15 through `_gh_api_read`. A 404 on the log directory means `no_project_log`. Any other read failure raises, and the caller fails closed (AD-4).
4. `final_merge_resume(issue, comments, attestation=None)`: after the existing checks, `None` gives `needs_attestation`. Otherwise it requires `attestation["verified"]`, blocked-comment `created_at` before `merged_at`, and a trusted `/reclarify` created after `merged_at` (AD-2). Any failure gives `final_merge_unverified`, with the cause in `attestation_reason`.
5. `authorize_target(..., comments=None, attestation=None)`: a closed candidate whose comment checks pass and that has no attestation returns `needs_attestation: true` (reason `issue_closed`, not authorized).
6. CLIs: `final-merge-resume` takes `--attestation-json` (offline) or `--attestation-repo` plus the optional `--attestation-default-branch` (fetch when needed; exit 2 on a read failure). `authorize-target` takes `--attestation-json` or `--fetch-attestation` (fetch for the validated repo when needed; exit 2 on a read failure).
7. clarify passes `--attestation-repo "${GITHUB_REPOSITORY}" --attestation-default-branch "${FINAL_MERGE_DEFAULT_BRANCH}"`, with the new step env `FINAL_MERGE_DEFAULT_BRANCH: ${{ github.event.repository.default_branch || '' }}`. The intake passes `--fetch-attestation` on its comments call.
8. The twin of `/implement-issue-claude` step 2 adds the attestation conditions (AD-3).

Alternatives: enforcing only in the intake (AD-1 B) leaves clarify posting a routed comment and dispatching for a forged block. Checking only the log and a merged PR (AD-2 B) accepts a forged block on a finished project whose log still reads `final-merge`.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the rule and its two callers must change together, or one caller would still authorize from comment markup.

1. **Phase 1 — fail closed without a repository attestation**. Files: `scripts/claude_issue_route.py`, `.github/workflows/clarify.yml`, `scripts/claude_issue_intake.sh`, `workflow-templates/.claude/commands/implement-issue-claude.md`, `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`, `README.md`, `agents.md`, `changelog.d/5513-final-merge-resume-attestation.md`. Done when `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py` (twin tests), and `tests/test_phase_skip_gate_telemetry_contract.py` pass, the clarify YAML parses, and `bash -n` passes on the intake script. The template-parity tests stay red until the twin sync, as the interim rule expects. Rollback: revert the phase PR, which restores the comment-only rule.
   protected paths: .claude/commands/implement-issue-claude.md (twin-first)

## Implementation Steps

1. `scripts/claude_issue_route.py`: add the constants, `parse_final_merge_log`, `final_merge_attestation`, and `fetch_final_merge_attestation`; extend `final_merge_resume` and `authorize_target` and their docstrings; add the CLI flags; update the module docstring.
2. `.github/workflows/clarify.yml` `Decide clarify route`: add the `FINAL_MERGE_DEFAULT_BRANCH` env var and the `--attestation-*` flags, and update the step comment (§15 budget).
3. `scripts/claude_issue_intake.sh`: pass `--fetch-attestation` on the comments call, and update the header and step 1b comments.
4. `workflow-templates/.claude/commands/implement-issue-claude.md` step 2: add the attestation bullet.
5. Tests, README (Claude issue implementer failure modes), `agents.md` (the intake item), and the changelog fragment.

## Files & Modules

- `scripts/claude_issue_route.py`
- `.github/workflows/clarify.yml`
- `scripts/claude_issue_intake.sh`
- `workflow-templates/.claude/commands/implement-issue-claude.md`
- `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`
- `README.md`, `agents.md`
- `changelog.d/5513-final-merge-resume-attestation.md` [new]

## Tests

- `parse_final_merge_log` on the #5119 log shape, and on logs missing each line.
- `final_merge_attestation`: verified on the #5119 shape; refused for no log, a log for another issue or repo, two matching logs, a non-final-merge stage, a branch without the `claude/implement-plan-issue-<N>-` prefix, a missing `Final PR`, a PR number mismatch, an unmerged PR, a PR into another base, a PR from another head or a fork.
- `final_merge_resume`: `needs_attestation` without an attestation; eligible with a verified one; `final_merge_unverified` for an unverified attestation, a blocked comment created after the merge (the forged-block scenario on a finished project), and a `/reclarify` only before the merge.
- `authorize_target`: comments, then attestation, then authorized; an unrelated closed issue still requests neither.
- `fetch_final_merge_attestation` with a stub reader: the call budget, the 404 → `no_project_log` mapping, and a raised error on other failures.
- Intake end to end: a closed final-merge resume with a verified attestation is queued. A forged block (no log, unmerged PR, or block after merge) is rejected with `issue_closed`. A failed attestation read is rejected with `authorization_read_failed`.
- clarify step end to end: a verified resume routes `final_merge_resume`; a forged block keeps `reason=issue_closed outcome=skip`; the default branch comes from the env var with no repo read.
- Twin contract: the new step 2 bullet.

## Risks & Mitigations

- A legitimate resume fails closed when the project log did not reach the default branch with `Stage: final-merge`. By construction (steps 11 and 11a) the final PR carries the step 11 log, and the human can still start the next stage by hand.
- A project restarted under a second log for the same issue is refused as `ambiguous_project_log`. This is fail-closed and visible in the clarify notice.
- A trusted user can still re-run the verify-activation of a genuine finished project whose log reads `final-merge`, by re-adding `ai:claude-blocked` to its closed issue. That needs the genuine pre-merge blocked comment, and it only re-runs a read-mostly stage of a real project.

## Rollout

Ships to consumers with the next `@stable` (clarify is reusable, and its scripts are staged from coding-workflows). The command change reaches `.claude/` at the `[claude-twin-sync]` copy. No new env var or repo variable needs setting.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Where is the attestation enforced? — Picked: A — in the shared `final_merge_resume` rule, with both callers (clarify and the intake) fetching it through one helper. Alternatives: B — only in the intake's `authorize_target`; C — only in clarify. Why: the finding names the shared rule, and one rule keeps clarify from dispatching a forged block that the intake would then refuse. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What attests a final-merge stage? — Picked: A — one default-branch progress log for the issue at a `final-merge` stage naming a same-repo final PR merged into the default branch, the blocked comment created before that merge, and a trusted `/reclarify` after it. Alternatives: B — the log and the merged PR only, with no time binding; C — only a merged PR found from the issue's close event. Why: a finished project's log can keep `Stage: final-merge`, so without the time binding a newly forged block on it would pass. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Does `/implement-issue-claude` step 2 also list the attestation? — Picked: A — yes, edit the twin (twin-first, the phase PR holds for the sync). Alternatives: B — no, leave the command unchanged because the intake is the boundary. Why: §1 says the safer option wins, and step 2 claims to be "the same rule as `final_merge_resume`", which would be stale otherwise. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] How does an attestation read failure behave? — Picked: A — fail closed: clarify keeps the closed-issue skip with a warning, the intake rejects `authorization_read_failed`, and a 404 on the log directory counts as `no_project_log`. Alternatives: B — treat every read failure as ineligible (`issue_closed`). Why: this matches the intake's existing read-failure reason, so a transient error is distinguishable from a forged block. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] What if several logs match the issue? — Picked: A — fail closed (`ambiguous_project_log`), and also when more than 5 `issue-<N>-*.md` candidates exist. Alternatives: B — take the first log in name order. Why: §1; picking one silently could attest from the wrong project. Applied in: phase 1. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Protected paths: phase 1 edits `.claude/commands/implement-issue-claude.md` through its `workflow-templates/.claude/commands/` twin only (interim twin-first rule, until #4785).
- The finding was filed on the #5222 project branch, which this project targets (`Integration branch:` line). The #5222 final PR #5225 is an open draft.
