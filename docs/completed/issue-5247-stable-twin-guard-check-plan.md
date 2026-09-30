# Run the Claude twin sync-state check on PRs into stable, with guard-path provenance

Source issue: shubhodeep1/coding-workflows#5247 (https://github.com/shubhodeep1/coding-workflows/issues/5247)
Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
Security pass: skip (ai:security: automation-produced issue)

## Summary

The CI step "Claude twin sync state (CLAUDE.md §28.C)" in `.github/workflows/ci.yml` skips every event on `stable`. A PR into `stable` can therefore change `.claude/settings.json` or a `.claude/hooks/**` file, with or without its twin, and the ordinary review workflow can auto-merge it. This plan runs the check on PRs into `stable` against the PR's `stable` base, adds a guard-path provenance rule for `stable`, and skips only promotion pushes, whose content is already a `main` commit.

## Context

- Security audit finding `stable-pr-guard-check-skipped` (issue #5247, `A01:2021-Broken Access Control`, medium, `Refs #3576`), filed against the #4785 project branch `claude/implement-plan-issue-4785-twin-first-claude-sync` (final PR #4804, draft).
- The step (project branch `ci.yml:194-229`) enforces `scripts/claude_twin_sync.py check` only on PRs into `main` (base: the merge commit's first parent) and pushes to `main` (base: `github.event.before`). Every other event prints "the twin sync state is enforced on PRs into main and pushes to main; skipping." and exits 0 (`ci.yml:217`).
- It skips `stable` because a promotion range spans many PRs: a sync of `.claude/X` followed by a newer, not yet synced twin edit of `X` reads as `.claude/` being ahead (the #4785 log's `plan-deviation` lesson).
- How `stable` moves:
  - `promote-main-to-stable.yml` fast-forwards `stable` to a `main` commit (`git merge --ff-only`, then `git push origin HEAD:refs/heads/stable`), so a promotion's head is always a commit on `main`.
  - Patches and workflow-heal fixes reach `stable` through PRs into `stable` (`auto-release-stable.yml`), which the review workflow reviews and auto-merges like any other PR.
  - The release job pushes one assembled-changelog commit (`mark-stable.yml:613`, `test-and-mark-stable.yml:5600`) that touches no `.claude/` path.
  - `mark-stable.yml` "Verify CI passed on stable" refuses a release while CI on the `stable` tip is failing, so a false positive on a `stable` push blocks releases.
- On `main`, a guard path (`GUARD_PATH_PREFIXES` `hooks/`, `GUARD_PATH_FILES` `settings.json`, `settings.local.json`) reaches `.claude/` from its twin only through a sync PR that the repository owner reviews and merges (`claude-twin-sync.yml`, AD-2 of #4785). Nothing comparable exists for a PR straight into `stable`.
- Binding rules: §1 (security first), §5 (minimal change), §6 (no renames; the step name and existing flags stay; new identifiers unique), §9 (tabs in Python, 2-space YAML), §15 (one REST call, documented), §19 (`Refs #5247` only), §20 (changelog fragment), §23.E (the step uses `github.token`, never a session variable), §27 (`ci.yml` is 54,946 bytes, far under 480,000).

## Goals

- G1: On a `pull_request` event into `stable`, the step runs `claude_twin_sync.py check` with base = the merge commit's first parent (the `stable` tip), exactly as for `main`. A PR into `stable` that changes a non-excluded `.claude/` file to content other than its twin fails CI.
- G2: On every checked `stable` event, `check` also gets `--guard-provenance-ref <main tip>`. A guard path changed in the range must then equal the same `.claude/` path on `main` (same blob and mode, or absent on both). Otherwise it is a violation, even when `.claude/` and its twin match. A backport of guard content that `main` already carries passes.
- G3: On a `push` to `stable`, the step first asks GitHub whether the pushed commit is already on `main` (`GET /repos/{repo}/compare/{sha}...main`, status `ahead` or `identical`). If it is, the push is a promotion: the step logs a skip and exits 0. Any other status, or a failed call, runs the full `stable` check (fail closed).
- G4: PRs into `main` and pushes to `main` behave exactly as before: same base and no provenance rule. Every other event still skips.
- G5: `check` without `--guard-provenance-ref` returns the same result as before. The JSON output only gains a `guard_provenance_ref` key.
- G6: Docs: the `agents.md` "Sync-state check" bullet describes the `stable` rules, and `changelog.d/5247-stable-twin-guard-check.md` records the security fix (§20).

## Non-goals

- Changing the `main` rules, the sync workflow, `merge-check`, or the guard path list.
- Checking `workflow-templates/.claude/**` twin edits that reach consumers at `@stable`. That risk predates this project and #4785 accepted it.
- Making CI a required check or changing any ruleset (§23.C, operator action).
- Editing any `.claude/**` path: this plan touches none.

## Constraints

- §6: new identifiers `guard_provenance_ref` (parameter, CLI flag `--guard-provenance-ref`, JSON key) were checked against `scripts/claude_twin_sync.py`, which has no `provenance` identifier. New shell variables in the step are prefixed `twin_` and are unique in the step.
- §15: the step makes at most one REST call per run, and only on a `push` to `stable`. No other `gh` call exists in the step to extend. The call is documented in the step.
- §23.E: the call authenticates with `github.token` passed as `GH_TOKEN` in the step `env`, set only on `push` events, so the PR-controlled checkout of a `pull_request` run never gets it from this step.

## Approach

- **Script:** `check_not_ahead(repo, base, head, guard_provenance_ref=None)`. When a ref is given, every guard path changed in `base..head` is compared with `.claude/<rel>` at that ref. The comparison runs before the "both copies missing" early exit, so deleting a guard file on `stable` alone is caught. A mismatch adds a violation whose reason says the guard change must land on `main` first. The CLI's `check` gains `--guard-provenance-ref`, resolved with `resolve_commit` (fail closed, exit 2).
- **CI step:** the event `case` gains two branches:
  - a `pull_request` into `stable` uses the first parent;
  - a `push` to `stable` asks the compare API. A promotion (`ahead` / `identical`) skips. Otherwise the step uses `github.event.before`. A failed call or empty status is logged and treated as not a promotion.

  For both `stable` branches the step fetches `refs/heads/main` at depth 1 and passes its sha as `--guard-provenance-ref`. A failed fetch fails the step (`set -e`).
- **Alternatives rejected:** see AD-1 to AD-4.

## Phases & Merge Strategy

A single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase for one issue.

1. **Phase 1 — `stable` twin check with guard provenance.**
   - **Scope:** the Implementation Steps below.
   - **Files:** `scripts/claude_twin_sync.py`, `.github/workflows/ci.yml`, `tests/test_claude_twin_sync.py`, `agents.md`, `changelog.d/5247-stable-twin-guard-check.md` [new].
   - **Done when:**
     - `tests/test_claude_twin_sync.py` passes locally, including the new unit and CI-step tests;
     - `yamllint` and `ruff` pass as CI runs them;
     - `python3 scripts/claude_twin_sync.py check --base origin/<base> --head HEAD` passes on the phase branch;
     - `git diff --name-only origin/<base>... -- .claude` is empty.
   - **Rollback:** revert the PR. The step returns to skipping `stable`.

## Implementation Steps

Phase 1:

1. **`scripts/claude_twin_sync.py`**
   - `check_not_ahead`: add the keyword parameter `guard_provenance_ref: str | None = None`. For each changed path, when the parameter is set and `is_guard_path(rel)` holds, compare `blob_at(repo, head, path)` with `blob_at(repo, guard_provenance_ref, path)` on sha and mode, and record a violation on any difference (one side missing counts). This runs before the "both missing" `continue`. Add `guard_provenance_ref` to the returned dict.
   - `main`: `check` gets `--guard-provenance-ref` (default empty). A non-empty value is resolved with `resolve_commit` and passed on.
   - Update the module docstring's `check` line and the function docstring.
2. **`.github/workflows/ci.yml`**, step "Claude twin sync state (CLAUDE.md §28.C)" (name, `env` keys, and `main` branches unchanged):
   - add `GH_TOKEN: ${{ github.event_name == 'push' && github.token || '' }}` to the step `env`;
   - add the `pull_request` / `stable` and `push` / `stable` branches (G1, G3);
   - fetch `main` at depth 1 and build the `--guard-provenance-ref` argument for the `stable` branches (G2);
   - keep the skip message for every other event, reworded to name `stable`'s rule;
   - comment the §15 audit (the only `gh` call in the step).
3. **`tests/test_claude_twin_sync.py`**
   - Unit tests for `check_not_ahead` with a provenance ref:
     - a guard change equal to `main` passes;
     - a guard change with both copies matching but not on `main` fails;
     - a guard deletion on both copies fails while `main` keeps the file;
     - a non-guard change ignores the ref;
     - without the ref, the old results hold.
   - CLI test for `--guard-provenance-ref` (exit 0/1/2).
   - CI-step tests: extract the step's `run` body from `ci.yml` and run it with `bash` in a temporary repo that has a bare `origin`, a copy of the script, and a stub `gh` on `PATH`. Cases:
     - a PR into `stable` that changes a hook without its twin fails;
     - a PR into `stable` whose hook change matches the twin but not `main` fails;
     - a PR into `stable` that backports `main`'s hook passes;
     - a promotion push to `stable` (stub prints `ahead`) skips, even over a range that would fail;
     - a non-promotion push to `stable` (stub prints `diverged`) is checked and fails;
     - a failing `gh` is checked and fails;
     - a PR into `main` with a matching guard change still passes (no provenance rule);
     - an unrelated event skips.
4. **`agents.md`**: rewrite the "Sync-state check" bullet of "Claude twin sync" for the `stable` rules (G1–G3).
5. **`changelog.d/5247-stable-twin-guard-check.md`** [new]: `<!-- changelog: security -->` entry per §20.D.
6. **Progress log** `docs/implement-plan/issue-5247-stable-twin-guard-check.md`.

## Files & Modules

- `scripts/claude_twin_sync.py`
- `.github/workflows/ci.yml`
- `tests/test_claude_twin_sync.py`
- `agents.md`
- `changelog.d/5247-stable-twin-guard-check.md` [new]
- `docs/plans/issue-5247-stable-twin-guard-check-plan.md` [new] (this plan)
- `docs/implement-plan/issue-5247-stable-twin-guard-check.md` [new] (log)

## Tests

- **Unit and CLI:** the new `check_not_ahead` and `main(["check", …])` cases, on throwaway git repos.
- **CI step, behavioural:** the extracted `run` body executed with `bash` against temporary repos and a stub `gh`, covering every event branch (step 3).
- **Existing:** the whole `tests/test_claude_twin_sync.py` module and `tests/test_ci_shared_shell_block_guard.py`; `yamllint -s .github/workflows/ci.yml`; `ruff check --select E,F --ignore E501 scripts/claude_twin_sync.py`.
- **End-to-end:** the project's validation run and the next PR into `stable` exercise the step on GitHub.

## Risks & Mitigations

- **A promotion push whose compare call fails gets the strict check and can fail on a pending sync, which blocks the release gate.** ACCEPTED: it needs both an API failure and a pending sync of a twin changed after its last sync, and a re-run clears it. Failing closed is the §1 choice.
- **A legitimate `stable`-only guard fix now fails CI.** ACCEPTED (AD-2): land it on `main` first (through the sync PR), then backport or promote.
- **A PR into `stable` that carries a whole `main` range can read as ahead.** ACCEPTED (AD-4): promotions use `promote-main-to-stable.yml`'s push, which is skipped.
- **The compare API adds a network dependency to CI on `stable` pushes.** Mitigated: one call, `?per_page=1`, and a failure degrades to the full check.

## Rollout

The fix ships with the #4785 project: this project's final PR merges into `claude/implement-plan-issue-4785-twin-first-claude-sync`, and #4804 carries it to `main`. `stable` gets it at the next promotion. No flag and no consumer impact: `ci.yml` is not a consumer template. Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-09-29] How does CI tell a promotion push to `stable`, which it should skip, from a push it must check? — Picked: A — ask the compare API whether the pushed commit is already on `main` (`ahead` or `identical` means promotion), and run the full check when the call fails. Alternatives: B — treat a push of more than one commit as a promotion; C — never skip `stable` pushes. Why: B is wrong both ways, because a merge-commit PR merge pushes two or more commits and a promotion can move one commit. C reintroduces the #4785 promotion false positive, which blocks the release gate. A skips only content `main` already checked (§1). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as "verified sync provenance" for a guard change into `stable`? — Picked: A — the guard `.claude/` file at the head must equal the same path on `main`'s tip (blob and mode, or absent on both). Alternatives: B — refuse every guard change into `stable`; C — accept any version the path ever had on `main`, which needs a full-history fetch in CI. Why: A blocks guard content that never passed `main`'s owner-reviewed sync, allows a backport, and needs one depth-1 fetch (§1, §5). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Does the provenance rule also apply to PRs into `main`? — Picked: A — no, only to `stable` events. Alternatives: B — apply it to `main` too. Why: on `main` the owner-reviewed sync PR is the provenance, and the finding is about `stable` (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should a PR into `stable` whose head is already on `main` (a promotion opened as a PR) skip like a promotion push? — Picked: A — no, every PR into `stable` is checked. Alternatives: B — skip it after the same compare call. Why: promotions are pushes by `promote-main-to-stable.yml`, and the finding asks that PRs into `stable` fail closed (§1). Applied in: phase 1. Status: pending review

## Notes

- `.claude/scripts/security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5247` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`, so the security pass is skipped.
- Issue base `claude/implement-plan-issue-4785-twin-first-claude-sync` is not the default branch, so this project ends after its final merge (`Activation: n/a`) and the final-merge stage closes #5247.

## References

- Issue #5247; audit tracker #3576; issue #4785 and its plan `docs/plans/issue-4785-twin-first-claude-sync-plan.md` (project branch); final PR #4804
- `.github/workflows/ci.yml`, `scripts/claude_twin_sync.py`, `.github/workflows/promote-main-to-stable.yml`, `.github/workflows/mark-stable.yml`
