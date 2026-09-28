# Allow the read-only `git remote -v`, `git branch --show-current`, and `wc -l` shapes without the Auto-mode classifier

Source issue: shubhodeep1/coding-workflows#4759 (https://github.com/shubhodeep1/coding-workflows/issues/4759)
Base branch: main
Security pass: run

## Summary

An unattended Claude Code session had the read-only chain
`git remote -v && git branch --show-current && gh api repos/<owner>/<repo> --jq .default_branch && wc -l <file>`
denied because the Auto-mode classifier was unavailable. Add exact
`permissions.allow` rules for the three reads that no rule covered, so this
shape is approved deterministically and never depends on the classifier.

## Context

- Issue #4759 was filed by `.claude/scripts/permission_prompts.py`
  (CLAUDE.md §23.I) from session `session_01X34Rkb13B5HJabtRPdz8ni` on
  2026-09-28. The reason Claude Code gave was `Classifier unavailable`: the
  call was not approved by any allow rule or hook, so it fell through to the
  classifier, which was down, and was denied.
- The chain is the start-up read of `/implement-issue-claude` step 0 (resolve
  the repo slug and the default branch, then read
  `.claude/commands/implement-plan-claude.md`).
- `.claude/settings.json` `permissions.allow` already covers
  `gh api repos/*` (§23.D, the REST read), `git status *`, `git log *`,
  `git rev-parse *`, and other git reads. It has no rule for `git remote -v`,
  `git branch --show-current`, or `wc -l <file>`.
- Claude Code splits a compound command on `&&`, `;`, and `|` and approves
  it only when every subcommand matches an allow rule (or a hook allows the
  whole call). One unmatched subcommand sends the whole call to the
  classifier.
- `.claude/hooks/gh_api_write_guard.py` (§23.H) returns no decision for this
  chain, because `git remote -v`, `git branch --show-current`, and a
  standalone `wc -l <file>` are not among its safe helpers.
- `workflow-templates/.claude/settings.json` is a byte-identical copy that
  consumer repos receive through the `.claude/` sync; the two must stay equal.
- The issue's "How to fix" lists, in order: change the command file to an
  allowlisted shape; add a helper; for reads only, add an exact
  `permissions.allow` rule.

## Goals

- `Bash(git remote -v)`, `Bash(git branch --show-current)`, and
  `Bash(wc -l *)` are in `permissions.allow` of both
  `.claude/settings.json` and `workflow-templates/.claude/settings.json`,
  which stay byte-identical.
- A test in `tests/test_permission_prompts.py` fails if any of the three
  rules is missing from either file.
- `agents.md` (§23.I section) names the rules and why they exist, and a
  `changelog.d/` fragment records the change (§20).

## Non-goals

