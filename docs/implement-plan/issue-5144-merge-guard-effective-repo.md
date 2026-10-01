# Implement-Plan Log — §21 merged-PR guard: judge the repository and target ref each git command actually uses

- Plan: docs/plans/issue-5144-merge-guard-effective-repo-plan.md
- Source issue: shubhodeep1/coding-workflows#5144
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5144-merge-guard-effective-repo   Final PR: #5163 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5173
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_012bXVKFrhBSuPGD6croSjLV   safety net trig_01RP2KhR9JbLBzUcw6grxKSj   hand-back trig_01Fjv8W8mmrB1x4cjoELtP4c
- Last updated: 2026-10-01
- Last note: review round 7 (workflow round 1 on head b6a2a6f4a806, run 36813093085, ledger `6543b32c…8b85`): twin sync `b6a2a6f` had landed (both copies `10546fc7…be8f`, `test_template_copies_are_identical` passes), so the hold is lifted and this header no longer says the PR waits on a sync. Rejected: the empty-destination refspec finding (git rejects `<src>:` for push with `fatal: invalid refspec`), the `GIT_DIR` inspection finding (git run from inside a linked worktree's git dir reads that worktree's HEAD; a new end-to-end test pins it), and the `cd … 2>/dev/null` finding (safe fallback with a warning, by design). No hook change this round; waiting on the next review round.

## Phases
1. [ ] Phase 1 — effective repository and push target ref for the §21 guard   — PR #5173 open (waiting on review); review rounds: 7; interventions: 0 — protected paths: `.claude/hooks/pr_merge_status_guard.py`
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
- AD-8 [phase 1/1 — review round 1, 2026-09-29] The review suggests falling back for any non-`refs/` destination containing `/` (such as `heads/x`); how wide should that rule be? — Picked: A — only the `heads/`, `tags/` and `remotes/` shorthands git expands against the remote. Alternatives: B — every destination containing `/`. Why: B would send every `claude/…` branch push back to the session checkout and undo the issue's fix; the reviewer's example is a shorthand. Applied in: PR #5173 review round 1. Status: pending review
- AD-9 [phase 1/1 — review round 1, 2026-09-29] Where does an unresolvable refspec fall back to, the session checkout or the effective repository's checked-out branch? — Picked: A — the session checkout, the old behaviour, the same as an unresolvable directory. Alternatives: B — the effective repository's checked-out branch. Why: the review asks for the old target; a detached worktree under B would allow with only a warning. Applied in: PR #5173 review round 1. Status: pending review
- AD-10 [phase 1/1 — review round 3, 2026-09-30] How should the guard judge a bulk push (`--all`, `--branches`, `--mirror` and the prefixes git expands to them, the `:` matching refspec), which writes branches it cannot list one by one? — Picked: A — judge the checked-out branch as before (a block still blocks) and otherwise ask for confirmation, with no API call and also on the default branch. Alternatives: B — list every local branch and judge each (one REST call per branch); C — treat it as unresolvable and judge the session checkout with a warning (the AD-9 path). Why: A closes the silent allow without growing the §21.D budget, and never allows what the old check blocked; B scales API calls with the branch count, C still lets unlisted branches through. Applied in: PR #5173 review round 3. Status: pending review
- AD-11 [phase 1/1 — review round 3, 2026-09-30] A `*` pattern refspec (`refs/heads/*:refs/heads/*`) is also a bulk push; should it leave the AD-9 fallback? — Picked: A — keep the AD-9 fallback (session checkout, warning) and add the same confirmation prompt when the pattern can write branches (not a tag pattern or a deletion). Alternatives: B — judge it like `--all` on the effective repository; C — leave it as the fallback only. Why: A keeps the earlier review's pinned fallback and closes the same gap; C leaves it open. Applied in: PR #5173 review round 3. Status: pending review

## Lessons
- [source:intervention] When a guard starts judging a user-supplied ref instead of local state, treat every shell word it reads (variables, globs, shorthands git expands, `-`-prefixed values) as unresolvable unless it is a plain literal, and fall back to the old check with a warning. (files: .claude/hooks/pr_merge_status_guard.py)
- [source:intervention] A command walker that tracks `cd` must keep the shell operators: a `cd` joined by `||`, `&` or `|` may not run in the current shell. (files: .claude/hooks/pr_merge_status_guard.py)
- [source:plan-deviation] A hook module loaded by tests through `importlib.util.spec_from_file_location` without registering it in `sys.modules` cannot use `@dataclass` under `from __future__ import annotations`; use a `NamedTuple` for small immutable records there. (files: .claude/hooks/pr_merge_status_guard.py, tests/test_pr_merge_status_guard.py)

