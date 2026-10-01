# Run the Claude twin sync-state guard from the protected base commit, not the PR checkout

Source issue: shubhodeep1/coding-workflows#5608 (https://github.com/shubhodeep1/coding-workflows/issues/5608)
Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
Security pass: skip (ai:security: automation-produced issue)

## Summary

The CI step "Claude twin sync state (CLAUDE.md §28.C)" in `.github/workflows/ci.yml` runs `python3 scripts/claude_twin_sync.py check` from the checkout. On a `pull_request` event that checkout is the PR's merge commit, so one PR can change a guard path (`.claude/hooks/**`, `.claude/settings*.json`) and its twin together and also weaken `scripts/claude_twin_sync.py` so the check passes. This plan runs the copy of the script that is on the protected base commit instead, and fails closed when that copy cannot be read.

## Context

- Security audit finding `pr-controlled-sync-guard` (issue #5608, `A08:2021-Software and Data Integrity Failures`, high, confidence 9/10, `Refs #3576`), filed against the #4785 project branch `claude/implement-plan-issue-4785-twin-first-claude-sync` (final PR #4804, draft), `ci.yml:280`.
- The step (project branch `ci.yml:195-280`) picks a `base` for each event:
  - a PR into `main` or `stable`: the merge commit's first parent (the base-branch tip GitHub built the merge on);
  - a push to `main`: `github.event.before`;
  - a push to `stable` that is not a promotion: `github.event.before`, or `main`'s tip when the push creates `stable`.

  It then runs `PYTHONDONTWRITEBYTECODE=1 python3 scripts/claude_twin_sync.py check --base "${base}" --head HEAD …` (`ci.yml:278`, `ci.yml:280`). `scripts/` is the checkout, which on a `pull_request` run is PR content.
- `agents.md` ("Guard paths fail closed in the sync-state check") already names the gap: "It does not cover a PR that also edits `.github/workflows/ci.yml` or `scripts/claude_twin_sync.py`: a `pull_request` run uses the PR's own copies of both".
- `main` does not carry `scripts/claude_twin_sync.py` yet; it arrives with the #4785 final PR #4804. `stable` gets it on the next promotion after that. Both bases therefore lack the script for a while.
- `scripts/claude_twin_sync.py` imports only the Python standard library, so a copy extracted to a temporary directory runs on its own.
- The step's test harness (`tests/test_claude_twin_sync.py`, the `ci` fixture) runs the step body with the script as an untracked file in the work tree, so the harness's base commits do not carry the script.
- Binding rules: §1 (security first), §5 (minimal change), §6 (no renames; the step name, `env:` keys and CLI flags stay; new shell variables unique in the step), §9 (2-space YAML), §15 (no new GitHub API call), §19 (`Refs #5608` only), §20 (changelog fragment), §27 (`ci.yml` is 65,782 bytes, far under 480,000).

## Goals

- G1: When the chosen trusted commit carries `scripts/claude_twin_sync.py`, the step extracts that blob (`git show <commit>:scripts/claude_twin_sync.py`) to a fresh temporary directory and runs it. The checkout's `scripts/claude_twin_sync.py` is not executed. A PR that weakens the script in the same change as a guard-path edit fails the check.
- G2: The trusted commit is the event's `base` (see Context). On `stable` events, when `base` lacks the script, `main`'s tip (already fetched for `--guard-provenance-ref`) is used instead.
- G3: Fail closed: if the trusted commit cannot be resolved, or its tree cannot be read, the step fails. A failed `git show` of a blob that `git ls-tree` listed also fails.
- G4: Bootstrap (AD-2): when every trusted commit resolves and none carries the script, the step logs a `::warning::` naming the commit(s) and runs the checkout's copy, as it does today. That is the case for `main` until #4804 merges and for `stable` until its next promotion; there is no guard on such a base to bypass.
- G5: The step's arguments, events, bases, skips and exit codes are otherwise unchanged. Every existing `tests/test_claude_twin_sync.py` test passes unchanged.
- G6: Docs: the `agents.md` twin-sync bullets say which copy of the script runs, and the known-gap sentence keeps only `.github/workflows/ci.yml`. `changelog.d/5608-run-twin-guard-from-base.md` records the security fix (§20).

## Non-goals

- The workflow file itself: a `pull_request` run still uses the PR's own `.github/workflows/ci.yml`, so a PR can still remove or edit the step (AD-3). That needs a `pull_request_target` or ruleset-level required workflow, a separate design and an operator action (§23.C).
- Changing `scripts/claude_twin_sync.py`, the sync workflow, or the check's rules.
- Editing any `.claude/**` path: this plan touches none.

## Constraints

- §6: new shell variables `twin_guard_script`, `twin_guard_dir`, `twin_guard_source`, `twin_guard_candidates`, `twin_guard_listing` were checked against the step (which uses `event_args`, `twin_on_stable`, `twin_head`, `twin_compare_status`, `twin_provenance`, `base`) and are unique there. No identifier is renamed.
- §15: no GitHub API call is added. Only local `git ls-tree` / `git show` reads of commits the step already fetched.
- §23.E: the step's `env:` is unchanged; `GH_TOKEN` stays push-only.
- §27: the step grows by about 40 lines (about 2 KB).

## Approach

- After the existing base fetch, and before the `check` call, the step builds `twin_guard_candidates=("${base}")` and appends `"${twin_provenance}"` on `stable` events. For each candidate it runs `git rev-parse --verify "<c>^{commit}"` (a failure exits the step, fail closed) and `git ls-tree --name-only "<c>" -- scripts/claude_twin_sync.py` (a failure exits the step). The first candidate that lists the path wins: `git show "<c>:scripts/claude_twin_sync.py" > "${twin_guard_dir}/claude_twin_sync.py"` into `twin_guard_dir="$(mktemp -d)"`.
- No candidate lists it → bootstrap `::warning::` and `twin_guard_script=scripts/claude_twin_sync.py`.
- The step logs `twin sync guard: running scripts/claude_twin_sync.py from <sha>` (or the bootstrap warning), so a reviewer can see which copy ran.
- Both `check` invocations use `python3 "${twin_guard_script}"` with the same arguments, keeping the substring `check --base "${base}" --head HEAD "${event_args[@]}"` that `test_ci_step_passes_the_pr_context_through_env` asserts.
- Alternatives rejected: see AD-1 and AD-2.

## Phases & Merge Strategy

A single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase for one issue.

1. **Phase 1 — run the twin sync-state guard from the base commit.**
   - Files: `.github/workflows/ci.yml`, `tests/test_claude_twin_sync.py`, `agents.md`, `changelog.d/5608-run-twin-guard-from-base.md` [new]. Protected paths: none.
   - Done: the new tests below pass, every existing `tests/test_claude_twin_sync.py` test passes, `yamllint -s .github/workflows/ci.yml` and `actionlint` pass, and `tests/test_workflow_file_size_limit.py` passes.
   - Rollback: revert the phase PR; the step returns to running the checkout's copy.

## Implementation Steps

1. `.github/workflows/ci.yml`, step "Claude twin sync state (CLAUDE.md §28.C)": add the trusted-copy selection after the base fetch (`ci.yml:274-276`), switch both `check` calls (`ci.yml:277-281`) to `"${twin_guard_script}"`, and extend the step comment with the issue #5608 rule.
2. `tests/test_claude_twin_sync.py`: add ci-step tests (the `ci` fixture) that commit a script to the base:
   - a PR into `main` whose checkout script is a stub that exits 0 still fails on a hook changed with its twin (the base copy ran);
   - a PR into `stable` with a permissive checkout script still fails on a guard change `main` does not carry;
   - a PR into `stable` whose base lacks the script but whose `main` carries it runs `main`'s copy;
   - a bootstrap base (no script anywhere) logs the warning and runs the checkout's copy;
   - an unreadable base commit fails the step;
   - a static test that the step never runs `python3 scripts/claude_twin_sync.py` directly outside the bootstrap branch.
3. `agents.md`: update the "Sync-state check" and "Guard paths fail closed" bullets.
4. `changelog.d/5608-run-twin-guard-from-base.md` [new], section `security`.

## Files & Modules

- `.github/workflows/ci.yml`
- `tests/test_claude_twin_sync.py`
- `agents.md`
- `changelog.d/5608-run-twin-guard-from-base.md` [new]

## Tests

- `python3 -m pytest -q -p no:cacheprovider tests/test_claude_twin_sync.py` (the `ci.yml` "Claude twin sync tests" step).
- `yamllint -s .github/workflows/ci.yml`, `actionlint .github/workflows/ci.yml` (when available), `python3 -m pytest -q tests/test_workflow_file_size_limit.py`, `python3 tests/test_ci_job_split_contract.py`.

## Risks

- A base whose script has a bug now decides the check for PRs that fix that bug. Such a fix lands on the base first, and the PR that carries it is checked by the old copy, which is the point of the change.
- A PR that changes the script's CLI in a way the base copy does not accept would fail against the old copy. The step keeps passing only flags every base copy since #5247 accepts.
- Bootstrap window (AD-2): until #4804 merges, PRs into `main` still run their own copy, as today.

## Rollout

Ships with the #4785 project: this project's final PR merges into `claude/implement-plan-issue-4785-twin-first-claude-sync`, and #4804 carries it to `main`. No operator step, no new variable, no consumer propagation (`ci.yml` is not a consumer template).

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which commit supplies the guard script the step runs? — Picked: A — the event's `base` commit (the merge commit's first parent, or `github.event.before`), with `main`'s tip as the fallback on `stable`. Alternatives: B — always `main`'s tip, which on a `stable` event would check against rules `stable` has not received; C — move the check to a `pull_request_target` workflow, which changes the trigger model and hands base-repo credentials to a job that reads PR content. Why: the smallest change that runs only protected-branch content, using commits the step already fetches (§1, §5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What does the step do when the trusted commit resolves but has no `scripts/claude_twin_sync.py` (`main` before #4804, `stable` before its next promotion)? — Picked: A — log a `::warning::` and run the checkout's copy, as today. Alternatives: B — skip the check, which drops the coverage #4804 gets now; C — fail closed, which would fail #4804 itself and every PR into `stable` until a promotion. Why: a base without the script has no guard to bypass, so A keeps today's coverage without adding risk, while an unreadable commit still fails closed (§1, §3). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Should this project also stop a PR from editing `.github/workflows/ci.yml` to drop the step? — Picked: A — no; record it as the remaining gap in `agents.md`. Alternatives: B — add a `pull_request_target` workflow that re-runs the check from the base. Why: the finding names the script, and B is a new privileged workflow that needs its own design and an operator ruleset change (§5, §23.C). Applied in: phase 1 (docs only). Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5608` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