- The sibling permission-prompt issues from the same classifier outage
  (#4749, #4751, #4760, #4761, #4762, #4767) are separate issues and
  separate projects (one issue, one chain). #4760's pattern is a subset of
  this one, so this change also removes its denial; that issue is not
  closed or edited here.
- No change to `gh_api_write_guard.py` or its safe-helper list.
- No change to any command file's prose.
- No wider rules (`git remote *`, `git branch *`, `wc *`): `git remote` and
  `git branch` have write subcommands (`add`, `set-url`, `-D`, `-m`).

## Constraints

- §23.I / the issue's fix order: exact allow rules are allowed only for reads
  and §23.B routine writes. All three rules are reads.
- §6: no existing allow rule is renamed or removed; the rules are additions.
- §5: the smallest change that settles the denial: three rules, no hook edit.
- §9: `settings.json` keeps its existing 6-space indentation (a sub-format
  convention the file already pins; no reformatting).
- §14 / §20: the settings file ships to consumer repos on the next `@stable`
  sync, so a changelog fragment is required.
- §28.C: both settings files are under `.claude/`, a protected path, so the
  phase needs a recorded `Protected-path approval:` before it starts.

## Approach

Add the three exact rules next to the existing git read rules in both
settings files. `git remote -v` and `git branch --show-current` are exact
(no wildcard), so they approve only those two argument lists. `wc -l *`
only counts lines, which cannot write or leak file contents.

Alternatives considered (see `## Auto-decisions`): extending the `gh api`
guard's safe helpers only helps chains that contain a `gh api` call and
widens a security hook; editing the command prose does not stop a model
from composing the same chain again; closing the issue as a transient
outage leaves the next outage to deny the same read.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — allow the three read shapes.**
   - Files: `.claude/settings.json`, `workflow-templates/.claude/settings.json`,
     `tests/test_permission_prompts.py`, `agents.md`,
     `changelog.d/4759-allow-read-only-git-wc.md` [new].
   - protected paths: `.claude/settings.json`,
     `workflow-templates/.claude/settings.json`.
   - Done when: both settings files carry the three rules and are
     byte-identical; the new test passes; the §23.I CI step
     (`tests/test_dispatch_workflow.py`, `tests/test_edit_comment.py`,
     `tests/test_permission_prompts.py`) and
     `tests/test_session_start_extract_repo_slug.py` (byte-identical settings
     guard) pass.
   - Rollback: revert the PR; the three rules disappear and the shapes go
     back to the classifier.

## Implementation Steps

1. `.claude/settings.json`: insert `"Bash(git remote -v)"`,
   `"Bash(git branch --show-current)"` after `"Bash(git rev-parse *)"`, and
   `"Bash(wc -l *)"` after them.
2. Copy the file byte for byte to `workflow-templates/.claude/settings.json`.
3. `tests/test_permission_prompts.py`: add a parametrized test over both
   settings paths asserting the three rules are present.
4. `agents.md`: one bullet in "Unattended helpers and permission prompt
   reports (CLAUDE.md §23.I)" naming the three rules and issue #4759.
5. `changelog.d/4759-allow-read-only-git-wc.md` [new], section `fixed`.

## Files & Modules

- `.claude/settings.json`
- `workflow-templates/.claude/settings.json`
- `tests/test_permission_prompts.py`
- `agents.md`
- `changelog.d/4759-allow-read-only-git-wc.md` [new]

## Tests

- Unit: the new test in `tests/test_permission_prompts.py`.
- Existing: `tests/test_session_start_extract_repo_slug.py` (settings copies
  byte-identical), `tests/test_gh_api_write_guard.py`,
  `tests/test_dispatch_workflow.py`, `tests/test_edit_comment.py`,
  `tests/test_check_in_status.py`, `tests/test_stale_routines.py`,
  `tests/test_implement_issue_claude_command.py` (all read the allow list).
- JSON validity: `python3 -m json.tool` on both files.

## Risks & Mitigations

- `wc -l *` could be combined with an output redirect. Mitigation: Claude
  Code checks redirects separately from prefix rules, and the existing
  `git log *` / `git show *` / `test *` rules carry the same exposure.
  ACCEPTED — read-only command, same posture as existing rules.
- The phase edits `.claude/**` and stops before it starts until a human
  approves (§28.C). ACCEPTED — the issue says to expect that stop.

## Rollout

Ships to this repo on the final PR's merge and to consumer repos on the next
`@stable` `.claude/` sync. No flag; revert the PR to roll back.

## Auto-decisions

- AD-1 [plan, 2026-09-28] How should the denied read chain be fixed? — Picked: A — add exact `permissions.allow` rules for `git remote -v`, `git branch --show-current`, and `wc -l *` in both settings copies. Alternatives: B — extend `gh_api_write_guard.py`'s safe helpers; C — reword `/implement-issue-claude` step 0 only; D — close the issue as a transient classifier outage. Why: the issue's fix list allows exact rules for reads, they approve the shape in any chain, and they are the smallest change (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How wide should the `wc` rule be? — Picked: A — `Bash(wc -l *)`. Alternatives: B — `Bash(wc *)`. Why: matches the observed shape and nothing more (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Should the sibling permission-prompt issues (#4760 and the others from the same outage) be folded in? — Picked: A — no; note that #4760's pattern is covered and leave every sibling to its own project. Alternatives: B — fold #4760 in and close it. Why: one issue, one chain (`/implement-issue-claude` rules). Applied in: no code change. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` returned `{"skip": false, "reason": "no skip label"}`.

## References

- Issue #4759; sibling issues #4749, #4751, #4760, #4761, #4762, #4767.
- CLAUDE.md §23.D, §23.H, §23.I, §28.C.
- `.claude/hooks/gh_api_write_guard.py`, `.claude/scripts/permission_prompts.py`.
