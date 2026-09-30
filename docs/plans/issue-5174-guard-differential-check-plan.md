# Guard differential check: run base and PR guard hooks side by side before a guard change lands

Source issue: shubhodeep1/coding-workflows#5174 (https://github.com/shubhodeep1/coding-workflows/issues/5174)
Base branch: main
Security pass: run

## Summary

Add a deterministic CI check that runs a corpus of adversarial command shapes through the base branch's `.claude/hooks/*_guard.py` hook and the PR's hook. It fails on any shape the PR's hook now treats less strictly with no warning, unless the loosening is approved (since #5326, by the base branch's `.github/guard_differential/intended_loosening.json`, not by the PR body). The reviewer panel missed three bypasses in #5144's merge-guard rewrite (PR #5173), and once the master session is retired nothing else would catch them.

## Context

- The guards are the security boundary of unattended sessions: the §21 merged-PR guard (`pr_merge_status_guard.py`), the §23.H `gh api` guard (`gh_api_write_guard.py`), the §25 watch guard (`pr_watch_guard.py`), and #4858's inline-edit guard (`inline_edit_guard.py`, still on `claude/implement-plan-issue-4858-inline-edit-guard`).
- PR #5173 at `b6dd693` passed the reviewer panel. The master's side-by-side run (issue comment 5891249253) found three push shapes the old guard blocked and the new one allowed silently: `HEAD:heads/<branch>`, unexpanded `$VAR` refspec words, and glob refspecs. It also found `cd x || git push`-style separators and a `-`-prefixed refspec source. `03c2487` fixed them.
- `docs/plans/retire-master-session-plan.md` retires the master session. Its phase 3 classifier and #4785's `scripts/claude_twin_sync.py` are not on `main` yet.
- `.github/workflows/ci.yml` runs on pull requests into `main` and `stable`. Its `lint` job holds one step per hook test suite.

## Goals

