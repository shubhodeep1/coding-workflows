# Move the pipeline's checking roles to Claude

## Summary

Run the pipeline's checking roles (security audit, runtime validation,
check-failure triage, and workflow-heal diagnosis) in fresh Claude Code
sessions (Opus 5.5) instead of GPT, for every repo and both pipelines,
reached through the existing Claude queue and hourly pickup, with a per-phase
repo variable to switch a phase back to GPT.

## Context

Claude already does the *doing* in the Claude pipeline (plan, implement,
fix, conformance), but every *checking* role still runs on `gpt-6-sol` in
GitHub Actions, including for Claude projects:

| Role | Where the model is called today |
|---|---|
| Security audit | `scripts/security_audit.sh:1160-1178` (one `codex exec`, raw findings JSON), called by `security-audit.yml` (weekly `0 8 * * 0`, dispatch, `workflow_call`) and synchronously by the orchestrator poller: `run_security_pass_inline` (defined at `scripts/orchestrate_poll_process.sh:6637`, called at `:7142`) runs `security_audit.sh` at `:6935-6938` |
| Runtime validation | `scripts/validate_process.sh` `run_validate_codex_attempt` (`:2955-2999`): discover (`:3191`) and diagnose (`:3948`); self-heal in `scripts/self_heal_validation.sh`; Docker harness runs on the Actions runner |
| Check-failure triage | `scripts/check_failure_triage.sh:322-337` (`codex exec` → `diagnosis.md`) |
| Workflow-heal diagnosis | `scripts/workflow_failure_heal_intake.sh:670-687` (`codex exec` → `diagnosis.md` with `## Classification`) |

Each call site writes one output file, and everything after it (normalising,
filters, waivers, dedupe, lineage caps, routing, labels, issues, Telegram)
depends only on that file's content. That makes a clean split point.

Actions cannot start Claude sessions. The only route is the
`ai:claude-issue-queue` in coding-workflows plus the hourly Claude issue
pickup session (`.claude/commands/claude-issue-pickup.md`), which already
serves two payload kinds (`claude_issue.v1`, `claude_pr_fix.v1`) through
`scripts/claude_issue_route.py`. A cloud session can download Actions
artifacts through the agent proxy and can run Docker once `dockerd` is
started (both verified 2026-09-27).

This plan follows `docs/plans/claude-release-track-plan.md` (Q17): its Claude-side
pieces (the new command, pickup/dispatch changes) ship on the Claude track;
its Actions-side pieces ship on the workflow track (Q29).

Decisions taken in the clarification round (2026-09-27):

| Q | Decision |
|---|---|
| Q1/Q8 | Move security, validation, triage, heal diagnosis; the reviewer panel is unchanged and out of scope |
| Q2/Q9 | Checks run in Claude Code sessions reached through the queue: Actions queues a `claude-check` item, the pickup starts a session, the session posts a machine-readable result, Actions resumes |
| Q3 | Everything, in every repo (Codex and Claude pipelines); per-phase repo vars; default Claude; `gpt` switches back |
| Q5/Q14 | No automatic GPT fallback. A per-phase deadline (default 6 h) fails the check closed with the phase's existing failure path plus a Telegram ERROR |
| Q6 | Opus 5.5 everywhere, high effort |
| Q10 | Always a fresh session that never saw the implementation |
| Q11 | Scheduled, manual and project-level security audits all move |
| Q12/Q26/Q33 | The validation session runs everything (dockerd, discover, harness, diagnose), with side effects off; Actions applies labels / fix-up issues / Telegram. Prompt self-heal is off in Claude mode; the session repairs the generated harness in-run, capped by `MAX_SELF_HEAL_ATTEMPTS` |
| Q13 | Triage skips `claude/*` PR heads (`/fix-claude-pr` owns them) |
| Q15 | Four ordered phases: shared plumbing, then security, validation, triage/heal |
| Q16 | ACCEPTED risk: consumer session network policy may block image / package pulls; preflight fails closed with a clear reason |
| Q25 | Input by artifact; result as a marked comment on the subject; trusted only from OWNER/MEMBER/COLLABORATOR; bound to request id and head SHA; downstream tail runs in Actions |

## Automation surface (CLAUDE.md §18.E)

