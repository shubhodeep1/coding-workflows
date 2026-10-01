# Pin the guard differential verifier to the base branch

Source issue: shubhodeep1/coding-workflows#5327 (https://github.com/shubhodeep1/coding-workflows/issues/5327)
Base branch: claude/implement-plan-issue-5174-guard-differential-check
Security pass: skip (ai:security: automation-produced issue)

## Summary

The `Guard differential check (issue #5174)` CI step runs `scripts/guard_differential.py` from the PR's own checkout. A PR can therefore weaken a guard hook and, in the same change, edit the verifier to exit 0 without comparing anything. This plan runs the base branch's copy of the verifier instead. The PR's hooks and corpora are only the data it checks. The verifier also reports every change to itself or to its CI steps, so reviewers see it.

## Context

- Security audit finding `pr-controlled-verifier` (high, confidence 9/10) at `.github/workflows/ci.yml:506`, on the #5174 project branch. The step does `git fetch … origin "${GUARD_DIFFERENTIAL_BASE_REF}"` and then runs `python3 scripts/guard_differential.py --base-ref FETCH_HEAD …` from the checked-out merge commit, which the PR controls.
- `ci.yml` runs on `pull_request` into `main` and `stable`, so both the workflow definition and the checkout come from the PR's merge ref. A `pull_request` workflow can only be protected against a changed *script*: a PR that edits the step itself controls whatever runs there. The fix therefore takes the verifier out of the PR's hands and machine-reports changes to the verifier and its steps. Blocking a changed step outright would need a workflow defined outside the PR (see Non-goals).
- Sibling findings on the same verifier, handled by their own issue-mode projects: #5325 (`warning-suppresses-guard-regression`) and #5326 (`author-controlled-loosening-exception`). This plan leaves the comparison rule and the `Intended loosening:` handling alone.
- `scripts/guard_differential.py` uses only the standard library and reads its corpus from `--repo-root` (default `.`), not from its own location. A copy under `$RUNNER_TEMP` therefore runs unchanged against the workspace. Its `sys.path[0]` is the copy's directory, so no module in the PR tree can shadow an import.

## Goals

- G1: The CI step runs the verifier blob from the fetched base commit (`FETCH_HEAD:scripts/guard_differential.py`), copied into `$RUNNER_TEMP`. Editing `scripts/guard_differential.py` in the PR has no effect on the check's verdict for that PR.
- G2: A base branch without the verifier (bootstrap: `main` / `stable` before the script lands there) runs the PR's copy. It says so in a `::warning::GUARD_DIFFERENTIAL verifier=head reason=base-has-no-verifier` line. A base with the verifier logs `GUARD_DIFFERENTIAL verifier=base source=<sha>:scripts/guard_differential.py`.
- G3: The verifier reports `::warning::GUARD_DIFFERENTIAL verifier_change path=<path>` when the PR changes `scripts/guard_differential.py` or any `Guard differential …` step in `.github/workflows/ci.yml`. It reports this whether or not a hook changed, and adds `verifier_changes=<n>` to its summary line. It does not change the exit code.
- G4: Tests cover G1–G3. One executes the real `ci.yml` step body in a scratch repository with a local `origin`, and proves that the base verifier runs and the head verifier does not. They also cover the bootstrap fallback and the `verifier_change` detection.
- G5: `agents.md`, the script docstring, and a changelog fragment document the pinned verifier, the bootstrap fallback, the new log lines, and the flag-compatibility rule.

## Non-goals

