# §21 merged-PR guard: judge the repository and target ref each git command actually uses

Source issue: shubhodeep1/coding-workflows#5144 (https://github.com/shubhodeep1/coding-workflows/issues/5144)
Base branch: main
Security pass: run

## Summary

`.claude/hooks/pr_merge_status_guard.py` (CLAUDE.md §21) judges every guarded `git commit` / `git push` by the branch of the session's own checkout, even when the command runs git somewhere else (`cd <worktree> && …`, `git -C <path> …`) or pushes to another ref (`git push origin HEAD:<ref>`). This plan makes the guard judge the repository each git command runs in and, for a push, the ref it pushes to, so twin syncs and fixer work from scratch worktrees are neither falsely blocked nor let through unguarded.

## Context

- Issue #5144 (owner-authored): on 2026-09-29 a twin-sync push from a detached worktree onto an open PR's branch (`claude/implement-plan-issue-4867-…-conformance-fix-2`) was blocked because the master session's main checkout sat on `claude/write-plan-retire-master-session`, whose PR #5123 had merged. By the same logic a push from a worktree that sits on merged history passes unguarded when the main checkout is clean.
- Twin syncs, held-merge conflict resolutions and fixer work run from scratch worktrees (`docs/operations/master-session.md` "Twin syncs (Q40)" step 5 pushes with `git push origin HEAD:<phase branch>`), and the retire-master plan (`docs/plans/retire-master-session-plan.md`) moves supervision into automation that works from worktrees.
- `_evaluate_bash` (`.claude/hooks/pr_merge_status_guard.py:972-1034`) calls `current_branch(cwd)` with `cwd` = the hook payload's `cwd`; `git_subcommands` (`:330-368`) already walks `git -C <path>` and leading `VAR=value` assignments but only to find the subcommand name.
- Every helper (`current_branch`, `default_branch`, `repo_slug`, `is_ancestor_of`, `merge_base`, `git_history_verdict`, …) takes a `cwd`, so judging another repository means passing a different `cwd`. Git run with its working directory inside a git dir (a `.git` directory or a linked worktree's `.git/worktrees/<name>`) resolves `HEAD`, config and ancestry from that git dir, which is how `GIT_DIR` / `--git-dir` can be honoured without new environment plumbing.
- CLAUDE.md §21.B–D document the detection rule, fail-open contract and API budget; `workflow-templates/CLAUDE.md` is a symlink to `CLAUDE.md`.
- The hook is a protected path. The phase runs twin-first under the interim automatic default (CLAUDE.md §28.C, `/implement-plan-claude` step 4): only `workflow-templates/.claude/hooks/pr_merge_status_guard.py` is edited; the root copy lands through a `[claude-twin-sync]` commit.

## Goals

- For each guarded git invocation in a Bash command, the guard resolves the **effective repository**: the payload `cwd`, changed by every earlier `cd <path>` in the same command line, then by `git -C <path>` (repeatable, each relative to the last), then by a `GIT_DIR=<path>` prefix or `--git-dir <path>` / `--git-dir=<path>` option (relative to the directory reached so far). Branch, default branch, slug and ancestry are all judged there.
- For `git push <remote> <src>:<dst>` (including `HEAD:<dst>`, a leading `+`, and `refs/heads/<dst>`), the guard checks `<dst>` for a merged PR with no open PR, with `<src>` as the commit that would stack on it. A detached HEAD pushing to an open PR's branch is allowed; a push of merged history to a merged branch with no open PR is blocked whatever the main checkout is on.
- `git push` with no refspec (or `HEAD`, or the checked-out branch's own name) judges the effective repository's current branch with `HEAD`, as today.
- When the directory cannot be resolved (a variable, command substitution, `~user`, `cd -`, a glob, a subshell, `pushd`/`popd`, an `export` of `GIT_DIR`, or a path that does not exist at hook time), that invocation keeps today's behaviour (the payload `cwd`'s current branch and `HEAD`) and the guard emits a warning naming the reason. It never fails open silently.
- §21.D budget is kept: one cached REST call per `(slug, branch)` pair judged; a command that judges the same pair twice issues one call.
- Every other behaviour is unchanged: the single-segment fast paths, the API-write confirmation safeguard, the escape hatch, the history fallback, the MCP push path, the block message and remediation text.
- CLAUDE.md §21.B and §21.D describe the effective repository, target-ref and budget rules.

## Non-goals

- Wrapper-prefixed git (`sudo git`, `env GIT_DIR=… git`, `bash -c "git …"`) and git inside a subshell `( … )` stay unguarded or judged as today, exactly as `git_subcommands` treats them now.
- Upstream tracking config (`branch.<name>.merge`) for a bare `git push`: the current branch name is judged, as today.
- The MCP push path (`_evaluate_mcp_push`), which already judges the named remote branch.
- `docs/operations/master-session.md`: its "detach the main checkout" workaround is not written down there, so nothing to remove.

## Constraints

- §6: no public identifier is renamed or removed. `git_subcommands`, `blocking_pull_request`, `_unreachable_outcome`, `_warn`, `_request_confirmation`, `evaluate` keep their signatures and behaviour; new helpers get new, unique names (checked against the module).
- §15 / §21.D: at most one REST call per `(slug, branch)` pair per evaluation, served from the existing 300-second cache when fresh; a cache-derived block is re-verified live, as today.
- §21.C fail-open contract: unresolvable input degrades to today's behaviour plus a warning, never to a silent allow; a guard exception still allows with a warning.
- Hook output protocol: one JSON object on stdout at most, so several judged targets merge into one `systemMessage` and at most one `permissionDecision: ask`; a block (exit 2) wins.
- §9: tabs in Python. §20: one `changelog.d/` fragment. §28.C: protected paths edited twin-first only.

## Approach

Add a small command walker next to `git_subcommands` that replays the shell segments in order, tracking the effective directory across `cd` segments and marking it unresolvable on the constructs listed in the goals. For each segment whose command is `git` with a guarded subcommand, it resolves `-C` / `GIT_DIR` / `--git-dir` and returns one or more **targets** `(repo dir, branch or None, tip, reaches_remote, fallback reason)`: a commit yields the current branch with `HEAD`; a push yields one target per branch refspec (`<dst>` with `<src>` as tip), or the current branch with `HEAD` when it names none. Deletions (`--delete`, `:<dst>`) and tag refspecs push no work onto a branch and yield no target. `_evaluate_bash` then judges each distinct target with the existing three-condition rule (moved into one per-target function whose unreachable-GitHub path returns its warning or ask instead of printing it), memoising the PR list per `(slug, branch)`, and prints one merged result.

Alternative considered: exporting `GIT_DIR` into every helper's subprocess environment. Rejected: every helper would need an extra parameter for a rare form, while running git from inside the git dir gives the same answers with no signature changes.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue is one change to one hook.

1. **Phase 1 — effective repository and push target ref** — protected paths: `.claude/hooks/pr_merge_status_guard.py` (edited only in its `workflow-templates/.claude/hooks/` twin, interim twin-first default). Files: the twin hook, `tests/test_pr_merge_status_guard.py`, `CLAUDE.md` (§21.B, §21.D), `changelog.d/5144-merge-guard-effective-repo.md` [new]. Done: the new tests pass against the twin; every existing test passes except `test_template_copies_are_identical`, which stays red until the `[claude-twin-sync]` copy and then goes green with the whole file. Rollback: revert the phase PR; the guard judges the session checkout again.

## Implementation Steps

1. `workflow-templates/.claude/hooks/pr_merge_status_guard.py`: add the command walker (effective directory across `cd`, `git -C`, `GIT_DIR=`, `--git-dir`; unresolvable constructs) and the push refspec parser, returning guard targets.
2. Same file: move the per-branch logic of `_evaluate_bash` into a per-target judge that memoises PR lists per `(slug, branch)` and returns its verdict, warnings and ask reason; add a non-printing twin of `_unreachable_outcome` that `_unreachable_outcome` wraps (MCP path unchanged).
3. Same file: `_evaluate_bash` judges every target, blocks when any blocks, otherwise prints one merged JSON result. Update the module docstring.
4. `tests/test_pr_merge_status_guard.py`: load the twin for the new tests and cover the acceptance list (worktree `cd … && git push origin HEAD:<open-PR branch>` from a main checkout on a merged branch is allowed; a push of merged history to a merged branch with no open PR is blocked whatever the main checkout is on; `git -C <path>` behaves the same; an unresolvable directory falls back to today's behaviour with a warning), plus the parser units, `GIT_DIR`, deletion/tag refspecs, and the one-call-per-pair budget.
5. `CLAUDE.md` §21.B / §21.D: document the effective repository, the push target ref, the unresolvable fallback, and the per-pair budget.
6. `changelog.d/5144-merge-guard-effective-repo.md`: `fixed` entry.

## Files & Modules

- `workflow-templates/.claude/hooks/pr_merge_status_guard.py`
- `.claude/hooks/pr_merge_status_guard.py` (via `[claude-twin-sync]` only; byte-identical to the twin)
- `tests/test_pr_merge_status_guard.py`
- `CLAUDE.md` (and therefore `workflow-templates/CLAUDE.md`, a symlink)
- `changelog.d/5144-merge-guard-effective-repo.md` [new]

## Tests

- Unit: the walker and refspec parser over `cd` chains (absolute, relative, `cd -P`, `cd --`), `git -C` chains, `GIT_DIR=` and `--git-dir`, each unresolvable construct, deletion and tag refspecs, `+src:dst`, `refs/heads/` prefixes, no-refspec pushes.
- Integration (real git repositories, stubbed `gh`): the four acceptance cases from the issue, `GIT_DIR`, and one REST call for two targets that share a `(slug, branch)` pair.
- Regression: every existing test in `tests/test_pr_merge_status_guard.py` (they load the root hook until the sync, the twin's behaviour for single-directory commands is unchanged), `tests/test_claude_md_section_numbers.py`, `tests/test_gh_api_write_guard.py`, `tests/test_update_workflows_guardrails.py`.
- CI: the existing `ci.yml` step "Merged-PR commit guard hook tests (CLAUDE.md §21)" runs the file; no new step.

## Risks & Mitigations

- A shell construct the walker misreads could judge the wrong directory — mitigation: anything outside the plain `cd` / `-C` / `GIT_DIR` forms is unresolvable and falls back to today's behaviour with a warning.
- `cd <path>` into a directory created earlier in the same command does not exist at hook time — ACCEPTED — falls back to today's behaviour with a warning.
- A pipeline component's `cd` (`cd x | …`) does not persist in bash but is treated as persisting — ACCEPTED — rare, and the resolved directory is still a real repository the user named.
- Several warnings in one command make a long `systemMessage` — ACCEPTED — each reason is one line.

## Rollout

Ships to this repo when the final PR merges, and to consumer repos on the next `@stable` sync of `workflow-templates/.claude/`. No flag; `CLAUDE_PR_MERGE_GUARD=off` still disables the merged-PR check. Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Should the `--git-dir <path>` / `--git-dir=<path>` git option be honoured like the `GIT_DIR` prefix the issue names? — Picked: A — yes, same rule. Alternatives: B — only the forms the issue lists. Why: `--git-dir` selects the repository exactly as `GIT_DIR` does, so leaving it out keeps the same wrong-checkout bug for that form. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How should `GIT_WORK_TREE` / `--work-tree` affect the judged repository? — Picked: A — not at all; the repository (and so `HEAD`) comes from `GIT_DIR` or directory discovery, which a work tree setting does not change. Alternatives: B — judge from the work tree path. Why: git itself reads `HEAD` from the git dir; judging the work tree could pick a different repository than the one git writes to. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which directory changes besides `cd` are modelled? — Picked: A — none; `pushd`, `popd`, subshells `( … )` / `{ … }`, an `export` or bare assignment of `GIT_DIR`, and a `cd` target with `$`, backticks, globs, `~user` or `-` are unresolvable and fall back to today's behaviour with a warning. Alternatives: B — model `pushd`/`popd` as a directory stack. Why: §5 and the issue's fallback rule; the fallback is safe. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Are branch deletions (`--delete`, `:<ref>`) and tag refspecs judged? — Picked: A — no; they land no commits on a branch, so nothing can be stranded. Alternatives: B — judge them like branch pushes. Why: blocking the deletion of a merged branch would be a false block. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] What does a push with no branch refspec (`git push`, `git push origin`, `--all`, `--mirror`, `--tags`) judge? — Picked: A — the effective repository's current branch with `HEAD`, as today. Alternatives: B — read the branch's upstream config. Why: §5; the issue only asks for explicit `<src>:<ref>` targets. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] For a refspec without a colon naming a local branch other than the checked-out one (`git push origin feature/y`), what is judged? — Picked: A — that branch, with the local ref as the tip. Alternatives: B — the checked-out branch, as today. Why: that ref is what the push writes; the same rule as `<src>:<ref>`. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] How are several guarded git invocations in one command combined? — Picked: A — judge each distinct target, block if any blocks, otherwise print one JSON result with every warning and at most one ask. Alternatives: B — judge only the last invocation. Why: a block on any stranding push is the §21 invariant, and the hook protocol takes one JSON object. Applied in: phase 1 PR. Status: pending review

## Notes

- The security pass runs: `security_pass_skip.py` returned `{"skip": false, "reason": "no skip label"}`.

## References

- Issue #5144; CLAUDE.md §21, §28.C; `.claude/commands/implement-plan-claude.md` step 4 (interim twin-first default); `docs/operations/master-session.md` "Twin syncs (Q40)"; `docs/plans/retire-master-session-plan.md`.