| Item | This plan |
|---|---|
| New or extended scripts | New: `scripts/claude_check.py`, `scripts/llm_external_seam.sh` (P1), both called only from workflows and phase scripts. Extended: `scripts/claude_issue_route.py`, `scripts/claude_issue_intake.sh`, `scripts/claude_issue_queue_watchdog.sh` (P1); `scripts/security_audit.sh`, `scripts/orchestrate_poll_process.sh` (P2); `scripts/validate_process.sh` (P3); `scripts/check_failure_triage.sh`, `scripts/workflow_failure_heal_intake.sh` (P4). No script needs a manual run (§18.A). |
| Scheduler / PR-push entry points | Requests: each phase's existing triggers. `security-audit.yml` (cron `0 8 * * 0`, dispatch, `workflow_call`), the orchestrator poll (`orchestrate_poll.yml`, called by the `ai-orchestrate-poll.yml` cron wrapper), `validate.yml` (`workflow_call` from its existing callers), `check_failure_triage.yml` (`workflow_call` from the `check_run: completed` wrappers), `workflow-failure-heal-intake.yml` (`repository_dispatch`, `workflow_run`). Queue: `claude-issue-intake.yml` (`repository_dispatch`, new type `claude-check`) and the existing hourly Claude issue pickup session. Resume: new `claude_check_resume.yml`, called by `workflow-templates/ai-claude-check-resume.yml` (consumers) and `.github/workflows/internal-claude-check-resume.yml` (this repo), both on `issue_comment: created`, the internal one also on `workflow_dispatch`. Deadlines: the existing `claude-issue-queue-watchdog.yml` (cron `17 * * * *`). |
| Long-running supervisor (§18.C) | None new. The existing Claude issue pickup (a `supervisor` in the registry) gains the `check` item type, and the existing watchdog gains deadline handling. The resume workflows are event-driven: each run verifies one result comment, resumes one phase run, and exits. A lost resume run is recovered by a `workflow_dispatch` replay with `comment_id` or, if none happens, by the watchdog's deadline, which fails the check closed. |
| DB gate (§18.D) | Not applicable: no database operation. |
| §18.F registry | One new entry in `docs/scripts-pending-removal.md` (P1), shown below in that file's per-entry block format, plus one added preflight check on the existing Claude issue pickup entry. |

The P1 PR adds this entry to `docs/scripts-pending-removal.md`, filling in its
PR number and merge date:

```markdown
### `.github/workflows/claude_check_resume.yml` + `.github/workflows/internal-claude-check-resume.yml` + `workflow-templates/ai-claude-check-resume.yml` + `scripts/claude_check.py` + `scripts/llm_external_seam.sh`

- **Introduced in:** #<P1 PR> (<merge date>)
- **Type:** long-running
- **Removal trigger:** permanent — review annually (or sooner, once every phase is back on `gpt` in every repo)
- **Removal preflight checks:**
  - For this repo and every repo in `.github/ai/consumer_repos.json`, `gh variable list --repo <repo>` shows `SECURITY_PASS_ENGINE`, `VALIDATION_ENGINE`, `CHECK_TRIAGE_ENGINE` and `WORKFLOW_HEAL_ENGINE` all explicitly `gpt`.
  - `gh api "repos/shubhodeep1/coding-workflows/issues?labels=ai:claude-issue-queue&state=open&per_page=100"` returns no issue whose title starts with `[claude-issue-queue] check `.
  - `rg -n 'llm_seam_run' scripts/` returns only the seam's own definition, once the call sites are removed in the same PR.
  - `PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_workflow_script_refs.py` returns `All workflow script references resolve to existing files.`
- **Owner:** @shubhodeep1
```

The same PR adds one preflight check to the existing
`.claude/commands/claude-issue-pickup.md` entry, because removing the pickup
would strand pending checks:

```markdown
  - `gh api "repos/shubhodeep1/coding-workflows/issues?labels=ai:claude-issue-queue&state=open&per_page=100"` returns no issue whose title starts with `[claude-issue-queue] check ` (no pending `claude_check.v1` item).
```

## Goals

- G1. With default settings, no `codex exec` / OpenCode call is made for the
  four roles: each queues a `claude_check.v1` item and resumes from a Claude
  result.
- G2. Setting `SECURITY_PASS_ENGINE`, `VALIDATION_ENGINE`,
  `CHECK_TRIAGE_ENGINE` or `WORKFLOW_HEAL_ENGINE` to `gpt` restores today's
  behaviour for that phase exactly (same code path, same logs).
- G3. The prompt a Claude session answers is byte-identical to the prompt
  GPT would have received for the same run; the downstream tail is unchanged.
- G4. A check with no trusted result after its deadline fails closed through
  the phase's existing failure path and sends one Telegram ERROR; it never
  passes silently.
- G5. A result is accepted only when its marker names the open request id,
  the subject's current head SHA (where the phase has one), and a trusted
  author association.
- G6. Check-failure triage never runs for a `claude/*` PR head.
- G7. `/implement-plan-claude` steps 9 and 10 read the resumed run's verdict,
  never the queue-only run's, so a queued audit is never mistaken for a clean
  pass.

## Non-goals

- The review panel, consolidator, summariser, GPT editor, conflict resolver
  (Q8).
- Clarify, plan, implement, orchestrate/judge, workflow-log analysis, the
  weekly retro.
- Removing the GPT paths: they stay, selectable per phase (G2, §6).
- Faster pickup than hourly; pickup liveness beyond today's watchdog.
- Consumer environment network policy changes (Q16).

## Constraints