- G1: `scripts/guard_differential.py` fails on #5173's `b6dd693` for the three review shapes and passes on `03c2487`.
- G2: It makes no GitHub API calls: the hooks run with a stub `gh`, no tokens, and `GIT_ALLOW_PROTOCOL=file`.
- G3: Corpora under `tests/guard_corpus/<hook>.txt` exist for all four guards and hold the issue's seed shapes.
- G4: `ci.yml` runs the check on every pull request that changes a hook tree, as its own step, plus a unit-test step.
- G5: A shape approved by the base branch's `.github/guard_differential/intended_loosening.json` for the PR's head commit passes the check and is reported as an intended loosening. A shape listed only under `Intended loosening:` in the PR body still fails and is reported as `pr_body_listed=true` (superseded by security finding #5326: the PR author writes the body).
- G6: `agents.md` documents the corpus files, the rule, and the `GUARD_DIFFERENTIAL` log prefix.

## Non-goals

- Fixing any guard hook. The gaps the corpora surface on `main` are recorded under Notes, not fixed here: they are protected `.claude/**` edits and out of scope (§5).
- Editing `scripts/claude_twin_sync.py` or the retire-master classifier, which do not exist on `main` yet (AD-2).
- Classifying `settings.json` wiring changes (retire-master phase 3 owns that).
- Running `ci.yml` on phase PRs into project branches (AD-6).

## Constraints

- §5 minimal change set: one new script, corpus files, one test file, two `ci.yml` steps, docs, a changelog fragment.
- §6: no existing identifier renamed. The new names (`guard_differential.py`, `GUARD_DIFFERENTIAL`, `tests/guard_corpus/`, the step names) collide with nothing.
- §9: Python uses tabs; YAML stays 2-space.
- §15: no GitHub API calls in the check. CI fetches the base branch with git and reads the PR body from `GITHUB_EVENT_PATH`.
- §18: wired into the existing `ci.yml` `lint` job (PR trigger), so no manual script and no removal-registry entry: the check is permanent.
- §20: one changelog fragment.
- §27: `ci.yml` stays far below 480,000 bytes.
- §28.C: no `.claude/**` edit, so the phase is not protected-path.

## Approach

For each hook tree (`.claude/hooks/` and `workflow-templates/.claude/hooks/`) in which any `.py` file changed between base and head, the script copies both sides' whole hooks directory, because a hook may load its siblings. It then runs every corpus whose hook exists on either side. Each shape runs through both sides in a fresh scenario: a scratch git repo with origin `https://github.com/o/r.git` and a stub `gh`. The merge guard's scenario reproduces the #5173 review's stranded state: the checkout on merged `feature/x`, a detached worktree, and `feature/open` with an open PR. Each run gets a cold `TMPDIR` cache and scratch `HOME`, with `CLAUDE_*`, `GIT_*`, `GH_TOKEN`, and `GITHUB_TOKEN` removed.

Decisions are parsed from the exit code (2 = block, other non-zero = error) and the `hookSpecificOutput.permissionDecision` / `systemMessage` JSON. They are ranked block = deny > ask > none > allow = error. A shape fails when head < base and the head gave no warning, unless the shape is listed. A hook missing on one side counts as `none`. A changed guard with no corpus fails, a new or deleted one included (AD-9, which narrows AD-7).

Alternatives considered: running the check only on the hook file that changed (rejected, because sibling loading means a tokenizer change alters the inline guard); comparing the twin against the live hook (rejected, because the same-path comparison covers twin-first PRs and sync PRs alike); and reading the PR body over the API (rejected by the issue: no API calls).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue is one self-contained change.

1. **Phase 1: guard differential check.** Adds the script, the corpora, the tests, the `ci.yml` steps, the `agents.md` section, and the changelog fragment.
   - Files: see Files & Modules.
   - Done: `tests/test_guard_differential.py` passes. The script fails on `b6dd693` and passes on `03c2487` against `f736cad`, with `ci.yml` wired and `yamllint` clean.
   - Rollback: revert the PR. Nothing else depends on the script.

## Implementation Steps

1. `scripts/guard_differential.py` [new]: corpus loader, `Intended loosening:` parser, scenario builders, hook runner, comparison, CLI (`--base-ref`, `--head-ref`, `--pr-body-file`, `--corpus-dir`, `--repo-root`, `--all`, `--json`).
2. `tests/guard_corpus/{pr_merge_status_guard,gh_api_write_guard,pr_watch_guard,inline_edit_guard}.txt` [new]: seeded with the issue's shapes and the #5173 review's shapes.
3. `tests/test_guard_differential.py` [new]: parsing, the loosening rule on fake hooks, the git and CLI paths, the shipped corpora and scenario against the real hooks, determinism of the current hooks against themselves, and the `ci.yml` / `agents.md` wiring.
4. `.github/workflows/ci.yml`: in the `lint` job after the `gh api` guard tests, add `Guard differential tests (issue #5174)` and the pull-request-only `Guard differential check (issue #5174)`.
5. `agents.md`: a "Guard differential check" section, plus `GUARD_DIFFERENTIAL` in both stable log prefix registries.
6. `changelog.d/5174-guard-differential-check.md` [new].

## Files & Modules

- `scripts/guard_differential.py` [new]
- `tests/guard_corpus/pr_merge_status_guard.txt` [new]
- `tests/guard_corpus/gh_api_write_guard.txt` [new]
- `tests/guard_corpus/pr_watch_guard.txt` [new]
- `tests/guard_corpus/inline_edit_guard.txt` [new]
- `tests/test_guard_differential.py` [new]
- `.github/workflows/ci.yml`
- `agents.md`
- `changelog.d/5174-guard-differential-check.md` [new]

## Tests

- Unit and integration: `tests/test_guard_differential.py`, which gets its own `ci.yml` step.
- Acceptance (manual, recorded in the PR): `python3 scripts/guard_differential.py --base-ref f736cad --head-ref b6dd693` exits 1 with the three review shapes among the regressions. The same command with `--head-ref 03c2487` exits 0.
- Existing suites touching `ci.yml`, `agents.md`, and changelog fragments stay green.

## Risks & Mitigations

- A flaky scenario fails unrelated hook PRs → the current hooks are run against themselves in a test (`test_current_hooks_are_deterministic_against_themselves`), and every run uses a fresh scenario and cache.
- CI time grows on hook PRs → roughly 15 s per tree; the check skips PRs that change no hook.
- A PR body edited after the push is not re-read → documented; push again (AD-5).
- Guard changes on project branches are not checked until their final PR → ACCEPTED (AD-6): the default branch and every sync PR are gated.

## Rollout

The check lands with the PR and runs on the next hook-changing pull request. No flag and no consumer impact: `ci.yml` and the script are this repo's own. Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which decision changes count as loosening? — Picked: A — every drop in the order block = deny > ask > none > allow = error without a warning, including `none` → `allow`. Alternatives: B — only block/deny/ask → allow/none, exactly as the issue words it. Why: §1 security first; for the `gh api` guard an explicit `allow` auto-approves a call the normal permission flow would have judged. Applied in: this PR. Status: pending review
- AD-2 [plan, 2026-09-29] How to run the check "from the #4785 sync" when `scripts/claude_twin_sync.py` and the retire-master classifier are not on `main`? — Picked: A — the `ci.yml` step gates every PR into `main`, sync PRs included, whose auto-merge waits for green checks; document it in `agents.md` and leave the script untouched. Alternatives: B — also edit the retire-master plan's phase 3; C — wait for #4785. Why: §5, and the wiring target does not exist yet. Applied in: this PR. Status: pending review
- AD-3 [plan, 2026-09-29] The inline-edit guard is not on `main`. Ship its corpus? — Picked: A — ship it; it runs as soon as the hook exists on either side. Alternatives: B — omit it until #4858 lands. Why: the issue asks for it, and it is inert until then. Applied in: this PR. Status: pending review
- AD-4 [plan, 2026-09-29] What does `x` in the issue's `cd x` / `git -C x` seeds point at? — Picked: A — the scratch worktree for separator shapes (the review's bypass) and a missing directory for `-C` / `GIT_DIR` / subshell shapes, so each seed has a block-or-warn expectation. Alternatives: B — an existing clean worktree for all of them, which would make #5144's intended `git -C` behaviour fail. Why: keeps the corpus adversarial rather than encoding intended loosening. Applied in: this PR. Status: pending review
- AD-5 [plan, 2026-09-29] Where does CI read the PR body? — Picked: A — the `pull_request` event payload; a later body edit needs a push. Alternatives: B — add the `edited` trigger to `ci.yml` (reruns the 60-minute `lint` job); C — an API read (the issue forbids API calls). Why: no API calls, no extra runs. Applied in: this PR. Status: pending review
- AD-6 [plan, 2026-09-29] `ci.yml` runs only on PRs into `main` / `stable`. Widen it for project-branch phase PRs? — Picked: A — no; the final PR and every sync PR into `main` are gated. Alternatives: B — a separate workflow for every PR base. Why: §5, and the issue asks for a `ci.yml` step. Applied in: no code change. Status: pending review
- AD-7 [plan, 2026-09-29] A changed guard with no corpus file? — Picked: A — fail the check when the guard exists on both sides; a new guard needs none to pass (it still fails where it answers `allow`). Alternatives: B — skip silently. Why: every guard stays covered. Applied in: this PR. Status: pending review
- AD-8 [plan, 2026-09-29] Gaps the corpora show on `main` (see Notes)? — Picked: A — record them here and in the report, fix nothing in the hooks. Alternatives: B — fix them in this PR (protected `.claude/**` edits, out of scope). Why: §5 and §28.C. Applied in: no code change. Status: pending review
- AD-9 [phase 1/1 — review round 4, 2026-09-29] A changed `*_guard.py` that exists on only one side (new or deleted) and has no corpus? — Picked: A — fail it as `missing_corpus`, like a guard on both sides; a local run without `--head-ref` also counts untracked hook files as changed. Alternatives: B — keep AD-7's carve-out (a new guard needs no corpus). Why: §1; without a shape a new guard that answers `allow` (a loosening from `none`, AD-1) is never run, so a bypass moved into a new guard file would pass silently, and AD-7's own reason is that every guard stays covered. Applied in: PR #5187. Status: pending review

## Notes

- On `main`, `gh_api_write_guard.py` answers `allow` for `gh api repos/o/r/issues/1/comments -F body=@/tmp/body.md` and `-F body=@-`. CLAUDE.md §23.D says file-backed fields always prompt under §23.H. The check records this as the baseline and flags no regression, since the base already allows it.
- On `main` and on #5173, `pr_merge_status_guard.py` gives no decision for `(cd missing-dir && git push)`: the subshell hides the push from its segmenter. The base has the gap too, so it is not a regression.
- On #4858's branch the root `.claude/hooks/inline_edit_guard.py` lags its twin (`env -i python3 -c …` is denied only by the twin), as `docs/operations/master-session.md` records.
- Security finding #5326 replaced the PR-body `Intended loosening:` exemption with the base-branch policy file (G5 above), in #5326's own issue-mode project.

## References

- Issue #5174; #5144 and PR #5173 (review comment 5891249253); #4785; #4858; `docs/plans/retire-master-session-plan.md`.
