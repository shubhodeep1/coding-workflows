# Claude issue intake authorizes the target issue and its dispatcher

Source issue: shubhodeep1/coding-workflows#4620 (https://github.com/shubhodeep1/coding-workflows/issues/4620)
Base branch: main
Security pass: skip (ai:security: automation-produced issue)

## Summary

`claude-issue-intake.yml` queues a write-capable Claude implementation session
for any issue number in any registered repository, after checking only that
the repository is registered. This plan makes the intake verify the live
target issue, its author, and the dispatcher's authority on the target
repository before it queues anything.

## Context

- Finding `intake-does-not-authorize-target-issue` (A01:2021 Broken Access
  Control, high, `scripts/claude_issue_route.py:220`), filed by
  `.github/workflows/security-audit.yml`, tracker Refs #3576.
- `validate_payload` (`scripts/claude_issue_route.py:203-236`) checks the
  schema, the repo slug, that the repo is in `.github/ai/consumer_repos.json`
  (or is coding-workflows), the issue number, and the trigger. Nothing checks
  that the issue exists, is open, is an issue and not a pull request, who
  wrote it, or who sent the dispatch.
- `scripts/claude_issue_intake.sh` then opens the `ai:claude-issue-queue`
  item, and the Claude issue pickup (`.claude/commands/claude-issue-pickup.md`)
  starts an Opus session in Auto mode running `/implement-issue-claude` on it.
- Clarify's own gate (`.github/workflows/clarify.yml:20-21`, `:450-463`) only
  routes an opened issue whose author is a `User` with `OWNER`, `MEMBER`, or
  `COLLABORATOR` association (or `github-actions[bot]`), or a `/reclarify`
  comment by such a `User`. Anyone who can send a `claude-issue`
  `repository_dispatch` or run the intake's `workflow_dispatch` skips that
  gate and can name any issue in any registered repo, including one opened by
  an outside user whose body then becomes the session's task input.
- Constraints: CLAUDE.md §1 (security first), §5 (minimal change), §6
  (no renames; new identifiers unique), §9 (tabs; YAML 2-space), §15 (GitHub
  API hygiene), §18 (runs inside the existing intake workflow; no new
  script), §20 (changelog fragment for a security fix), §27 (workflow size;
  the intake workflow is small).

## Goals

1. The intake queues an item only when **all** of these hold, checked
   against live GitHub data:
   - every dispatcher login (`github.actor`, and `github.triggering_actor`
     when it differs) has `admin` or `write` permission on the target repo;
   - the target number is an issue (not a pull request) in the target repo
     (its `repository_url` names that repo, so a transferred issue is
     refused) and is open;
   - its author is trusted by clarify's rule (a `User` with `OWNER` /
     `MEMBER` / `COLLABORATOR` association, or `github-actions[bot]`), or a
     trusted `User` commented `/reclarify` on it.
2. A refused dispatch writes nothing to the target issue with `GH_PAT` (no
   label, no comment), logs `CLAUDE_ISSUE_INTAKE rejected reason=<reason>`,
   emits `::error::`, sends a Telegram ERROR, and exits 1.
3. The decision is a pure, unit-tested function in
   `scripts/claude_issue_route.py` with a CLI subcommand the shell driver
   calls; the shell only performs the reads.
4. A legitimate dispatch (trusted author, owner PAT) behaves exactly as
   today.

## Non-goals

- Binding the dispatch to the reporter's clarify run (`reporter_run_url`):
  the dispatcher authorization check covers the same threat with data GitHub
  sets itself (AD-1).
- A second author gate inside `/implement-issue-claude` (AD-5).
- Changing clarify, the handoff script, the pickup, or the queue format.
- Changing the existing `invalid_payload` / `queue_failed` failure paths.

## Constraints

- §6: no existing identifier is renamed or removed. New identifiers:
  `TRUSTED_ISSUE_AUTHOR_ASSOCIATIONS`, `TRUSTED_ISSUE_BOT_AUTHOR`,
  `DISPATCHER_ALLOWED_PERMISSIONS`, `RECLARIFY_COMMAND_PREFIX`,
  `is_trusted_issue_author`, `has_trusted_reclarify`, `authorize_target`,
  `_cmd_authorize_target`, CLI subcommand `authorize-target`, env vars
  `CLAUDE_ISSUE_DISPATCHER` / `CLAUDE_ISSUE_TRIGGERING_ACTOR` (default empty,
  which refuses), shell function `reject`, log reason keys
  `dispatcher_unknown`, `dispatcher_not_authorized`,
  `authorization_read_failed`, `target_not_issue`, `target_repo_mismatch`,
  `issue_closed`, `untrusted_issue_author`. Each was checked unused in its
  scope.
- §15: at most 1 permission read per distinct dispatcher login (1, or 2 on a
  re-run by another user), 1 issue read, and a paginated comments read only
  when the author is untrusted. No existing call in the intake returns this
  data (the only existing reads are the queue list), so the new calls carry a
  comment saying so.
- §4: the new env vars default to empty; empty means "unknown dispatcher" and
  refuses (fail closed), which is the secure default.

## Approach

`authorize_target(validated, issue, dispatcher_permissions, comments=None)`
returns `{"authorized", "reason", "needs_comments"}`. The shell:

1. validates the payload (unchanged);
2. reads `repos/<repo>/collaborators/<login>/permission` (`.permission`) for
   each distinct dispatcher login;
3. reads `repos/<repo>/issues/<N>`;
4. calls `authorize-target`; when it answers `needs_comments`, reads the
   issue comments (`--paginate`, `per_page=100`) and calls it again;
5. queues only on `authorized: true`; otherwise `reject <reason>`.