- **§6.** Nothing renamed or removed. New identifiers (repo-wide grep on
  2026-09-27, zero hits): payload `claude_check.v1`; queue title prefix
  `[claude-issue-queue] check `; label `ai:claude-check-dispatched`;
  markers `<!-- ai:claude-check-request:v1 … -->`,
  `<!-- ai:claude-check-result:v1 … -->`; repository_dispatch event
  `claude-check`; repo vars `SECURITY_PASS_ENGINE`, `VALIDATION_ENGINE`,
  `CHECK_TRIAGE_ENGINE`, `WORKFLOW_HEAL_ENGINE` (values `claude` | `gpt`,
  default `claude`) and `SECURITY_PASS_CLAUDE_DEADLINE_HOURS`,
  `VALIDATION_CLAUDE_DEADLINE_HOURS`, `CHECK_TRIAGE_CLAUDE_DEADLINE_HOURS`,
  `WORKFLOW_HEAL_CLAUDE_DEADLINE_HOURS` (default `6`); env vars
  `CLAUDE_CHECK_BUNDLE_DIR`, `CLAUDE_CHECK_RESULT_FILE`,
  `VALIDATE_LLM_ENGINE`, `VALIDATE_SIDE_EFFECTS`; engine value
  `claude-session`; shell function `llm_seam_run`; `claude_check.py`
  subcommands `payload`, `marker`, `find-result`, `engine`;
  `claude_issue_route.py` subcommand `check-queue-issue` and `item_type`
  value `check`; `validate_process.sh` flag `--apply-result`; artifact name
  `claude-check-<request id>`; result reasons `unsupported_kind`,
  `network_policy`; log keys `prompt_drift`, `resume=skipped
  reason=unknown_kind`; files
  `scripts/claude_check.py`, `scripts/llm_external_seam.sh`,
  `.github/actions/claude-check-request/action.yml`,
  `.github/workflows/claude_check_resume.yml`,
  `workflow-templates/ai-claude-check-resume.yml`,
  `.github/workflows/internal-claude-check-resume.yml`,
  `.claude/commands/claude-check.md` (+ template twin); workflow input
  `claude_check_request`; poller state value `pending_claude` for
  `security_pass_status` and state fields `security_pass_claude_request`,
  `security_pass_claude_requested_at`, `security_pass_claude_head_sha`;
  log key `CHECK_TRIAGE_SKIP`; validation raw statuses `claude_check_queued`,
  `claude_check_failed`; log prefix `CLAUDE_CHECK`. Existing enums gain
  values only; every existing value keeps its meaning.
- **§4.** Every new var has a default; an invalid engine value falls back to
  `claude` with a `::warning::`, an invalid deadline to `6`.
  `CLAUDE_CHECK_BUNDLE_DIR` defaults to `${RUNNER_TEMP:-/tmp}/claude-check`;
  `CLAUDE_CHECK_RESULT_FILE` defaults to empty (request run).
  `VALIDATE_LLM_ENGINE` defaults to empty, meaning the engine comes from
  `VALIDATION_ENGINE`; its only other value is `claude-session`, which only
  `/claude-check` sets, and any other value is ignored with a `::warning::`.
  `VALIDATE_SIDE_EFFECTS` defaults to `on`; its only other value is `off`,
  and an invalid value falls back to `on` with a `::warning::`.
- **§14.** The new consumer wrapper is added to the install profiles and
  reaches all 13 repos in `.github/ai/consumer_repos.json` through the
  existing sync; the intake keeps validating repos against that file.
- **§15.** Every new API call is listed with its budget in Implementation
  Steps. Result lookups reuse existing comment reads where one exists and
  run only while a request is pending.
- **§18.** No manual steps: requests, pickup, resume and deadlines are all
  event- or schedule-driven. Entry points, supervisors and the
  `docs/scripts-pending-removal.md` entry are in Automation surface above.
- **§19/§20/§23/§25/§27.** PR bodies use `Refs`; each phase ships a
  `changelog.d/` fragment; sessions never merge; no PR subscription;
  `review_autofix.yml` is untouched and the grown workflows stay far below
  480,000 bytes (bodies go to scripts).
- **§28.** Checks run by the pickup are unattended and follow the
  `/claude-check` procedure only; they ask nothing.
