# §21 merged-PR guard: read git push options in order, negations included

Source issue: shubhodeep1/coding-workflows#6088 (https://github.com/shubhodeep1/coding-workflows/issues/6088)
Base branch: claude/implement-plan-issue-5144-merge-guard-effective-repo
Security pass: skip (ai:security: automation-produced issue)

## Summary

`git push --delete --no-delete origin HEAD:claude/stale` pushes a branch, but the §21 merged-PR guard stops at `--delete` and skips the merged-PR check. Read every `git push` option in order the way git 2.43 does, including negations and abbreviations, decide what the push writes only from the final option state, and block a push whose options the guard cannot read.

## Context

Security audit finding `negated-delete-skips-branch-push` (high, STRIDE: Tampering) against `.claude/hooks/pr_merge_status_guard.py:723` on the #5144 project branch. `_push_refspec_targets` returns `[]` (no target, so no check) as soon as `_is_push_delete_option` matches a word. git's parse-options reads options in order, and the last of `--delete` / `--no-delete` wins, so the guard and git disagree about what the push writes.

Verified with git 2.43.0 against a local bare remote (2026-10-02):

| Command | git writes | Guard today |
| --- | --- | --- |
| `git push --delete --no-delete origin HEAD:a` | creates branch `a` | no target (skipped) |
| `git push -d --no-del origin HEAD:c2` | creates `c2` | no target (skipped) |
| `git push --tags --no-tags origin` | pushes the current branch | no target (skipped) |
| `git push --all --no-all origin HEAD:e` | creates only `e` | asks, but judges the current branch instead of `e` |
| `git push --b origin`, `git push --m origin` | every branch / a mirror push | judges the current branch, no bulk prompt |
| `git push --recurse-submodules check origin` | pushes the current branch | judges a branch named `origin` |
| `git push --no-d …`, `--no-a …`, `--no-t …`, `--n …`, `--zzz …`, `-z …` | nothing (git exits: ambiguous or unknown option) | treated as harmless flags |
| `git push origin HEAD:ac --delete` | `--delete` is still read as an option (options and positionals interleave) | same |

The guard is a protected path (`.claude/**`). In coding-workflows the twin `workflow-templates/.claude/hooks/pr_merge_status_guard.py` is edited first and reaches `.claude/hooks/` through a `[claude-twin-sync]` commit (CLAUDE.md §28.C interim twin-first default). The #5144 project branch already carries both copies, identical at `ce455fe`.

## Goals

- `git push` option words are read left to right with git 2.43's rules for `git push`: exact long names, unambiguous prefixes, `--no-<name>` and its unambiguous prefixes, the `--no-verify` / `--verify` pair, short-option clusters, `-o` / `--repo` / `--receive-pack` / `--exec` / `--push-option` / `--recurse-submodules` values (attached or the next word), and `--` / `--end-of-options` ending the options. Options after a positional are still options.
- Whether a push deletes, is a bulk push (`--all` / `--branches`, `--mirror`), or pushes tags is decided from the final state after all words, so `--delete --no-delete`, `--tags --no-tags`, `--all --no-all` and `--mirror --no-mirror` are judged like the push git actually runs.
- An option word the guard cannot read blocks the push with exit 2 and a reason that names the word (AD-2). That covers an unknown option, an ambiguous prefix, a value given to an option that takes none, and a value option with no value. git 2.43 rejects all of these too.
- `-h`, `--help`, `--help-all`, `--git-completion-helper` and `--git-completion-helper-all` print usage or a list and write nothing, so they yield no target (AD-6).
- Every existing passing case keeps its result, except `--m` and `--b`, which now count as bulk flags because git reads them as `--mirror` and `--branches` (AD-5).

## Non-goals

- Shell expansions in positional words (`git push $FLAGS origin`) (AD-8).
- `git commit` parsing, the MCP push path, the API-call budget, and the directory walker.
- Other git versions' option tables. The table is git 2.43's; an option a newer git adds blocks until the table learns it (AD-2).

## Constraints

- §1: security first. A word the guard cannot read must not lead to a silent allow.
- §5: change only the push option reader and the docs that describe it.
- §6: `_is_push_delete_option`, `_push_option_takes_next_word`, `_is_push_bulk_flag`, `_is_push_tags_only_flag` and the `_PUSH_*` constants stay, with their meaning (AD-7). `GuardTarget` gains one field at the end with a default, so every existing positional construction keeps working.
- §9: tabs in Python.
- §15 / §21.D: no new API calls. A blocked unreadable push issues none.
- §20: one changelog fragment (`security`).
- §21: CLAUDE.md §21.B describes the new reading.
- §28.C: twin-first for the protected hook.

