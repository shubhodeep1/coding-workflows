# Claude automation follows the Codex single-issue and orchestrator processes

## Summary

Make Claude-routed work follow the same shape as the Codex pipelines. A single
`ai:claude` issue ships as one `claude/issue-<N>` pull request, the way a Codex
issue ships as one `ai/issue-<N>` PR. A multi-phase plan run by
`/implement-plan-claude` is split into a tracking issue plus sub-issues that run
in parallel dependency waves, with a judge after each wave, the way
`/implement-plan-ai` hands a plan to the orchestrator. Claude stays the driver
throughout, and no Codex workflow learns any Claude logic, so a repo can switch
between the two pipelines at any time.

## Context

Today a Claude-routed issue and a Claude-run plan both go through one long
chain, `/implement-plan-claude`. The issue path enters it in *issue mode* with
a one-phase plan (`.claude/commands/implement-issue-claude.md` steps 6–8,
`.claude/commands/implement-plan-claude.md` "Issue Mode"):

| Stage | Codex standalone issue | Claude issue today | Codex orchestrator (`/implement-plan-ai`) | `/implement-plan-claude` today |
|---|---|---|---|---|
| Clarify | `clarify.yml`; a human answers `/answer` when questions exist | auto-decided, `AD-<n>` (§28) | sub-issue clarify auto-answered | auto-decided |
| Plan | `plan.yml` posts an "Implementation plan" comment; auto-approved by `AUTO_IMPLEMENT_ON_CLEAR_PLAN` | `docs/plans/issue-<N>-…-plan.md` + progress comment summary | decomposer splits the plan into a DAG (`orchestrate.yml`, `prompts/mode-orchestrate.txt`) | the plan's Phases section as a checklist |
| Branch / PRs | one `ai/issue-<N>` PR into the base | project branch + draft final PR + one phase PR | `orchestrator/project-<N>`, sub-PRs `ai/issue-*`, eager draft final PR | project branch + draft final PR + one PR per phase |
| Execution | one implement run | one phase | sub-issues in parallel waves; wave judge (`scripts/orchestrate_poll_process.sh:22074-22700`) | phases one after another |
| Review | reviewer panel + GPT editor + review-blocked judge | reviewer panel + Claude fixer (`review_autofix.yml:773`) | same as Codex | reviewer panel + Claude fixer |
| Extra stages | none | conformance, security, validation, completion PR, verify-activation, deploy-activate | security pass (≤5), validation (≤3), final merge | conformance, security, validation, completion, final merge, activation |
| Tracking | issue labels | progress comment | `ai:orchestrator-tracking` issue | progress log only |

The user wants (session decisions below): single Claude issues to follow the
Codex single-issue shape, and multi-phase Claude plans to follow the
orchestrator shape, while keeping Claude's own clarify, plan file, and review
loop, and keeping every piece of Claude logic out of the Codex workflows.

Facts found while planning that bind the design:

- **Claude cloud sessions cannot read repository variables.** The agent proxy
  refuses Actions variable paths (CLAUDE.md §23.A, "Web sessions"). A
  per-repo switch must be read in Actions and carried to the session.
- **Any issue an owner opens is routed.** `workflow-templates/ai-clarify.yml:16`
  skips only `ai:orchestrator-tracking`, `ai:security-audit`, and `ai:retro`.
  Everything else reaches `scripts/claude_issue_route.py` `route_issue`
  (`:215-244`), which defaults to Claude and lets `ai:codex` win over
  `ai:claude`. A new Claude tracking issue would be routed as work.
- **Codex can reuse a Claude-posted plan.** `implement.yml:1862-1888` takes the
  newest comment whose body starts `Implementation plan` from an OWNER,
  MEMBER, or COLLABORATOR (or `github-actions[bot]` / `codex-bot[bot]`). The
  MCP identity of a Claude session is the repo owner, so its comment
  qualifies. Comment-triggered workflows match only the start of a comment
  (`startsWith(…, '/answer')`, `'/approved'`, `'/reclarify'`), so a comment
  that starts `Implementation plan` cannot trigger them.
- **Draft PRs are not reviewed.** `review_autofix.yml:553-555` skips a draft
  PR (`draft_or_skip_ai`), and `ready_for_review` starts a review.
- **`security-audit.yml` `ref`** audits a branch's changes since its
  merge-base with the default branch, and files follow-ups whose
  `Integration branch:` is that branch.
- **Chain review detection is keyed on the head prefix.**
  `.claude/scripts/check_in_status.py:109` (`CLAUDE_FIXER_HEAD_PREFIX =
  "claude/implement-plan-"`, used at `:253` and `:637`) detects review
  hand-offs and keeps the 6-hour stuck window only for that prefix;
  `scripts/claude_pr_sweep.py` loads the same module.
- **Lessons ingestion is keyed on the head prefix.** `issue_pr_status.yml:543`
  ingests progress-log lessons only for `claude/implement-plan-*` and
  `claude/verify-activation-*` heads.
