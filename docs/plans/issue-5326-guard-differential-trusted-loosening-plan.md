# Guard differential check: accept intended loosening only from a base-branch policy, never from the PR body

Source issue: shubhodeep1/coding-workflows#5326 (https://github.com/shubhodeep1/coding-workflows/issues/5326)
Base branch: claude/implement-plan-issue-5174-guard-differential-check
Security pass: skip (ai:security: automation-produced issue)

## Summary

The guard differential check (`scripts/guard_differential.py`, issue #5174) passes a regression when the PR body lists it under `Intended loosening:`. The PR author writes that body, so any author can weaken a guard and exempt the loosening themselves. This plan makes the PR body data only: an exception counts only when a policy file read from the **base** ref names the hook, the exact corpus shape, and the PR's head commit.

## Context

- The security audit of #5174's project branch filed #5326 (A01 Broken Access Control, high, confidence 10/10) against `scripts/guard_differential.py:584`, where `intended=loosened and is_intended(shape, listed)` accepts `listed` straight from `--pr-body-file`.
- `.github/workflows/ci.yml` step `Guard differential check (issue #5174)` writes the PR body from `GITHUB_EVENT_PATH` to a file and passes it as `--pr-body-file`. The body is author-controlled, and the retire-master classifier that was meant to hold such a PR for the operator ("retire-master Q3: A") is not on `main`, so today the listing's only effect is a green check.
- The check already trusts the base ref: it runs the base hooks and the base corpus, so a PR cannot hide a loosening by deleting a corpus shape. A policy read from the same base ref sits inside that same trust boundary.
- This repo's PRs, unattended ones included, are authored by the owner account, so a `author_association` check on the PR author would gate nothing (AD-1).
- The base branch is #5174's project branch (open draft final PR #5185); `ci.yml` runs only on PRs into `main` / `stable`, so the change takes effect when #5185 merges.

## Goals

- G1: A shape listed under `Intended loosening:` in the PR body, with no matching base-branch policy entry, still fails the check (exit 1, `::error::GUARD_DIFFERENTIAL regression …`). This is the exploit scenario of #5326.
- G2: A shape passes as an intended loosening only when `.github/guard_differential/intended_loosening.json` **at the base ref** has an entry whose `hook`, `shape`, and `head_sha` match the loosened shape's hook, its verbatim corpus line, and the PR's head commit. Entries in the PR's own copy of the file are never read.
- G3: A run with no known head revision (a working-tree run without `--head-sha` or `--head-ref`) matches no policy entry, so it fails closed.
- G4: `ci.yml` passes the PR head commit from the event payload (`github.event.pull_request.head.sha`, through `env:`) as `--head-sha`; still no GitHub API calls (§15).
- G5: A malformed policy file at the base ref, or a malformed `--head-sha`, exits 2 (setup error) when a hook tree is compared; a PR that changes no hook still skips.
- G6: The docstring, `agents.md`, the `ci.yml` comment, the unreleased #5174 changelog fragment, and #5174's plan goal G5 describe the new rule.

## Non-goals

- Verifying who approved an entry beyond the base-branch merge: `approved_by` and `reason` are required audit fields, not checked against GitHub (no API calls, §15).
- Holding policy-edit PRs for the operator (the retire-master classifier owns sensitive-path routing and is not on `main`).
- Changing any hook, corpus shape, or the loosening rule itself.
- Removing `--pr-body-file` or `intended_loosening()` (§6).

## Constraints

- §1: security first; every unmatched or unverifiable case fails closed.
- §5: one script, its tests, one `ci.yml` step, one new policy file, docs.
- §6: no identifier renamed or removed. `--pr-body-file`, `intended_loosening()`, `is_intended()`, `ShapeResult.intended`, the JSON `intended` field, the `intended_loosening` log line, and the summary line keep their names; `intended` now means "approved by the base policy". New names (`--head-sha`, `LOOSENING_POLICY_PATH`, `LooseningApproval`, `load_loosening_policy`, `pr_body_listed`, `approved_by`) collide with nothing in the script or its tests.
- §9: Python tabs; YAML 2-space.
- §15: no GitHub API calls; the head SHA comes from the event payload.
- §18: wired into the existing `ci.yml` step; no new script to run by hand, no removal-registry entry.
- §20: correct the unreleased `changelog.d/5174-guard-differential-check.md` sentence instead of a second fragment (AD-5).
- §27: `ci.yml` stays far below 480,000 bytes.
- §28.C: no `.claude/**` edit, so the phase is not protected-path.

## Approach

`run_check` gains a `head_sha` input. It resolves the head revision as: `--head-sha` when given (must be 40 or 64 lowercase hex), else the commit `--head-ref` resolves to, else none. When at least one hook tree is compared, it reads the policy with `git cat-file -p <base_ref>:.github/guard_differential/intended_loosening.json` (absent means no entries) and validates it strictly: a JSON object with an `exceptions` list whose entries each carry non-empty strings `hook` (a hook file stem), `shape`, `head_sha` (40 or 64 lowercase hex), `approved_by`, and `reason`. It keeps only the entries whose `head_sha` equals the head revision.

`compare_hook_dirs` gets those approvals. A loosened shape is `intended` only when an approval names its hook and its exact corpus text. The PR body is still parsed; a loosened shape it lists is marked `pr_body_listed`, which the regression line (`pr_body_listed=true`) and the JSON output report, but it never excuses the regression. The failure hint names the policy path and the entry to add on the base branch, and says a new push needs a new entry.

Alternatives considered: dropping exceptions entirely (every intended loosening would need an operator to merge past a red required check, §23.C); gating on the PR author's association (every PR here is the owner's, so it gates nothing); binding to the hook tree's object id instead of the head commit (survives unrelated pushes, but the issue asks for head-revision scope and a merge commit's tree can differ from the PR's).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the fix is one self-contained change to one script and its wiring.