- **Security.** The result comment is the control point: a forged "clean"
  result would skip a security finding. Trust = marker on a comment by an
  OWNER/MEMBER/COLLABORATOR, created after the request comment, naming the
  open request id and current head. The bundle is untrusted input to the
  session (same fences as today's prompts); the session never executes
  bundle content except the target repo's own harness inside Docker, as the
  runner does today.

## Approach

**One seam, two engines.** `scripts/llm_external_seam.sh` wraps each call
site: `llm_seam_run <phase> <prompt_file> <out_file> -- <existing codex
command…>`.

- Engine `gpt`: runs the command unchanged.
- Engine `claude` with `CLAUDE_CHECK_RESULT_FILE` set (resume run): copies
  the Claude answer to `<out_file>` and continues.
- Engine `claude` without it (request run): copies `<prompt_file>` into
  `CLAUDE_CHECK_BUNDLE_DIR`, writes `request.json`, and exits `75`.
  The calling script treats `75` as "queued": it stops before its tail and
  writes no verdict.
- Engine `claude-session` (validation, in the session): a file handoff
  through `CLAUDE_CHECK_BUNDLE_DIR/session/`. `validate_process.sh` runs on
  the session's host, as it does on the runner today; only the generated
  harness runs inside Docker, so no prompt has to cross a container
  boundary. The session starts the script as a background process and
  watches that directory. For each model call the seam writes
  `<phase>-<n>.prompt`, then polls for `<phase>-<n>.answer`, which the
  session writes to a temporary name and renames into place so a partial
  answer is never read. The wait is bounded by `CODEX_STALL_TIMEOUT_SECONDS`
  (default `600`), the same idle bound as a codex run. On timeout the seam
  returns non-zero, and the phase takes its existing stalled-engine path.
  Otherwise it copies the answer to `<out_file>` and continues in-process.

**Lifecycle of one check.**

1. *Request* (Actions). The phase script exits `75`. The composite action
   `claude-check-request` uploads `CLAUDE_CHECK_BUNDLE_DIR` as artifact
   `claude-check-<request id>` (7-day retention), posts the request marker
   comment on the subject, and queues the item: a `claude-check`
   repository_dispatch to coding-workflows (consumers, `GH_PAT`, the same
   path as `claude_issue_handoff.sh`), or a direct queue issue (runs inside
   coding-workflows, `GITHUB_TOKEN`, the same path as `claude_pr_sweep.py`).
   Request id = `<kind>-<run id>-<run attempt>`.
2. *Pickup*. `queue-pending` returns `item_type: check`. The pickup starts
   one Opus 5.5 session in the **target** repo (high effort, two-step start)
   running `/claude-check <queue issue URL>`, then labels the queue issue
   `ai:claude-check-dispatched` and edits in the `Dispatched:` line. It
   leaves the issue open for deadline tracking.
3. *Session* (`/claude-check`). It downloads the artifact and, per kind:
   - `security`: answers the rendered audit prompt with a JSON array in the
     exact `security_audit.sh` raw schema;
   - `triage`: answers with the five-section Markdown;
   - `heal`: answers with `## Classification` first, from the closed list;
   - `validation`: checks out the target ref, starts `dockerd`, runs
     `validate_process.sh` in the background with
     `VALIDATE_LLM_ENGINE=claude-session` and `VALIDATE_SIDE_EFFECTS=off`,
     answers each discover/diagnose prompt file as it appears (the
     `claude-session` handoff above), and may repair only the generated
     `validation/` files,
     up to `MAX_SELF_HEAL_ATTEMPTS` times.

   It then posts one result comment on the subject (marker + fenced
   payload, split across numbered comments above 60,000 characters) and
   closes the queue issue as completed. On any procedure failure it posts
   `status=failed` with the reason.
4. *Resume* (Actions). The subject's `issue_comment` event runs
   `claude_check_resume.yml` (consumer wrapper `ai-claude-check-resume.yml`,
   internal wrapper here). It verifies the marker and trust, then calls the
   phase's reusable workflow with `claude_check_request=<id>`. That run
   rebuilds the context, the seam feeds it the Claude answer, and the
   existing tail runs. The orchestrator poller does not wait for this: it
   reads the result comment on its next tick.
5. *Deadline*. The queue watchdog (hourly) finds `claude_check.v1` items
   past `created_at` + the phase deadline with no result. With `GH_PAT` it
   posts a `status=deadline_expired` result (trusted, same marker), sends a
   Telegram ERROR, and closes the item. Each phase's resume and the poller
   route `failed` / `deadline_expired` into the phase's existing
   engine-failure path.

The prompt a session answers is the one GPT would have received (G3). The
resume run rebuilds it and records `CLAUDE_CHECK prompt_drift=<true|false>`,
comparing its sha256 with the request's. Drift is accepted, since the
binding is the request id plus head SHA.

Alternatives rejected: Claude via OpenRouter inside Actions (Q2 answered B);
the session doing labels and issues itself (Q25/Q33); a separate queue or
pickup (duplicates the only session-starting path and its depth limits).

## Decisions

### D1 — One seam wrapper at every call site

- **Chosen:** `llm_external_seam.sh` wraps the four existing `codex exec`
  sites; exit `75` means queued.
- **Alternatives considered:** a Claude-specific rewrite of each phase.
- **Why:** prompt building and the tail stay byte-identical across engines,
  so `gpt` remains a true switch-back (G2, G3) and every tail test still
  applies.

### D2 — Result on the subject, not on the queue issue

- **Chosen:** the session comments on the subject in the target repo
  (tracking issue, audit tracker, final PR, failing PR); heal and runs
  without a subject use the queue issue in coding-workflows.
- **Alternatives considered:** always the queue issue.
- **Why:** a session started in a consumer repo can only reach that repo
  through the proxy, and the subject's `issue_comment` event is the resume
  trigger.

### D3 — The queue issue stays open until a result exists

- **Chosen:** the pickup labels check items `ai:claude-check-dispatched`
  instead of closing them; the session or the deadline closes them.
- **Alternatives considered:** close at dispatch as for issues.
- **Why:** the watchdog needs an open record to enforce the deadline, and
  `queue-pending` already filters on the dispatched label.

## Phases & Merge Strategy

Accepted as ordered phases (Q15): P1 ships inert plumbing; P2, P3 and P4 each
need P1 and are independent of each other. Each role phase is complete on its
own (its engine var defaults to `claude` only for that role). Under
`/implement-plan-claude` the phases merge in order into one project branch
and reach `main` in one final PR. The Claude-side files in each phase ship to
consumers on the Claude track from `docs/plans/claude-release-track-plan.md`;
until a compatible Claude release exists they ship with `@stable` as today.

1. **P1 — Shared plumbing (inert).** Payload, markers, seam, request action,
   intake/queue/pickup support, `/claude-check` skeleton with the result and
   failure protocol, resume workflow and wrapper routing no kinds yet,
   watchdog deadline handling.
   - Done when: unit and contract tests pass; a `workflow_dispatch` of the
     internal resume wrapper with an unknown kind logs `CLAUDE_CHECK
     resume=skipped reason=unknown_kind`; no phase behaviour changes.
   - Rollback: revert; nothing queues `claude_check.v1` yet.
2. **P2 — Security.** `security_audit.sh`, `security-audit.yml`, the poller's
   security pass, `/implement-plan-claude` step 9, `/claude-check` kind
   `security`.
   - Done when: tests pass; with defaults, a dispatch of `security-audit.yml`
     queues, a session posts the result, and the resume run writes the
     tracker comment and follow-ups; the poller moves a project through
     `pending_claude` to `passed` or blocked.
   - Rollback: set `SECURITY_PASS_ENGINE=gpt` (instant) or revert.
3. **P3 — Validation.** `validate_process.sh`, `validate.yml`, poller
   queued-run handling, `/implement-plan-claude` step 10, `/claude-check` kind
   `validation`.
   - Done when: tests pass; with defaults, a standalone validation run queues
     (`raw_status=claude_check_queued`), the session posts the result files,
     and the resume run applies labels / fix-ups exactly as a GPT run would.
   - Rollback: `VALIDATION_ENGINE=gpt` or revert.
4. **P4 — Triage and heal.** `check_failure_triage.sh`/`.yml` (`claude/*`
   skip + engine), heal intake, `/claude-check` kinds `triage` and `heal`.
   - Done when: tests pass; a failing check on a `claude/*` PR logs a skip; on
     another PR it queues and the resume run files the `ai:check-triage`
     issue; a heal report queues and resumes into the same routing.
   - Rollback: `CHECK_TRIAGE_ENGINE=gpt` / `WORKFLOW_HEAL_ENGINE=gpt` or
     revert (the `claude/*` skip is independent and stays).

## Implementation Steps

### P1 — Shared plumbing

1. `scripts/claude_check.py` [new] (stdlib, tabs):
   - `payload build|parse`: strict key set, in the same style as
     `claude_issue_route.parse_pr_fix_text`: `repo`, `kind`
     (`security|validation|triage|heal`), `request`, `subject` (issue/PR
     number), `head` (40-hex or `none`), `run` (Actions run id holding the
     artifact), `deadline_hours`, `url`. That is 8 keys, within the 10-key
     `client_payload` cap.
   - `marker request|result`: build and parse
     `<!-- ai:claude-check-request:v1 request=<id> kind=<k> head=<sha|none> -->`
     and
     `<!-- ai:claude-check-result:v1 request=<id> kind=<k> head=<sha|none> status=<ok|failed|deadline_expired> part=<i>/<n> -->`.
   - `find-result --repo --subject --request`: one paginated
     `GET /issues/{n}/comments`. It accepts only comments whose
     `author_association` is OWNER/MEMBER/COLLABORATOR, created after the
     request comment, with a matching request, kind and head. It reassembles
     the parts and prints the payload file path and status; exit 3 = none
     yet.
   - `engine <phase>`: resolves the engine var with the defaults and
     warnings from §4.
2. `scripts/llm_external_seam.sh` [new] — as in Approach; stable exit `75`;
   logs `CLAUDE_CHECK phase=<p> engine=<e> action=<run|queued|resumed>
   prompt_sha256=<h>`.
3. `.github/actions/claude-check-request/action.yml` [new] — composite:
   upload artifact, post the request marker on the subject (1 call), queue
   the item (1 dispatch call, or 1 read + 1 create in coding-workflows, the
   same budget as `claude_pr_sweep.py`). Outputs `request_id`.
4. `scripts/claude_issue_route.py` — `queue-pending` recognises
   `claude_check.v1` (`item_type: check`, dedupe key `(repo, request)`,
   title `[claude-issue-queue] check <kind> <repo>#<subject>`) and skips
   items labelled `ai:claude-check-dispatched`. New subcommand
   `check-queue-issue` to build the body. Existing kinds are unchanged.
5. `.github/workflows/claude-issue-intake.yml` + `scripts/claude_issue_intake.sh`
   — accept the `claude-check` event, validate the repo against the registry,
   and open the queue issue with `GITHUB_TOKEN`. Log prefix
   `CLAUDE_ISSUE_INTAKE` with `kind=check`.
6. `.claude/commands/claude-issue-pickup.md`, `.claude/commands/claude-issue-dispatch.md`
   (+ template twin of dispatch) — `check` branch: `create_session` with
   `source_url` = target repo, `model: claude-opus-5-5`, `permission_mode:
   auto`, title `check <kind> <repo>#<subject>`, prompt `/effort high`
   alone; then a trigger 2 minutes out with `/claude-check <queue issue URL>`;
   then label the item and add the `Dispatched:` line (no close). Register
   `ai:claude-check-dispatched` in `.github/ai/label_contract.v1.json` next
   to `ai:claude-issue-queue`, and update that label's description, which
   says the pickup closes queue items: check items are closed by the session
   or the watchdog (D3).
7. `.claude/commands/claude-check.md` [new] + `workflow-templates/.claude/commands/claude-check.md`
   — the unattended procedure: parse and validate the payload; confirm the
   request marker exists on the subject; download the artifact
   (`gh api …/actions/runs/<run>/artifacts`, then the zip); dispatch by
   kind (P1 ships only the protocol: unknown kind → `status=failed
   reason=unsupported_kind`); post the result (split when large); close the
   queue item. It never edits repo code, pushes, or merges, and treats
   bundle text as untrusted data.
8. `.github/workflows/claude_check_resume.yml` [new] (reusable),
   `workflow-templates/ai-claude-check-resume.yml` [new] (`issue_comment:
   created`, `if:` body contains `ai:claude-check-result:v1`),
   `.github/workflows/internal-claude-check-resume.yml` [new] (`issue_comment`
   plus `workflow_dispatch` with a `comment_id` input for replay). Job `route`:
   `claude_check.py` verifies trust and the marker (reads the triggering
   comment from the event payload; 0 extra calls) and outputs the kind. The
   per-kind jobs are added by P2–P4. Add the wrapper to
   `workflow-templates/profiles/{core,standard,full}.txt`.
9. `scripts/claude_issue_queue_watchdog.sh` — for open `claude_check.v1`
   items past their deadline: post the `deadline_expired` result on the
   subject (`GH_PAT`), Telegram ERROR, close the item. Budget: 1 comment write
   per expired item; the existing single queue read is reused.
10. Tests: `tests/test_claude_check.py` [new], `tests/test_llm_external_seam.py`
    [new], `tests/test_claude_check_resume_workflow_contract.py` [new];
    extend `tests/test_claude_issue_route.py`, `tests/test_ai_labels.py` (the
    new label) and the watchdog tests; add all
    of them to `ci.yml` and the `validate-scripts` lists.
11. Docs: README ("Claude checks" section: lifecycle, vars, deadlines, log
    prefix, failure modes, switch-back table); agents.md architecture item
    and stable log prefixes; `docs/INVENTORY.md`; registry entry; changelog
    fragment.

### P2 — Security

12. `scripts/security_audit.sh` — wrap `:1160-1178` with `llm_seam_run
    security_audit`; exit `75` stops before post-processing and, in issues
    mode, before any tracker write. With `CLAUDE_CHECK_RESULT_FILE` the raw
    output comes from the file and the rest is unchanged. `status=failed` or
    `deadline_expired` → the existing "engine exited without usable result"
    path.
13. `.github/workflows/security-audit.yml` — engine from
    `vars.SECURITY_PASS_ENGINE`; new input `claude_check_request` (default
    `''`, dispatch and `workflow_call`); on exit `75` run the request action
    (subject = the tracker issue; head = the audited SHA); in resume mode,
    fetch the result with `claude_check.py find-result` and export
    `CLAUDE_CHECK_RESULT_FILE`; `run-name` carries the request id when set
    (G7). The wrapper `ai-security-audit.yml` is unchanged
    (the resume job calls the reusable directly).
14. `claude_check_resume.yml` job `security` →
    `uses: …/security-audit.yml@stable` with `ref` from the request bundle and
    `claude_check_request`.
15. `scripts/orchestrate_poll_process.sh` `run_security_pass_inline` — on
    exit `75` set `security_pass_status=pending_claude`, record
    `security_pass_claude_request`, `security_pass_claude_requested_at` and
    `security_pass_claude_head_sha`, and
    have the poll workflow's post-step run the request action (subject =
    tracking issue). On later ticks while `pending_claude`: head moved →
    abandon the request (re-audit); else `find-result` (1 paginated read per
    pending project per tick; audited: the tick has no existing
    tracking-issue comment listing to reuse). A result feeds the existing
    parse/waiver/fix-issue path. `failed`, `deadline_expired`, or now past the
    deadline → `security_pass_fail_closed "engine_unavailable" …` with
    detail `claude_<status>`. `ensure_security_pass_state_fields` accepts the
    new value and fields.
