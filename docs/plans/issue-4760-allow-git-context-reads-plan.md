# Allowlist the read-only git context reads that preface a default-branch lookup

Source issue: shubhodeep1/coding-workflows#4760 (https://github.com/shubhodeep1/coding-workflows/issues/4760)
Base branch: main
Security pass: run

## Summary

An unattended session ran `git remote -v && git branch --show-current && gh api repos/<owner>/<repo> --jq .default_branch` and Auto mode denied it ("Classifier unavailable"). The `gh api repos/*` read is already allowlisted, but `git remote -v` and `git branch --show-current` are not, so the whole command depended on the Auto-mode classifier. This plan adds exact allow rules for those two read-only commands, so every subcommand of the pattern is approved by a narrow allow rule before the classifier runs.

## Context

- The issue was filed by `.claude/scripts/permission_prompts.py` (CLAUDE.md §23.I) from session `session_01X34Rkb13B5HJabtRPdz8ni`. The issue signature is `sig=3ced71ed47bc`.
- `.claude/settings.json` allowlists `Bash(gh api repos/*)` plus many narrow git rules (`git fetch *`, `git status *`, `git log *`, `git show *`, `git rev-parse *`, `git ls-remote *`, …). It has no rule for `git remote` or `git branch`.
- `.claude/hooks/gh_api_write_guard.py` (CLAUDE.md §23.H) approves a command only when it holds nothing but `gh api` calls and its safe helpers (`cd`, `sleep`, `echo`, `true`, and pipe filters). A `git …` item beside the read makes the guard give no decision, so the allow list or the classifier decides.
- Claude Code docs (auto mode config): narrow Bash allow rules such as `Bash(npm test)` stay in effect in Auto mode and resolve before the classifier. Only broad rules (`Bash(*)`, wildcarded interpreters) are suspended. A PreToolUse hook `allow` only skips the interactive prompt: deny and ask rules still apply, and the docs do not say it skips the classifier.
- Sibling issues from the same one-minute outage window (2026-09-28T07:36Z): #4759 (this pattern plus `&& wc -l <file>`), #4749, #4751, #4761, and #4750. #4750 (an exact-allowlisted MCP tool) and #4751 (`git fetch * && git show *`, both allowlisted) were denied too. This fix removes the pattern's dependence on the classifier as documented. It cannot prevent denials if a classifier outage also overrides allow rules (see Risks, AD-4).
- `workflow-templates/.claude/settings.json` must stay byte-identical to `.claude/settings.json` (`tests/test_gh_api_write_guard.py::test_template_parity`). Consumer repos receive it on the `@stable` sync (§14).

## Goals

- `.claude/settings.json` and `workflow-templates/.claude/settings.json` each carry exactly `Bash(git remote -v)` and `Bash(git branch --show-current)`, stay byte-identical, and stay valid JSON.
- No wildcard `git remote` or `git branch` rule exists, so `git remote set-url|add|remove` and `git branch -D|-m|…` still go to the classifier.
- A test asserts that every subcommand of the #4760 example command matches an allow rule, and that `gh_api_write_guard.py` never asks for it.
- `agents.md`'s Permissions note mentions the two read-only git context reads, and a `changelog.d/` fragment records the change (§7, §20).

## Non-goals

- The `wc -l <file>` part of #4759 and the other outage-window patterns (#4749, #4750, #4751, #4761, #4762, #4767). Each has its own issue and project (AD-3).
- Changing how `permission_prompts.py` files "Classifier unavailable" denials.
- Adding `git` commands to the `gh api` guard's safe helpers (AD-1).
- Command-file prose changes. The call shape came from the model, not from a command step.

## Constraints

- §5 minimal change set: two allow rules, one test, one doc line, one changelog fragment.
- §6: no existing rule, identifier, or hook changes. New rules are additions only.
- §9: `settings.json` keeps its existing 2-space JSON indentation. Tests use tabs like the surrounding file.
- §14: the template copy ships to consumers on the next `@stable` sync. No new consumer repo is onboarded.
- §23.I: never widen a permission for a destructive or administrative action. Both rules are exact read-only commands.
- §28.C: the phase edits `.claude/**`, a protected path Claude Code never auto-approves, so `/implement-plan-claude` stops before the phase and asks on this issue.

## Approach

Add two exact (no-wildcard) allow rules next to the existing git read rules in both settings files. Per the auto-mode docs, Claude Code matches each subcommand of `a && b && c` against the allow list. With `git remote -v`, `git branch --show-current`, and the existing `gh api repos/*` all matched, the command resolves without the classifier.

Alternatives considered (AD-1): making the `gh api` guard treat these git reads as safe helpers (the docs say a hook `allow` does not skip the classifier, and it would only cover commands that contain `gh api`); or prose-only command-file edits (advisory, so the pattern would recur).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for one standalone issue.

1. **Phase 1 — allow the two read-only git context reads.**
   - Files: `.claude/settings.json`, `workflow-templates/.claude/settings.json`, `tests/test_gh_api_write_guard.py`, `agents.md`, `changelog.d/4760-allow-git-context-reads.md` [new].
   - protected paths: `.claude/settings.json` (and its template copy under `workflow-templates/.claude/`).
   - Done when: both settings files carry the two exact rules and no `git remote`/`git branch` wildcard; the files stay byte-identical; the new test and the full `tests/test_gh_api_write_guard.py` pass; the docs line and the fragment are present.
   - Rollback: revert the phase commit. Removing the two rules only restores classifier-dependence for this pattern.

