# Guard differential check: fail closed on settings-only guard disablement

Source issue: shubhodeep1/coding-workflows#5328 (https://github.com/shubhodeep1/coding-workflows/issues/5328)
Base branch: claude/implement-plan-issue-5174-guard-differential-check
Security pass: skip (ai:security: automation-produced issue)

## Summary

`scripts/guard_differential.py` (issue #5174) only runs when a hook `*.py` file changes, so a PR that edits only `.claude/settings.json` and its `workflow-templates/` twin can rewire a guard to `python3 -c 'pass' "$CLAUDE_PROJECT_DIR"/.claude/hooks/<guard>.py` and pass every check. This plan adds both settings files to the check's change detection and fails closed on any change to a guard's wiring that the check cannot verify.

## Context

- Security audit finding `settings-only-guard-disablement` (high, confidence 9/10) at `scripts/guard_differential.py:619`, filed as #5328 against the #5174 project branch (`Refs #3576`, the audit tracker).
- `scripts/guard_differential.py:616-622`: `changed_paths` diffs only `HOOK_TREES`, and `run_check` keeps only `*.py` paths, so a settings-only PR prints `status=skipped reason=no-hook-change`.
- Both `settings.json` copies wire four guard entries: `pr_merge_status_guard.py` twice (Bash; `mcp__github__push_files|mcp__github__create_or_update_file`), `gh_api_write_guard.py` (Bash), and `pr_watch_guard.py` (`mcp__.*__subscribe_pr_activity`), each as `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<hook>.py`.
- The existing wiring tests (`tests/test_pr_merge_status_guard.py` and similar) check that the hook path appears in the command string. The attacker's command keeps the path as an argument, so they still pass.
- `.github/workflows/ci.yml` step `Guard differential check (issue #5174)` runs the script on every pull request into `main` and `stable`, including this project's final PR chain into `main`.

## Goals

- G1: A diff that changes only `.claude/settings.json` or `workflow-templates/.claude/settings.json` is no longer skipped. The check compares the guard wiring on both sides.
- G2: Rewiring a guard to `python3 -c 'pass' "$CLAUDE_PROJECT_DIR"/.claude/hooks/<guard>.py` (the finding's exploit) fails the check with exit 1 in both settings files.
- G3: Removing a guard entry, changing its matcher to anything that does not cover the base matcher, lowering its timeout, changing any other key of the entry (`type`, `async`, …), setting `disableAllHooks`, changing the top-level `env` object (AD-6), or making the head file unparseable fails the check.
- G4: A change that keeps the guard verifiably executing passes. That covers an identical entry, a canonical `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<hook>.py` command whose hook file exists at the head, a plain tool-name matcher widened to a superset, a raised timeout, and a settings edit outside guard wiring (such as `permissions.allow`).
- G5: A wiring change listed under `Intended loosening:` in the PR body passes and is reported as intended. The listing uses the identity the error line prints.
- G6: No GitHub API calls are added (§15). `agents.md`, the `ci.yml` step comment, and a changelog fragment describe the new rule.

## Non-goals

- New non-guard hook entries, such as a `PermissionRequest` hook that answers `allow`, and `permissions.*` rule changes. Retire-master phase 3's classifier owns rule diffs. This is recorded as AD-4.
- Tightening the per-hook string wiring tests in `tests/test_*_guard.py` (§5). The new check supersedes them for this class.
- Any `.claude/**` edit: this phase touches none, so it is not protected-path (§28.C).

## Constraints

- §1 security first: an unverifiable change fails closed.
- §5 minimal change set: one script extended, its tests, one `ci.yml` comment, `agents.md`, one changelog fragment.
- §6: no existing identifier, CLI flag, or `GUARD_DIFFERENTIAL` log key is renamed or removed. The summary line gains keys only at its end (`settings=`, `wiring_regressions=`), and the new names collide with nothing in the script.
- §9: tabs in Python; YAML stays 2-space.
- §15: git reads only, no API calls.
- §18: runs from the existing `ci.yml` step (pull request trigger), so no new script or removal-registry entry is needed.
- §20: one fragment, `changelog.d/5328-settings-guard-wiring-check.md`.

## Approach

`changed_paths` also diffs the two settings files, and untracked ones in working-tree mode. For each settings file that changed, the script parses base and head (at the ref, or the working tree without `--head-ref`). It then extracts every **guard wiring**: a hook entry, under any event, whose `command` names `.claude/hooks/<name>_guard.py`. For every base wiring it looks for a head wiring with the same event and hook that:

1. has the same `command`, or a command that is exactly `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<hook>.py` with `<hook>.py` present in the head's matching hooks tree (`.claude/hooks/` or `workflow-templates/.claude/hooks/`);
2. has a matcher that covers the base matcher: identical; absent, empty, or `*`; or, when both are plain tool-name lists (`A|B`), a superset;
3. has a timeout at least the base timeout (absent = Claude Code's 60-second default);
4. has every other entry key and group key (anything but `command`, `timeout`, `matcher`, `hooks`) equal to the base.

A base wiring with no such head wiring is a **wiring regression** with identity `settings:<file>:<event>:<matcher>:<hook>`. `disableAllHooks` turning truthy (`settings:<file>:disableAllHooks`) and an unparseable or non-object head file (`settings:<file>:unparseable`) are regressions too. An unparseable base contributes no wiring. Each regression prints one `::error::GUARD_DIFFERENTIAL wiring_regression settings=… event=… matcher=… hook=… reason=… shape=…` line and exits 1. A listed identity prints `GUARD_DIFFERENTIAL intended_wiring_change …` instead.

Alternatives: running the head's wired command through a shell against the corpus (AD-2 B) was rejected, because a command that runs the guard only in CI (`[ -d /home/runner ] || exit 0; …`) would pass. Failing every change with no verification (AD-2 C) would block harmless widenings.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the finding is one self-contained change to one script.

1. **Phase 1: settings guard-wiring check.** Extends `scripts/guard_differential.py` and its tests, and updates the `ci.yml` step comment, `agents.md`, and a changelog fragment.
   - Files: see Files & Modules.
   - Done: `tests/test_guard_differential.py` passes, including the exploit (G2) for both settings files and each G3/G4/G5 case. The script at the phase head, against the base branch, reports `status=pass` for an unchanged repo and exit 1 for the exploit applied to the working tree. `yamllint` is clean on `ci.yml`.
   - Rollback: revert the PR. Nothing else depends on the new code.

## Implementation Steps

1. `scripts/guard_differential.py`: add `SETTINGS_FILES` (hooks tree → settings path), a `GuardWiring` dataclass, `WiringResult`, `extract_guard_wiring`, `read_settings`, `matcher_covers`, and `compare_settings_wiring`. Extend `changed_paths` with the settings paths, and extend `Report` (`settings`, `wiring`, `wiring_regressions`, `failed`), `run_check`, and `print_report`. Update the module docstring.
2. `tests/test_guard_differential.py`: unit tests for extraction, matcher coverage, and every G2–G5 case, plus CLI tests on the `hook_repo` fixture extended with a settings file (working tree and `--head-ref`, both settings paths).
3. `.github/workflows/ci.yml`: update the step comment to say it also runs when either settings file changes.
4. `agents.md` "Guard differential check" section: add the wiring rule, its identity format, and its output lines.
5. `changelog.d/5328-settings-guard-wiring-check.md` [new].

## Files & Modules

- `scripts/guard_differential.py`
- `tests/test_guard_differential.py`
- `.github/workflows/ci.yml` (comment only)
- `agents.md`
- `changelog.d/5328-settings-guard-wiring-check.md` [new]

## Testing & Verification

- `python3 -m pytest -q -p no:cacheprovider tests/test_guard_differential.py`.
- Manual: apply the finding's exploit to both settings files in the working tree and run `python3 scripts/guard_differential.py --base-ref HEAD`. Expect exit 1 with two `wiring_regression` lines per guard entry. Revert, then expect `status=skipped`.
- `yamllint` on `ci.yml`; the repo's other hook test suites stay green.

## Risks

- **A legitimate wiring change now needs an `Intended loosening:` entry**, which under the retire-master Q3: A rule makes a sync carrying it wait for the operator. That is the intended fail-closed cost.
- **Matcher semantics**: only plain tool-name lists are compared as sets. Any regex matcher change fails closed.
- **`ci.yml` only gates PRs into `main` / `stable`** (#5174 AD-6), so phase PRs into project branches are not checked. This is unchanged.

## Rollout

Ships with the #5174 project's final PR into `main`. There is no flag, and the step is already wired.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which settings entries are "mandatory" guard wiring? — Picked: A — every hook entry, under any event, whose command names `.claude/hooks/<name>_guard.py`, plus the `disableAllHooks` kill switch. Alternatives: B — `PreToolUse` entries only; C — every hook entry, guards or not. Why: covers the finding and the one-key kill switch of the same class without failing every harmless non-guard hook edit (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What verifies that a changed guard command still executes the guard? — Picked: A — only the exact canonical form `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<hook>.py` with that hook file present at the head, a covering matcher, no lower timeout, and all other keys equal; everything else fails closed. Alternatives: B — run the head's wired command through a shell against the corpus and accept matching outcomes; C — fail every change with no verification. Why: §1; B passes a command that runs the guard only under CI, while A is verifiable by construction and still lets harmless edits through. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How does an intended wiring change pass? — Picked: A — list its printed identity (`settings:<file>:<event>:<matcher>:<hook>`) under the existing `Intended loosening:` section, where it counts as loosening. Alternatives: B — no escape hatch. Why: reuses the #5174 mechanism, and the operator still sees it. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Added non-guard hooks that could auto-allow (for example a `PermissionRequest` hook answering `allow`) and `permissions.*` rule changes? — Picked: A — out of scope, recorded under Notes. Alternatives: B — fail every added hook entry in this PR. Why: §5; the finding names changed guard matchers and commands, and retire-master phase 3 classifies rule diffs. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] An unparseable head settings file? — Picked: A — a wiring regression (exit 1), because Claude Code would drop every hook in it; an unparseable base contributes no wiring. Alternatives: B — a setup error (exit 2). Why: fail closed on the loosening side while keeping exit 2 for bad refs. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-30] A settings `env` change reaches every hook process (`CLAUDE_PR_MERGE_GUARD=off`, a `PATH` that shadows `python3`) while the guard command stays canonical. Cover it? — Picked: A — fail closed on any change to the top-level `env` object (identity `settings:<file>:env`). Alternatives: B — a denylist of sensitive keys; C — leave it out of scope. Why: §1; it is the same settings-only guard disablement class, neither settings file has ever had an `env` key, so the rule costs nothing, and a denylist is porous. Applied in: phase 1 PR. Status: pending review

## Notes

- Residual risk (AD-4): a PR can still add a new hook entry that answers `allow` or a `permissions.allow` rule without this check failing. Both are visible in review, and retire-master phase 3 classifies them for the #4785 sync.
- `security_pass_skip.py` reason: `ai:security: created and labelled by the issue automation`.

## References

- Issue #5328; audit tracker #3576; #5174 and its plan `docs/plans/issue-5174-guard-differential-check-plan.md`; `docs/plans/retire-master-session-plan.md` (Q3, Q9).