16. `.github/workflows/orchestrate_poll.yml` — post-step "Queue Claude checks"
    (the request action for each request file the tick wrote).
17. `.claude/commands/implement-plan-claude.md` (+ twin) step 9 — after
    dispatching, wait for the run whose `run-name` carries the request id
    (resume run) and read its conclusion; a queue-only run is never a pass
    (G7). `.claude/commands/claude-check.md` kind `security`.
18. Tests: `tests/test_security_audit_workflow_contract.py`,
    `tests/test_orchestrate_poll_process.py` (pending, result, drift,
    deadline), `tests/test_implement_plan_claude_command.py`,
    `tests/test_security_audit_claude_engine.py` [new].

### P3 — Validation

19. `scripts/validate_process.sh` — `run_validate_codex_attempt` goes
    through the seam (`validate_discover`, `validate_diagnose`).
    - New `VALIDATE_LLM_ENGINE` (default empty, §4): `claude-session`
      overrides the engine resolved from `VALIDATION_ENGINE` and selects the
      seam's file handoff (Approach).
    - New `VALIDATE_SIDE_EFFECTS` (default `on`, §4): `off` skips labels,
      fix-up issues, comments, Telegram and
      `dispatch_self_heal_improvements`, and still writes the result files.
    - New `--apply-result <dir>` mode reads the session's result files and
      runs only the side-effect tail.
    - Engine `claude` in Actions → write `raw_status=claude_check_queued`
      and exit `0` after requesting.
    - `attempt_self_heal_and_reexec` is disabled when the engine is not
      `gpt` (Q26).
