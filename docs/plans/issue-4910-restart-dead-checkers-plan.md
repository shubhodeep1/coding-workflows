# Restart dead /implement-plan-claude checkers from the hourly Claude issue pickup

Source issue: shubhodeep1/coding-workflows#4910 (https://github.com/shubhodeep1/coding-workflows/issues/4910)
Base branch: main
Security pass: run

## Summary

A project checker keeps an `/implement-plan-claude` chain alive by re-arming
itself with `send_later`. When a re-arm fails, nothing is pending and the
project stalls. The operator-approved restart rule (Q63: A) lives only in the
prompt of the ad hoc "Master poller" session. This project moves it into the
hourly Claude issue pickup, where a script decides and the model applies the
decision, as with `stale_routines.py`. It also re-queues an issue whose
progress log names a checker that no longer exists (OWNER scope addition,
2026-09-29 02:10Z).

## Context

- Issue #4910 (OWNER), plus two OWNER comments:
  - **Condition 4 evidence (01:38Z):** the poller's title-only match missed a
    live stage of #4787. Decide by lineage and branch, treat a session created
    in the last 90 minutes as active even when idle, and test that case.
  - **Scope addition (02:10Z):** when the log of an open, non-blocked
    `ai:claude` issue names a checker that `get_session` reports as missing,
    re-queue the issue the way `/reclarify` does, at most once per 24 hours.
    Incident: #4552's checker `session_011CnkbqqMBikJa8LZhcPhzr` is gone and
    the project sat at `final-merge` from 2026-09-27.
