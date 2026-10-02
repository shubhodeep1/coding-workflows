# §21 merged-PR guard: block a guarded git call after a `cd` that CDPATH may redirect

Source issue: shubhodeep1/coding-workflows#6090 (https://github.com/shubhodeep1/coding-workflows/issues/6090)
Base branch: claude/implement-plan-issue-5144-merge-guard-effective-repo
Security pass: skip (ai:security: automation-produced issue)

## Summary

The effective-repository walker that #5144 added to `.claude/hooks/pr_merge_status_guard.py` resolves `cd <dir>` against the current directory, but Bash first looks a relative operand up in `CDPATH`. With `CDPATH=.. cd .git && git push origin HEAD:claude/stale`, Bash enters the parent repository's `.git` while the guard judges the child's, so a push that strands work can be allowed. This plan makes every guarded `git commit` / `git push` after such a `cd` (or `pushd`) a block that names the reason and the workaround, instead of a judgement on a repository Bash may not be in.

## Context

- Issue #6090 is the security-audit follow-up (STRIDE: Tampering, medium, confidence 9/10) filed against the #5144 project branch `claude/implement-plan-issue-5144-merge-guard-effective-repo`, at `_cd_destination` in `guard_targets` (`.claude/hooks/pr_merge_status_guard.py:1107`). Its recommendation: account for inherited and inline `CDPATH` when resolving `cd`, and deny the write when the effective repository cannot be determined.
- Bash 5.2 semantics, checked in this session: a `cd` operand that does not start with `/` and is not `.`, `..`, `./…` or `../…` is searched in each `CDPATH` entry first (`.git` counts, since it does not start with `./`). An inline `CDPATH=.. cd .git`, a bare `CDPATH=..;` and an inherited exported `CDPATH` all redirect it. `CDPATH= cd sub` (empty) does not. `cd ./.git`, `cd ..` and `cd /abs` ignore `CDPATH`. `pushd` searches `CDPATH` the same way. A `CDPATH=` prefix on `:` persists only in POSIX mode.
- Today an unresolvable directory falls back to judging the session checkout with a warning (CLAUDE.md §21.B). For `CDPATH` that fallback is still the wrong repository, so it does not close the finding.
- #6038 (open, deferred by the operator until #5163 merges) lists `CDPATH=<dir> cd wt && git push …` among several gaps and proposes the fallback for it. This plan closes only the `CDPATH` gap, because the #5144 project's security pass needs it closed on the project branch. The rest of #6038 is untouched.
- The hook is a protected path. The phase runs twin-first under the interim automatic default (CLAUDE.md §28.C, `/implement-plan-claude` step 4): only `workflow-templates/.claude/hooks/pr_merge_status_guard.py` is edited, and the root copy lands through a `[claude-twin-sync]` commit. `workflow-templates/CLAUDE.md` is a symlink to `CLAUDE.md`.

## Goals

- `CDPATH=.. cd .git && git push origin HEAD:<b>`, `CDPATH=..; cd sub && git commit -m x`, `export CDPATH=..; cd sub && git push`, and `cd sub && git push` with `CDPATH` inherited non-empty by the hook are blocked (exit 2). The stderr names `CDPATH` and the workaround (`cd ./<dir>` or an absolute path).
- The same holds for `pushd <dir>`, and for a `CDPATH`-eligible `cd` after the directory is already unknown.
- No block when `CDPATH` cannot apply: an operand starting with `/`, `.` or `..`; an unquoted `~` / `~/…`; `cd` with no operand; `cd -`; an empty inline `CDPATH= cd sub`; no `CDPATH` anywhere. Those keep today's resolution.
- A deletion, tag-only, or `--tags` push after such a `cd` writes no branch and is still not judged.
- The block issues no GitHub API call (§21.D budget unchanged or lower).

## Non-goals

- Modelling Bash's `CDPATH` search (see AD-1).
- The other #6038 gaps: wrapper prefixes, `bash -c` / `eval`, groups and subshells, `case` arms, `~` in revisions, `enable -n cd`, an `eval`-defined `cd`.
- Changing the fallback for directories that are unknown for reasons other than `CDPATH`.
- A `CDPATH` set only as an unexported shell variable in a profile the Bash tool sources. The hook cannot see it (ACCEPTED, see Risks).