20. `.github/workflows/validate.yml` (+ `internal-validate.yml` inputs,
    consumer wrapper unchanged) — engine from `vars.VALIDATION_ENGINE`.
    - Claude mode skips Docker bootstrap and runs the request action.
      Subject: the tracking issue, or the PR bound to `target_ref`. Bundle:
      ref, SHA, inputs, `.ai/validate.yml` hints.
    - Resume (`claude_check_request`, carried in `run-name`) runs
      `--apply-result`, so
      `collect_status`, the artifact and "Enforce validation outcome" behave
      as today.
    - `failed` / `deadline_expired` → `raw_status=claude_check_failed`.
21. Poller — `get_last_validation_run_info` consumers treat
    `raw_status=claude_check_queued` as still active (no redispatch, no
    verdict) and `claude_check_failed` like `codex_failure`; no new API calls.
22. `claude_check_resume.yml` job `validation` → `validate.yml` with the
    bundle's inputs. `.claude/commands/claude-check.md` kind `validation`:
    - start `dockerd`;
    - run the Q16 network preflight (pull the family base image and reach the
      package index); on failure post `status=failed reason=network_policy`;
    - run validate in `claude-session` mode;
    - repair only `validation/**`, capped at `MAX_SELF_HEAL_ATTEMPTS`;
    - post the status, metadata and diagnosis files.
