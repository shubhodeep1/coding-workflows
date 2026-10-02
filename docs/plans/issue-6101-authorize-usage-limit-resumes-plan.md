# Usage-limit resumes: only resume sessions the pickup's workflows started

Source issue: shubhodeep1/coding-workflows#6101 (https://github.com/shubhodeep1/coding-workflows/issues/6101)
Base branch: claude/implement-plan-issue-5660-resume-usage-limit-stops
Security pass: skip (ai:security: automation-produced issue)

## Summary

The usage-limit resume selector (`.claude/scripts/usage_limit_resumes.py`, issue #5660) picks any stopped session from the account-wide `list_sessions` (`mine: true`) listing. It never checks which repository the session works in or how it was started, so the Claude issue pickup can send a "continue from your latest instructions" trigger to an unrelated Auto-mode session (security finding #6101, A01 Broken Access Control). This plan makes the selector authorize every resume against the server-reported session source and lineage, and fail closed for anything it cannot place.

## Automation (CLAUDE.md §18.E)

- **Scripts:** no new script. Extends the existing selector `.claude/scripts/usage_limit_resumes.py`, which the pickup already runs on every wake (step 1a).
- **Scheduler entry point:** unchanged: the pickup's self-bound cron Routine `Claude issue pickup: hourly` and its catch-up wake (`.claude/commands/claude-issue-pickup.md` steps 1, 1a, 4). The new arguments have defaults, so the pickup's existing command line keeps working.
- **Supervisor:** none new. The Claude issue pickup supervisor is unchanged.
- **DB work:** none.
- **Future-removal registry:** no entry. The selector is part of the pickup supervisor, whose entry in `docs/scripts-pending-removal.md` stays as it is.

## Context

- Finding #6101 (`.claude/scripts/usage_limit_resumes.py:443`, in `select`): "The pickup selects any eligible session returned by account-wide `mine: true` listing, without checking its repository or origin. It can schedule an unattended continuation for an unrelated auto-mode session." Recommendation: "Authorize resumes using server-reported session source and lineage against the pickup's registered workflows; fail closed for unknown origins."
- Server-reported fields on every `list_sessions` / `get_session` entry (read 2026-10-02 from this account):
  - `session_context.sources[].git_repository.url`, e.g. `https://github.com/shubhodeep1/coding-workflows`;
  - `origin`: `claude_code_mcp_seed` on every session started with `create_session` (the pickup's implementation and fixer sessions, checkers, stage sessions, fresh fixers), `desktop_app` on a session a person opened in the app;
  - `parent_session_id`: the session that called `create_session`; absent on a session a person opened.
- The chains the pickup resumes are not all rooted at the pickup. This session (`#6101 · issue … — implement`) has parent `session_01Db6EvqsPiaDHDTp8fUMeV8` (a `/fix-claude-pr` session, origin `claude_code_mcp_seed`), whose parent `session_01QAjMKN7ui2G42nCbJ39BJf` is an operator's `desktop_app` session with no parent. A pickup restarted with `— restart` also leaves every older chain rooted at the archived previous pickup.
- The pickup starts sessions in coding-workflows and in every repository of `.github/ai/consumer_repos.json` (`scripts/claude_issue_route.py queue-pending --registry .github/ai/consumer_repos.json`, `load_allowed_repos`), and `claude_session_janitor.session_repo` already parses `session_context.sources` the same way.
- Sibling finding #6102 changes the `need_input` check in the same file (`_skip_reason`); the two changes touch different lines.

## Goals

- A session is resumed only when all three hold, read from server-set fields:
  1. **Repository:** it has at least one GitHub source, and every GitHub source is coding-workflows (`--self-repo`) or a repository in the consumer registry (`--registry`). Compared case-insensitively.
  2. **Origin:** its `origin` is `claude_code_mcp_seed` (started by another session with `create_session`).
  3. **Lineage:** it carries a well-formed `parent_session_id` (`session_<x>` or `cse_<x>`).
- Anything else is skipped and listed under `skipped` with one of four new reasons: `no_repo` (no readable GitHub source), `foreign_repo` (a source outside the registered repositories), `unknown_origin` (any other or missing `origin`), `no_lineage` (no well-formed `parent_session_id`). Both signals (`text` and `rate_limit_info`) go through the check.
- An unreadable or malformed registry file narrows the allowed set to `--self-repo` and adds one line to `errors`; it never widens it and never stops the wake.
- The pickup's existing command line works unchanged: `--self-repo` defaults to `shubhodeep1/coding-workflows` and `--registry` to `.github/ai/consumer_repos.json` (relative to the working directory, the repository root where the pickup runs).

## Non-goals

- Requiring the parent chain to reach the pickup session (AD-1 B).
- Matching `environment_id` (AD-3).
- Changing the resume prompts, the ordering, the cap, the fire spacing, or the hold-off.
- The `need_input` change of #6102.

## Constraints

- §1: security first; the check fails closed.
- §5: minimal change, confined to the selector, its tests, and docs.
- §6: no existing identifier, output key, or skip reason is renamed; four skip reasons and two optional arguments are added. New names checked for clashes in the module (`AUTHORIZED_ORIGINS`, `DEFAULT_SELF_REPO`, `DEFAULT_REGISTRY_PATH`, `SOURCE_URL_PATTERN`, `load_allowed_repos`, `session_repos`, `_unauthorized_reason`).
- §15: no API call is added; the selector stays offline.
- §20: a `changelog.d/` fragment (security).
- §28.C protected paths: the selector lives under `.claude/scripts/`. Twin-first: edit `workflow-templates/.claude/scripts/usage_limit_resumes.py` only; `.claude/commands/claude-issue-pickup.md` has no twin, so its one-line doc change goes into the twin-sync blocker as a diff.

## Approach

Add `_unauthorized_reason(session, allowed_repos)` and call it from `_skip_reason` right after the `pickup` check, so an unauthorized session is never resumed whatever its other state. `select` receives the allowed set (a `frozenset` of lowercased slugs) from `main`, which builds it with `load_allowed_repos(registry_path, self_repo, errors)`. `session_repos` returns every `<owner>/<repo>` parsed from `session_context.sources[].git_repository.url` with the same URL pattern the janitor uses. `select`'s callers are `main` and the tests. The new parameter is keyword-only with a default of `None`, and `None` authorizes nothing, so a caller that forgets it fails closed (AD-5).

Alternatives are recorded as auto-decisions below.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: one issue, one phase, one PR into the project branch.

1. **Phase 1 — authorize resumes by source and lineage.**
   - Files: `workflow-templates/.claude/scripts/usage_limit_resumes.py` (twin of `.claude/scripts/usage_limit_resumes.py`), `tests/test_usage_limit_resumes.py`, `README.md`, `agents.md`, `changelog.d/6101-authorize-usage-limit-resumes.md` [new]; `.claude/commands/claude-issue-pickup.md` and `.claude/scripts/usage_limit_resumes.py` through the twin-sync blocker.
   - Done when: the selector skips unregistered-repository, wrong-origin, and parentless sessions with the new reasons; registered workflow sessions are still resumed; the tests below pass against the twin; docs describe the rule.
   - Rollback: revert the phase PR; the selector returns to the #5660 behaviour.

## Implementation Steps

1. `workflow-templates/.claude/scripts/usage_limit_resumes.py`:
   - constants `AUTHORIZED_ORIGINS = frozenset({"claude_code_mcp_seed"})`, `DEFAULT_SELF_REPO = "shubhodeep1/coding-workflows"`, `DEFAULT_REGISTRY_PATH = ".github/ai/consumer_repos.json"`, `SOURCE_URL_PATTERN`;
   - `load_allowed_repos(path, self_repo, errors)`: registry slugs plus the self repo, lowercased; unreadable / non-list registry → self repo only plus one `errors` line;
   - `session_repos(session)`: the GitHub slugs of `session_context.sources`;
   - `_unauthorized_reason(session, allowed_repos)`: `no_repo`, `foreign_repo`, `unknown_origin`, `no_lineage`, or None;
   - `_skip_reason(..., allowed_repos=...)` and `select(..., allowed_repos=None)` wire it in after `pickup`;
   - `build_parser`: `--self-repo`, `--registry`; `main` builds the set;
   - module docstring: the authorization rule and the new skip reasons.
2. `tests/test_usage_limit_resumes.py`: the default fixture becomes an authorized workflow session (source, origin, parent); the runner passes a registry file; new tests for each skip reason, both signals, case-insensitive repo match, consumer-registry repo accepted, multi-source with one foreign source rejected, unreadable registry narrows and reports, `select` without `allowed_repos` resumes nothing, and the pickup's argv (no new flags) still works from the repo root.
3. `README.md` (Claude issue implementer, step 1a list) and `agents.md` (usage-limit resumes, skip reasons): document the rule.
4. `changelog.d/6101-authorize-usage-limit-resumes.md` (`security`).
5. Twin-sync blocker: the script twin's target and sha256, and the pickup step 1a.2 diff (the `skipped` bullet lists the new reasons).

## Files & Modules

- `workflow-templates/.claude/scripts/usage_limit_resumes.py`
- `.claude/scripts/usage_limit_resumes.py` (via the twin sync)
- `.claude/commands/claude-issue-pickup.md` (no twin; diff in the blocker)
- `tests/test_usage_limit_resumes.py`
- `README.md`, `agents.md`
- `changelog.d/6101-authorize-usage-limit-resumes.md` [new]

## Testing

- `python3 -m pytest tests/test_usage_limit_resumes.py` (parity test red until the twin sync, as for #5660).
- Repo checks the parent project ran: `ruff` on the twin and the test.
- A real-data run of the twin on this account's live `list_sessions` page: the chain sessions in coding-workflows are still eligible, a `desktop_app` session is `unknown_origin`.

## Risks

- A legitimate session with a missing `origin` or `parent_session_id` is no longer resumed. It stays with the manual fallback the pickup already documents for older sessions; the skip reason names why.
- A consumer added to the registry after the pickup's checkout was refreshed is unknown until the pickup's next `git fetch` (step 0 refreshes the checkout every wake).
- Merge overlap with #6102 in the same function; resolved on whichever merges second.

## Rollout

Ships with the parent project #5660 (base branch `claude/implement-plan-issue-5660-resume-usage-limit-stops`, final PR #5678). No flag: the stricter rule is on as soon as the pickup's checkout carries it.

## Auto-decisions

- AD-1 [plan, 2026-10-02] How are resumes authorized? — Picked: A — every GitHub source in coding-workflows or the consumer registry, `origin` = `claude_code_mcp_seed`, and a `parent_session_id`; fail closed. Alternatives: B — also require the parent chain to reach the pickup session; C — repository check only. Why: server-set fields, no API call; B would stop resuming chains rooted at an operator session or a previous pickup (this session's own chain roots at a `desktop_app` session) and ancestors older than the 72-hour listing; C still resumes a person's own Auto-mode session in the same repository. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] Where does the allowed repository set come from? — Picked: A — optional `--registry` (default `.github/ai/consumer_repos.json`) plus `--self-repo` (default `shubhodeep1/coding-workflows`); an unreadable registry narrows to the self repo and adds an `errors` line. Alternatives: B — new required arguments; C — only the pickup's own `get_session` sources. Why: the pickup's command line keeps working, so no no-twin command change is needed for behaviour; C would drop the consumer-repo sessions the pickup starts; failing narrow keeps §1. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Must the session's `environment_id` match the pickup's? — Picked: A — no. Alternatives: B — yes. Why: §5; repository, origin, and lineage close the finding, and a chain started from another environment is a registered workflow too. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-02] Does `.claude/commands/claude-issue-pickup.md` change? — Picked: A — one doc line in step 1a.2 (`skipped` lists the new reasons), as a diff in the twin-sync blocker. Alternatives: B — leave it. Why: §7 keeps the operator text accurate; behaviour needs no command change. Applied in: twin-sync blocker. Status: pending review
- AD-5 [plan, 2026-10-02] What does `select` do when called without the allowed set? — Picked: A — `None` authorizes nothing (fail closed). Alternatives: B — self repo only; C — no check. Why: a caller that forgets the argument must not widen access. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `.claude/scripts/security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 6101` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
