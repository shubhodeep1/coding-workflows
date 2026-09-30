# Guard differential: a warning no longer excuses a loosened guard

Source issue: shubhodeep1/coding-workflows#5325 (https://github.com/shubhodeep1/coding-workflows/issues/5325)
Base branch: claude/implement-plan-issue-5174-guard-differential-check
Security pass: skip (ai:security: automation-produced issue)

## Summary

`scripts/guard_differential.py` fails a PR whose guard hook treats a corpus shape less strictly than the base hook, but it skips the failure whenever the new hook printed any non-empty `systemMessage`. A changed guard can therefore turn a blocked shape into pass-through by printing a warning, and CI reports a pass. This plan makes every drop in strictness fail, with or without a warning, and keeps the warning as a diagnostic in the output.

## Context

- Security audit finding `warning-suppresses-guard-regression` (issue #5325, severity high, `scripts/guard_differential.py:576`, audit tracker #3576), filed against the #5174 project branch.
- The check was added by issue #5174 (PR #5187, project branch `claude/implement-plan-issue-5174-guard-differential-check`, final PR #5185, still a draft). Its rule is at `scripts/guard_differential.py:576`: `loosened = STRICTNESS[head.decision] < STRICTNESS[base.decision] and not head.warned`.
- The `Intended loosening:` section of the PR body is the check's only sanctioned way to accept a loosening, and a listed shape makes a #4785 sync wait for the operator (retire-master Q3: A). The warning exemption was a second, unreviewed way around the check.
- The #5174 plan's done condition still holds after the change: against `f736cad`, `b6dd693` still fails (its regressions carry no warning) and `03c2487` still passes. No shape in either run is a loosening with a warning (checked locally, 2026-09-30).
- The project branch changes no hook against `main`, so its final PR #5185 still skips the check.

## Goals

- G1: A shape whose head decision ranks below its base decision (block = deny > ask > none > allow = error) is a regression whether or not the head printed a `systemMessage`, unless the PR body lists it under `Intended loosening:`.
- G2: The warning stays visible as a diagnostic: the `regression` line and the `--json` rows still report `+warning` / `"warned": true`.
- G3: The docstring, the remediation message, the `ci.yml` step comment, `agents.md`, and the unreleased #5174 changelog fragment describe the new rule. None of them tells a PR to "fall back with a warning" any more.
- G4: `tests/test_guard_differential.py` covers a warned block → none, a warned block → ask, and a warned none → allow as regressions, and an equally strict head with a warning as clean.

## Non-goals

- No change to the strictness order, the corpora, the scenarios, the `Intended loosening:` parser, or the CI wiring.
- No change to any `.claude/**` hook.
- The #5174 plan and progress log are records of that project and stay as written.

## Constraints

- §1: security first. The check is the gate for guard changes once the master session is retired.
- §5: the smallest change. One condition, its tests, and the text that describes it.
- §6: no identifier is renamed or removed. `Outcome.warned`, `ShapeResult.loosened`, and the `GUARD_DIFFERENTIAL` output keys stay as they are.
- §9: tabs in Python, 2-space YAML.
- §15: the check still makes no GitHub API calls.
- §18: no new script. The check already runs from `ci.yml`.
- §20: see AD-2.
- §27: `ci.yml` gains no bytes beyond a comment edit.
- §28.C: no `.claude/**` edit, so the phase is not protected-path.

## Approach

Drop `and not head.warned` from the loosening rule in `compare_hook_dirs`. `Outcome.warned` is still recorded and printed, so a reviewer sees that the new hook warned. The remediation message now says to keep the base strictness or list the shape under `Intended loosening:`.

Alternative considered: keep the exemption only when the head answers `ask` or `none` (a fail-open with a warning). Rejected, because a `none` still hands a formerly blocked shape to the normal permission flow, which auto-approves in Auto mode. That is the silent loosening the check exists to stop.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the fix is one condition plus its tests and docs.

1. **Phase 1: fail on a warned loosening.** Change the rule, the tests, and the four texts that describe it.
   - Files: see Files & Modules.
   - Done: `tests/test_guard_differential.py` passes with the new cases. `python3 scripts/guard_differential.py --base-ref f736cad --head-ref b6dd693` exits 1 and `--head-ref 03c2487` exits 0. `yamllint` is clean on `ci.yml`.
   - Rollback: revert the PR. Nothing else depends on the exemption.

## Implementation Steps

1. `scripts/guard_differential.py`: in `compare_hook_dirs` (line 576), `loosened = STRICTNESS[head.decision] < STRICTNESS[base.decision]`. Update the module docstring (step 4 of "What it does", lines 27-32) and the remediation text in `print_report` (lines 700-706).
2. `tests/test_guard_differential.py`: flip the two warned cases in `test_loosening_rule` to regressions, add warned none → allow (regression) and silent → warned none (clean), add a test that the `regression` line still shows `+warning`, and update the module docstring.
3. `.github/workflows/ci.yml`: the `Guard differential check (issue #5174)` step comment drops "without a warning".
4. `agents.md`: the "Guard differential check" **Rule** bullet states that a warning does not excuse a loosening and stays a diagnostic.
5. `changelog.d/5174-guard-differential-check.md`: "became less strict without a warning" becomes "became less strict, with or without a warning" (AD-2).

## Files & Modules

- `scripts/guard_differential.py`
- `tests/test_guard_differential.py`
- `.github/workflows/ci.yml`
- `agents.md`
- `changelog.d/5174-guard-differential-check.md`
- `docs/implement-plan/issue-5325-fail-warned-guard-loosening.md` [new] (progress log)

## Tests

- Unit: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_guard_differential.py`.
- End to end (local): the script against `f736cad` with `b6dd693` (exit 1) and `03c2487` (exit 0), and `--base-ref origin/main --all` on the project branch (exit 0).
- Lint: `yamllint` on `.github/workflows/ci.yml`.

## Risks & Mitigations

- A future guard PR that falls back with a warning on a shape the base blocked now fails CI. ACCEPTED — that is the finding's recommendation; such a PR lists the shape under `Intended loosening:`, and the sync waits for the operator.
- The edit to the #5174 fragment could conflict with a later #5174 stage that edits the same line. Mitigation: the edit changes one phrase, and the #5174 project has passed its conformance audit.

## Rollout

Lands on the #5174 project branch and reaches `main` with #5174's final PR #5185. No flag, no migration, no consumer-side step.

## References

- Issue #5325 (this finding), audit tracker #3576.
- Issue #5174, PR #5187, final PR #5185, `docs/plans/issue-5174-guard-differential-check-plan.md`.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How far should a warning count once the head is less strict? — Picked: A — not at all: every drop in strictness is a regression, and the warning is only printed. Alternatives: B — keep the exemption when the head answers `ask` or `none`; C — keep the exemption only for `ask`. Why: §1; a `none` with a warning still hands a formerly blocked shape to the normal permission flow, and the issue asks for failure "regardless of `systemMessage`". Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which changelog entry records the fix? — Picked: A — correct the one phrase in the unreleased `changelog.d/5174-guard-differential-check.md` and add no new fragment. Alternatives: B — add `changelog.d/5325-fail-warned-guard-loosening.md` with `<!-- changelog: security -->` as well. Why: the warning exemption never reached `main` or `stable`, so a separate "fixed" entry would describe a behaviour no consumer saw (§20.F), while the #5174 fragment would otherwise describe the wrong rule. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should the #5174 plan and progress log be updated to the new rule? — Picked: A — no, they stay as that project's record; the rule is corrected in the code, tests, `agents.md`, `ci.yml`, and the changelog fragment. Alternatives: B — also edit the #5174 plan's Approach paragraph. Why: §5, and those files record what #5174 planned and did. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` returned `skip: true` (`ai:security: created and labelled by the issue automation`).