## Constraints

- §1: security first. The change only tightens the guard; it removes no block or ask.
- §5: minimal change in one function family (`guard_targets`, `_git_invocation_targets`, `_evaluate_bash`) plus a helper.
- §6: `GuardTarget` keeps its fields and their order. The new field `unjudgeable_reason` is appended with a default, so every existing constructor call keeps working. No identifier is renamed.
- §9: tabs in Python; Markdown unchanged in style.
- §15 / §21.D: no new API calls.
- §21.C: an unreadable payload or an internal error still fails open. The new block is decided from the command text and the hook's environment alone.
- §28.C: protected path, so twin-first.
- §20: one `changelog.d/` fragment (security section).

## Approach

`guard_targets` gains a `CDPATH` state: in effect when the hook's inherited `CDPATH` is non-empty, or once any earlier segment has a word containing `CDPATH`. A `cd` / `pushd` segment's own `CDPATH=` prefix decides for that segment alone: empty means off, non-empty means on. The prefix does not persist, because `cd` and `pushd` are regular builtins. A `cd` or `pushd` segment with an operand Bash may look up in `CDPATH` while it is in effect makes the directory **unjudgeable** for the rest of the command. That holds whether the directory was known or already unknown. Every later guarded call that would write a branch becomes a target carrying `unjudgeable_reason`. `_evaluate_bash` turns such a target into a block before any lookup.

Alternatives (recorded as AD-1): modelling the search adds an allow path that must match Bash exactly. The existing fallback reproduces the incorrect allow.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue is one change to one hook.

1. **Phase 1 — block guarded git calls after a CDPATH-redirectable `cd` / `pushd`.** Protected paths: `.claude/hooks/pr_merge_status_guard.py`, edited only in its `workflow-templates/.claude/hooks/` twin under the interim twin-first default.
   - Files: the twin hook, `tests/test_pr_merge_status_guard.py`, `CLAUDE.md` (§21.B), `changelog.d/6090-merge-guard-cdpath.md` [new].
   - Done: the new tests pass against the twin. Every existing test passes except `test_template_copies_are_identical`, which stays red until the `[claude-twin-sync]` copy, after which the whole file is green.
   - Rollback: revert the phase PR; the guard resolves `cd` as before.

## Implementation Steps

1. Twin hook (`workflow-templates/.claude/hooks/pr_merge_status_guard.py`):
   - Append `unjudgeable_reason: str = ""` to `GuardTarget` and document it.
   - Add `_cdpath_lookup_operand(executable, args, literal_tilde_words)`, which returns the first operand Bash may search in `CDPATH`, or "".
   - In `guard_targets`, track the `CDPATH` state and its source. Check `cd` / `pushd` segments before the existing directory logic. Mark the state from any earlier word containing `CDPATH`.
   - Pass the unjudgeable reason to `_git_invocation_targets`, which emits one unjudgeable target per call that writes a branch.
   - In `_evaluate_bash`, block such targets with a message naming the reason and the workaround.
   - Update the module and `guard_targets` docstrings.
2. `tests/test_pr_merge_status_guard.py`: unit tests on `twin_guard.guard_targets` (env controlled with `monkeypatch`) and e2e tests on the `worktree_repo` fixture through `_run_twin_hook`, which gains an optional `extra_env` parameter. An e2e test first proves with real Bash that `CDPATH` redirects the `cd`.
3. `CLAUDE.md` §21.B: one paragraph on the `CDPATH` block.
4. `changelog.d/6090-merge-guard-cdpath.md` [new], `<!-- changelog: security -->`.

## Files & Modules

- `workflow-templates/.claude/hooks/pr_merge_status_guard.py` (twin; `.claude/hooks/pr_merge_status_guard.py` follows in the `[claude-twin-sync]` commit)
- `tests/test_pr_merge_status_guard.py`
- `CLAUDE.md` (`workflow-templates/CLAUDE.md` is a symlink to it)
- `changelog.d/6090-merge-guard-cdpath.md` [new]

## Tests