23. `/implement-plan-claude` step 10 — read the verdict from the resume run
    (the run whose `run-name` carries the request id); `claude_check_failed`
    is terminal like `codex_failure`.
24. Tests: `tests/test_validate_process_claude_engine.py` [new] (queued,
    side-effects off, apply-result parity with a GPT fixture), poller
    queued/failed handling, `tests/test_validate_target_ref_input.py`,
    command tests.

### P4 — Triage and heal

25. `check_failure_triage.yml` + `scripts/check_failure_triage.sh`
    - Add a `claude/*` head-ref skip in `scripts/check_failure_triage.sh`
      next to the fork gate (`:174-177`), using `HEAD_REF` (`:165`) from the
      PR payload the script already fetches (`:152`; 0 new calls), logging
      `CHECK_TRIAGE_SKIP reason=claude_pr` (G6). The workflow's
      `derive_check_name_key` job (`check_failure_triage.yml:50`), which
      holds the workflow-level fork gate, is unchanged.
    - Engine from `vars.CHECK_TRIAGE_ENGINE`; seam at `:322-337`; request
      subject = the PR; head = `head_sha`; new input `claude_check_request`.
    - The resume job passes the original six inputs from the bundle.
26. `workflow-failure-heal-intake.yml` + `scripts/workflow_failure_heal_intake.sh`
    - Engine from `vars.WORKFLOW_HEAL_ENGINE`; seam at `:670-687`.
    - Requests open the queue issue directly (subject = that queue issue,
      head = `none`).
    - The intake gains a `claude_check_request` dispatch input and re-runs
      from the stored payload. Its global concurrency group stays in force.
27. `claude_check_resume.yml` jobs `triage` (reusable call) and `heal` (in
    coding-workflows: `gh workflow run workflow-failure-heal-intake.yml` with
    the request id, 1 call). `/claude-check` kinds `triage` and `heal` (answer
    format checked against `prompts/mode-check-failure-triage.txt` and
    `workflow_failure_heal.py` `CLASSIFICATIONS` before posting).
28. Tests: `tests/test_check_failure_triage_workflow_security.py` (claude skip,
    engine), `tests/test_check_failure_triage_claude_engine.py` [new],
    `tests/test_workflow_failure_heal.py` (engine, resume, deadline →
    Telegram only, no issue).
29. Docs for P2–P4: README phase sections and vars tables, agents.md phase
    items 11/13/14, CLAUDE.md untouched; one changelog fragment per phase.

## Files & Modules

