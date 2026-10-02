# §21 merged-PR guard: a later `--no-tags` cancels `git push --tags`

Source issue: shubhodeep1/coding-workflows#6089 (https://github.com/shubhodeep1/coding-workflows/issues/6089)
Base branch: claude/implement-plan-issue-5144-merge-guard-effective-repo
Security pass: skip (ai:security: automation-produced issue)

## Summary

The §21 merged-PR guard on the #5144 project branch treats `git push --tags <remote>` with no refspec as a tags-only push and judges nothing. It never reads `--no-tags`, so `git push --tags --no-tags origin` (git pushes the checked-out branch) is also skipped, and a push that strands work on a merged branch goes through unguarded. This plan makes the guard track the last `--tags` / `--no-tags` word, as git does.

## Context

- Issue #6089 (security audit follow-up, `Refs #3576`, STRIDE Tampering, medium): `.claude/hooks/pr_merge_status_guard.py:747` on `claude/implement-plan-issue-5144-merge-guard-effective-repo`. `_push_refspec_targets` (`:688-797`) sets `pushes_tags = True` on `--tags` / `--tag` / `--ta` (`_is_push_tags_only_flag`, `:839-842`) and, with no refspec, returns `[]` (`:746-749`). `--no-tags` falls through to the generic "skip any `-` word" branch (`:736-738`), so the earlier `--tags` stays in force.
- Verified against git 2.43 with a local bare remote and `--dry-run --porcelain`: `git push --tags --no-tags origin`, `--tags --no-ta`, `--tags --no-tag`, and `origin --tags --no-tags` all push the checked-out branch (they fail only on the missing upstream, like a bare `git push origin`). `--no-tags --tags origin` pushes only the tag. `--tags --no-t` is refused as ambiguous (`--no-tags` / `--no-thin`). git parses options anywhere among the positionals, and the last of `--tags` / `--no-tags` wins.
- The same audit filed sibling follow-ups on the same project branch: #6088 (`--no-delete` cancels `--delete`, `:723`) and #6090 (`CDPATH` selects another repository, `:1107`). Each is its own issue-mode project. This plan touches neither.
- The hook is a protected path. The phase runs twin-first under the interim automatic default (CLAUDE.md §28.C, `/implement-plan-claude` step 4): only `workflow-templates/.claude/hooks/pr_merge_status_guard.py` is edited, and the root copy lands through a `[claude-twin-sync]` commit. The #5144 tests already read the twin (`twin_guard`, `tests/test_pr_merge_status_guard.py:1207-1215`).
- CLAUDE.md §21.B (base branch, around line 926) says `--tags` (or `--tag`) with no refspec is not judged; `workflow-templates/CLAUDE.md` is a symlink to `CLAUDE.md`.

## Goals

- `--no-tags` and the unambiguous prefixes git accepts for it (`--no-ta`, `--no-tag`) cancel an earlier `--tags`, and a later `--tags` cancels an earlier `--no-tags`, wherever they sit among the push arguments. Only a push whose final state is tags-only, with no refspec, yields no target.
- `git push --tags --no-tags origin` from a checkout on a merged branch with no open PR is blocked, as `git push origin` is.
- The same holds when the directory cannot be resolved: `cd $X && git push --tags --no-tags origin` falls back to the session checkout with a warning instead of yielding no target.
- Every other behaviour is unchanged: `--tags` alone, `--tags` with a refspec, deletions, bulk pushes, the fast paths, the API budget, the block and warning text.

## Non-goals