## Approach

Add a table of git 2.43's `git push` options (long name, short name, effect, value kind, negatable, alias group), taken from `git push -h` and `--git-completion-helper-all`. Add one resolver that mirrors `parse_long_opt` / `parse_short_opt` in git's `parse-options.c`. It returns, for one word, the effects in order (`delete`, `all`, `mirror`, `tags`, each set or unset), whether the next word is the option's value, and a reason when git would reject the word. `_push_refspec_targets` walks the words with it, keeps `delete`, `all`, `mirror` and `tags` as state, and only after the last word decides:

1. a word git rejects → one target with the new `unreadable_reason`, judged by nobody: `_evaluate_bash` blocks it;
2. a help word → no target;
3. `delete` set → no target (git deletes or refuses);
4. `all` or `mirror` set → the bulk target, as today;
5. no refspec and `tags` set → no target; no refspec → the current branch, as today;
6. otherwise the refspecs, as today.

The four helpers become thin wrappers over the resolver, so they agree with the walker. The unresolvable-directory path in `_git_invocation_targets` passes an unreadable target through unchanged.

Alternatives considered: handling only `--no-delete` (AD-1 B), which leaves `--tags --no-tags` silently skipped; and asking instead of blocking on an unreadable word (AD-2 B), which stalls unattended sessions at a prompt for a command git itself rejects.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for one standalone issue.

1. **Phase 1: read git push options in order.** Files: the guard twin, its tests, CLAUDE.md §21.B, a changelog fragment. Protected paths: `.claude/hooks/pr_merge_status_guard.py` (twin-first). Done when the new tests pass against the twin, `tests/test_pr_merge_status_guard.py` passes except `test_template_copies_are_identical` until the twin sync, and the guard's results for the commands in the Context table match git's behaviour. Rollback: revert the PR. The guard returns to the old reader.

## Implementation Steps

1. `workflow-templates/.claude/hooks/pr_merge_status_guard.py`: add `_PushOptionSpec` (a `NamedTuple`) and `_PUSH_OPTION_TABLE`, plus the resolver `_read_push_option_word(token)`, which follows `parse_long_opt` / `parse_short_opt`. Abbreviations are ambiguous unless the matches are aliases of one option. The `no-` prefix forms are handled, and so is `--verify` as the negation of `--no-verify`. A short `h` means usage. `-o` takes the rest of the cluster or the next word.
2. Same file: rewrite the option loop of `_push_refspec_targets` to apply the effects in order and decide from the final state (Approach 1–6). Update its docstring.
3. Same file: add `unreadable_reason: str = ""` to `GuardTarget`. Pass such a target through in `_git_invocation_targets`'s unresolvable-directory branch. In `_evaluate_bash`, add a block message for it before judging, without an API call.
4. Same file: re-express `_is_push_delete_option`, `_push_option_takes_next_word`, `_is_push_bulk_flag`, `_is_push_tags_only_flag` on the resolver. Keep the `_PUSH_*` constants and their comments, and correct the bulk-prefix comment.
5. `tests/test_pr_merge_status_guard.py`: add parametrised target tests for the Context table, interleaved options, `--end-of-options`, help words, unreadable words in a resolvable and an unresolvable directory, and e2e hook runs: `--delete --no-delete` onto the merged branch blocks, `--no-delete --delete` still allows, an unknown option blocks with no `gh` call. Update the `--m` assertion (AD-5).
6. `CLAUDE.md` §21.B: one paragraph on in-order option reading, negations, and the block for an unreadable option.
7. `changelog.d/6088-merge-guard-push-option-negation.md` (`security`).

## Files & Modules

- `workflow-templates/.claude/hooks/pr_merge_status_guard.py` (twin; `.claude/hooks/pr_merge_status_guard.py` via the `[claude-twin-sync]` commit)
- `tests/test_pr_merge_status_guard.py`
- `CLAUDE.md`
- `changelog.d/6088-merge-guard-push-option-negation.md` [new]
- `docs/implement-plan/issue-6088-negated-delete-skips-branch-push.md` [new] (progress log)

## Tests