- [source:intervention] `git push` always reads its first positional as the repository, even when `--repo` is given (the option is only the default), so a push-argument parser must not shift the refspec list when it sees `--repo`. (files: .claude/hooks/pr_merge_status_guard.py)
- [source:intervention] `git push --tags <remote>` with no refspec pushes only tags, while `--all`, `--branches`, `--mirror` (and any unambiguous prefix git accepts, such as `--al`) and the `:` refspec write many branches; a guard that judges one branch per push must not map either to the checked-out branch alone. (files: .claude/hooks/pr_merge_status_guard.py)
- [source:intervention] In a shell `&&` chain, a `cd` behind a command that may fail is skipped with that command, and whatever follows `;`, a newline or `||` then runs in the original directory; a `cd` inside a list sent to the background with `&` never reaches the commands after it. A command walker must drop the tracked directory at those points. (files: .claude/hooks/pr_merge_status_guard.py)
- [source:intervention] A guard that predicts where a later command runs must treat a `cd` target it cannot enter (no search permission) like a missing one: the `cd` fails and the next command runs in the old directory. (files: .claude/hooks/pr_merge_status_guard.py)
- [source:intervention] A per-command memo that saves API calls must also remember a failed call, or a second target on the same key repeats the request the budget allows once. (files: .claude/hooks/pr_merge_status_guard.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the scheduled routine `dispatch shubhodeep1/coding-workflows#5144: deliver` in session session_01YLLYPSWJ9Xqr8pCYFPTRQE; permission mode auto.
- Progress comment: issue #5144 comment 5890591928.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- The session had no `mcp__github__*` tools; GitHub writes go through `gh api` REST (routine §23.B writes) after running `.claude/hooks/session-start.sh` to install `gh` (the repo was attached mid-session, so the SessionStart hook had not run).
- Twin sync needed: `workflow-templates/.claude/hooks/pr_merge_status_guard.py` → `.claude/hooks/pr_merge_status_guard.py` (twin sha256 `4f3b419ede0595ea0b332ec3e2a1b0dd2af0712852a19e1b577b7db16d40f4dc`); blocker posted on #5144.
- Master twin-sync review (PR #5173 comment 5891249253) declined the first twin; review round 1 fixed it. New twin sha256 `d1644620e449abe4a02fc50eb772bedf8878a8e7986e73a36440e15afe0577d2`; new blocker posted on #5144. The REST-call cap the review floated was not added: §21.D allows one call per `(slug, branch)`, and the cache and memo already bound it.
- /reclarify resume on 2026-09-30: stage session_01JicWiDdBc6wpUAoU4trx2e synced the project branch with main (fb1142a) and armed checker session_012bXVKFrhBSuPGD6croSjLV, safety net trig_01SqbKiGNT6dnRFYK1qXS6Vs and hand-back trig_012mdcTGEpsTFMU1AGMzQw2S; that wait ended with the workflow's review round 1 hand-off on head 56631e639350 (PR #5173 comment 5904182364).
- Review round 2 (session_01NBozj8UjGHoeHvyMSvFYXe, 2026-09-30): the project branch was merged into the phase branch (clean); new twin sha256 `b68e719062b0f22b6b8efcbcf4b04f22a678b2cdd3dd667d753c25399a03cf96` (the `.claude/` copy is still `d1644620…`); new blocker posted on #5144.
- /reclarify resume 2026-09-30 by session_0125JU5gc8ttYcB4X2Br61mc: twin sync e0a7d2b landed (sha256 b68e7190…cf96), hold lifted; project branch synced with main at 409df1a (clean, 315 tests passed); ai:claude-blocked removed; checker session_012bXVKFrhBSuPGD6croSjLV reused, safety net trig_018gRvYmHShnyXLqoP2RwHpg, hand-back trig_012s3YkgjLM3qrigvNyHaddK; Status back to IN_PROGRESS. That wait ended with the workflow's review round 1 hand-off on head e0a7d2ba4f03 (PR #5173 comment 5907537135).
- Review round 3 (session_01W7iJBQB6NhFrgqyuGtwa3p, 2026-09-30): the log on the phase branch had one `## Auto-decisions` section with AD-1…AD-9 and no duplicates, so nothing was removed. New twin sha256 `2eadc36a0bc7e1df519637aa8f3c73471c567adf4728396ce88b27231d624cf3` (the `.claude/` copy is still `b68e7190…cf96`); new blocker posted on #5144.
- Review round 4 (session_0146fXPsUDiKGDB3rQ1gtfAp, 2026-09-30): workflow round 1 on head 420ccd0b770b (ledger `2707b9ef…afc1`); both code findings fixed, the task gap rejected. New twin sha256 `76c3aa18f065f262e13f678c19af351054e59f5e144f93605f8f0d533f4a3a33` (the `.claude/` copy is still `2eadc36a…4cf3`); new blocker posted on #5144.
- Review round 5 (session_01EENJBCUqspaRDh83wPseSh, 2026-09-30): three reviewer runs on head f1c6650e5be6 after twin sync `f1c6650` (the `.claude/` copy and the twin were both `76c3aa18…3a33`); answered the latest hand-off (run 36725288304, ledger `728535fe…d1f2`). Project branch synced with main at 1b0c74e (clean). New twin sha256 `46091219ee440267a0df1b48782846f8a8543609b9f04e66c95b01bc27bcf878` (the `.claude/` copy is still `76c3aa18…3a33`); new blocker posted on #5144.
- Review round 6 (session_01MDVSYJFYwKFE8sXRsED47p, 2026-10-01): workflow round 1 on head 0f20e449ffde (ledger `ddc54be8…052f`); three code findings fixed in the twin, the redirect finding rejected, the stale header refreshed. New twin sha256 `10546fc7…be8f` (the `.claude/` copy is still `46091219…f878`); new blocker posted on #5144.
- Review round 7 (session_01Mm4vPcFoXxqihGf1c1VGDX, 2026-10-01): workflow round 1 on head b6a2a6f4a806 (ledger `6543b32c…8b85`) after twin sync `b6a2a6f`. Fixed the stale header (the task gap) and added `test_e2e_linked_worktree_git_dir_is_judged_by_its_own_head`; rejected the empty-destination refspec, `GIT_DIR` inspection and `cd … 2>/dev/null` findings. Neither hook copy changed (both `10546fc7…be8f`), so no twin sync is needed; checker session_012bXVKFrhBSuPGD6croSjLV reused.