1. **Phase 1: base-branch loosening policy.** Script change, policy file, tests, `ci.yml` step, docs.
   - Files: see Files & Modules.
   - Done: `tests/test_guard_differential.py` passes, including the #5326 exploit test (PR-body listing alone exits 1) and the policy tests; `yamllint` is clean on `ci.yml`; the other guard and workflow test suites touched by the docs still pass.
   - Rollback: revert the PR. The check then accepts PR-body listings again; nothing else depends on the policy file.

## Implementation Steps

Phase 1:
1. `scripts/guard_differential.py`: add `LOOSENING_POLICY_PATH`, the `LooseningApproval` dataclass, `load_loosening_policy(repo_root, base_ref)` (strict validation, `SetupError` on a bad file), and head-revision resolution in `run_check` (new `head_sha` keyword; the policy loads only when a tree is compared).
2. Same file: `compare_hook_dirs` takes `approvals`; `intended` comes from approvals only; add `ShapeResult.pr_body_listed` / `approved_by`; print `pr_body_listed=true` on regressions, `approved_by=` on intended lines, the new JSON field, and the updated hint.
3. Same file: `--head-sha` CLI flag; docstring and `intended_loosening()` docstring say the body is data.
4. `.github/guard_differential/intended_loosening.json` [new]: `{"version": 1, "exceptions": []}`.
5. `.github/workflows/ci.yml`: the check step passes `--head-sha "${GUARD_DIFFERENTIAL_HEAD_SHA}"` from `env: GUARD_DIFFERENTIAL_HEAD_SHA: ${{ github.event.pull_request.head.sha }}`; update its comment.
6. `tests/test_guard_differential.py`: replace the two tests that encoded the PR-body exemption; add policy, head-revision, and wiring tests.
7. `agents.md`, `changelog.d/5174-guard-differential-check.md`, `docs/plans/issue-5174-guard-differential-check-plan.md` (G5): describe the policy rule.

## Files & Modules

- `scripts/guard_differential.py`
- `.github/guard_differential/intended_loosening.json` [new]
- `.github/workflows/ci.yml`
- `tests/test_guard_differential.py`
- `agents.md`
- `changelog.d/5174-guard-differential-check.md`
- `docs/plans/issue-5174-guard-differential-check-plan.md`

## Tests