## Implementation Steps

Phase 1:
1. `.claude/settings.json`: after `"Bash(git rev-parse *)",` (line 27) insert `"Bash(git remote -v)",` and `"Bash(git branch --show-current)",`. Copy the file byte for byte to `workflow-templates/.claude/settings.json`.
2. `tests/test_gh_api_write_guard.py`: add a test parametrized over `SETTINGS_PATH` and `TEMPLATE_SETTINGS_PATH` that:
   - asserts both exact rules are present;
   - asserts no rule starts with `Bash(git remote ` or `Bash(git branch ` other than those two;
   - splits the #4760 example on `&&` and asserts each subcommand equals an exact rule or matches a `Bash(<prefix> *)` rule by prefix;
   - asserts `guard.evaluate` does not return `ask` for the example.
3. `agents.md`: extend the "Permissions:" bullet (around line 1027) with the exact read-only `git remote -v` / `git branch --show-current` context reads.
4. `changelog.d/4760-allow-git-context-reads.md` [new]: a `fixed` entry per CLAUDE.md §20.D.

## Files & Modules

- `.claude/settings.json`
- `workflow-templates/.claude/settings.json`
- `tests/test_gh_api_write_guard.py`
- `agents.md`
- `changelog.d/4760-allow-git-context-reads.md` [new]

## Tests

- Unit: the new test in `tests/test_gh_api_write_guard.py`, run with `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_gh_api_write_guard.py`. That file has its own `ci.yml` step (line 421).
- Regression: every other test that reads `permissions.allow`: `tests/test_check_in_status.py`, `test_dispatch_workflow.py`, `test_edit_comment.py`, `test_implement_issue_claude_command.py`, `test_permission_prompts.py`, `test_pr_merge_status_guard.py`, `test_security_pass_skip.py`, and `test_stale_routines.py`.
- JSON validity: `python3 -m json.tool` on both settings files.
- Manual (live): nothing to run by hand. The next session that runs the pattern either passes silently or files a new occurrence on this signature.

## Risks & Mitigations

- A wildcard would approve `git remote set-url` or `git branch -D`. Mitigation: exact rules only, pinned by the test (AD-2).
- Evidence from #4750 and #4751 suggests a classifier outage may deny even allowlisted calls. ACCEPTED (AD-4): the documented behaviour is that narrow allow rules resolve before the classifier. If an outage overrides that, no settings change can prevent it, and the reporter will record new occurrences on this signature.
- Parallel projects for sibling issues (#4759, #4761, …) may edit the same allow-list hunk. Mitigation: the second PR to merge resolves the conflict (`[claude-merge-resolve]`), keeping both sides' rules.

## Rollout

Ships to this repo when the final PR merges into `main`. Consumer repos get the template copy on the next `@stable` sync (§14). No flag and no migration. Rollback is a revert.

## References

- Issue #4760. Siblings: #4749, #4750, #4751, #4759, #4761, #4762, #4767.
- CLAUDE.md §23.H (gh api guard), §23.I (permission prompt reports), §28 (auto-decisions).
- `.claude/hooks/gh_api_write_guard.py`, `.claude/scripts/permission_prompts.py`.

## Auto-decisions

- AD-1 [plan, 2026-09-28] How should the #4760 pattern stop depending on the Auto-mode classifier? — Picked: A — add exact read-only allow rules `Bash(git remote -v)` and `Bash(git branch --show-current)` to both settings files. Alternatives: B — make `gh_api_write_guard.py` treat the two git reads as safe helpers; C — only change command-file prose to run the default-branch read on its own. Why: the docs say narrow allow rules resolve before the classifier, while a hook `allow` does not skip it and only covers commands with `gh api`; prose alone does not stop recurrence. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Exact or wildcard git rules? — Picked: A — exact rules only. Alternatives: B — `Bash(git remote *)` and `Bash(git branch *)`. Why: wildcards would approve `git remote set-url|add|remove` and `git branch -D|-m`, which §23.I forbids widening. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Fold the overlapping #4759 pattern (`… && wc -l <file>`) into this project? — Picked: A — no, keep to #4760. Alternatives: B — also allowlist `wc -l`. Why: `/implement-issue-claude` runs one issue per chain, and #4759 has its own queued project. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-28] #4750 and #4751 show allowlisted calls denied in the same outage window, so the fix may not prevent a denial during a full classifier outage. Proceed? — Picked: A — proceed, and record the residual risk. Alternatives: B — stop and ask whether to close #4760 as a transient outage. Why: the documented precedence says narrow allow rules skip the classifier; closing an issue this session did not open is a §23.C ask-first operation. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-28] Where does the new test live? — Picked: A — `tests/test_gh_api_write_guard.py`, beside the existing settings parity and wiring tests. Alternatives: B — `tests/test_permission_prompts.py`. Why: that file already loads both settings paths and the guard, and it has its own `ci.yml` step. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` returned `{"skip": false, "label": null, "reason": "no skip label"}`.