- Unit, each case on the twin:
  - inline, bare, `export`, and inherited `CDPATH` make the call unjudgeable;
  - `pushd` does the same;
  - so does a `CDPATH` `cd` after an unknown directory;
  - a git call before the `cd` keeps its directory;
  - `./`, `..`, absolute, `~/`, no-operand, and `-` operands are not affected;
  - empty inline `CDPATH=` is not affected;
  - no `CDPATH` behaves as before;
  - deletion and `--tags` pushes yield no target.
- E2E on `worktree_repo`:
  - the exploit shape is blocked with no `gh` call: the session checkout is rebuilt and allowed, `repo/wt` is a plain directory, and `CDPATH=<tmp> cd wt` reaches the stranded worktree;
  - the inherited-environment variant is blocked too;
  - `CDPATH=<tmp> cd ./wt && git push …` is still judged normally.
- Full `tests/test_pr_merge_status_guard.py`, plus the repo's lint and format checks for the touched files.

## Risks & Mitigations

- **False block in sessions whose environment exports `CDPATH`.** Every `cd <relative> && git push` is blocked there. Mitigation: the stderr names the one-character workaround (`cd ./<dir>`). ACCEPTED: §1 security first, and `CDPATH` is rare in agent shells.
- **A `CDPATH` set only as an unexported variable in a sourced profile is invisible to the hook.** ACCEPTED: the hook cannot read the Bash tool's shell state; documented in §21.B.
- **Over-approximation:** any earlier word containing `CDPATH` (even `unset CDPATH`) turns the rule on. ACCEPTED: clearing could only be trusted if the earlier command ran, which the walker cannot prove (AD-3).

## Rollout

The change ships with the #5144 project's final PR #5163 into `main`, and reaches consumer repos with the next `@stable` `.claude/` sync. There is no flag. `CLAUDE_PR_MERGE_GUARD=off` still disables the merged-PR check, including this block.

## Auto-decisions

- AD-1 [plan, 2026-10-02] How should the guard treat a `cd` whose operand Bash may look up in `CDPATH` while `CDPATH` may be set? — Picked: A — block the guarded calls after it as unjudgeable, without modelling the search. Alternatives: B — model Bash's `CDPATH` search and judge the directory it picks; C — treat the directory as unknown and fall back to the session checkout with a warning (#6038's proposal). Why: C reproduces the incorrect allow, B adds an allow path that must match Bash exactly, and A only tightens with a trivial workaround. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] Block (exit 2) or ask (permission prompt) for an unjudgeable call? — Picked: A — block. Alternatives: B — ask. Why: the issue says deny, an ask stalls unattended sessions, and the workaround is one edit. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Which `CDPATH` sources count, and can an earlier `unset CDPATH` or empty assignment clear one? — Picked: A — the hook's inherited non-empty `CDPATH`, the `cd`'s own non-empty prefix, and any earlier word containing `CDPATH` turn it on. Only the `cd`'s own empty `CDPATH=` prefix turns it off, for that `cd` alone. Alternatives: B — also honour `unset CDPATH` and empty assignments in earlier segments. Why: the walker cannot prove an earlier command ran (`false && unset CDPATH`), so only setting is safe to model. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-02] Does the rule cover `pushd`, and a `cd` after the directory is already unknown? — Picked: A — yes to both. Alternatives: B — `cd` with a known directory only, leaving the rest to #6038. Why: `pushd` searches `CDPATH` identically, and the existing fallback makes the same incorrect allow in both cases. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-02] Does the block apply to `git commit` as well as `git push`? — Picked: A — both. Alternatives: B — push only, with a warning on commit. Why: the issue says deny the write, both are §21-guarded writes, and the workaround is the same. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-02] #6038 also lists the `CDPATH` row, and its operator sequencing defers that work until #5163 merges. Should this issue fix the `CDPATH` row now, on the project branch? — Picked: A — yes, only the `CDPATH` row, on the project branch named by the issue. Alternatives: B — leave it to #6038 and close this issue as a duplicate. Why: #6090 is the #5144 project's own security follow-up, which its security pass needs fixed on the project branch, and the deferral covered #6038's broader scope. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py --issue 6090` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #6090; parent project #5144 (plan `docs/plans/issue-5144-merge-guard-effective-repo-plan.md`, final PR #5163, phase PR #5173, conformance fix PR #6070); related #6038; audit tracker #3576.
- CLAUDE.md §21, §28.