- The restart rule, exact (issue body, and `docs/operations/master-session.md`
  "Dead-checker restart rule (Q63, as tightened 2026-09-29)"). A project
  checker is a non-archived session whose title contains
  `implement-plan <slug> — checker`, optionally after a `#<issue> · PR #<pr> — `
  prefix (#4886). Restart it only when ALL of these hold:
  1. no enabled trigger has `persistent_session_id` equal to the checker;
  2. for an `issue-<N>-…` slug, issue #N is open and not `ai:claude-blocked`;
  3. the checker is not `need_input` and not waiting on a permission prompt;
  4. no other non-archived session of the project is `RUNNING`,
     `REQUIRES_ACTION` or `need_input`, or was created or updated in the last
     90 minutes;
  5. the checker was not restarted in the last 3 hours, by a marker the
     script reads.
- Restart action: a one-shot `create_trigger` into the checker two minutes
  out, named `implement-plan <slug>: check-in`, with the prompt given in the
  issue. The stale Routine sweep (CLAUDE.md §26.G) already matches that name.
- Budget (issue): one `list_sessions` page and one `list_triggers` page per
  wake, plus one issue read per distinct candidate project. REST only (§15).
- Facts read on 2026-09-29 from this account:
  - `list_sessions` (`mine: true`, `limit: 100`) is ordered by `created_at`,
    newest first, and one page covered about 15 hours. Records carry `id`,
    `title`, `session_status`, `status_bucket`, `created_at`, `updated_at`,
    `parent_session_id`, `tags`, `session_context.sources[].git_repository.url`,
    `post_turn_summary.status_category` (top level or under
    `external_metadata`) and `external_metadata.current_branches` (a
    `{"": "<branch>"}` map). The harness saves a large result to a file inside
    an untrusted-data envelope around `{"ccr": {"data": [...], "has_more": …}}`.
  - `list_triggers` (`enabled: true`, `limit: 100`) returned 46 triggers in a
    `{"data": [...], "has_more": false}` object. Names are cut at 60
    characters. Every `implement-plan <slug>: safety net` prompt reads
    `Safety net for /implement-plan-claude <plan path>: … get_session <checker id>; …`.
  - Project logs lag: many name `Check-in: none` while a checker runs, and
    #4701's log names the checker that died.
  - Checkers live for the whole project, so most are older than one page.
- The pickup (`.claude/commands/claude-issue-pickup.md`) runs hourly, holds
  `create_trigger`, `list_triggers` and `get_session`, and runs
  `scripts/claude_issue_route.py` from `scripts/`. It has no
  `workflow-templates/` twin.
- `/reclarify` is a trusted comment starting with `/reclarify`
  (`clarify.yml` `issue_comment` gate). It routes an `ai:claude` issue back
  through the intake to the queue. `/implement-issue-claude` step 4 resumes a
  project whose checker is missing, and the chain arms a new checker.
- Interim twin-first rule (#4750 Q40: A, restated in this issue as
  "Protected paths: `.claude/**`, so use the interim twin-first rule"):
  - edit `.claude/**` only through its `workflow-templates/.claude/**` twin;
  - push the phase PR, post a `hold` claim on its head, and stop BLOCKED with
    an `ai:claude-blocked:v1` comment that lists every file to copy;
  - `claude-issue-pickup.md` has no twin, so its exact diff goes in that
    comment (the #4817 and #4887 precedent).
- Related work in flight, all separate from this project:
  - #4887 (PR #4937) adds pickup step 3a, a session sweep with its own
    `list_sessions` cursor page;
  - #4817 (PR #4846) adds pickup step 3.4;
  - #4886 adds the `#<issue> · PR #<pr> — ` title prefix.

## Goals

- `scripts/claude_checker_restart.py` [new], two subcommands, one JSON line
  each:
  - `scan --sessions-file S --triggers-file T --repo <owner>/<repo>
    --self <pickup id> --state-out F`:
    - reads the saved newest `list_sessions` page and the `list_triggers`
      page;
    - lists open `ai:claude` issues of `--repo` (one REST call);
    - reads the progress logs of their project branches through git (one
      `git ls-remote`, one shallow `git fetch`, no REST);
    - writes its state to `F` and prints
      `{"lookup": [ids], "candidates": n, "state": F, "errors": [...]}`.
  - `decide --state F [--lookup-file L]`:
    - applies the five conditions and the re-queue rule;
    - prints `{"restart": [...], "requeue": [...], "skipped": [...],
      "errors": [...]}`.
- Checkers are found three ways:
  - checker titles on the page;
  - the checker id each enabled `implement-plan <slug>: safety net` prompt
    names;
  - the checker id the progress log of an open, non-blocked `ai:claude`
    issue of `--repo` names.
  A checker that is not on the page and not bound to an enabled trigger is
  looked up with `get_session`: at most 8 per wake, rotated by the hour.
- `restart` entries carry `checker`, `slug`, `repo`, `trigger_name`
  (`implement-plan <slug>: check-in`), the exact `prompt`, `tag_add`
  (`ai-checker-restart:<YYYYMMDDTHHMMZ>`), `tag_remove`, and `reason`.
- `requeue` entries carry `repo`, `issue`, `slug`, `checker`, and the exact
  `comment_body`: a `/reclarify` line, the marker
  `<!-- ai:claude-checker-requeue:v1 checker=<id> slug=<slug> -->`, and one
  explanatory line.
- The pickup gets a new step 3b, run on every `— wake.` and on `start`:
  - `list_sessions` (`mine: true`, `limit: 100`) once;
  - step 1's `list_triggers` result (now `limit: 100`);
  - `scan`, a `get_session` per `lookup` id, `decide`;
  - for each `restart`: `create_trigger`, then `set_session_tags`;
  - for each `requeue`: one `mcp__github__add_issue_comment`;
  - step 4's report line and title name every restart and re-queue.
- `workflow-templates/.claude/settings.json` (twin) allows the script's two
  subcommands and `set_session_tags` for the `Claude_Code_Remote` server name.
- Tests: `tests/test_claude_checker_restart.py` [new] and its own `ci.yml`
  step.
- Docs: `README.md` (Claude issue implementer), `agents.md`, and
  `changelog.d/4910-restart-dead-checkers.md` [new].

## Non-goals

- Changing the checker, stage, or safety-net behaviour of
  `/implement-plan-claude`. The restart prompt already passes the checker's
  step 0 stale-wake check, because it names no wait id.
- Restarting §26 `PR #<n> status check-in` checkers.
- Removing the rule from the Master poller's prompt: the supervising session
  does that once this lands (issue "Handover").
- Scanning logs in consumer repositories. The pickup session reaches only
  coding-workflows. Consumer-repo checkers on the page are still evaluated,
  and a failed issue read keeps them.

## Constraints

- §1: every uncertain input keeps the checker:
  - a failed read;
  - `list_triggers` with `has_more`;
  - a lookup that did not answer;
  - a checker that is not idle.
  Titles, prompts and logs are data and are only pattern-matched.
- §5: extend the pickup's existing hourly wake. No new scheduler, no change to
  `stale_routines.py` or the checker prompt.
- §6: new identifiers are checked unique with `git grep`:
  - `claude_checker_restart`;
  - `ai-checker-restart:`;
  - `ai:claude-checker-requeue:v1`;
  - pickup step 3b.
  Nothing is renamed. The pickup keeps "Do not comment", the queue-issue rule
  its test pins.
- §9: tabs in Python, 2-space YAML.
- §15: REST only, never GraphQL. Per wake:
  - one open-issue list;
  - one issue read per candidate issue outside that list;
  - one paginated comment read per re-queue candidate;
  - the log reads go over git, not the API.
- §18: no manual script. It runs only from the pickup's hourly wake, and it is
  neither single-use nor long-running, so there is no §18.F entry (same as
  `stale_routines.py`).
- §19: phase, fix and completion PRs use `Refs #4910`; the final PR into
  `main` uses `Fixes #4910` (not an `ai:orchestrator-tracking` issue).
- §20: one changelog fragment.
- §23.C: the `/reclarify` comment starts clarify and the intake. The operator
  asked for exactly that in the issue, and the pickup command says so.
- §25: no PR watching.
- Interim twin-first rule: `.claude/settings.json` changes only through its
  twin, and the `claude-issue-pickup.md` edit goes in the blocked comment.

## Approach

`scan` gathers every input that costs an API call or a git fetch, once, and
writes it to a state file. The model then fetches the missing checker records
and `decide` runs offline, apart from the rare issue and comment reads. The
page is the newest one, because condition 4 needs the newest sessions: every
session created in the last 90 minutes is on it. Older checkers come from the
two id sources above. Those are exactly the dead-during-a-wait case (the stage
still has its 24-hour safety net) and the logged-checker case. The restart
marker is a tag on the checker session itself. It survives the stale Routine
sweep, needs no extra read, and dies with the checker.

## Phases & Merge Strategy

Single phase: issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Restart script, pickup step 3b, settings twin, docs.** Files: see Files & Modules.
   - Done when:
     - the new tests pass;
     - `ruff check` is clean on the new Python;
     - the existing pickup and settings contract tests pass on the synced
       copy. The pickup-text and allow-rule assertions fail until the twin
       sync, as the interim rule expects.
   - Rollback: revert the PR. Restart tags are inert, and a re-queued issue
     simply resumes.

## Implementation Steps

1. `scripts/claude_checker_restart.py` [new]:
   - loaders for the session page (envelope, `{ccr:{data}}`, `{data}`, bare
     array), the trigger page, and the lookup file;
   - the title, slug and branch parsers;
   - `scan`, `decide`, and the CLI.
2. `tests/test_claude_checker_restart.py` [new], and a `ci.yml` step
   "Dead-checker restart tests (Claude issue pickup, issue #4910)".
3. `workflow-templates/.claude/settings.json`: four allow rules plus
   `mcp__Claude_Code_Remote__set_session_tags`.
4. The `claude-issue-pickup.md` edit, written as an exact diff for the blocked
   comment:
   - step 0 tools;
   - step 1 `limit: 100`;
   - step 2's exits into 3b;
   - new step 3b;
   - step 4 report;
   - Rules and Tool Access.
5. `README.md`, `agents.md`, and the changelog fragment.

## Files & Modules

- `scripts/claude_checker_restart.py` [new]
- `tests/test_claude_checker_restart.py` [new]
- `.github/workflows/ci.yml`
- `workflow-templates/.claude/settings.json` (→ `.claude/settings.json` at sync)
- `.claude/commands/claude-issue-pickup.md` (no twin; applied at sync from the blocked comment)
- `README.md`, `agents.md`
- `changelog.d/4910-restart-dead-checkers.md` [new]
- `docs/plans/issue-4910-restart-dead-checkers-plan.md` [new], `docs/implement-plan/issue-4910-restart-dead-checkers.md` [new]

## Tests

Unit tests stub `gh_api` and the git reader:
- each of the five conditions blocks a restart on its own, and all five
  together restart;
- the #4787 evidence case: the checker has no trigger, and a child stage with
  an unmatched title was created 30 minutes ago → no restart;
- lineage, branch, title and issue-start membership, and the 90-minute
  created and updated windows;
- `RUNNING`, `REQUIRES_ACTION` and `need_input` project sessions block;
  archived ones do not;
- a restart tag younger than 3 hours blocks, an older one does not, and old
  tags are listed for removal;
- a zombie checker whose sibling checker has a trigger is skipped;
- `list_triggers` with `has_more` restarts nothing;
- safety-net and log ids produce lookups, capped and rotated. A lookup that is
  missing from the file keeps the checker;
- a `not_found` lookup for a logged checker re-queues once:
  - a trusted `/reclarify` in the last 24 hours blocks it;
  - a blocked or closed issue blocks it;
  - an active project session blocks it;
  - a `BLOCKED` or finished log blocks it;
- the prefix title forms, the envelope and other input shapes, exit codes,
  and budgets (one open-issue list; each issue and comment list read once);
- CI wiring. A pickup-text test is left to the sync (see Rollout).

## Risks & Mitigations

- **A stage session older than the page is still active, so the restart
  starts a duplicate stage.** Mitigations:
  - the newest page holds every session created in the last 90 minutes;
  - a checker that hands off always creates its next stage in that same turn;
  - issue projects stop on `ai:claude-blocked`.
  ACCEPTED for a stage older than about 15 hours that is still running.
- **A dead checker with no pending safety net, no log entry, and older than
  the page is not found.** ACCEPTED: the Master poller keeps its rule until
  this lands, and the stage's safety net catches the rest.
- **The `/reclarify` comment starts workflows.** That is the intended
  re-queue. It is limited to once per 24 hours per issue by any trusted
  `/reclarify`.
- **`set_session_tags` fails after the trigger was created.** The checker is
  alive again, so condition 1 blocks a second restart until it idles. Report
  the failure.

## Rollout

The change ships on the final PR's merge. The pickup reads its command file
fresh on each wake (step 0 "Fresh code"), so the next wake after the sync
merges runs step 3b. The settings twin reaches consumer repos on the next
`@stable` sync (§14). There the allow rules name a coding-workflows-only
script and are harmless. After it lands, the supervising session removes Q63
from the Master poller's prompt.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does the decision script live? — Picked: A — a new `scripts/claude_checker_restart.py`, coding-workflows-only like `scripts/claude_issue_route.py`. Alternatives: B — `.claude/scripts/` plus a twin (a protected path, and it would ship to consumers that have no pickup); C — a `claude_issue_route.py` subcommand (routing code that #4817 is editing). Why: no protected path for the script, and its tests run before the sync. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Which `list_sessions` page does the pickup read? — Picked: A — the newest page every wake. Alternatives: B — a cursor walk over older pages (condition 4 would miss the newest sessions); C — several pages (breaks the budget). Why: condition 4 needs every session created in the last 90 minutes, and only the newest page holds them. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How are checkers older than the page found? — Picked: A — the checker id named by each enabled `implement-plan <slug>: safety net` prompt, and the id the progress log of an open, non-blocked `ai:claude` issue names, each looked up with `get_session` (at most 8 per wake, rotated by the hour). Alternatives: B — page titles only (misses the #4701 case); C — a cursor over older pages (see AD-2). Why: a checker that died in a wait leaves its stage's safety net pending, and the log path is needed for the missing-checker addition anyway. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] What is condition 5's marker? — Picked: A — a session tag `ai-checker-restart:<YYYYMMDDTHHMMZ>` set on the checker with `set_session_tags`, read from its session record. Alternatives: B — the fired restart trigger (the stale Routine sweep deletes ended `implement-plan <slug>: …` Routines); C — an issue comment (issue projects only, and it starts workflows). Why: durable, free to read, and scoped to the checker. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Which checker states can be restarted (condition 3)? — Picked: A — only `SESSION_STATUS_IDLE`, with `status_category` not `need_input` and bucket not `BLOCKED`. Alternatives: B — also a `RUNNING` checker (it is already awake). Why: §1, the smallest set that satisfies the rule. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Which sessions belong to a project (condition 4)? — Picked: A — the operator's lineage, branch and title rules, plus the issue-start titles (`Issue #<N> — implement`, `issue <repo>#<N> — implement`, `implement-issue-claude — #<N>`) of an issue project. Alternatives: B — the operator's rules only. Why: an extra match can only prevent a restart, and the issue-start session is the project's first stage. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] What if another checker of the same slug is alive? — Picked: A — skip the dead one (`sibling_checker_alive`). Alternatives: B — restart both. Why: the dead one is a zombie the next stage archives. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] What if the `list_triggers` page has more? — Picked: A — restart nothing that wake. Alternatives: B — proceed on the partial page. Why: condition 1 cannot be proven. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-09-29] How is an issue re-queued? — Picked: A — the pickup posts a `/reclarify` comment with a hidden `ai:claude-checker-requeue:v1` marker through `mcp__github__add_issue_comment`. Alternatives: B — dispatch `claude-issue-intake.yml` (not one of the dispatch helper's six workflows, so it needs protected changes); C — write a queue item directly (breaks the #4621 binding). Why: "the way `/reclarify` does" in the operator's words. Applied in: phase 1. Status: pending review
- AD-10 [plan, 2026-09-29] What does "at most once per 24 hours" count? — Picked: A — any trusted `/reclarify` comment on the issue in the last 24 hours. Alternatives: B — only the pickup's own marker comments. Why: a human `/reclarify` already resumed it. Applied in: phase 1. Status: pending review
- AD-11 [plan, 2026-09-29] Which projects does the missing-checker scan cover? — Picked: A — open `ai:claude` issues of the pickup's repository without `ai:claude-blocked`, whose log names a checker and is neither `BLOCKED` nor finished (`Activation:` LIVE, n/a, or deploy-activate started), and with no active project session on the page. Alternatives: B — also consumer repositories (the pickup session cannot read them). Why: the operator's rule, applied only where it can be read. Applied in: phase 1. Status: pending review
- AD-12 [plan, 2026-09-29] How are the progress logs read? — Picked: A — one `git ls-remote` and one shallow `git fetch` of the matching project branches, then `git show`. Alternatives: B — one REST contents read per issue. Why: no API calls (§15). Applied in: phase 1. Status: pending review
- AD-13 [plan, 2026-09-29] Where does the step run? — Picked: A — a new step 3b after step 3 on every `— wake.` and `start`, also reached when the queue is empty or its read failed. Alternatives: B — only when the queue read succeeds. Why: a queue outage must not also stop restarts. Applied in: phase 1. Status: pending review
- AD-14 [plan, 2026-09-29] How does phase 1 edit `.claude/**`? — Picked: A — twin-first per Q40, as the issue says: the settings twin, the pickup diff in the blocked comment, a hold claim, and a stop at BLOCKED. Alternatives: B — edit `.claude/**` directly (Auto mode's classifier may drop the edits silently). Why: the operator's standing rule. Applied in: phase 1. Status: pending review

## Notes

- #4887's step 3a reads a `list_sessions` cursor page. When both land, the
  pickup reads two pages per wake, one for each feature. Each stays within its
  own issue's budget. If they are later merged into one read, this step needs
  the newest page.

## References

- #4910, #4887, #4886, #4817, #4787, #4785, #4750 (Q40), #4701, #4552
- `.claude/scripts/stale_routines.py`, `docs/operations/master-session.md` (Q63)