- Moving the check into a `pull_request_target` / `workflow_run` workflow, or requiring it through a ruleset. Either one would execute PR-supplied hook code in a privileged context (cache poisoning, secret exposure). A ruleset is a §23.C administrative write. See AD-1 and Risks.
- Changing the loosening rule, warning handling, or `Intended loosening:` parsing (#5325, #5326).
- Changing any `.claude/**` hook (no protected-path edit).

## Constraints

- §1: security first. §5: minimal change set: one CI step body, additive script functions, tests, docs, and one fragment.
- §6: no rename. New identifiers (`VERIFIER_SCRIPT_PATH`, `CI_WORKFLOW_PATH`, `GUARD_STEP_NAME_PREFIX`, `verifier_changes`, `guard_differential_steps`, `Report.verifier_changes`, and the shell variables `guard_differential_verifier_dir` and `guard_differential_base_verifier`) were checked against the script, its test, and `ci.yml`.
- §9: tabs in Python, 2-space YAML.
- §15: no GitHub API calls. The base comes through `git fetch`, and the verifier blob through `git ls-tree` / `git show`.
- §18: the change stays inside the existing `ci.yml` step. No new script and no removal-registry entry.
- §20: one fragment, `changelog.d/5327-pinned-guard-verifier.md` (`security`).
- §27: `ci.yml` is 61,648 bytes and grows by under 2 KB.
- Flag compatibility: the step may pass only flags the *base* verifier accepts (`--base-ref`, `--pr-body-file`). A new flag lands in the script first and is used by the step in a later PR. This is documented in `agents.md`.

## Approach

1. **Pinned verifier (ci.yml step).** After fetching the base, the step reads `git ls-tree --name-only FETCH_HEAD -- scripts/guard_differential.py` into a variable, so a failing `git` aborts under `set -e`. When the blob exists, the step writes `git show FETCH_HEAD:scripts/guard_differential.py` to `${RUNNER_TEMP}/guard-differential-verifier/guard_differential.py`. Otherwise it copies the workspace script there and prints the bootstrap warning. It then runs that copy with the same flags as today. The PR's hooks, corpora, and body stay the data the verifier reads from the workspace and the event payload.
2. **Verifier-change report (script).** `verifier_changes(repo_root, base_ref, head_ref)` compares `scripts/guard_differential.py` between the base and the head (the working tree when `--head-ref` is absent). It also compares the text of every `ci.yml` step whose name starts with `Guard differential`, extracted by `guard_differential_steps()`. `run_check` stores the result on `Report.verifier_changes`, and `print_report` emits one `::warning::` line per entry before anything else. The warnings run from the base verifier, so a PR cannot silence them without editing the step. Such an edit is itself reported by the next base verifier, and it shows in the diff the reviewer panel reads.

Alternatives: see AD-1 to AD-4.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the fix is one CI step and its support code, and splitting it would leave either an unused report or an unpinned step.

1. **Phase 1: pin the verifier and report verifier changes.**
   - Files: see Files & Modules.
   - Done: `tests/test_guard_differential.py` passes, including the new step-execution tests. `yamllint` and `actionlint` are clean on `ci.yml`, and `ruff` is clean on the script and the test.
   - Rollback: revert the PR. The step then runs the PR's copy again, as before.

## Implementation Steps

1. `.github/workflows/ci.yml`, step `Guard differential check (issue #5174)`: add a comment block about the pinned verifier (#5327), and the `ls-tree` / `show` / bootstrap logic before the `python3` call. The call runs `${RUNNER_TEMP}/guard-differential-verifier/guard_differential.py`.
2. `scripts/guard_differential.py`: add the constants, `_side_text()`, `guard_differential_steps()`, `verifier_changes()`, the `Report.verifier_changes` field, the `run_check` wiring, the `print_report` lines, and the `verifier_changes=` summary field. Update the module docstring.
3. `tests/test_guard_differential.py`: unit tests for `guard_differential_steps()` and the CLI's `verifier_change` lines (script edit, guard-step edit, unrelated `ci.yml` edit, exit code unchanged). Two tests execute the real step body (base verifier present / absent), and the wiring test asserts the pinned path.
4. `agents.md`: a "Pinned verifier (issue #5327)" bullet, the output lines, and the flag-compatibility rule in the "Guard differential check" section.
5. `changelog.d/5327-pinned-guard-verifier.md` [new].

## Files & Modules

- `.github/workflows/ci.yml`
- `scripts/guard_differential.py`
- `tests/test_guard_differential.py`
- `agents.md`
- `changelog.d/5327-pinned-guard-verifier.md` [new]

## Tests

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_guard_differential.py` (its own `ci.yml` step already exists).
- `yamllint .github/workflows/ci.yml`, `actionlint` when available, and `ruff check` on the changed Python files.

## Risks & Mitigations

- **A PR that edits the step itself still controls what runs.** This is inherent to `pull_request` workflows. The edit shows in the diff the reviewer panel reads, and once it reaches the base, the next PR's base verifier reports any step change. Hard enforcement needs a base-defined workflow or a ruleset (§23.C). That is recorded as a residual risk (AD-4), not implemented here.
- **A hook that detects the harness** (its scratch `HOME`, stub `gh`) can answer differently under test. This is inherent to executing hook code, and unchanged by this plan.
- **Flag skew.** A PR whose step passes a flag the base verifier lacks fails with exit 2. The flag-compatibility rule in `agents.md` covers it.
- **Bootstrap window.** Until the verifier is on `main` / `stable`, PRs there run their own copy, which is the same trust as today. The warning makes it visible.

## Rollout

The change lands with the #5174 project's final PR. On PRs into `main`, the step runs the head copy with the bootstrap warning until #5185 merges, and the base copy after that. There is no flag and no consumer impact: `ci.yml` and the script are this repo's own. Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Where should the pinned verifier run? — Picked: A — keep the `pull_request` step and run the base commit's `scripts/guard_differential.py` blob, copied into `$RUNNER_TEMP`, against the checked-out merge commit. Alternatives: B — a `pull_request_target` / `workflow_run` workflow defined on the default branch; C — keep the PR's verifier and only add checks. Why: B executes PR hook code in a privileged context (cache poisoning, secrets) and needs a §23.C ruleset change to be enforced; C leaves the exploit open; A closes the reported exploit with the smallest change (§1, §5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What runs when the base branch has no verifier yet? — Picked: A — run the PR's copy and print `::warning::GUARD_DIFFERENTIAL verifier=head reason=base-has-no-verifier`. Alternatives: B — fail the step; C — skip the check. Why: a base without the script has no step either, so the PR already controls the whole check there; B would block #5185, the final PR that first lands the verifier on `main`; C drops a check that runs today. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What should a PR's change to the verifier or its CI steps do? — Picked: A — report it as a `::warning::GUARD_DIFFERENTIAL verifier_change path=…` line and a summary count, without failing. Alternatives: B — fail when a hook and a verifier path change in the same PR; C — fail on any verifier-path change. Why: the pinned base verifier already ignores a changed script, and a PR that edits the step bypasses any in-step failure, so B and C add no security while blocking legitimate fixes such as #5325 / #5326. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Which `ci.yml` changes count as verifier changes? — Picked: A — only the steps whose name starts with `Guard differential`, compared as text. Alternatives: B — any change to `ci.yml`. Why: `ci.yml` changes often for unrelated jobs, and a warning on every such PR would train reviewers to ignore it. Applied in: phase 1 PR. Status: pending review

## Notes

- `.claude/scripts/security_pass_skip.py` returned `skip: true` (`ai:security: created and labelled by the issue automation`).
- The issue's text asks nothing outside its finding. No issue text was left out of scope.

## References

- Issue #5327; #5174 and its final PR #5185; sibling findings #5325, #5326.