- **An escalation judge is in flight.** `docs/plans/retire-master-session-plan.md`
  phase 1 (PR #5164 into the project branch, final PR #5132, both open) adds
  `.claude/commands/escalation-judge.md` and `.claude/scripts/escalation_ledger.py`:
  a §28.C failure stop marked `kind=escalation stop=<id>` goes to a fresh
  Opus judge that picks `budget`, `descope`, or `close`, and only the Q8
  human-only stops (ask-first, no tools, depth limit) page a human.
- **`move-checking-roles-to-claude-plan.md`** also extends the intake
  (`claude-check` items). Both plans touch `scripts/claude_issue_route.py` and
  `scripts/claude_issue_intake.sh`; see Risks.

### Decisions (clarification round, 2026-10-02)

| Q | Decision |
|---|---|
| Q1: A | Single issues keep Claude's auto-decided clarify (`AD-<n>` on the progress comment); no `/answer` wait |
| Q2: A | The plan file stays the source of truth, and the full plan is also posted on the issue as a comment starting `Implementation plan` |
| Q3: A | One `claude/issue-<N>` PR straight into the issue base; no project branch, draft final PR, or phase PR |
| Q4: C | Of the extra stages, single issues keep only the security pass; conformance, validation, completion PR, and activation are dropped |
| Q5: A | Multi-phase plans get the orchestrator process built in Claude logic; no Codex workflow changes |
| Q6: A | Claude drives everything: decomposition, wave judge, implementation, fixes |
| Q7: A | Multi-phase projects keep conformance, plan archival, verify-activation / deploy-activate after validation |
| Q8: A | Existing switches decide the driver: `ai:claude` / `ai:codex` per issue, `AI_ISSUE_IMPLEMENTER` per repo |
| Q9: A | The driver is chosen at the start; work in flight finishes on its driver |
| Q10: A | The Claude flow uses only Claude labels, never the Codex phase labels |
| Q11: A | (as Q7) |
| Q12: A | Rollout to coding-workflows and every consumer via `@stable`, behind a switch back to today's chain |
| Q13: A | This plan, single-issue flow first, then the wave flow |
| Q14: A | Repo variable `CLAUDE_ISSUE_FLOW` (`pr` default when unset, `project` = today's chain), read by the Claude intake and carried in the queue payload; `/implement-plan-claude` defaults to the wave flow and takes a trailing `— legacy` for today's chain. No manual per-repo setup |
| Q15: A | Draft PR first; security audit of `claude/issue-<N>` (≤5 cycles); then ready → review rounds → auto-merge; review-round fixes are not re-audited (accepted risk) |
| Q16: A | The single-issue PR commits its plan straight to `docs/completed/issue-<N>-<topic>-plan.md` |
| Q17: A | The single-PR flow is a sub-mode of `/implement-plan-claude` issue mode, reusing its checker, stage sessions, claims, hand-back, and log |
| Q18: A | Orchestrator-style DAG decomposition with `files_touched`; siblings sharing a file are serialized; each plan phase maps to ≥1 sub-issue and two phases never share one |
| Q19: A | Sub-issue sessions start through the existing route (clarify → intake queue → hourly pickup) |
| Q20: C | No cap on sub-issues in flight beyond the wave |
| Q21: A | Sub-issues always use the single-PR flow, whatever `CLAUDE_ISSUE_FLOW` says |
| Q22: A | Wave judge (Opus, high): `complete` / `in_progress` (fix-up sub-issues) / `failed` (stop) |
| Q23: A | `scripts/claude_issue_handoff.sh` ignores tracking issues; the Codex clarify trigger is unchanged |
| Q24: A | Sub-issues skip their own security pass, verified by `security_pass_skip.py` (label, marker, `Refs #<tracking>`, same #4623 trust rules) |
| Q25: A | Multi-phase blockers are reported on the tracking issue |
| Q26: A | `lint_plan_archival_completeness.py` covers the new tracking label |
| Q27: A | Labels `ai:claude-tracking` and `ai:claude-subissue` |
| Q28: A | Two phases, matching the two parts |
| Q29: B | Any failure to read `CLAUDE_ISSUE_FLOW` uses the default `pr`, with a warning |
| Q30: A | New failure stops go to the `retire-master-session` escalation judge; until it is on the default branch they stop at `BLOCKED` |
| Q31: B | `ai:codex` + `/reclarify` cannot hand over an issue while a Claude PR for it is open |

### Derived decisions (follow from the answers above; change them in review if wrong)

| ID | Decision | Follows from |
|---|---|---|
| D1 | The Q31 lock is a label, `ai:claude-pr-open`: the flow adds it when it opens its first PR for the issue and removes it when that PR merges or closes; the router keeps a labelled issue on Claude. A human can remove the label to unlock | Q31 B; the router sees only issue JSON, so a label costs no API call (§15) |
| D2 | `route_issue` order becomes: orchestrator-managed → Codex-only labels → E2E title → **`ai:claude-tracking` (Claude, reason `claude_tracking`)** → **`ai:claude-subissue` (Claude, `claude_subissue`)** → **`ai:claude-pr-open` (Claude, `claude_pr_open`)** → `ai:codex` → `ai:claude` → `AI_ISSUE_IMPLEMENTER` | Q9, Q23, Q31; otherwise `AI_ISSUE_IMPLEMENTER=codex` would send a tracking issue to Codex clarify |
| D3 | `CLAUDE_FIXER_HEAD_PREFIX` in `check_in_status.py` becomes a tuple that also matches `claude/issue-`; such heads get chain review-round detection and the chain's stuck window | Q3, Q17 |
| D4 | The `issue_pr_status.yml:543` condition gains `claude/issue-`, so single-issue lessons are ingested | Q16 keeps the progress log; this step is Claude-only, not Codex logic |
| D5 | The wave DAG lives in `docs/implement-plan/<slug>.dag.json` on the project branch, validated and scheduled by a new helper `.claude/scripts/claude_project_waves.py` | Q18 |
| D6 | Decomposition gets 3 attempts (as `orchestrate.yml`), then stop `decompose-failed`; a wave gets at most 3 judge fix-up rounds (as `MAX_RECOVERY_ATTEMPTS=3`), then stop `wave-judge-failed` | Q22, Q30 |
| D7 | The final-merge stage closes the tracking issue (`completed`, `ai:merged`) and ticks its checklist once the final PR merged | Q26 needs the ticks; the orchestrator leaves its tracking issue open, but a stale open Claude tracking issue would be swept by nothing |
| D8 | `/implement-issue-claude` refuses an issue labelled `ai:claude-tracking` (`tracking issue — owned by project <slug>`) | Q23 belt and braces; a `/reclarify` or a future blocked-issue `requeue` could still queue one |

## Goals

- **G1:** With `CLAUDE_ISSUE_FLOW` unset or `pr`, a Claude-routed standalone
  issue produces exactly one PR, head `claude/issue-<N>`, base the issue base,
  plus one comment starting `Implementation plan` and the progress comment.
  No `claude/implement-plan-issue-<N>-*` branch is created. Verified by
  command-text tests and one real issue after rollout.
- **G2:** That PR is a draft until the security pass is clean (or skipped for
  a verified automation-produced or sub-issue), then ready; it merges only
  through `review_autofix.yml` Claude-fixer mode. Verified by command-text
  tests and the real issue's PR timeline.
- **G3:** With `CLAUDE_ISSUE_FLOW=project`, the issue runs today's chain
  unchanged. A project already in flight on `claude/implement-plan-issue-<N>-*`
  finishes on today's chain whatever the variable says. Verified by
  `tests/test_implement_issue_claude_command.py` and a payload test.
- **G4:** While `ai:claude-pr-open` is on an issue, the router returns
  `claude` even with `ai:codex`. Verified by `tests/test_claude_issue_route.py`.
- **G5:** `/implement-plan-claude <plan>` on a new project creates one
  `ai:claude-tracking` issue and, per wave, one `ai:claude-subissue` per DAG
  node whose dependencies are merged; every sub-issue lands as a
  `claude/issue-<N>` PR into `claude/implement-plan-<slug>`; a wave judge runs
  after each wave; then conformance, security, validation, completion, final
  merge, and activation run as today. Verified by command-text tests, helper
  tests, and one real multi-phase project after rollout.
- **G6:** `/implement-plan-claude <plan> — legacy`, and any project whose log
  predates this plan, runs the sequential-phase chain unchanged. Verified by
  command-text tests.
- **G7:** A tracking issue never starts a Claude or Codex implementation
  (handoff ignores it, router keeps it on Claude, the command refuses it).
  Verified by route and handoff tests.
- **G8:** `lint_pr_body_auto_close.py` rejects `Fixes/Closes/Resolves #T` for
  an `ai:claude-tracking` issue, and `lint_plan_archival_completeness.py`
  requires its checklist ticked or de-scoped. Verified by their tests.
- **G9:** A sub-issue skips its own security pass only when
  `security_pass_skip.py` verifies the label, marker, and tracking reference.
  Verified by `tests/test_security_pass_skip.py`.
- **G10:** No file under `.github/workflows/` that runs Codex changes:
  `clarify.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`,
  `orchestrate*.yml`, `validate.yml`, `security-audit.yml` are untouched.
  The only workflow edits are Claude-only: `claude-issue-intake.yml` (if its
  env needs the variable name) and the Claude lessons step in
  `issue_pr_status.yml` (D4). Verified by the PR diffs.

## Non-goals

- Changing the Codex pipelines or the orchestrator (`orchestrate_poll_process.sh`,
  `orchestrate.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`).
- Adding a `/answer` clarify round or a `/approved` gate to the Claude flow
  (Q1, Q2).
- Moving security or validation into Claude sessions (that is
  `move-checking-roles-to-claude-plan.md`).
- Building the escalation judge (`retire-master-session-plan.md` phase 1).
  This plan only adds stop ids for it.
- Re-auditing review-round fixes in the single-PR flow (Q15, accepted risk).
- Mid-flight hand-over between drivers (Q9, Q31).
- A concurrency cap on sub-issues (Q20).

## Constraints

- **§6 naming immutability.** No existing identifier is renamed or removed.
  `/implement-issue-claude`, `/implement-plan-claude`, the `claude_issue.v1`
  payload, `AI_ISSUE_IMPLEMENTER`, `ai:claude`, `ai:codex`,
  `ai:claude-blocked`, `CLAUDE_FIXER_HEAD_PREFIX` (kept as the name of the
  widened value or kept alongside a new tuple constant), and the
  `claude/implement-plan-<slug>` project branch all keep their meaning. New
  identifiers (`CLAUDE_ISSUE_FLOW`, `flow:` payload key, `ai:claude-tracking`,
  `ai:claude-subissue`, `ai:claude-pr-open`, `claude/issue-<N>`,
  `claude_project_waves.py`, stop ids `issue-security-cap`,
  `wave-judge-failed`, `subissue-unmerged`, `decompose-failed`) were checked
  for collisions on 2026-10-02 (`grep` across `.py`, `.sh`, `.yml`, `.md`,
  `.json`): none exist. `claude/issue-` appears only as fixture data in
  `tests/test_orchestrate_poll_process.py:11915`.
- **§10:** no MongoDB collection, index, or contract is touched.
- **§14:** every change reaches consumer repos through the existing `@stable`
  sync: `.claude/**` through the `workflow-templates/.claude/` twins, labels
  through `.github/ai/label_contract.v1.json` and `ai-sync-labels.yml`,
  `CLAUDE.md` through `workflow-templates/CLAUDE.md`. No consumer needs a
  manual step. `consumer_repos.json` is unchanged.
- **§15:** the intake adds one REST GET per queued issue
  (`repos/<repo>/actions/variables/CLAUDE_ISSUE_FLOW`), next to the reads it
  already makes for authorization; there is no existing call that returns a
  repository variable, so it cannot be merged into one. The router and
  handoff add no calls (labels are already in the issue JSON). The wave flow
  creates issues with MCP writes and waits with the existing
  `check_in_status.py --issues` loop (one GET per sub-issue per hourly
  check-in). `security_pass_skip.py` keeps its three-GET budget.
- **§18:** no standalone script. `claude_project_waves.py` is a helper the
  `/implement-plan-claude` stage sessions call; the existing intake workflow,
  hourly pickup, and project checker are the schedulers.
- **§19:** the tracking issue is referenced only with `Refs #T`; the rule and
  its lint extend to `ai:claude-tracking`.
- **§20:** each phase adds one `changelog.d/` fragment.
- **§21, §25, §26:** unchanged; the single-PR flow's checker is the
  `/implement-plan-claude` project checker, which is the §26 check-in for its
  PRs.
- **§27:** no workflow file grows by more than a few lines.
- **§28:** the single-PR flow and the wave flow are both in §28.A scope (issue
  mode and `/implement-plan-claude`); §28.A/§28.C text is updated for the new
  stops and modes.
- **Protected paths (§28.C):** both phases edit `.claude/**`. In
  coding-workflows they follow the interim twin-first rule (edit
  `workflow-templates/.claude/**` twins; `.claude/commands/claude-issue-pickup.md`
  has no twin, so its diff goes in the twin-sync blocker).

## Approach

### Shared: the flow switch (phase 1)

1. `scripts/claude_issue_intake.sh` reads `CLAUDE_ISSUE_FLOW` for the target
   repo with `GH_PAT` (`gh api repos/<repo>/actions/variables/CLAUDE_ISSUE_FLOW
   --jq .value`). `pr` or `project` is used as is; 404 (unset), any other
   value, or any read failure gives `pr` with a `::warning::` and a
   `CLAUDE_ISSUE_FLOW_READ result=<unset|invalid|failed> flow=pr` log line
   (Q29 B).
2. `scripts/claude_issue_route.py` gains an optional `flow` field in the
   validated payload, the fire text (`flow: pr|project`), the queue issue, and
   the queue binding (issue #4621), so the pickup sees it as a parsed field.
   A payload without `flow` (an item queued before this change) parses as
   `pr`.
3. `claude-issue-dispatch.md` step 2 and `claude-issue-pickup.md` step 3 put
   it in the start prompt: `/implement-issue-claude <url> — flow <flow>`.
4. `/implement-issue-claude` picks the flow: an existing
   `claude/implement-plan-issue-<N>-*` project → today's chain (G3); an issue
   with `ai:claude-subissue` → `pr` (Q21); otherwise the `— flow` argument,
   defaulting to `pr` when absent (a hand-run or an old queue item).

### Part 1: the single-PR flow (phase 1)

`/implement-issue-claude` with flow `pr` writes the plan, then follows
`/implement-plan-claude` in a new **issue PR sub-mode**. The plan header
gains `Flow: pr` (today's chain writes `Flow: project`; a plan without the
line is today's chain). Stages, all using the existing checker, stage
sessions, claims, hand-back, and progress log:

1. **Plan and post** (start session). Write the plan as today, but at
   `docs/completed/issue-<N>-<topic>-plan.md` (Q16), and post its full text
   on the issue as one comment whose first line is `Implementation plan —
   <title>` followed by the plan and a `<!-- ai:claude-plan:v1 -->` marker
   (Q2). Post the progress comment as today.
2. **Implement** on `claude/issue-<N>` forked from `origin/<issue base>`
   (append `-2`, `-3` on a collision with an unrelated branch), commit the
   plan, the progress log `docs/implement-plan/<slug>.md`, and the change,
   verify per step 5, push, open the PR **as a draft** into `<issue base>`
   (`Fixes #<N>` when the base is the default branch, `Refs #<N>` otherwise;
   §19), and add `ai:claude-pr-open` to the issue (D1).
3. **Security pass** (Q15). `Security pass: skip` (verified per Q24 or the
   existing #4623 labels) goes straight to stage 4. Otherwise dispatch the
   audit with `ref=claude/issue-<N>` through the dispatch helper, wait on the
   run, read it as step 9 does today, and wait on any follow-ups (they route
   to Claude with `Integration branch: claude/issue-<N>`, each a single-PR
   flow into that branch with its own pass skipped). Cap 5 cycles; on
   exhaustion stop `issue-security-cap` (Q30).
4. **Ready and review.** Mark the PR ready (`update_pull_request`,
   `draft: false`); `ready_for_review` starts the reviewer panel in
   Claude-fixer mode; review rounds, conflicts, and blocks run as steps 7 / 7a
   today.
5. **Close.** On merge: when the base is not the default branch, close the
   issue (`completed`) and add `ai:merged`, as the final-merge stage does
   today; remove `ai:claude-pr-open`; finish the progress comment with the
   auto-decision list (§28.E); archive the checker. When the PR is closed
   unmerged, remove `ai:claude-pr-open` and stop with the existing
   closed-PR hand-back.

Dropped for this flow (Q4 C): project branch, final PR, conformance,
validation, completion PR, verify-activation, deploy-activate.

### Part 2: the wave flow (phase 2)

`/implement-plan-claude <plan>` without `— legacy`, on a project with no log
yet, writes `Flow: waves` in the log; a log without that line runs the
sequential chain (G6). Stages:

1. **Project branch and draft final PR** as step 3a today.
2. **Decompose** (`decompose 1/1`). The stage reads the plan and writes a DAG
   in the `orchestrate_decomposition.v1` shape (`issues[]` with `id`,
   `title`, `body`, `files_touched`, `plan_phase`; `dependency_edges`) to
   `docs/implement-plan/<slug>.dag.json`. `claude_project_waves.py validate`
   rejects an empty `files_touched`, a cycle, a phase with no node, or a node
   spanning two phases (Q18), and adds edges so no two nodes in one wave share
   a file or a hot file from `.github/ai/hot_files.json` when present.
   `claude_project_waves.py waves` prints the wave list. Three invalid
   attempts → stop `decompose-failed` (D6).
3. **Tracking issue** `[Claude project] <plan title>`, label
   `ai:claude-tracking`, body: plan path, project branch, final PR, a
   `<!-- ai:claude-tracking:v1 slug=<slug> -->` marker, and a checklist with
   one `- [ ]` line per DAG node (wave, local id, title, sub-issue link once
   created). The log records `Tracking issue: #T`. The final PR body gains
   `Refs #T`.
4. **Wave `<w>`** — create one issue per node whose dependencies are all
   merged: title `<plan title> — <node title>`, labels `ai:claude` and
   `ai:claude-subissue`, body with the node spec, `Integration branch:
   claude/implement-plan-<slug>`, `files_touched`, `Refs #T`, and
   `<!-- ai:claude-subissue:v1 tracking=<T> id=<local id> -->`. The existing
   route starts each one (Q19): clarify → router (`claude_subissue`) →
   handoff → intake → pickup → `/implement-issue-claude <url> — flow pr`.
   All nodes of a wave start together (Q20 C). Each ships one
   `claude/issue-<N>` PR into the project branch and closes itself with
   `ai:merged` after the merge (Part 1 stage 5). The project checker waits
   with `check_in_status.py --issues <list>` (next stage
   `wave <w>/<W> — judge`).
5. **Wave judge** (`wave <w>/<W> — judge`, Opus, high). Sync the project
   branch; any sub-issue closed without `ai:merged` → stop
   `subissue-unmerged`. Otherwise compare the merged code with each node's
   spec and its plan phase:
   - `complete` → tick the nodes on the tracking issue, then the next wave
     (stage 4), or stage 6 after the last wave;
   - `in_progress` → open fix-up sub-issues (same labels and markers, local
     id `<id>-fix-<k>`), wait on them, re-judge; at most 3 rounds per wave,
     then stop `wave-judge-failed` (D6);
   - `failed` → stop `wave-judge-failed`.
6. **Conformance, security, validation, completion, final merge,
   activation** exactly as steps 8–13 today (Q7). The final-merge stage also
   ticks and closes the tracking issue (D7).

Stops in the wave flow (`decompose-failed`, `wave-judge-failed`,
`subissue-unmerged`, and every existing §28.C stop) post their
`<!-- ai:claude-blocked:v1 -->` comment and `ai:claude-blocked` label on the
**tracking issue** (Q25) and carry `kind=escalation stop=<id>`, so the
escalation judge picks them up (Q30); until `escalation-judge.md` is on the
default branch they stop at `BLOCKED` as today.

### Routing and guards

- `route_issue` gains the three forced-Claude rules (D2).
- `claude_issue_handoff.sh` exits 0 without claim, comment, or dispatch when
  the issue carries `ai:claude-tracking` (Q23), logging
  `CLAUDE_ISSUE_HANDOFF skipped reason=claude_tracking`.
- `/implement-issue-claude` step 2 refuses tracking issues (D8).
- `security_pass_skip.py` adds `ai:claude-subissue` to `SKIP_LABEL_MARKERS`
  with marker `^<!-- ai:claude-subissue:v1 tracking=\d+ id=\S+ -->$`, and its
  tracker rule: `Refs #T` names an issue labelled `ai:claude-tracking` whose
  body carries `<!-- ai:claude-tracking:v1 ` and whose author is the
  sub-issue's author (Q24). `SECURITY_PASS_SKIP_LABELS` in the router gains
  the label (advisory, as today).
- `lint_pr_body_auto_close.py` and `lint_plan_archival_completeness.py` treat
  `ai:claude-tracking` like `ai:orchestrator-tracking` (Q26, §19).

### Alternatives considered

- Reusing the Actions orchestrator with Claude sub-issues (rejected, Q5: it
  puts Claude logic in Codex workflows).
- The project checker starting sub-issue sessions directly (rejected, Q19:
  needs a duplicate-start guard against the intake route and deepens the
  session chain).
- Auditing after merge (rejected, Q15 B: code ships unaudited).

## Automation & Wiring (§18.E)

| Item | This plan |
|---|---|
| New or extended scripts | New helper `.claude/scripts/claude_project_waves.py` (phase 2), called only by `/implement-plan-claude` stage sessions. Extended: `scripts/claude_issue_intake.sh`, `scripts/claude_issue_route.py`, `.claude/scripts/check_in_status.py` (phase 1); `scripts/claude_issue_handoff.sh`, `.claude/scripts/security_pass_skip.py`, `scripts/lint_pr_body_auto_close.py`, `scripts/lint_plan_archival_completeness.py` (phase 2). Nothing needs a manual run (§18.A). |
| Scheduler / entry points | `claude-issue-intake.yml` (`repository_dispatch` `claude-issue`, `workflow_dispatch`) reads the variable; the hourly Claude issue pickup (`claude-issue-pickup.md`, Routine `Claude issue pickup: hourly`) starts sessions; the `/implement-plan-claude` project checker (hourly `send_later`) runs every wait; `clarify.yml` (unchanged) routes sub-issues; `lint-pr-body-auto-close.yml` and `lint-plan-archival.yml` (unchanged triggers) run the extended lints. |
| Long-running supervisor (§18.C) | None new. The pickup and the project checker are the existing supervisors. |
| DB gate (§18.D) | Not applicable: no database operation. |
| §18.F registry | No entry: `claude_project_waves.py` is a helper, not a single-use or long-running script or supervisor. |

## Phases & Merge Strategy

Two phases, one PR each (Q28). They have no merge-order dependency:

- Phase 2's sub-issues run whatever single-issue flow is live. Before phase 1
  merges that is today's chain, which already builds an issue on its
  `Integration branch:` (as security follow-ups do), with the sub-issue's own
  pass skipped through phase 2's `security_pass_skip.py` rule.
- Phase 1 already treats `ai:claude-subissue` as "always `pr`"; with phase 2
  absent no issue carries that label, so the rule is inert.

1. **Single-issue flow and the flow switch** (Part 1).
   - **Scope:** the `CLAUDE_ISSUE_FLOW` read and payload field; the dispatch
     and pickup prompt; the issue PR sub-mode of `/implement-plan-claude`;
     `/implement-issue-claude` flow selection, plan comment, and plan path;
     the `ai:claude-pr-open` lock (label contract, router rule, add/remove in
     both the new flow and today's issue-mode chain when its final PR opens
     and merges or closes); `check_in_status.py` prefix (D3); the
     `issue_pr_status.yml` lessons condition (D4); docs and fragment.
   - **Done:** with the variable unset, a test issue in coding-workflows ships
     as one `claude/issue-<N>` PR (draft → security → ready → merged) and
     closes; with `project` set on a consumer, a test issue runs today's
     chain; every listed test passes; template parity passes after the twin
     sync.
   - **Rollback:** revert the PR **except** the parser's acceptance of the
     optional `flow` key in `scripts/claude_issue_route.py`
     (`parse_fire_text` rejects any unknown line, so a full revert would make
     the pickup reject every queue item opened before the revert). Any
     in-flight `claude/issue-<N>` PR finishes through the §26 check-in and
     `/fix-claude-pr`, which handle every `claude/*` head. Set
     `CLAUDE_ISSUE_FLOW=project` on a repo for a per-repo rollback without a
     revert.

2. **Wave flow** (Part 2).
   - **Scope:** `Flow: waves` and `— legacy` in `/implement-plan-claude`;
     decompose, tracking issue, wave, and wave-judge stages; the new stop ids
     and tracking-issue blocker location; `claude_project_waves.py`; labels
     `ai:claude-tracking` / `ai:claude-subissue`; router rules for both;
     handoff skip; `/implement-issue-claude` tracking refusal;
     `security_pass_skip.py` rule; both lints; CLAUDE.md §19 / §28 and
     `unattended_system_instructions.md` §21 text; docs and fragment.
   - **Done:** a two-wave test plan in coding-workflows decomposes, opens a
     tracking issue and sub-issues, runs a wave judge per wave, and reaches
     the final merge; `— legacy` on another test plan runs the sequential
     chain; every listed test passes; template parity passes after the twin
     sync.
   - **Rollback:** revert the PR. A project started in wave mode keeps its
     `Flow: waves` line, which the reverted command ignores: its log still
     carries `Project branch:`, so its next stage resumes the sequential
     chain from the log's `## Phases` checklist, which wave mode ticks as each
     phase's nodes merge (phase 2 Implementation Step 6). Open sub-issues
     still finish through the single-issue flow and close themselves. The
     restored chain does not know tracking issues, so the revert PR's
     description lists the wave-mode projects in flight and the revert
     closes their tracking issues.

## Implementation Steps

### Phase 1

1. `.github/ai/label_contract.v1.json`: add `ai:claude-pr-open` ("A Claude
   pull request for this issue is open; ai:codex cannot hand it over until it
   merges or closes").
2. `scripts/claude_issue_route.py`:
   - `route_issue` (`:215-244`): `ai:claude-pr-open` → Claude,
     reason `claude_pr_open`, placed before the `ai:codex` check (D2);
   - payload: optional `flow` in `build_dispatch`/`validate_payload`
     (values `pr|project`, default `pr`), `FIRE_TEXT_KEYS` (`:153`),
     `build_fire_text` (`:394`), `parse_fire_text` (`:421`, missing key →
     `pr`), `build_queue_issue`, and the queue binding fields;
   - docstrings per §15.
3. `scripts/claude_issue_intake.sh`: after authorization, read
   `CLAUDE_ISSUE_FLOW` (Approach, shared step 1) and pass `--flow` to
   `validate-payload` / `queue-issue`. Add the variable name to the
   `claude-issue-intake.yml` step `env` only if the script needs it there.
4. `.claude/commands/claude-issue-dispatch.md` (twin first) step 1 accepts
   the optional `flow:` line; step 2's issue prompt becomes
   `/implement-issue-claude <url> — flow <flow>`.
   `.claude/commands/claude-issue-pickup.md` (no twin; diff in the sync
   blocker) step 3: the entry's `flow` fills `<flow>`.
5. `.claude/commands/implement-issue-claude.md` (twin first):
   - parse `— flow pr|project`; selection rules (Approach, shared step 4);
   - flow `pr`: plan path `docs/completed/…`, `Flow: pr` header line, post
     the `Implementation plan` comment (marker `<!-- ai:claude-plan:v1 -->`,
     one comment, edited in place on a `/reclarify` re-plan);
   - Rules section: drop "one project branch" wording for flow `pr`.
6. `.claude/commands/implement-plan-claude.md` (twin first): new
   "Issue PR mode" subsection under Issue Mode with stages 2–5 of Part 1;
   step 3a, 6, 9, 11, 11a, 12 each say what the mode skips; session titles
   (`… — security-pass 1/5`, `… — review round`); new stop id
   `issue-security-cap` with `kind=escalation`; `ai:claude-pr-open` add and
   remove points for both issue-mode flows.
7. `.claude/scripts/check_in_status.py` (twin first): widen the Claude-fixer
   head prefix to `("claude/implement-plan-", "claude/issue-")` at `:109`,
   `:253`, `:637`, keeping the existing name (§6); update the docstring.
8. `.github/workflows/issue_pr_status.yml:543`: add
   `startsWith(github.event.pull_request.head.ref, 'claude/issue-')`; update
   `scripts/ingest_implement_plan_lessons.py` docstring.
9. `scripts/claude_issue_handoff.sh`: the routing comment describes the
   single-PR flow and says `CLAUDE_ISSUE_FLOW=project` switches back.
10. Docs: README "Claude issue implementer", `agents.md` (switch,
    flows, lock label), CLAUDE.md §28.A (issue PR mode) and
    `workflow-templates/CLAUDE.md`; fragment
    `changelog.d/<pr>-claude-single-pr-issue-flow.md`.
11. Tests (below).

### Phase 2

1. `.github/ai/label_contract.v1.json`: add `ai:claude-tracking` and
   `ai:claude-subissue`.
2. `scripts/claude_issue_route.py`: `ai:claude-tracking` → Claude
   (`claude_tracking`) and `ai:claude-subissue` → Claude (`claude_subissue`),
   both before `ai:codex` (D2); add `ai:claude-subissue` to
   `SECURITY_PASS_SKIP_LABELS`.
3. `scripts/claude_issue_handoff.sh`: skip `ai:claude-tracking` (Q23).
4. `.claude/scripts/security_pass_skip.py` (twin first): the Q24 rule
   (Approach, Routing and guards), docstring budget unchanged.
5. `.claude/scripts/claude_project_waves.py` [new] (twin first, allowlisted
   in the `settings.json` twin, plain and `PYTHONDONTWRITEBYTECODE=1`):
   `validate --dag <file> --plan <file> [--hot-files <file>]` (exit 1 with
   reasons), `waves --dag <file>` (JSON wave list), `ready --dag <file>
   --merged <ids>` (next nodes). No API calls; docstring per §15.
6. `.claude/commands/implement-plan-claude.md` (twin first): `Flow: waves`
   and `— legacy`; stages decompose, tracking issue, wave, wave judge (Part
   2); tracking-issue blocker location; stop ids `decompose-failed`,
   `wave-judge-failed`, `subissue-unmerged` with `kind=escalation`; final
   merge ticks and closes the tracking issue (D7); progress-log template
   gains `Flow:`, `Tracking issue:`, and `## Waves`; the wave judge also
   ticks a plan phase in the log's `## Phases` checklist once every node
   mapped to it has merged, so the sequential chain can resume the project
   after a revert.
7. `.claude/commands/implement-issue-claude.md` (twin first): tracking
   refusal (D8); sub-issues force flow `pr` and read `Security pass:` via the
   script.
8. `scripts/lint_pr_body_auto_close.py` (`:88`, `:265`) and
   `scripts/lint_plan_archival_completeness.py`: a tracking-label set
   `{"ai:orchestrator-tracking", "ai:claude-tracking"}`, keeping
   `TRACKING_LABEL` (§6).
9. CLAUDE.md §19 and §28.A/§28.C, `workflow-templates/CLAUDE.md`,
   `unattended_system_instructions.md` §21 (text only), README, `agents.md`;
   fragment `changelog.d/<pr>-claude-wave-projects.md`.
10. Tests (below).

## Files & Modules

Phase 1:
- `.github/ai/label_contract.v1.json`
- `scripts/claude_issue_route.py`
- `scripts/claude_issue_intake.sh`
- `.github/workflows/claude-issue-intake.yml` (only if step 3 needs an env line)
- `scripts/claude_issue_handoff.sh`
- `.claude/commands/claude-issue-dispatch.md` + `workflow-templates/.claude/commands/claude-issue-dispatch.md`
- `.claude/commands/claude-issue-pickup.md` (no twin)
- `.claude/commands/implement-issue-claude.md` + twin
- `.claude/commands/implement-plan-claude.md` + twin
- `.claude/scripts/check_in_status.py` + twin
- `.github/workflows/issue_pr_status.yml`
- `scripts/ingest_implement_plan_lessons.py` (docstring)
- `README.md`, `agents.md`, `CLAUDE.md`, `workflow-templates/CLAUDE.md`
- `changelog.d/<pr>-claude-single-pr-issue-flow.md` [new]
- tests listed below

Phase 2:
- `.github/ai/label_contract.v1.json`
- `scripts/claude_issue_route.py`
- `scripts/claude_issue_handoff.sh`
- `.claude/scripts/security_pass_skip.py` + twin
- `.claude/scripts/claude_project_waves.py` [new] + twin [new]
- `.claude/settings.json` twin (allow rules)
- `.claude/commands/implement-plan-claude.md` + twin
- `.claude/commands/implement-issue-claude.md` + twin
- `scripts/lint_pr_body_auto_close.py`
- `scripts/lint_plan_archival_completeness.py`
- `CLAUDE.md`, `workflow-templates/CLAUDE.md`, `unattended_system_instructions.md`, `README.md`, `agents.md`
- `changelog.d/<pr>-claude-wave-projects.md` [new]
- tests listed below

## Data Model / Index Changes

None. No MongoDB collection, index, or contract is touched (§10).

## Tests

Unit (each runs in its existing `ci.yml` step, or a new step for the new
test file):

- `tests/test_claude_issue_route.py`: route order (D2) for `ai:claude-pr-open`,
  `ai:claude-tracking`, `ai:claude-subissue` with and without `ai:codex` and
  `AI_ISSUE_IMPLEMENTER=codex`; `flow` round-trips through dispatch, fire
  text, queue issue, and binding; a payload without `flow` parses as `pr`;
  invalid `flow` is rejected at validation.
- Intake script test (extend the existing intake test, or add
  `tests/test_claude_issue_intake_flow.py`): variable `project` → `flow:
  project`; 404, invalid value, and 403/5xx → `flow: pr` plus the warning and
  log line.
- `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`:
  a `claude/issue-<N>` head gets review-round detection and the stuck window;
  `claude/implement-plan-*` behaviour unchanged.
- `tests/test_security_pass_skip.py`: the sub-issue rule passes only with the
  label applied at creation by the author, the marker, and a tracking issue
  with the right label, marker, and author; each missing piece → `skip:
  false`.
- `tests/test_claude_project_waves.py` [new]: cycle, empty `files_touched`,
  unmapped phase, node spanning two phases, shared-file serialization,
  hot-file serialization, `ready` after partial merges.
- `tests/test_lint_pr_body_auto_close.py`, `tests/test_lint_plan_archival_completeness.py`:
  `ai:claude-tracking` behaves like `ai:orchestrator-tracking`.
- `tests/test_ai_labels.py`: the three labels exist with descriptions.
- Handoff test (extend the existing handoff test or add one): a
  tracking-labelled issue produces no label, comment, or dispatch.

Command-text tests:

- `tests/test_implement_issue_claude_command.py`: flow parsing and selection
  rules, plan path, `Implementation plan` comment, tracking refusal,
  sub-issue forcing `pr`.
- `tests/test_implement_plan_claude_command.py`: issue PR mode stages, draft →
  ready order, `issue-security-cap`, lock label add/remove; `Flow: waves`,
  `— legacy`, decompose / wave / judge stages, stop ids, tracking-issue
  blocker location, tracking close at final merge; the legacy chain text is
  still present.
- `tests/test_claude_md_section_numbers.py`: section numbers unchanged.
- Template parity tests: twins match after the `[claude-twin-sync]` copy.

End to end (after each phase merges, in coding-workflows):

- Phase 1: open a small test issue; expect one draft `claude/issue-<N>` PR,
  the plan comment, a security run with `ref=claude/issue-<N>`, the PR marked
  ready, a review round, auto-merge, and the issue closed. Then set
  `CLAUDE_ISSUE_FLOW=project` on one consumer and confirm a test issue there
  runs today's chain.
- Phase 2: run `/implement-plan-claude` on a two-wave test plan; expect the
  tracking issue, wave-1 sub-issues started by the pickup, a wave judge,
  wave-2 sub-issues, then the existing project stages. Run `— legacy` on a
  second test plan.

## Risks & Mitigations

- **Review-round fixes after the security audit are not re-audited (Q15).**
  ACCEPTED (Q15 A). The reviewer panel still reviews every push.
- **`GH_PAT` may lack permission to read repository variables in a consumer.**
  Then every issue there uses `pr`, and `project` cannot be selected (Q29 B).
  Mitigation: the warning and `CLAUDE_ISSUE_FLOW_READ result=failed` log line
  make it visible; a classic PAT with `repo` scope or a fine-grained PAT with
  "Variables: read" covers it. ACCEPTED — pending the first rollout check on a
  consumer with the variable set.
- **No cap on parallel sub-issues (Q20 C)** can exhaust the account's usage
  window on a wide wave. ACCEPTED (Q20 C); `claude-multi-account-pool-plan.md`
  is the planned relief.
- **Pickup latency.** Each wave waits up to about one pickup interval (hourly,
  catch-up 30 min) before its sessions start. ACCEPTED (Q19 A).
- **The escalation judge is not on the default branch yet.** Until
  `retire-master-session-plan.md` phase 1 merges, the new stops behave like
  today's: `BLOCKED` with a comment and a notification. Mitigation: the stop
  text says so; nothing here depends on the judge's code.
- **`retire-master-session` phase 2's blocked-issue sweep could `requeue` a
  blocked tracking issue** through the intake. Mitigation: D8 makes
  `/implement-issue-claude` refuse it, and that plan's sweep must treat
  `ai:claude-tracking` like a final-PR blocker (`wake-checker`). Whichever PR
  merges second adds the mapping; the phase-2 PR description of this plan
  states it.
- **`move-checking-roles-to-claude-plan.md` edits the same intake and router
  files.** Mitigation: both add optional fields; the second PR to merge
  resolves the conflict keeping both (§12 merge rules).
- **A human closes a Claude PR without removing `ai:claude-pr-open`.** The
  issue stays locked to Claude. Mitigation: the closed-PR stage removes it;
  the label description tells a human they may remove it.
- **Lock label in today's chain.** Phase 1 also adds and removes
  `ai:claude-pr-open` in today's issue-mode chain (final PR open → merge or
  close), so Q31 holds for both flows. A project already in flight when phase
  1 merges never gets the label until its next stage runs. ACCEPTED: the gap
  closes at that stage.
- **Wave-mode revert strands projects in flight.** Covered by the phase 2
  rollback (list them, finish with `— legacy`).
- **Decomposition quality.** A bad DAG wastes a wave. Mitigation: the
  validator rejects structural errors; the wave judge's `in_progress` /
  `failed` verdicts catch spec gaps; the conformance audit still runs before
  security.
- **Protected-path edits.** Both phases edit `.claude/**`. In coding-workflows
  they follow the interim twin-first rule and stop on the twin-sync blocker
  until the `[claude-twin-sync]` copy; a consumer receives the files only
  through the sync. ACCEPTED: this is the standing rule (§28.C).

## Rollout

- Each phase merges to `main` and reaches consumers with the next `@stable`
  release through the existing sync; labels are created by `ai-sync-labels.yml`.
- Default on: with `CLAUDE_ISSUE_FLOW` unset, new Claude issues use the
  single-PR flow as soon as phase 1 is live in a repo; new
  `/implement-plan-claude` projects use waves as soon as phase 2 is live.
- Per-repo switch back: `gh variable set CLAUDE_ISSUE_FLOW --body project -R
  <repo>` (an operator action, §23.C ask-first for Claude). Per project:
  `— legacy`.
- In-flight work is never migrated: issue-mode projects on
  `claude/implement-plan-issue-<N>-*` and sequential projects without
  `Flow: waves` finish as they started.
- Full rollback: revert the phase PR (see each phase's rollback).

## References

- `.claude/commands/implement-issue-claude.md`, `.claude/commands/implement-plan-claude.md` (Issue Mode, Check-in Loop, steps 3a–13)
- `.claude/commands/implement-plan-ai.md`, `.github/workflows/orchestrate.yml`, `scripts/orchestrate_poll_process.sh`, `prompts/mode-orchestrate.txt`
- `scripts/claude_issue_route.py`, `scripts/claude_issue_intake.sh`, `scripts/claude_issue_handoff.sh`, `.github/workflows/claude-issue-intake.yml`, `.claude/commands/claude-issue-dispatch.md`, `.claude/commands/claude-issue-pickup.md`
- `.claude/scripts/check_in_status.py`, `.claude/scripts/security_pass_skip.py` (issue #4623), `scripts/claude_pr_sweep.py`
- `.github/workflows/implement.yml:1862-1888`, `.github/workflows/review_autofix.yml:553-555`, `:773`, `.github/workflows/security-audit.yml`, `.github/workflows/issue_pr_status.yml:543`
- `scripts/lint_pr_body_auto_close.py`, `scripts/lint_plan_archival_completeness.py`, CLAUDE.md §19
- `docs/plans/retire-master-session-plan.md` (escalation judge; PR #5164, final PR #5132)
- `docs/plans/move-checking-roles-to-claude-plan.md`, `docs/completed/claude-multi-account-pool-plan.md` (superseded by `docs/plans/claude-actions-worker-pool-plan.md`)