Any read failure refuses as `authorization_read_failed` (AD-3).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan:
the change is one gate in one workflow step and cannot be split usefully.

1. **Phase 1 — intake authorizes the target issue and dispatcher.**
   - Files: `scripts/claude_issue_route.py`, `scripts/claude_issue_intake.sh`,
     `.github/workflows/claude-issue-intake.yml`,
     `tests/test_claude_issue_route.py`, `README.md`, `agents.md`,
     `changelog.d/4620-intake-authorizes-target-issue.md` [new].
   - Done when: the new unit and shell-driver tests pass, every existing test
     in `tests/test_claude_issue_route.py` and
     `tests/test_implement_issue_claude_command.py` passes, and the intake
     step passes `github.actor` / `github.triggering_actor` through `env:`.
   - Rollback: revert the PR; the intake returns to the repo-only check.

## Implementation Steps

1. `scripts/claude_issue_route.py`: add the constants, `is_trusted_issue_author`,
   `has_trusted_reclarify`, `authorize_target`, `_cmd_authorize_target`, and
   the `authorize-target` subcommand (`--validated-json`, `--issue-json`,
   `--permissions-json`, optional `--comments-json`); update the module
   docstring.
2. `scripts/claude_issue_intake.sh`: add `reject`, the permission / issue /
   comments reads (`gh_retry_to_file`), the authorize call, and the header
   comment; queue only when authorized.
3. `.github/workflows/claude-issue-intake.yml`: bind
   `CLAUDE_ISSUE_DISPATCHER: ${{ github.actor }}` and
   `CLAUDE_ISSUE_TRIGGERING_ACTOR: ${{ github.triggering_actor }}` in the
   queue step's `env:`; update the header comment.
4. Tests (below); `README.md` "Claude issue implementer" flow step 3 and
   failure modes; `agents.md` item 15; changelog fragment (`security`).

## Files & Modules

- `scripts/claude_issue_route.py`
- `scripts/claude_issue_intake.sh`
- `.github/workflows/claude-issue-intake.yml`
- `tests/test_claude_issue_route.py`
- `README.md`
- `agents.md`
- `changelog.d/4620-intake-authorizes-target-issue.md` [new]

## Tests

- Unit: `authorize_target` accepts a trusted author, a trusted bot author,
  and an untrusted author with a trusted `/reclarify`; refuses an unknown
  dispatcher, a `read`/`none` dispatcher, any unauthorized login among two,
  a pull request, a number mismatch, a transferred issue, a closed issue, an
  untrusted author without a vouching comment, a `/reclarify` by a
  non-collaborator or a `Bot`, and asks for comments only when needed.
- Shell driver (stubbed `gh`): the happy path reads permission and issue and
  queues; an unauthorized dispatcher, an untrusted author, a closed issue,
  and a read failure each exit 1 with `rejected reason=…` and make no write
  to the target issue and no queue issue; an untrusted author with a trusted
  `/reclarify` queues; a re-run by a second login checks both.
- Workflow wiring: the queue step binds both actor env vars from GitHub
  context, never from the payload.
- CI: `ci.yml` already runs `tests/test_claude_issue_route.py`.

## Risks & Mitigations

- A legitimate dispatch is refused because `GH_PAT` cannot read collaborator
  permission on a consumer repo → the PAT already labels and comments there
  (repo scope, §14), which implies the read; a refusal is loud (red run,
  Telegram ERROR) and `/reclarify` retries.
- A transient read failure refuses without telling the issue → ACCEPTED —
  the handoff's routing comment is already on the issue, the run is red, and
  the Telegram ERROR names the retry (AD-3).

## Rollout

Merging to `main` activates it: `repository_dispatch` and `workflow_dispatch`
run the intake from the default branch. Consumer repos need no sync; their
handoff payload is unchanged.

## Auto-decisions

- AD-1 [plan, 2026-09-27] How should each dispatch be bound to an authorized party? — Picked: A — check that every dispatcher login GitHub reports for the run (`github.actor`, and `github.triggering_actor` when different) has `admin` or `write` permission on the target repo. Alternatives: B — verify the payload's `reporter_run_url` is a live clarify run in the target repo; C — both. Why: the actor is set by GitHub and cannot be forged in the payload, while a run URL only proves some run exists and cannot be tied to one issue. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Which issue authors may be implemented? — Picked: A — clarify's rule: a `User` with `OWNER`/`MEMBER`/`COLLABORATOR` association or `github-actions[bot]`, or any author when a trusted `User` commented `/reclarify`. Alternatives: B — trusted authors only, ignoring `/reclarify`; C — no author check. Why: matches the gate the dispatch bypasses, so a collaborator can still vouch for an outside issue as clarify allows today. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What does a refused or unverifiable dispatch write? — Picked: A — nothing on the target issue; `CLAUDE_ISSUE_INTAKE rejected` log, `::error::`, Telegram ERROR, exit 1, also for read failures. Alternatives: B — the existing `fail` path (label + comment on the target); C — B for read failures only. Why: until authorization succeeds the target is unverified, so an unauthorized dispatcher must not be able to make `GH_PAT` write to arbitrary issues. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Should the intake queue a closed issue? — Picked: A — refuse as `issue_closed`. Alternatives: B — queue it and let `/implement-issue-claude` stop. Why: the recommendation asks to verify the live target, clarify skips closed issues, and a refused closed issue costs no session. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Add a second author gate inside `/implement-issue-claude`? — Picked: A — no; the intake is the only producer of queue items the pickup trusts (`github-actions[bot]`), so one gate there closes the path. Alternatives: B — also gate in the command file. Why: §5 minimal change; a human running the command by hand chooses the issue themselves, and the command file ships to every consumer repo. Applied in: no code change. Status: pending review