- `--no-delete` cancelling `--delete` / `-d` (issue #6088) and `CDPATH` (issue #6090).
- Negations of the bulk flags (`--no-all`, `--no-branches`, `--no-mirror`): an uncancelled bulk flag only adds a confirmation prompt on top of judging the checked-out branch, so it never allows silently (AD-1).
- `push.followTags` or other config that adds refs to a push.

## Constraints

- §6: no identifier renamed or removed. `_is_push_tags_only_flag` and `_PUSH_TAGS_ONLY_FLAG` keep their meaning; the new constant `_PUSH_NO_TAGS_FLAG` and helper `_is_push_no_tags_flag` were checked against the module and the tests and are unique.
- §5: the change stays inside the option loop of `_push_refspec_targets` plus one helper, so it merges cleanly beside the #6088 and #6090 fixes that edit the same file.
- §15 / §21.D: no new API calls; the change only decides which targets exist.
- §9: tabs in Python. §20: one `changelog.d/` fragment. §28.C: protected paths edited twin-first only.

## Approach

In the option loop of `_push_refspec_targets`, add a branch next to the `--tags` one: when `_is_push_no_tags_flag(token)` is true, set `pushes_tags = False` and continue. `_is_push_no_tags_flag` mirrors `_is_push_tags_only_flag`: the word is at least `--no-ta` long and is a prefix of `--no-tags`. Because both branches overwrite the same flag, the last word wins, which is git's rule. The no-refspec return at `:746-749` then sees the effective state. The unresolvable-directory path (`_git_invocation_targets`, `:958-971`) already calls `_push_refspec_targets` and falls back when it returns targets, so it needs no change.

Alternative considered: list every negatable push option and replay git's parse-options in full. Rejected under §5 and the one-issue rule: `--no-delete` belongs to #6088, and the bulk negations are not a silent allow.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue is one defect in one function.

1. **Phase 1 — honour `--no-tags` in the push parser** — protected paths: `.claude/hooks/pr_merge_status_guard.py` (edited only in its `workflow-templates/.claude/hooks/` twin, interim twin-first default). Files: the twin hook, `tests/test_pr_merge_status_guard.py`, `CLAUDE.md` (§21.B), `changelog.d/6089-merge-guard-negated-tags.md` [new]. Done: the new tests pass against the twin; every existing test passes except `test_template_copies_are_identical`, which stays red until the `[claude-twin-sync]` copy. Rollback: revert the phase PR.

## Implementation Steps

1. `workflow-templates/.claude/hooks/pr_merge_status_guard.py`: add `_PUSH_NO_TAGS_FLAG = "--no-tags"` beside `_PUSH_TAGS_ONLY_FLAG` with a comment on the prefixes git accepts; add `_is_push_no_tags_flag`; in `_push_refspec_targets` reset `pushes_tags` on it; update the docstring to say the last of `--tags` / `--no-tags` decides.
2. `tests/test_pr_merge_status_guard.py`: unit cases for `_is_push_no_tags_flag`; `guard_targets` cases where a later `--no-tags` (any accepted spelling, either side of the remote) judges the checked-out branch, and a later `--tags` still yields no target; the unresolvable-directory fallback; an end-to-end block from a stranded checkout.
3. `CLAUDE.md` §21.B: say a later `--no-tags` cancels `--tags`.
4. `changelog.d/6089-merge-guard-negated-tags.md`: `fixed` entry.

## Files & Modules

- `workflow-templates/.claude/hooks/pr_merge_status_guard.py`
- `.claude/hooks/pr_merge_status_guard.py` (via `[claude-twin-sync]` only; byte-identical to the twin)
- `tests/test_pr_merge_status_guard.py`
- `CLAUDE.md` (and therefore `workflow-templates/CLAUDE.md`, a symlink)
- `changelog.d/6089-merge-guard-negated-tags.md` [new]

## Tests

- Unit: `_is_push_no_tags_flag` accepts `--no-tags`, `--no-tag`, `--no-ta` and rejects `--no-t`, `--no-thin`, `--tags`, `--no-delete`, `--no-verify`.
- Target parsing: `git push --tags --no-tags origin`, `--tags --no-tag`, `--ta --no-ta`, `origin --tags --no-tags`, `--no-tags origin` judge the checked-out branch with `HEAD`; `--no-tags --tags origin` and `--tags --no-tags --tags origin` yield no target; `cd $WORKTREE && git push --tags --no-tags origin` falls back to the session checkout with a reason.
- End to end (real git repository, stubbed `gh`, the existing `worktree_repo` fixture): `git push --tags --no-tags origin` from the stranded checkout exits 2 naming the merged PR.
- Regression: all of `tests/test_pr_merge_status_guard.py` (only `test_template_copies_are_identical` red until the sync), `tests/test_claude_md_section_numbers.py`.
- CI: the existing `ci.yml` step "Merged-PR commit guard hook tests (CLAUDE.md §21)"; no new step.

## Risks & Mitigations

- A future git release adds another negatable option starting `no-ta…`, making `--no-ta` ambiguous — ACCEPTED — git would then refuse the push, so treating it as `--no-tags` judges a branch and can only block or ask, never allow silently.
- Merge conflict with the #6088 fix in the same option loop — mitigation: a separate, adjacent branch of the loop; the conflict resolver keeps both.

## Rollout

Ships with the #5144 project when its final PR merges into `main`, and to consumer repos on the next `@stable` sync of `workflow-templates/.claude/`. No flag; `CLAUDE_PR_MERGE_GUARD=off` still disables the merged-PR check. Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-10-02] Should this fix also honour `--no-delete` and the bulk-flag negations (`--no-all`, `--no-branches`, `--no-mirror`)? — Picked: A — no; only `--no-tags`. Alternatives: B — fold all negations in. Why: `--no-delete` is issue #6088's own project, and a cancelled bulk flag only adds a confirmation prompt, never a silent allow (§5, one issue per chain). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] Which spellings cancel `--tags`? — Picked: A — `--no-tags` and the prefixes git 2.43 accepts (`--no-ta`, `--no-tag`), last word wins in either order. Alternatives: B — only the exact `--no-tags`. Why: git accepts the prefixes (verified), so B leaves the same bypass for `--no-tag`. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Where does the changelog entry go? — Picked: A — a new fragment `changelog.d/6089-merge-guard-negated-tags.md`. Alternatives: B — edit the #5144 fragment. Why: §20.B one fragment per PR at a unique path; #6088 and #6090 change the same feature and could edit the same fragment. Applied in: phase 1 PR. Status: pending review

## Notes

- The security pass is skipped: `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #6089; sibling issues #6088, #6090; project #5144 (final PR #5163); CLAUDE.md §21, §28.C; `.claude/commands/implement-plan-claude.md` step 4 (interim twin-first default).