- Unit: `guard_targets` for each Context-table command, and resolver/helper assertions per word.
- E2E: `_run_twin_hook` against the `worktree_repo` fixture (real git, stub `gh`): the exploit command blocks with `pull/41`, and an unreadable option blocks without calling `gh`.
- Full `tests/test_pr_merge_status_guard.py` and the repo's standard Python test run. The template-parity test stays red until the twin sync.
- Manual (already done while planning): each command's git behaviour, checked against a local bare remote with git 2.43.0.

## Risks & Mitigations

- A git newer than 2.43 adds a push option, and the guard blocks a push that uses it. ACCEPTED: the reason names the word and says to spell out a known option or drop it. Blocking is the failure mode the issue asks for (AD-2).
- The resolver drifts from git's abbreviation rules. Mitigation: each rule has a test case taken from the observed git 2.43 behaviour above.
- Template parity stays red until the twin sync. Mitigation: hold claim and twin-sync blocker per the twin-first default.

## Rollout

Ships with the #5144 project (its final PR into `main`), then to consumers through the `@stable` `.claude/` sync. There is no flag. `CLAUDE_PR_MERGE_GUARD=off` still disables the whole guard, including the new block.

## Auto-decisions

- AD-1 [plan, 2026-10-02] Which negations are applied in order? — Picked: A — every `git push` option, via git 2.43's full option table with its abbreviation and `--no-` rules, so `--no-delete`, `--no-tags`, `--no-all` / `--no-branches` and `--no-mirror` all count. Alternatives: B — only `--no-delete` and its prefixes. Why: `--tags --no-tags origin` is the same silent skip (verified), and the recommendation asks to parse options in order. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] What happens to a push option word the guard cannot read (unknown, ambiguous prefix, a value where none is taken, a missing value)? — Picked: A — block with exit 2 and a reason naming the word. Alternatives: B — ask for confirmation like a bulk push; C — skip the word as today. Why: the issue says to deny writes whose target cannot be determined; git 2.43 rejects every such word, so no working push is blocked; a block hands an unattended session a reason it can act on, while an ask stalls it. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Where does that block apply? — Picked: A — wherever the guard runs, on the default branch and in an unresolvable directory too, but not with `CLAUDE_PR_MERGE_GUARD=off`. Alternatives: B — only off the default branch. Why: the written branch is unknown, so the default-branch skip cannot apply (the same scope as the AD-10 bulk prompt); the escape hatch keeps its documented meaning. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-02] Should `--recurse-submodules <value>` take the next word? — Picked: A — yes, like the other value options (prefix `--recu`). Alternatives: B — leave it as a flag. Why: git reads the next word as its value (verified), and a full option table would be wrong without it; today `git push --recurse-submodules check origin` judges a branch named `origin`. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-02] `--b` and `--m` are unambiguous prefixes git expands to `--branches` and `--mirror` (verified); the existing test asserts `--m` is not a bulk flag. Follow git? — Picked: A — yes, treat both as bulk flags and correct that assertion. Alternatives: B — keep the four-character minimum. Why: under B a mirror push skips the bulk prompt AD-10 added. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-02] How are `-h` (alone or in a cluster), `--help`, `--help-all` and `--git-completion-helper[-all]` treated? — Picked: A — as git does: it prints and exits without writing, so the push yields no target. Alternatives: B — block them as unreadable. Why: they write nothing, and blocking `git push -h` would be a false block. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-10-02] What happens to the existing helpers and constants? — Picked: A — keep every identifier and re-express the four helpers on the new resolver, so they agree with the walker. Alternatives: B — leave them unchanged and unused. Why: §6 forbids removal, and B would leave helpers that disagree with the parser. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-10-02] Should a shell expansion in a positional word (`git push $FLAGS origin`) count as undeterminable? — Picked: A — no, out of scope; positional words keep today's handling. Alternatives: B — block any push with an expansion before its refspecs. Why: §5, the issue is about option negation, and B would block the common `git push "$REMOTE" "$BRANCH"`. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- The base branch is the #5144 project branch (draft final PR #5163 into `main`), so this project ends after its final merge into that branch, and the issue is closed explicitly (Issue Mode).

## References

- Issue #6088; audit tracker #3576; base project #5144 (final PR #5163), its AD-4 (deletions not judged) and AD-10 (bulk prompt).
- git 2.43 `builtin/push.c` option table and `parse-options.c` (`parse_long_opt`, `parse_short_opt`, `parse_options_step`).
