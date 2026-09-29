# Implement-Plan Log — Guard differential check: run base and PR guard hooks side by side before a guard change lands

- Plan: docs/plans/issue-5174-guard-differential-check-plan.md
- Source issue: shubhodeep1/coding-workflows#5174   Progress comment: 5891958656
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5174-guard-differential-check   Final PR: #5185 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance fix PR (see ## Conformance)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_019rGgoLZBfTcYZUkbRAYsRN (project checker; per-wait safety net and hand-back ids are in the stage report)
- Last updated: 2026-09-29
- Last note: conformance 1/3: CONFORMANT (all goals G1-G6 met, G1 re-run: b6dd693 exits 1, 03c2487 exits 0); one stale-doc concern fixed in the conformance fix PR (the guard differential steps now run in `tests-hooks-and-orchestrator`, not `lint`, after #4874's job split came in with the main sync).

## Phases
1. [x] Phase 1 — guard differential check (script, corpora, tests, ci.yml steps, agents.md, changelog) — PR #5187 merged 2026-09-29 (a295948); review rounds: 5 (round 5: all 3 findings rejected, no verdict bot configured, so the project stopped BLOCKED and the owner merged the PR per Q1: A); interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — fix PR (stale `lint` job wording in scripts/guard_differential.py, agents.md, changelog.d/5174-guard-differential-check.md) (pre-security)

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which decision changes count as loosening? — Picked: A — every drop in the order block = deny > ask > none > allow = error without a warning, including `none` → `allow`. Alternatives: B — only block/deny/ask → allow/none, exactly as the issue words it. Why: §1 security first; for the `gh api` guard an explicit `allow` auto-approves a call the normal permission flow would have judged. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How to run the check "from the #4785 sync" when `scripts/claude_twin_sync.py` and the retire-master classifier are not on `main`? — Picked: A — the `ci.yml` step gates every PR into `main`, sync PRs included, whose auto-merge waits for green checks; document it and leave the script untouched. Alternatives: B — also edit the retire-master plan's phase 3; C — wait for #4785. Why: §5, and the wiring target does not exist yet. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] The inline-edit guard is not on `main`. Ship its corpus? — Picked: A — ship it; it runs as soon as the hook exists on either side. Alternatives: B — omit it until #4858 lands. Why: the issue asks for it, and it is inert until then. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] What does `x` in the issue's `cd x` / `git -C x` seeds point at? — Picked: A — the scratch worktree for separator shapes and a missing directory for `-C` / `GIT_DIR` / subshell shapes, so each seed has a block-or-warn expectation. Alternatives: B — an existing clean worktree for all of them, which would make #5144's intended `git -C` behaviour fail. Why: keeps the corpus adversarial rather than encoding intended loosening. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Where does CI read the PR body? — Picked: A — the `pull_request` event payload; a later body edit needs a push. Alternatives: B — add the `edited` trigger to `ci.yml`; C — an API read (the issue forbids API calls). Why: no API calls, no extra runs. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] `ci.yml` runs only on PRs into `main` / `stable`. Widen it for project-branch phase PRs? — Picked: A — no; the final PR and every sync PR into `main` are gated. Alternatives: B — a separate workflow for every PR base. Why: §5, and the issue asks for a `ci.yml` step. Applied in: no code change. Status: pending review
- AD-7 [plan, 2026-09-29] A changed guard with no corpus file? — Picked: A — fail when the guard exists on both sides; a new guard needs none to pass (it still fails where it answers `allow`). Alternatives: B — skip silently. Why: every guard stays covered. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Gaps the corpora show on `main` (plan Notes)? — Picked: A — record them, fix nothing in the hooks. Alternatives: B — fix them in this project (protected `.claude/**` edits, out of scope). Why: §5 and §28.C. Applied in: no code change. Status: pending review
- AD-9 [phase 1/1 — review round 4, 2026-09-29] A changed `*_guard.py` that exists on only one side (new or deleted) and has no corpus? — Picked: A — fail it as `missing_corpus`, like a guard on both sides; a local run without `--head-ref` also counts untracked hook files as changed. Alternatives: B — keep AD-7's carve-out (a new guard needs no corpus). Why: §1; without a shape a new guard that answers `allow` (a loosening from `none`, AD-1) is never run, so a bypass moved into a new guard file would pass silently, and AD-7's own reason is that every guard stays covered. Applied in: PR #5187. Status: pending review

## Lessons
- [source:intervention] When a test harness filters the caller's environment to isolate a subprocess, apply the same filter to every other source merged into that environment (scenario or fixture overrides). Otherwise a later override can undo the isolation. (files: scripts/guard_differential.py)
- [source:intervention] A "required input is missing" gate must test for an empty parsed input, not only an absent key: a comments-only file parses to an empty list and otherwise passes the gate with zero checks run. (files: scripts/guard_differential.py)
- [source:intervention] A coverage gate ("every X needs a test corpus") must apply to new X too, not only to X that existed before: an exemption for new items lets a loosening move into a new file and skip the check. In working-tree mode, `git diff <base>` omits untracked files, so add `git ls-files --others --exclude-standard` when the detector feeds a gate. (files: scripts/guard_differential.py)
- [source:conformance] After syncing the default branch into a project branch, re-read the project's own docs for names the sync may have changed (a CI job, a step, a path): a clean merge can leave the docs pointing at a structure that no longer exists. (files: .github/workflows/ci.yml, agents.md)

## Notes
- 2026-09-29: phase 1 stopped BLOCKED at review round 5 (issue comment 5899421387; that BLOCKED status was never committed to this log because no PR was in flight). The owner answered Q1: A, merged PR #5187 as a295948, and commented `/reclarify` (issue comment 5900159063). The resumed session synced main into the project branch (a50ae2e, clean merge that brought #4874's CI job split) and ran conformance 1/3.
- Session started with the repo attached mid-session, so `gh` came from running `.claude/hooks/session-start.sh` by hand. No `mcp__github__*` tools were available; GitHub writes use `gh api` routine calls (§23.B/§23.H).
- Pre-existing guard gaps on `main` (not regressions, recorded per AD-8): `gh_api_write_guard.py` allows `gh api … -F body=@<file>` although CLAUDE.md §23.D says file-backed fields always prompt; `pr_merge_status_guard.py` gives no decision for `(cd <dir> && git push)`.
