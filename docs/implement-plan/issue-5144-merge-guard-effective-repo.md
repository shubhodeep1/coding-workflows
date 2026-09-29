# Implement-Plan Log — §21 merged-PR guard: judge the repository and target ref each git command actually uses

- Plan: docs/plans/issue-5144-merge-guard-effective-repo-plan.md
- Source issue: shubhodeep1/coding-workflows#5144
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5144-merge-guard-effective-repo   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from main; phase 1 starts twin-first.

## Phases
1. [ ] Phase 1 — effective repository and push target ref for the §21 guard — protected paths: `.claude/hooks/pr_merge_status_guard.py`
   - Command walker: effective directory across `cd`, `git -C`, `GIT_DIR=`, `--git-dir`; unresolvable constructs fall back with a warning
   - Push refspec parser: `<src>:<dst>` judged on `<dst>` with `<src>` as tip; deletions and tags skipped; no refspec judges the current branch
   - Per-target judge with one cached REST call per `(slug, branch)`; one merged hook result
   - Tests in `tests/test_pr_merge_status_guard.py` (acceptance list of #5144) reading the twin; CLAUDE.md §21.B / §21.D; `changelog.d/5144-merge-guard-effective-repo.md`
   - Done: new tests pass against the twin; all other tests pass except `test_template_copies_are_identical` until the `[claude-twin-sync]` copy

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Should the `--git-dir <path>` / `--git-dir=<path>` git option be honoured like the `GIT_DIR` prefix the issue names? — Picked: A — yes, same rule. Alternatives: B — only the forms the issue lists. Why: `--git-dir` selects the repository exactly as `GIT_DIR` does. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How should `GIT_WORK_TREE` / `--work-tree` affect the judged repository? — Picked: A — not at all; `HEAD` comes from `GIT_DIR` or directory discovery. Alternatives: B — judge from the work tree path. Why: git reads `HEAD` from the git dir. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which directory changes besides `cd` are modelled? — Picked: A — none; `pushd`/`popd`, subshells, an `export` or bare assignment of `GIT_DIR`, and `cd` targets with `$`, backticks, globs, `~user` or `-` fall back to today's behaviour with a warning. Alternatives: B — model `pushd`/`popd`. Why: §5 and the issue's fallback rule. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Are branch deletions (`--delete`, `:<ref>`) and tag refspecs judged? — Picked: A — no; they land no commits on a branch. Alternatives: B — judge them like branch pushes. Why: blocking the deletion of a merged branch would be a false block. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] What does a push with no branch refspec judge? — Picked: A — the effective repository's current branch with `HEAD`, as today. Alternatives: B — read the upstream config. Why: §5. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] For `git push origin <other local branch>`, what is judged? — Picked: A — that branch, with the local ref as the tip. Alternatives: B — the checked-out branch, as today. Why: that ref is what the push writes. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] How are several guarded git invocations in one command combined? — Picked: A — judge each distinct target, block if any blocks, otherwise one JSON result. Alternatives: B — judge only the last invocation. Why: §21 invariant; the hook protocol takes one JSON object. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the scheduled routine `dispatch shubhodeep1/coding-workflows#5144: deliver` in session session_01YLLYPSWJ9Xqr8pCYFPTRQE; permission mode auto.
- Progress comment: issue #5144 comment 5890591928.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- The session had no `mcp__github__*` tools; GitHub writes go through `gh api` REST (routine §23.B writes) after running `.claude/hooks/session-start.sh` to install `gh` (the repo was attached mid-session, so the SessionStart hook had not run).