- Unit (`tests/test_guard_differential.py`):
  - a PR-body listing alone leaves the regression (`intended` false, `pr_body_listed` true);
  - an approval excuses only its own hook and shape;
  - `load_loosening_policy` accepts the shipped file and an absent file, and rejects bad JSON, a non-object, a missing field, an empty string, a bad `head_sha`, and a hook with a `.py` suffix.
- CLI, in a scratch repository:
  - the #5326 exploit (loosened hook plus a PR-body listing) exits 1 with `pr_body_listed=true`;
  - a base-ref entry for the `--head-ref` commit exits 0 with `intended_loosening … approved_by=`;
  - an entry for another commit, another hook, or only in the head's copy of the file exits 1;
  - a working-tree run fails closed, and passes with a matching `--head-sha`;
  - a malformed base policy or `--head-sha` exits 2;
  - a malformed policy with no hook change still skips.
- Wiring: the `ci.yml` step passes `--head-sha` from `github.event.pull_request.head.sha` through `env:`, still with no `gh api`; `agents.md` names the policy path; the shipped policy parses.
- The existing real-hook and corpus tests run unchanged.

## Risks & Mitigations

- A later PR could add a policy entry on the base branch and get it auto-merged by the review workflow. ACCEPTED — that is the check's existing trust boundary (the base hooks and base corpus are trusted the same way); the entry is a separate, reviewed change scoped to one head commit, hook, and shape, and carries `approved_by` / `reason` for audit.
- An intended loosening needs a new entry after every push to the PR (including base merges). ACCEPTED — that is the head-revision scope #5326 asks for; the hint says so.
- A malformed policy on the base branch fails every hook-changing PR. Mitigation: strict validation with a precise `SetupError`, and PRs that change no hook skip before the policy is read.

## Rollout

Lands on #5174's project branch through this project's final PR, then reaches `main` with #5185. No flag: the check is new on `main`, so no released behaviour changes. Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Where may an intended-loosening exception come from? — Picked: A — a policy file read only from the base ref, each entry scoped to hook, verbatim shape, and PR head commit, with required `approved_by` / `reason` audit fields. Alternatives: B — no exceptions at all; an operator merges past the red check (§23.C); C — keep the PR body but require an OWNER/MEMBER PR author. Why: A is machine-verifiable with no API call and sits in the check's existing base-ref trust boundary; C gates nothing here because every PR is the owner's. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What does the PR body's `Intended loosening:` section do now? — Picked: A — keep parsing it (§6) but as data only: a listed shape without a policy entry still fails, and its regression line carries `pr_body_listed=true`. Alternatives: B — ignore the body, leaving `--pr-body-file` a no-op; C — require both the body listing and the policy entry. Why: keeps the flag and parser meaningful for reviewers without letting the author authorize anything. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Where does the policy live, and in what format? — Picked: A — `.github/guard_differential/intended_loosening.json`, `{"version": 1, "exceptions": [...]}`. Alternatives: B — `tests/guard_corpus/intended_loosening.json`; C — a markdown list. Why: separate from the corpus that PRs routinely edit, never matched by the corpus loader's `*.txt` glob, and strictly parseable. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] How does the check learn the head revision? — Picked: A — a new `--head-sha` flag fed from `github.event.pull_request.head.sha` in CI; otherwise the `--head-ref` commit; otherwise none, and no entry matches. Alternatives: B — the checked-out merge commit, which changes on every base move; C — the head hook tree's object id. Why: the event payload is GitHub's, not the author's, and #5326 asks for head-revision scope. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] New changelog fragment or correct #5174's unreleased one? — Picked: A — correct the one sentence in `changelog.d/5174-guard-differential-check.md`, no new fragment. Alternatives: B — add `changelog.d/5326-….md` as a `security` entry as well. Why: the PR-body exemption never reached a release, so a separate security entry would describe behaviour no release carried and contradict the #5174 entry. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Update #5174's plan goal G5, which specifies the PR-body exemption? — Picked: A — amend G5 to the policy rule with a pointer to #5326, so #5174's later conformance and activation audits grade the fixed behaviour. Alternatives: B — leave it, and let #5174's audits flag the fix as a regression. Why: the goal is superseded by a security finding filed against that project. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5326; issue #5174, its plan `docs/plans/issue-5174-guard-differential-check-plan.md`, and final PR #5185; security audit tracker #3576.
