# gh api guard: prompt for file-backed field values

Source issue: shubhodeep1/coding-workflows#4619 (https://github.com/shubhodeep1/coding-workflows/issues/4619)
Base branch: main
Security pass: skip (ai:security: automation-produced issue)

## Summary

`.claude/hooks/gh_api_write_guard.py` (CLAUDE.md §23.H) auto-approves `gh api repos/<local>/issues/1/comments -F body=@/path/to/credential`. `gh` reads the local file and publishes its contents as a comment. This plan makes every file-backed request value force the permission prompt.

## Context

- Security audit finding `api-guard-allows-file-backed-comment` (issue #4619, high, confidence 10/10, `A01:2021-Broken Access Control`) at `.claude/hooks/gh_api_write_guard.py:642`, tracked by the AI Security Audit Tracker #3576.
- `gh api --help`: an `-F/--field` value that starts with `@` is read from the named file (`@-` reads standard input), while `-f/--raw-field` values are sent literally. `--input <file>` sends the file as the request body on any method, and field flags then go into the query string.
- `classify()` checks only field **keys** against each routine endpoint's allowlist (`gh_api_write_guard.py:639-644`), never the values. So `-F body=@<any file>` on a routine endpoint is routine, and a single such call is approved (`permissionDecision: allow`).
- A plain "no decision" is not a safe fallback: `.claude/settings.json` allows `Bash(gh api repos/*)`, so a call the guard leaves undecided can still be approved by the allow list. A file-backed value must force `ask`.
- The same file read also leaks through calls classified as **read**: `-X GET ... -F q=@<file>` puts the file in the query string, a GraphQL variable `-F v=@<file>` sends it in the request body, and `-X GET ... --input <file>` sends it as the body (`classify()` tests `_READ_METHODS` before `--input`, `gh_api_write_guard.py:625-628`).
- The documented ways to post a body do not read files: `mcp__github__update_pull_request`, `mcp__github__add_issue_comment`, `mcp__github__update_issue_comment` (CLAUDE.md §23.D item 4, `/implement-issue-claude` Tool Access), or an inline `-f body=...`. Actions scripts that use `-F body=@file` (`scripts/post_review_comment.sh`, `scripts/workflow_failure_heal_intake.sh`) run in Actions, where the hook never runs.

## Goals

- Every `gh api` call with an `-F`/`--field` value starting with `@` is classified `write` and forces `permissionDecision: ask`, whatever the method, endpoint, or repository, GraphQL included.
- Every `gh api` call with `--input` is classified `write` on every method, including GET and HEAD.
- `-f`/`--raw-field` values starting with `@` stay as they are today (sent literally, so no file is read).
- The ask reason names the file-backed flag, so the prompt explains itself.
- CLAUDE.md §23.H/§23.D, `agents.md`, and the hook docstring describe the new rule. A `changelog.d/` fragment records the security fix.

## Non-goals

- An allowance for file-backed values from a "private generated-content directory" (the recommendation's optional second half). See AD-2.
- Changing the Actions-side scripts that use `-F body=@file`. The hook does not run there.
- Any other change to the classification rules, the safe-helper list, or `settings.json`.

## Constraints

- §1: security first. The fix prompts in more cases and never allows more.
- §6: no identifier is renamed or removed. `classify`, `parse_gh_api_args`, `KIND_*`, `DECISION_*`, and the parsed-dict shape stay the same. Only new private names are added, and each is checked for clashes in the module.
- §9: tabs in Python, matching the file.
- §14: the hook ships to consumers through the `.claude/` sync. `workflow-templates/.claude/hooks/gh_api_write_guard.py` must stay byte-identical (`test_template_parity`). `workflow-templates/CLAUDE.md` is a symlink to `../CLAUDE.md`.
- §15: no new GitHub API calls. The hook still issues none.
- §20: add a fragment under `changelog.d/`. Never edit `CHANGELOG.md` directly.
- §23.H: the guard still fails closed.

## Approach

In `classify()`, before the GraphQL and read-method branches:

1. If any parsed field has role `field` (`-F`/`--field`) and its value starts with `@`, return `write` with the description `<METHOD> <endpoint> with file-backed field <key>`.
2. If `parsed["input"]` is not `None`, return `write` with the description `<METHOD> <endpoint> with --input`. This moves the existing `--input` check ahead of the read-method check.

`parse_gh_api_args` already records the role of each field, so nothing else changes. The GraphQL branch's existing `query=@` check stays as defence in depth.

Alternatives considered: allowing `@` paths under a session scratchpad directory (AD-2), and checking values only on routine endpoints (AD-1). Both were rejected because they are weaker or depend on paths that cannot be verified.

## Phases & Merge Strategy

A single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for a standalone issue.

1. **Phase 1: file-backed values and `--input` always prompt.**
   - Files: `.claude/hooks/gh_api_write_guard.py`, `workflow-templates/.claude/hooks/gh_api_write_guard.py`, `tests/test_gh_api_write_guard.py`, `CLAUDE.md` (§23.D item 4, §23.H), `agents.md`, `changelog.d/4619-gh-api-guard-file-backed-fields.md`.
   - Done when:
     - every file-backed or `--input` case in Tests asks;
     - every existing allow, no-decision, and ask case not involving file-backed values keeps its outcome;
     - `tests/test_gh_api_write_guard.py` passes;
     - template parity holds.
   - Rollback: revert the PR. The guard goes back to its previous classification.

## Implementation Steps

1. `.claude/hooks/gh_api_write_guard.py` `classify()`: add the file-backed field check and the method-independent `--input` check ahead of the GraphQL and `_READ_METHODS` branches. Update the module docstring's `write` bullet.
2. Copy the hook byte-for-byte to `workflow-templates/.claude/hooks/gh_api_write_guard.py`.
3. `tests/test_gh_api_write_guard.py`:
   - Move the three observed prompts that used `-F body=@…` into a new expectation, `FILE_BACKED_CALLS`, which asserts `ask`.
   - Move `ALLOWED_SIMPLE_CALLS`'s `-F body=@/tmp/body.md` and `NO_DECISION_COMMANDS`'s `-F body=@"$F"` into the same expectation.
   - Add cases: the issue's exploit, `@-` stdin, `--field=` and `-Fkey=` spellings, an array key, a GET query field, a GraphQL variable, `--input` on GET and HEAD, the `{owner}/{repo}` placeholders, and the ask reason naming the flag.
   - Add allow cases for `-f body=@mention` (raw, literal).
4. CLAUDE.md §23.H table and §23.D item 4, and `agents.md`'s guard paragraph: state that a file-backed `-F` value or `--input` always prompts, and point body text to the MCP tools or an inline `-f body=...`.
5. `changelog.d/4619-gh-api-guard-file-backed-fields.md` (`<!-- changelog: security -->`).

## Files & Modules

- `.claude/hooks/gh_api_write_guard.py`
- `workflow-templates/.claude/hooks/gh_api_write_guard.py`
- `tests/test_gh_api_write_guard.py`
- `CLAUDE.md` (and so `workflow-templates/CLAUDE.md`, a symlink)
- `agents.md`
- `changelog.d/4619-gh-api-guard-file-backed-fields.md` [new]

## Tests

- Unit (`tests/test_gh_api_write_guard.py`, its own `ci.yml` step): the new `FILE_BACKED_CALLS` ask cases, the raw-field allow cases, the reason text, and every existing case run unchanged except the five moved ones.
- Hook process: an `ask` JSON is emitted on stdout for the issue's exploit command.
- Repo checks: `python3 -m pytest -q tests/test_gh_api_write_guard.py`, plus the neighbouring hook tests (`tests/test_pr_watch_guard.py`, `tests/test_pr_check_in_reminder.py`) to confirm the settings wiring is untouched.

## Risks & Mitigations

- Sessions that edited PR bodies or progress comments with `-F body=@file` now get a prompt, and an unattended session can stop at it. Mitigation: CLAUDE.md §23.D item 4 and `/implement-issue-claude` already route those edits through the GitHub MCP tools, and the docs now say so where the guard is described. ACCEPTED: a prompt is the safe failure mode for a credential-exfiltration path (§1).
- A body that starts with `@` passed via `-F` (for example `@octocat please review`) now prompts. ACCEPTED: `gh` would try to read it as a file anyway, so that call was already broken.

## Rollout

Ships to this repo on merge. It reaches the consumers in `.github/ai/consumer_repos.json` on the next `@stable` sync of `.claude/`. There is no flag and no migration. Rollback is a revert.

## References

- Issue #4619 (finding `api-guard-allows-file-backed-comment`), tracker #3576.
- PR #4592 (introduced the guard), `changelog.d/4592-gh-api-permission-guard.md`.
- CLAUDE.md §23.B, §23.D, §23.H.

## Auto-decisions

- AD-1 [plan, 2026-09-27] Which calls does a file-backed `-F` value make a write? — Picked: A — every call, any method, endpoint, or repository, GraphQL variables included. Alternatives: B — only routine write endpoints. Why: a GET query field or GraphQL variable sends the file off-box too, and the `Bash(gh api repos/*)` allow rule would still approve those. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Should file-backed values be allowed from a private generated-content directory (the recommendation's optional half)? — Picked: A — no allowance: every file-backed value prompts, and bodies go through the GitHub MCP tools or an inline `-f body=...`. Alternatives: B — allow `@<path>` that resolves under the session scratchpad. Why: the hook cannot verify a path is private or its contents are safe (a session can be steered to copy a secret there). No interactive flow depends on it, since the documented paths are MCP tools. §1 and §5. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Should `--input` on GET/HEAD, classified read today, also prompt? — Picked: A — yes, `--input` is a write on every method. Alternatives: B — leave it, as outside the finding. Why: same file-read exfiltration path in the same function, and a one-line reordering fixes it (CLAUDE.md §12.B, latent bug in adjacent code). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Should `-f`/`--raw-field` values starting with `@` prompt too? — Picked: A — no, they stay as today. Alternatives: B — prompt for them as well. Why: `gh` sends raw fields literally and reads no file, and `-f body=@user ...` is a common benign mention. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] What happens to the existing test fixtures that recorded file-backed calls as allowed or undecided? — Picked: A — keep the observed commands and move them to a new ask expectation. Alternatives: B — delete them. Why: they are real commands from stage sessions, and they are the best regression cases for this fix. Applied in: phase 1 PR. Status: pending review