- New: `scripts/claude_check.py`, `scripts/llm_external_seam.sh`,
  `.github/actions/claude-check-request/action.yml`,
  `.github/workflows/claude_check_resume.yml`,
  `.github/workflows/internal-claude-check-resume.yml`,
  `workflow-templates/ai-claude-check-resume.yml`,
  `.claude/commands/claude-check.md`, `workflow-templates/.claude/commands/claude-check.md`,
  tests listed above, `changelog.d/*`.
- Edited: `scripts/claude_issue_route.py`, `scripts/claude_issue_intake.sh`,
  `scripts/claude_issue_queue_watchdog.sh`, `.github/workflows/claude-issue-intake.yml`,
  `.github/ai/label_contract.v1.json`,
  `.claude/commands/claude-issue-pickup.md`, `.claude/commands/claude-issue-dispatch.md`
  (+ twin), `.claude/commands/implement-plan-claude.md` (+ twin),
  `scripts/security_audit.sh`, `.github/workflows/security-audit.yml`,
  `scripts/orchestrate_poll_process.sh`, `.github/workflows/orchestrate_poll.yml`,
  `scripts/validate_process.sh`, `.github/workflows/validate.yml`,
  `.github/workflows/internal-validate.yml`, `scripts/check_failure_triage.sh`,
  `.github/workflows/check_failure_triage.yml`,
  `scripts/workflow_failure_heal_intake.sh`, `.github/workflows/workflow-failure-heal-intake.yml`,
  `workflow-templates/profiles/*.txt`, `.github/workflows/ci.yml`,
  `.github/workflows/test-and-mark-stable.yml` / `mark-stable.yml` (test lists),
  `README.md`, `agents.md`, `docs/INVENTORY.md`, `docs/scripts-pending-removal.md`.

## Tests

- **Unit:** `claude_check.py` (strict payload parse, marker round-trip, trust
  rules including forged author / wrong request / stale head / result
  before request, part reassembly, engine var defaults); seam (all four
  modes, exit codes, prompt copy byte-identical).
- **Parity:** for each phase, a fixture where the same raw model output
  given via `CLAUDE_CHECK_RESULT_FILE` and via a stubbed `codex` produces an
  identical tail result (files, labels called, issue bodies).
- **Contract:** resume workflow, request action, wrapper profiles, engine
  var defaults in every workflow, the `claude/*` triage skip.
- **Poller:** `pending_claude` transitions, head-moved abandon, deadline
  fail-closed, queued-validation handling.
- **End to end (after merge):** dispatch `security-audit.yml` in this repo
  with defaults, then confirm the queue item, a session, the result comment,
  the resume run, and the tracker comment; repeat for validation (standalone,
  `target_ref`), a forced failing check on a non-Claude PR, and a heal
  report; set each engine var to `gpt` once and confirm today's logs.

## Risks & Mitigations

- A forged or replayed "clean" result → trust by author association +
  request id + head + created-after-request; the deadline and `failed`
  paths fail closed; covered by unit tests.
- Pickup down → no check runs; the watchdog flags stale items at 3 h and
  fails checks closed at their deadline with a Telegram ERROR. ACCEPTED —
  pickup liveness is out of scope.
- Hours of added latency per check (hourly pickup plus session time) →
  ACCEPTED (Q9); the per-phase deadlines bound it.
- Session depth: check sessions sit one link below the pickup (≤ 3 deep), so
  they are well within the 8-link limit.
- Poller comment reads while pending → bounded to pending projects, 1 read
  per tick.
- Result comments over 65,536 characters → numbered parts; the resume run
  fails closed on a missing part.
- ACCEPTED — pending discovery (Q16): consumer session network policy may
  block image or package pulls; the preflight posts
  `status=failed reason=network_policy`, which fails closed with a clear
  alert.
- Opus cost across 13 repos → per-phase `gpt` switch; triage volume is
  already bounded by dedupe and the lineage cap, and `claude/*` PRs are
  skipped.

## Rollout

1. Implement after `docs/plans/claude-release-track-plan.md` (Q17), phases in
   order.
2. This repo gets the behaviour on merge (`@main` wrappers). Consumers get
   the Actions side with the next `@stable` release and the new wrapper and
   commands through the sync.
3. Defaults are `claude`. Per phase, `<PHASE>_ENGINE=gpt` switches back
   instantly with no deploy. Reverting a phase's PR removes it entirely.
4. Watch for the first week: `CLAUDE_CHECK` logs, `deadline_expired` alerts,
   and queue watchdog alerts.

## References

- `docs/plans/claude-release-track-plan.md`
- `scripts/security_audit.sh`, `scripts/orchestrate_poll_process.sh`,
  `scripts/validate_process.sh`, `scripts/check_failure_triage.sh`,
  `scripts/workflow_failure_heal_intake.sh`, `scripts/claude_issue_route.py`,
  `.claude/commands/claude-issue-pickup.md`, `.claude/commands/implement-plan-claude.md`
- Issue #4525 (routine runs lack session tools); PR #4334 (a failed audit
  must never read as a pass)
- CLAUDE.md §4, §6, §14, §15, §18, §20, §25, §26, §27, §28
