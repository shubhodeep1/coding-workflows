# Replace the Claude session automation with a Claude CLI engine inside the codex pipelines

## Summary

Retire the claude.ai-session-driven unattended Claude automation (issue
queue, pickup, `/implement-plan-claude` stage chain, §26 checkers, fixer
hand-offs) and make the Claude Code CLI (`claude -p`) a selectable engine
inside the existing codex GitHub Actions pipelines. Claude is the default
engine in every role, codex stays one variable away, accounts rotate through
an OIDC-gated token broker, and every blocked step gets an automated judge so
the pipeline runs with no human watching.

## Context

### Why

An audit on 2026-10-03 (this plan's clarification session) compared the two
automation stacks:

- The session-based Claude automation is about 8,400 lines of scripts, hooks
  and workflows plus 347 KB of command text. Roughly half to three quarters
  of it exists only because claude.ai sessions cannot be started, scheduled or
  woken from Actions (#4525): checker sessions, hand-back Routines, the
  lineage-depth limit, the session janitor, the stale-Routine sweep, claims,
  permission-prompt reports. Roughly 60–65% of the incidents logged in
  `docs/operations/master-session.md` and `changelog.d/` between 2026-09-25
  and 2026-10-02 were caused by that session architecture (one account's
  weekly limit killing every `send_later` chain, 66 orphaned sessions, a
  708k-token pickup session, stage sessions stuck `PENDING` for 11 hours).
- The unattended chain is ten days old on `main` (#4302, 2026-09-23) and left
  104 open `claude/*` PRs (80 drafts), 142 open Claude-routed issues and 1,612
  `claude/*` branches in this repo.
- The codex pipelines already run headless in Actions. Model calls take a
  prompt on stdin and write output files; nothing but the thread-resume shim
  parses codex's event stream. The OpenCode cutover of `review_autofix.yml`
  (`docs/completed/opencode-review-autofix-cutover-plan.md`) is the precedent
  for swapping a harness role by role.
- The worker-pool spike (`claude-workers`, runs 36956599290 … 37010604783,
  S1–S21) proved `claude -p` with a `claude setup-token` subscription token
  works in Actions: `--model claude-opus-5-5`, `--effort high|low`, usage data
  in `rate_limit_event`, a Haiku usage probe, concurrent accounts.

### Inventory (2026-10-03)

| Item | Count / fact |
|---|---|
| Open PRs in coding-workflows | 106: 104 `claude/*` heads, 2 `orchestrator/*` (#4142, #6043) |
| Open issues in coding-workflows | 152: 142 Claude-related (66 automation-infra, 45 queue/meta, 30 real codex-pipeline bugs / heal reports, 1 keyword false positive) |
| `claude/*` branches | 1,612 in coding-workflows; 757 across consumers (544 in `tele-funtoken-msg-scoring`) |
| Consumer open `claude/*` PRs | 10, all in `tele-funtoken-msg-scoring` (#4577, #4581–#4588), interactive product work; #4588 by kishor-cosmodea |
| Consumer open Claude-labelled issues | 0 |
| `ai:claude*` labels in consumers | 8 repos |
| `.claude/` in consumers / root `CLAUDE.md` | 9 / 12 repos |
| `claude-workers` | PR #1 (pool wrapper), spike branches `claude/eager-cori-7gq5zg`, `claude/inspiring-curie-y09am6`, `claude/pool-spike-r8` |
| Codex call sites | ~32 `codex … exec` sites; OpenCode for the review roles |
| Sync behaviour | `update_workflows.yml:202-206` never deletes upstream-removed files; `sync_ai_labels` never deletes labels |
| Snapshot dry run | Restoring `db0d0fc6` and cherry-picking re-applied 23/50 codex commits; the result put `review_autofix.yml` at 547,551 bytes (over GitHub's 512,000 limit) and broke consumer wrappers that pass `claude_fixer_converged_head`, `target_ref`, `ref`. Rejected (Q7) in favour of forward removal |

Raw inventory files were produced in the planning session's scratchpad; the
implementing session regenerates every list it acts on (Phase 1) rather than
trusting this snapshot.

### Operator decisions (clarification session, 2026-10-03)

| Q | Answer | Meaning |
|---|---|---|
| Q1 | A | Claude engine authenticates with subscription tokens (`claude setup-token`) and rotates across accounts |
| Q2 | A | Keys are stored once and served by an OIDC-gated token broker; consumers hold no copies |
| Q4 | A+B+C | Every codex role moves to Claude (plan/clarify, implement, judges and review write roles, the rest); Claude is the default; codex is restorable by variable |
| Q5 | B | The broker is a Cloudflare Worker on the `FT_GAMES_CF` account |
| Q6 | — | `/implement-plan-claude` and `/implement-issue-claude` stay as entry points into the codex pipelines, forcing Claude Opus 5.5 at high effort everywhere; Claude-fixer mode in `review_autofix` stays, using the CLI |
| Q7 | A | Forward removal on today's `main` (no snapshot) |
| Q8 | A | Codex-pipeline fixes delivered by Claude PRs are kept |
| Q9 | A | Product work merged into consumers is kept |
| Q10 / Q26 | B → A | Close the 112 automation and queue issues; return the 30 real-work issues to codex |
| Q11 / Q27 | A | Close every open `claude/*` PR in coding-workflows; leave the 10 `tele-funtoken-msg-scoring` product PRs open |
| Q12 / Q30 | A | Freeze first: delete the automation Routines, archive the automation sessions; the operator's master session stays |
| Q13 | A | Consumer repos are attached for the audit and the teardown |
| Q14 | A | Port P1 (single-issue security pass), P2 (never-repeat escalation ledger, inside the unblock judge), P3 (standalone clarify auto-decide + AD log), P4 (activation verification), P5 (CLI permission policy), P6 (retarget after a merged base) |
| Q15 | A | One generic unblock judge; the RB, security-exhaustion, project and integration-conflict judges stay and hand over to it when they give up |
| Q16 / Q33 | A | Human-only blocks become a dormant, clearly marked placeholder plus an `ai:operator-step` issue and Telegram; the project continues; activation verification reports DORMANT |
| Q17 | A | The two commands become thin hand-offs that add an `ai:engine-claude` label |
| Q18 | B | The six-vendor reviewer panel is unchanged |
| Q19 | A | Claude-fixer mode = the editor, consolidator, conflict resolver and RB judge run `claude -p` inside the review job; hand-off comments, claims, verdict bot and session sweep are removed |
| Q20 | A | `AI_ENGINE` (default `claude`) plus `AI_ENGINE_<ROLE>`; existing model / `THINKING_LEVEL_*` variables accept Claude model ids; `AI_ENGINE=codex` restores current behaviour exactly |
| Q21 | A | Opus 5.5 at each role's existing thinking level; Sonnet 5.5 for utility roles (summaries, materiality, retro) |
| Q22 | A | All pool accounts ≥ 90% → that run falls back to codex on OpenRouter with a Telegram note |
| Q23 | A | Operator adds `CLAUDE_POOL_TOKEN_<NAME>` to `claude-workers`; a workflow there pushes the pool to the Worker with a dedicated Workers-scoped Cloudflare token (this answer is the §24.G approval) |
| Q24 | A | The broker returns the pool; the job probes each account with Haiku and uses the least-used one under 90% |
| Q25 | A | The broker accepts a caller in `consumer_repos.json` (or coding-workflows) whose `job_workflow_ref` is `shubhodeep1/coding-workflows/.github/workflows/*` at any ref |
| Q28 | A | Delete every `claude/*` branch with no open PR left after Q27 |
| Q29 | A | A retired-files and retired-labels list in the sync removes Claude assets from consumers |
| Q31 | A | Phase breakdown below |
| Q32 | A | Risks below accepted as pending discovery |
| Q34 | A | Superseded material is handled per Q39 |
| Q35 | A | `CLAUDE_FIXER_ENABLED` keeps its name and becomes the switch for the Claude-engine review write roles (§6 repurpose approved) |
| Q36 | — | `/verify-activation` and `/deploy-activate` stay as manual commands run by the operator in Claude Code desktop; their unattended mode is removed |
| Q37 | A | CLAUDE.md §26, §28 and §23.I keep their headings with a short "Retired" stub |
| Q38 | A | The keep / rewrite / retire split in "Approach — Retirement map" |
| Q39 | B | Delete the 7 unimplemented Claude-automation plans, all `docs/implement-plan/*` logs, `docs/operations/master-session.md`, and the `docs/completed/` plans whose subject is the Claude automation |
| Q40 | A | The operator sets `CLAUDE_FIXER_ENABLED=false` on coding-workflows before Phase 0, so this plan's own PRs take the codex autofix and auto-merge path; Phase 5c turns it back on |

## Decisions

Derived from the operator answers above, recorded so the implementer does not
re-decide them.

### D1 — Claude unavailable means codex for that run

- **Chosen:** Any failure to obtain a Claude credential falls back to the codex
  path for that run, logs `AI_ENGINE_FALLBACK reason=<…>`, and sends at most one
  Telegram note per workflow run. Failures include:
  - the broker is unreachable;
  - OIDC is unavailable (fork PRs, or a consumer wrapper without
    `id-token: write`);
  - every account is gated;
  - every probe failed.
- **Alternatives considered:** fail the run; wait for a usage reset.
- **Why:** follows Q22 (never stall). It also makes every phase
  independently mergeable.

### D2 — `ai:codex` and `ai:engine-claude` labels

- **Chosen:** `ai:codex` keeps its existing meaning ("this issue goes to
  codex") and forces `AI_ENGINE=codex` for every role of that work.
  `ai:engine-claude` forces Claude Opus 5.5 at `high` in every role.
  `ai:codex` wins if both are present.
- **Alternatives considered:** retire `ai:codex`.
- **Why:** §6 keeps the existing label's meaning, and Q17 defines the
  Claude label.

### D3 — Model and effort for a Claude role

- **Chosen:** The model is the role's existing model variable if its value
  starts with `claude-`, else the role's Claude default (Opus 5.5, or
  Sonnet 5.5 for utility roles). Effort is the role's existing
  `THINKING_LEVEL_*` / reasoning value, mapped
  `none|minimal → low`; `low|medium|high|xhigh` stay unchanged; `max` is
  allowed. Codex model variables are never rewritten.
- **Alternatives considered:** new `CLAUDE_MODEL_<ROLE>` variables.
- **Why:** Q20 and Q21. Switching back to codex needs no other change.

### D4 — Thin commands ship with the removal

- **Chosen:** Phase 2 rewrites `/implement-plan-claude` and
  `/implement-issue-claude` into thin hand-offs, in the same PR that removes
  the chain they drive. The `ai:engine-claude` label they add stays inert
  until Phase 6.
- **Alternatives considered:** rewrite them in Phase 6.
- **Why:** after Phase 2 the old commands would be broken, and Q31 requires
  every phase to be independently mergeable.

### D5 — Which completed plans are deleted

- **Chosen:** Delete the explicit list in the retirement map. Keep the plans
  of codex fixes that a Claude session delivered.
- **Alternatives considered:** delete every plan produced by a Claude session.
- **Why:** Q39 targets plans whose subject is the Claude automation, and Q8
  keeps the codex fixes.

## Goals

- **G1 — Session automation gone.** After Phase 2 no workflow, script, hook or
  command starts, wakes, schedules, claims for or archives a claude.ai session.
  Verified: `grep -rnE "send_later|create_session|create_trigger|claude-issue-queue|check_in_status|claude_fix_claim|stale_routines|claude_session_janitor"`
  over `.github/ scripts/ .claude/ workflow-templates/` returns only the
  retired-files list and changelog text.
- **G2 — Teardown complete.** The open `claude/*` PRs in coding-workflows
  are closed. The 112 automation and queue issues are closed as not planned.
  The 30 real-work issues are back on codex. Every `claude/*` branch with no
  open PR is deleted, in coding-workflows and in every consumer. The automation
  Routines are deleted and the automation sessions archived.
  Verified by the Phase 1 report counts (re-queried after the work).
- **G3 — Claude everywhere by default.** With no variables set, every model
  role except the six reviewer slots runs `claude -p`. Verified by the
  `claude-engine-smoke.yml` matrix and by `AI_ENGINE_SELECTED role=<r>
  engine=claude` log lines in one real run per role.
- **G4 — Codex one switch away.** `AI_ENGINE=codex` (or `AI_ENGINE_<ROLE>=codex`)
  produces the exact pre-plan command line for that role. Verified by unit tests
  that compare the codex branch of every call site with the pre-plan command
  string.
- **G5 — Model and effort per role.** Each role's model and effort come from its
  existing variables (D3); `ai:engine-claude` work uses `claude-opus-5-5` at
  `high` in every role. Verified by `AI_ENGINE_SELECTED … model=… effort=…`
  log lines and unit tests of the resolver.
- **G6 — Keys in one place.** No repository except `claude-workers` holds a
  `CLAUDE_POOL_TOKEN_*` secret, and no consumer holds any Claude credential.
  Adding an account = adding one secret in `claude-workers`; it is live after
  the next key-sync run (hourly, or dispatch). Verified by a test that greps
  coding-workflows and the templates for `CLAUDE_POOL_TOKEN`, and by the
  broker rejecting a caller outside the allowlist.
- **G7 — No silent stalls.** Every blocked state in the inventory ("Approach —
  Unblock judge") is either handled by an existing judge or by the unblock judge.
  Every terminal state is a closed item with a report and a Telegram CRITICAL,
  or an `ai:operator-step` issue on a project that continues.
- **G8 — Ports land.** P1, P3, P4, P5 and P6 are live, and P2 is part of G7.
  Each is verified by its own tests and one live run.
- **G9 — Consumers clean.** After the first sync following Phase 2, no
  consumer has the retired `.claude/` files or the retired labels. Verified by a
  contents/labels read across the 13 consumers.

## Non-goals

- Changing the six-vendor reviewer panel (Q18).
- Changing the orchestrator's process (waves, judges, security pass,
  validation) beyond adding the engine switch, the unblock judge and the ports.
- Reverting product work in consumer repos (Q9).
- Interactive Claude Code tooling: `/write-plan`, `/investigate-issue`,
  `/analyze-log`, `/apply-url`, `/apply-analysis`, `/audit-plans`, `/seed-repo`,
  `/implement-plan-ai`, `/validate-consumer-issue`, `/verify-activation` and
  `/deploy-activate` (manual), the §21 / §23.H / §25 hooks.
- `claude-branch-review` (reviewer-comment mode for `claude/**` pushes with no
  PR) and `CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS`: they serve interactive
  sessions' pushes and stay.
- Buying or managing Claude accounts; Anthropic API-key billing (Q1).
- DigitalOcean or Cloudflare credentials for model roles.

## Constraints

- **§6 naming.** Every identifier removed here is listed in "Retired
  identifiers" with its current usage; the operator's answers (Q6, Q10–Q12,
  Q19, Q26–Q30, Q35, Q37–Q39) are the §2 approval. Kept for compatibility:
  - the `claude_fixer_converged_head` input of `review_autofix.yml`, declared,
    ignored and marked deprecated, because pinned consumer wrappers pass it;
  - `CLAUDE_FIXER_ENABLED`, repurposed per Q35;
  - `ai:codex`, unchanged meaning (D2);
  - `ai:claude-handoff-failed` and the other retired label *names*, which are
    reserved in the label contract and never reused.

  New identifiers were checked unique on 2026-10-03:
  - Variables: `AI_ENGINE`, `AI_ENGINE_<ROLE>`, `CLAUDE_POOL_TOKEN_<NAME>` (in
    `claude-workers` only), `CF_BROKER_DEPLOY_TOKEN`.
  - Labels: `ai:engine-claude`, `ai:operator-step`, `ai:unblock-closed`.
  - Markers: `<!-- ai:unblock:v1 … -->`, `<!-- ai:auto-decisions:v1 -->`,
    `<!-- ai:activation:v1 … -->`.
  - Files: `.github/ai/claude_engine.json`, `scripts/ai_engine.sh`,
    `scripts/claude_engine.py`.
  - Log prefixes: `AI_ENGINE_SELECTED`, `AI_ENGINE_FALLBACK`, `CLAUDE_POOL`,
    `UNBLOCK_JUDGE`.
- **§10.** No MongoDB collection is touched.
- **§14.** No consumer is added; templates and `.claude/` reach consumers
  through the `@stable` sync. Consumer wrapper `permissions:` gain
  `id-token: write` (Phase 4).
- **§15.** Teardown reads are paginated REST lists. Closes, label edits and
  branch deletions are batched: branch deletions go through `git push origin
  --delete` with up to 100 refs per push, not one API call each. The engine
  adds no GitHub API calls; the broker makes none. The unblock judge scans
  with one search per label set per poll tick.
- **§18.** No manual scripts. Teardown runs from the implementing session
  through the session tools. Everything recurring is a workflow:
  - key sync: hourly cron plus dispatch in `claude-workers`;
  - unblock scan: inside the existing poll tick;
  - activation verification: on merge and on project completion.
- **§19.** No auto-close keywords against `ai:orchestrator-tracking` issues
  in any PR body. Issues are closed through the API.
- **§20.** One `changelog.d/<pr>-<slug>.md` fragment per phase PR.
- **§21.** The implementing session confirms its branch has no merged PR before
  every commit.
- **§22–§24.** Cloudflare Worker deploys are §24.C self-serve. The Worker
  secret is written only by the key-sync workflow. PR closes, issue closes,
  branch deletions, Routine deletions and session archives are §23.C
  operations, approved by this plan's answers (Q11, Q26–Q30). No merge is
  performed by the session (Q40: auto-merge does it).
- **§25 / §26.** The implementing session arms no §26 check-in ("no check-in"
  for this task) and never watches a PR.
- **§27.** `review_autofix.yml` is 458,436 bytes (22 KB under the 480,000 split
  mark). Phase 2 removes the fixer hand-off hunks first. Every Claude branch
  added to a workflow lives in `scripts/`. Each phase PR checks `wc -c` against
  the guard.
- **Security.**
  - Pool tokens are never printed. They are masked the moment the broker
    response is parsed, written only to a `0600` file under `RUNNER_TEMP`,
    and deleted at job end.
  - Sandboxed roles (clarify, review editor) never receive a token inside the
    container; a host-side relay injects it.
  - Workers' `GH_TOKEN` stays the existing `GH_PAT`, and the P5 policy applies.

## Approach

### Execution model

This plan is implemented by **one interactive Claude Code session** that the
operator starts, not by the orchestrator. It works phase by phase. Each phase
is one PR into `main`, except where noted.

The operator sets `CLAUDE_FIXER_ENABLED=false` on coding-workflows before the
session starts (Q40; the session cannot, because the web proxy refuses the
Actions-variables API). With it off, the `claude/*` phase PRs take the normal
review path: reviewer panel, GPT-6-sol editor, RB judge, then auto-merge
after a clean review with green checks. The session:

- says "no check-in" for this task and arms no §26 checker;
- never watches or polls its PRs (§25);
- reports a PR that is still blocked after the RB judge to the operator, and
  fixes it only when asked.

Phases are independently mergeable (D1, D4), so the session can open them
without waiting for earlier ones to merge.

### Retirement map (Q38)

| Area | Retire | Rewrite | Keep |
|---|---|---|---|
| CLAUDE.md (and its `workflow-templates/CLAUDE.md` symlink) | §26, §28, §23.I bodies → stubs (Q37) | §0 exception, §12 preamble/§12.G cross-refs, §23.B (§26 references), §25.B/§25.C (§26 references), FINAL REMINDER (§28 reference) | everything else; section numbers unchanged (`tests/test_claude_md_section_numbers.py`) |
| agents.md | "Interactive post-push PR status check-in" (933–1192), "Unattended helpers and permission prompt reports (CLAUDE.md §23.I)" (1193–1250) | Claude-fixer paragraph in "Workflow architecture" (§§ around 80–150); "Interactive slash-command model selection" (809–932) references; "Models in use" table (adds the engine column) | rest |
| README.md | Claude issue intake / queue / pickup / dispatcher material; `CLAUDE_ISSUE_ROUTINE_TOKEN` row; implement-plan lessons paragraph (Memory System) | Required Variables (engine, broker, fixer) | rest |
| `.claude/commands/` + twins | `claude-issue-pickup.md` (no twin), `claude-issue-dispatch.md`, `fix-claude-pr.md` | `implement-plan-claude.md` (thin hand-off), `implement-issue-claude.md` (thin hand-off), `verify-activation.md` and `deploy-activate.md` (remove the `— unattended` mode and chain references; manual use only) | `analyze-log`, `apply-analysis`, `apply-url`, `audit-plans`, `implement-plan-ai`, `investigate-issue`, `seed-repo`, `validate-consumer-issue`, `write-plan` |
| `.claude/scripts/` + twins | `check_in_status.py`, `claude_fix_claim.py`, `stale_routines.py`, `permission_prompts.py`, `dispatch_workflow.py`, `edit_comment.py` | `security_pass_skip.py` → trust rules move to `scripts/security_pass_skip.py` for P1, `.claude/` copy retired | — |
| `.claude/hooks/` + twins, `.claude/settings.json` | `pr_check_in_reminder.py`, `permission_prompt_logger.py`, their settings entries, allow rules that exist only for retired helpers | `session-start.sh` (remove Claude-automation-only output) | `gh_api_write_guard.py`, `pr_merge_status_guard.py`, `pr_watch_guard.py` |
| `scripts/` | `claude_issue_handoff.sh`, `claude_issue_intake.sh`, `claude_issue_queue_watchdog.sh`, `claude_issue_route.py`, `claude_pr_sweep.py`, `claude_session_janitor.py`, `ingest_implement_plan_lessons.py`, `review_autofix_step_claude_fixer_handoff.sh` | — | — |
| Workflows | `claude-issue-intake.yml`, `claude-issue-queue-watchdog.yml`; `claude-pr-catch-all` job of `review_autofix_sweep.yml`; Claude routing step in `clarify.yml` (around 420–560); fixer hand-off branch in `review_autofix.yml` (gate around 760–800 and hand-off steps); lessons step in `issue_pr_status.yml` (538–570); `claude/implement-plan-*` target authorisation in `validate.yml` (#4734, #4791) | `workflow-templates/review_rb_judge_dispatch.yml`, `workflow-templates/ai-review.yml` (stop passing `claude_fixer_converged_head`) | — |
| Tests | `test_check_in_session_targeting`, `test_check_in_status`, `test_check_in_status_hand_back`, `test_claude_issue_route`, `test_claude_pr_sweep`, `test_claude_session_janitor`, `test_dispatch_workflow`, `test_edit_comment`, `test_implement_issue_claude_command`, `test_implement_plan_claude_command`, `test_ingest_implement_plan_lessons`, `test_permission_prompts`, `test_pr_check_in_reminder`, `test_security_pass_skip` (moved), `test_stale_routines`, `test_session_titles`, `test_review_autofix_claude_fixer_mode` (rewritten in 5c) | registrations in `ci.yml`, `mark-stable.yml` (≈436–455), `test-and-mark-stable.yml` "Claude asset tests" (≈4451–4475) | `test_claude_md_section_numbers`, `test_check_integration_pr_readiness`, guard tests |
| Labels (`.github/ai/label_contract.v1.json` 212–240) | `ai:claude`, `ai:claude-blocked`, `ai:claude-handoff-failed`, `ai:claude-issue-queue`, `ai:claude-issue-queue-stale`, `ai:permission-prompt` | — | `ai:codex` (D2) |
| Docs (Q39) | `docs/plans/`: `claude-actions-worker-pool`, `claude-codex-process-parity`, `claude-multi-account-pool`, `claude-fixer-unattended-convergence`, `retire-master-session`, `move-checking-roles-to-claude`, `claude-release-track` (`-plan.md`); all of `docs/implement-plan/`; `docs/operations/master-session.md`; `docs/completed/`: `e2e-dummy-implement-plan-claude`, `issue-4550-…`, `4620`, `4621`, `4622`, `4623`, `4734`, `4787`, `4791`, `4798`, `4886`, `4887`, `4948`, `4990`, `5293`, `5664` (`-plan.md`) | — | `docs/completed/` plans of kept work (4618, 4619, 4653, 4665, 4707, 4723, 4891, 5119, 5627); `confine-human-steps-to-activation-plan.md`; `claude-code-tooling-learnings-plan.md` |
| `changelog.d/` | unreleased fragments whose subject is removed code: `4586-checker-action-field`, `4609-claude-pr-deterministic-skip`, `4734-validate-stacked-and-stable-targets`, `4787-checker-renames-only-itself`, `4798-standalone-helper-calls`, `4869-conflict-sweep-skips-draft-claude-prs`, `4886-numbered-session-titles`, `4887-archive-finished-sessions`, `4948-protected-path-twin-first-default`, `4990-pickup-throughput`, `5293-unchained-git-sync-calls`, `5889-release-gates-claude-assets`, `check-in-session-depth-limit`, `claude-issue-routed-alert-silent` (re-check each against the tree at implementation time) | — | the rest |

#### Retired identifiers (§6)

| Identifier | Kind | Current usage |
|---|---|---|
| `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`, `CLAUDE_FIXER_VERDICT_BOT_LOGIN` | repo vars | fixer hand-off / verdict trust in `review_autofix.yml`, `check_in_status.py` |
| `CLAUDE_FIX_CLAIM_LEASE_HOURS`, `CLAUDE_FIX_HAND_BACK_CAP`, `CLAUDE_PR_SWEEP_MIN_AGE_HOURS` | repo vars | `review_autofix_sweep.yml:377-381`, claims, catch-all |
| `CLAUDE_ISSUE_ROUTED_ALERT_LEVEL`, `CLAUDE_ISSUE_UPSTREAM_REPO`, `CLAUDE_ISSUE_ROUTINE_ID`, `CLAUDE_ISSUE_QUEUE_STALE_HOURS`, `AI_ISSUE_IMPLEMENTER` | repo vars | `clarify.yml` routing, intake, watchdog |
| `CLAUDE_PR_SWEEP_QUEUE_TOKEN`, `CLAUDE_ISSUE_QUEUE_TOKEN`, `CLAUDE_ISSUE_ROUTINE_TOKEN` | secrets / env | queue producers (already deprecated for the routine token) |
| `<!-- ai:claude-fixer-handoff:v1 … -->`, `<!-- ai:claude-fixer-verdict:v1 … -->` (+ v2 digest), `<!-- ai:claude-fix-claim:v1 … -->`, `<!-- ai:claude-issue-progress:v1 -->`, `<!-- ai:permission-prompt:v1 … -->`, `claude_pr_fix.v1` / queue payload markers | comment markers | fixer, claims, queue |
| `claude-pr-catch-all` | job id | `review_autofix_sweep.yml` |

Variables and secrets themselves are deleted from repository settings by the
operator only if wanted (the session cannot reach the variables API).
Unused ones are harmless.

### Claude engine (Phases 3, 5)

- **Selection.** `scripts/ai_engine.sh` provides `ai_engine_for_role <role>`.
  It returns `codex` or `claude` in this order:
  1. the work-item labels (`ai:codex` beats `ai:engine-claude`, D2);
  2. `AI_ENGINE_<ROLE>`;
  3. `AI_ENGINE`;
  4. the code default.

  The code default is `codex` until a role's cutover phase sets that role's
  default to `claude`. After Phase 5d every role defaults to `claude`. It also
  provides `ai_engine_model <role>` / `ai_engine_effort <role>` (D3), and
  logs `AI_ENGINE_SELECTED role= engine= model= effort= source=`.
- **Role names** (the `<ROLE>` in `AI_ENGINE_<ROLE>`):
  - Clarify and plan: `CLARIFY`, `CLARIFY_RESPOND`, `PLAN`.
  - Implement: `IMPLEMENT`, `IMPLEMENT_REPAIR`, `IMPLEMENT_DIAGNOSE`.
  - Orchestrator: `ORCHESTRATE`, `WAVE_JUDGE`, `STALL_JUDGE`,
    `INTEGRATION_JUDGE`, `SECURITY_JUDGE`, `UNBLOCK_JUDGE`.
  - Review write roles: `REVIEW_EDITOR`, `REVIEW_CONSOLIDATOR`,
    `CONFLICT_RESOLVER`, `RB_JUDGE`.
  - Validation: `VALIDATE`, `VALIDATE_SELF_HEAL`, `VALIDATION_REFRESH`.
  - Other single roles: `SECURITY_AUDIT`, `CHECK_TRIAGE`, `WORKFLOW_HEAL`,
    `ACTIVATION_VERIFY`.
  - Log analysis: `LOG_ANALYSIS`, `LOG_AUDIT`, `LOG_SUMMARY`, `RETRO`.
  - Review utility roles: `MATERIALITY`, `SUMMARISER`, `BEHAVIOURAL_SMOKE`.

  The six reviewer slots have no engine switch (Q18).
- **Utility roles on Sonnet 5.5** (Q21): `LOG_SUMMARY`, `RETRO`,
  `MATERIALITY`, `SUMMARISER`, `BEHAVIOURAL_SMOKE`. Every other role uses Opus 5.5.
- **Invocation.** `claude_run <role> <prompt_file> <out_file> <workdir>
  [session_id]` in `scripts/ai_engine.sh` runs:

  ```
  claude -p --model <m> --effort <e> \
    --system-prompt-file <support>/unattended_system_instructions.md \
    --setting-sources "" --settings <run>/claude-settings.json \
    --strict-mcp-config --disable-slash-commands \
    --exclude-dynamic-system-prompt-sections \
    --tools <role tool set> --permission-mode <role mode> \
    --output-format stream-json --verbose [--session-id|--resume <id>] \
    < <prompt_file> > <run>/transcript.jsonl
  ```

  `scripts/claude_engine.py extract` writes the final `result` text to
  `<out_file>`, the same file the codex path writes, so downstream parsing is
  unchanged. Write roles use `--permission-mode bypassPermissions` together
  with the P5 deny rules in `--settings`. Read-only roles use `--tools`
  limited to `Read,Grep,Glob` plus the specific Bash patterns they need.
  `--session-id` / `--resume` replace `codex_thread_reuse.sh`'s
  `exec resume` for implement retries inside one job.
- **Context gate (Q32).** `--bare` cannot be used because it never reads OAuth
  credentials (`claude --help`, CLI 2.1.288). The Phase 3 smoke run asserts
  that the start-up input tokens of a no-op run are below 25,000 and that a
  marker string placed only in `CLAUDE.md` is not visible to the model. If
  CLAUDE.md still loads, `claude_run` moves the checkout's `CLAUDE.md` into
  `RUNNER_TEMP` for the call and restores it in a `trap`; commit steps
  already stage explicit paths. The smoke run then re-asserts.
- **Failure classification.** `claude_engine.py classify` maps the transcript to
  `success | auth_failed | usage_limit | crashed | timeout`, following the pool
  plan's spike evidence S7 and S15. `usage_limit` and `auth_failed` re-run on
  the next account (that account excluded), up to the pool size. Then D1
  applies and the codex path runs. `crashed` / `timeout` follow the role's
  existing retry rules unchanged.
- **Stall guard.** `claude_run` is wrapped by the existing
  `codex_stall_guard.sh` / `codex_heartbeat.sh`. They watch output growth,
  which stream-json provides, and keep their names (§6). A
  `--engine claude` flag only changes the log label.
- **Telemetry.** `cost_audit.py` learns the stream-json `result` fields
  (`total_cost_usd`, `usage`, `modelUsage`) beside the codex "tokens used"
  line.
- **Sandboxed roles.** Clarify (`clarify_isolated_run.sh`) and the review
  editor (`review_untrusted_sandbox.sh`) already run the model inside a
  network-isolated container behind a host broker. Phase 3 adds
  `scripts/claude_anthropic_relay.py`, which follows `clarify_openrouter_broker.py`:
  - it listens on the same unix socket pattern;
  - it forwards to `https://api.anthropic.com` and swaps a dummy bearer for the
    real OAuth token held on the host;
  - the container gets `ANTHROPIC_BASE_URL=http://127.0.0.1:8765` and the dummy
    token, so the real token never enters the container.

  **Gate:** the smoke run proves one clarify and one editor call through the
  relay. If the relay cannot authenticate, the implementing session stops and
  asks the operator before Phase 5a/5c ships those roles. The other roles do
  not depend on it.
- **Install.** `.github/actions/install-claude` (pinned
  `@anthropic-ai/claude-code` version from `.github/ai/claude_engine.json`,
  Node 22, npm cache) mirrors `install-opencode`.

### Token broker (Phase 4)

- **Worker** `claude-pool-broker` on the `FT_GAMES_CF` account,
  `*.workers.dev` route. Source: `tools/claude-pool-broker/` in
  coding-workflows (reviewed, tested). The implementing session deploys it
  with `wrangler deploy` (§24.C) and records it in agents.md
  "Cloudflare resources" (§24.F).
- **Request:** `POST /v1/pool` with `Authorization: Bearer <GitHub OIDC
  token>` (audience `coding-workflows-claude-pool`). The Worker:
  - verifies the JWT against `https://token.actions.githubusercontent.com/.well-known/jwks`
    (signature, `iss`, `aud`, `exp`, `nbf`, `iat` ≤ 10 min old);
  - requires `repository_owner == "shubhodeep1"`;
  - requires `repository` ∈ `{shubhodeep1/coding-workflows} ∪ consumer_repos.json`.
    It fetches the list from `raw.githubusercontent.com/shubhodeep1/coding-workflows/main/.github/ai/consumer_repos.json`
    and caches it for 10 minutes, keeping the last good copy if a fetch fails;
  - requires `job_workflow_ref` to start with
    `shubhodeep1/coding-workflows/.github/workflows/` (Q25).

  On success it returns `{"accounts":[{"name":…,"token":…}],"probe_model":…,"gate":0.9}`.
  On failure it returns `403` with a reason code; `5xx` triggers D1. It
  writes no logs that contain tokens.
- **Storage:** one Worker secret `CLAUDE_POOL_TOKENS` (JSON array), written only
  by the key-sync workflow.
- **Key sync:** `claude-workers/.github/workflows/claude-pool-key-sync.yml`
  runs hourly, on `workflow_dispatch`, and on push to `main`. Each run:
  1. reads `toJSON(secrets)` keys matching `^CLAUDE_POOL_TOKEN_[A-Z0-9_]+$`
     (spike S1);
  2. normalises each token: strips whitespace and masks it (spike S3);
  3. writes the whole array with
     `PUT /accounts/{id}/workers/scripts/claude-pool-broker/secrets`,
     authenticated with `CF_BROKER_DEPLOY_TOKEN` (`<account_id>:<token>`,
     Workers Scripts Edit only).

  Deleting an account secret removes that account on the next run.
- **Client:** `.github/actions/claude-pool-token`, used once per job that has
  a Claude role. It:
  1. requests the OIDC token (`ACTIONS_ID_TOKEN_REQUEST_URL`, audience above);
  2. calls the broker and masks every token;
  3. runs the Haiku probe per account (`claude -p --model claude-haiku-4-5-20251001
     "Reply OK" --output-format stream-json --verbose --strict-mcp-config
     --setting-sources ""` in an empty dir, spike S8) and reads the
     `five_hour` / `seven_day` utilisation;
  4. writes the least-used account under the gate (ties go to the
     alphabetically first name) to `RUNNER_TEMP/claude-pool/token` (mode 0600)
     plus the ordered fallback list.

  It outputs `available=true|false` and `reason`, then deletes the files in a
  post step. `available=false` → D1.
- **Permissions:** every reusable workflow job that contains a Claude role gets
  `id-token: write`. Every `workflow-templates/ai-*.yml` wrapper that calls one
  gets `id-token: write` in its `permissions:`, propagated by the sync.
  Consumers whose sync is stale fall back via D1 until they sync:
  `tele-funtoken-msg-scoring` (update workflow disabled),
  `fun-token-multi-chain` (update runs failing since 2026-09-30),
  `btc_sweeper` (`@main` pins) and `atlas-bridge.gd` (`@stable` name pins).

### `/implement-plan-claude`, `/implement-issue-claude` and the engine label (Phases 2, 6)

- **`/implement-plan-claude <plan>`** follows `/implement-plan-ai`'s procedure.
  It dispatches the orchestrator with the plan as `project_description` and
  the new optional input `engine: claude`. `orchestrate.yml` then labels the
  tracking issue `ai:engine-claude`, and the orchestrator copies the label to
  every sub-issue and PR it creates (Phase 6).
- **`/implement-issue-claude <issue>`** adds `ai:engine-claude` to the issue
  and posts `/reclarify`, so the standalone pipeline (re)starts with the label
  in force.
- **Label effect.** `ai_engine.sh` reads the label from the issue, the PR,
  or the PR's linked issue. Every role of that work runs Claude
  `claude-opus-5-5` at `high`. On the review job, that is Claude-fixer mode
  (Q19/Q35), unless `CLAUDE_FIXER_ENABLED=false`.

### Claude-fixer mode (Phase 5c, Q19, Q35)

`CLAUDE_FIXER_ENABLED` (default `true`) now means: on review runs for
`ai:engine-claude` work, and on any review run where
`ai_engine_for_role REVIEW_EDITOR` resolves to `claude`, the editor,
consolidator, conflict resolver and RB judge run through `claude_run` inside
the review job. The review flow is otherwise unchanged:

- push and re-trigger tail;
- `MAX_AUTOFIX_ITERATIONS`;
- RB judge dispatch;
- auto-merge after a clean review with fresh checks.

`false` forces those four roles to codex/OpenCode regardless of
`AI_ENGINE`. The `claude/*` head special case and the hand-off,
verdict-bot, claim and catch-all machinery are gone (Phase 2), so a
`claude/*` PR is reviewed like any other PR.

### Unblock judge (Phase 7, Q15, Q16/Q33, P2)

- **Scan.** One new step in `orchestrate_poll_process.sh`'s tick. Per tick it
  makes one batched REST search for open issues and PRs carrying:
  - `ai:blocked`, `ai:needs-human`, `ai:scope-blocked`, `ai:destructive-blocked`;
  - `ai:harness-broken`, `ai:security-pass-failed`, `ai:validation-failed`,
    `ai:resolver-escalated`, `*-failed`;
  - plus failed projects in state.

  It picks items blocked ≥ 30 minutes whose latest `<!-- ai:unblock:v1 … -->`
  marker is older than 6 hours or absent, and dispatches
  `unblock_judge_dispatch.yml` (modelled on `review_rb_judge_dispatch.yml`),
  at most 5 per tick.
- **Inputs.**
  - Block record: kind, reason code, label, item.
  - Fingerprint: stop id plus a normalised failure signature, as in
    `escalation_ledger.py` on PR #5164.
  - Evidence:
    - the last 20 comments;
    - the failing run log tail (≤ 400 lines);
    - the PR diff (≤ 120 KB);
    - the state slice.
  - Prior `ai:unblock:v1` verdicts.
  - The project spec, marked untrusted.
- **Verdicts:**
  - `retry_budget`: name a narrower fix; +1 attempt for this fingerprint only.
  - `auto_answer`: take the RECOMMENDED option and record an AD entry.
  - `descope`: revert the failing part, record it, and re-run the security pass
    and validation.
  - `override_guard`: scope/destructive guards only. Paths are audited and
    never include `.github/workflows/**`, `.claude/**` or `scripts/**` in
    coding-workflows.
  - `reissue`.
  - `accept_with_followup`: the existing waiver path.
  - `operator_step`: Q33.
  - `close`.
- **The system acts on each verdict itself.** It reuses the existing
  `/judge_resume`, `/revalidate`, `/re-security-pass`, `/approved` and
  `/answer` handlers by posting the command as the trusted pipeline identity.
- **Hard limits.**
  - Never repeat a verdict for the same fingerprint.
  - At most 2 rounds per item and 6 per project.
  - Never waive a security pass, never mark validation passed, never merge past
    a failing required check, never invent a credential, SHA or secret value.
- **`operator_step` (Q33).** For a block only a human can clear (a missing
  credential, a §22.B / §23.C / §24.D operation):
  1. The judge completes the step dormant: a feature flag defaulting off, or a
     documented placeholder env var named `…_UNSET_OPERATOR_STEP`.
  2. It opens one `ai:operator-step` issue with exact instructions and sends
     Telegram WARNING.
  3. It unblocks the item. The project continues, and activation verification
     (P4) reports the feature DORMANT until the operator acts.
- **Terminal.** When the caps are reached, or the item is still blocked 24
  hours after the last round, the judge:
  - closes it as not planned with a report and `ai:unblock-closed`;
  - for a project, closes the tracking issue through the API (§19) and sets
    the state to `abandoned`;
  - sends one Telegram CRITICAL.
- **Subsumes** these existing stops:
  - stall-judge `escalate_human`;
  - the clarify loop breaker (`orchestrate_parse_and_post_answer.sh:158-186`);
  - `BLOCKED:` verdicts;
  - the scope/destructive latches;
  - `*-failed` dead ends;
  - `MAX_FINAL_MERGE_ATTEMPTS`, `ai:force-merge`;
  - project-judge terminals (`MAX_JUDGE_CYCLES`, `MAX_RECOVERY_ATTEMPTS`,
    `JUDGE_REPEAT_FINGERPRINT_MAX`, unparseable judge output);
  - validation terminals (`harness_error`, `infeasible`, recovery exhausted,
    `MAX_VALIDATE_CYCLES`);
  - the security-pass bypass paths (P:5053, P:5290, P:5334);
  - the `MAX_MERGE_DEFERRALS` alert;
  - RB-judge give-ups (unparseable twice, refused `merge_with_followup`);
  - the side-pipeline escalations (`check_failure_triage.sh:94`,
    `workflow_failure_heal_intake.sh:126`).

  It also fixes the wave wedge: an orchestrated issue with a human-only label
  is now judged instead of holding its wave forever.

### Ports (Phase 8, Q14)

- **P1 — single-issue security pass.** After a standalone PR's review is
  clean and before auto-merge, `review_autofix.yml` dispatches
  `security-audit.yml` with `ref` = the PR head. That is one step whose body
  lives in `scripts/`.
  - Findings open follow-up issues with `Integration branch: <head>`, fixed by
    the normal pipeline.
  - The cycle cap is 5, using the orchestrator's `MAX_SECURITY_PASS_CYCLES`.
  - Skip rules come from `scripts/security_pass_skip.py`, moved from
    `.claude/scripts/` with the #4623 verified-trust rules, so follow-ups of
    follow-ups do not recurse.
  - Exhaustion goes to the existing security-exhaustion judge, then the
    unblock judge.
- **P3 — standalone clarify auto-decide.** When `clarify.yml` posts questions
  on a standalone issue, it immediately posts the answer built from each
  question's RECOMMENDED option, through the orchestrator's existing
  auto-answer path. It also writes one `<!-- ai:auto-decisions:v1 -->` comment
  listing `AD-<n>` entries (question, pick, alternatives, why). That comment is
  updated in place and repeated in the merged PR's body. The 60-minute
  stall-ladder auto-answer remains as a backstop.
- **P4 — activation verification.** A job runs in `issue_pr_status.yml` on
  merge of a standalone PR, and on project completion in the poller.
  - It runs the `ACTIVATION_VERIFY` role with the adapted
    `prompts/mode-activation-verify.txt`, built from `verify-activation.md`'s
    scope table. It grades LIVE / DORMANT and lists gaps.
  - Code-fixable gaps open one issue routed through the pipeline.
  - Operator gaps open or extend the `ai:operator-step` issue.
  - It posts `<!-- ai:activation:v1 verdict=… -->` and sends Telegram.
- **P5 — CLI permission policy** (Phase 3). `scripts/claude_settings.json.tmpl`
  is rendered per run:
  - `PreToolUse` hook → `gh_api_write_guard.py` from the support checkout;
    headless "ask" becomes a denial (spike S14).
  - Deny rules: `Bash(gh pr merge*)`, `Bash(gh api * -X DELETE*)`,
    `Bash(git push --force*)`, `Bash(git push * --delete*)`,
    `Edit|Write(//<checkout>/.github/workflows/**)` unless
    `ALLOW_WORKFLOW_EDITS=true`, `Edit|Write(//<checkout>/.claude/**)`.
  - An `env` block with no session tokens.
- **P6 — retarget after a merged base.** Before implement and before each
  review run, `scripts/retarget_merged_base.sh` checks whether the PR's base
  branch is the head of a merged PR. If so, it retargets the PR to that PR's
  base with one `PATCH`. It re-checks the base read when the PR is not stacked
  (§15).

### Alternatives considered

- **Snapshot to `db0d0fc6` and cherry-pick:** rejected (Q7). The dry-run
  evidence is in Context.
- **Secret copies in every consumer:** rejected (Q2).
- **Central execution in `claude-workers`** (the superseded worker-pool plan):
  rejected. It splits every stage across two repos and bills minutes twice.
- **Organization on the Team plan:** rejected. It means transferring 15 repos,
  and org secrets don't reach private repos on Free / Pro.
- **Anthropic API key with `--bare`:** rejected (Q1, subscription pool).

## Phases & Merge Strategy

Each phase is one PR into `main` of coding-workflows unless stated, merged by
the review pipeline's auto-merge (Q40). Every phase is safe to merge in any
order:

- the engine falls back to codex whenever Claude is unavailable (D1);
- role defaults move to `claude` only inside their own cutover phase;
- the commands are rewritten together with the chain they lose (D4);
- operational phases (0, 1) change GitHub objects, not code paths other phases
  depend on.

**0. Freeze.**
- Scope: the operator sets `CLAUDE_FIXER_ENABLED=false`. The session deletes
  the automation Routines and archives the automation sessions (Q12/Q30), and
  opens one small PR flipping `scripts/claude_issue_route.py`'s default
  implementer to `codex`, so no new issue enters the Claude queue.
- Done: `list_triggers` shows none of the automation Routine names;
  `list_sessions` shows none of the automation titles un-archived; the PR is
  merged; a new test issue is routed `codex`.
- Rollback: revert the PR. Routines and sessions are not restored, by design.

**1. Teardown of GitHub objects** (operational, no PR).
- Scope: close PRs (Q27), close or return issues (Q26), delete branches (Q28)
  in coding-workflows, `claude-workers` and consumers.
- Done: the final counts report in the session; G2.
- Rollback: closed PRs and issues can be reopened. Deleted branches are
  recoverable from the session's saved list of `ref → sha` with `git push origin
  <sha>:refs/heads/<ref>` for 90 days, while GitHub keeps the objects.

**2. Forward removal.**
- Scope: the retirement map, Q39 docs, the retired-files and retired-labels
  lists for the sync, the thin commands (D4), test-registry updates.
- Files: see Implementation Steps.
- Done:
  - G1 grep clean;
  - CI green;
  - `review_autofix.yml` smaller than before;
  - `tests/test_claude_md_section_numbers.py` green;
  - consumers clean after the next stable sync (G9).
- Rollback: revert the PR (consumers keep whatever the sync removed until the
  next sync re-adds from a reverted stable).

**3. Claude engine plumbing (inert).**
- Scope: `install-claude`, `ai_engine.sh`, `claude_engine.py`,
  `claude_anthropic_relay.py`, the P5 settings template,
  `.github/ai/claude_engine.json`, `claude-engine-smoke.yml`
  (dispatch-only). No call site changes; every role's code default stays
  `codex`.
- Done: unit tests; smoke run green on the context gate, the relay gate and a
  no-op per tool profile (with a token from the broker if Phase 4 is in,
  otherwise skipped with `available=false` asserted).
- Rollback: revert.

**4. Token broker.**
- Scope: Worker source and tests, the deploy (session, §24.C), the
  `claude-pool-token` action, the `claude-workers` key-sync workflow (its own
  PR in `claude-workers`, merged by the operator), `id-token: write` in the
  reusable jobs and wrapper templates.
- Done:
  - the Worker answers 403 to a non-allowlisted caller in its tests;
  - the smoke run in coding-workflows gets `available=true` and probes ≥ 1
    account;
  - key sync shows N accounts after a dispatch.
- Rollback: revert; D1 sends every role to codex.

**5. Role cutovers** (four PRs; each sets its roles' code default to `claude`
and wires the call sites to `ai_engine.sh`, with the codex branch unchanged):
- **5a** clarify, clarify-respond, plan
  (`clarify.yml`, `orchestrate_clarify_respond.yml`, `clarify_isolated_run.sh`,
  `plan.yml`, `scripts/run_plan_codex.sh`).
- **5b** implement, repair, diagnose
  (`implement.yml`, `codex_thread_reuse.sh` call path,
  `implement_diagnose_post_codex_failure.sh`).
- **5c** orchestrator judges and review write roles plus Claude-fixer mode:
  - `orchestrate.yml` and `orchestrate_poll_process.sh` (sites around
    6389, 8569, 14444, 20563, 22472);
  - `review_apply_fixes.sh`, `review_consolidate.sh`,
    `review_conflict_resolve.sh`, `review_rb_judge.sh`;
  - `review_untrusted_sandbox.sh`;
  - `review_autofix.yml` env;
  - the operator sets `CLAUDE_FIXER_ENABLED=true` (or deletes the variable)
    after merge.
- **5d** everything else:
  - `validate_process.sh`, `self_heal_validation.sh`, `validation-refresh.yml`,
    `validation_discovery_bootstrap.py`;
  - `security_audit.sh`, `check_failure_triage.sh`,
    `workflow_failure_heal_intake.sh`;
  - `workflow-log-analysis.yml`, `workflow_retro_fanout.sh`;
  - the review utility roles.

For each: done = one live run per role logs `AI_ENGINE_SELECTED … engine=claude`
and succeeds, plus `AI_ENGINE_<ROLE>=codex` on a second run logs the codex
command; rollback = revert, or set `AI_ENGINE_<ROLE>=codex`.

**6. Engine label plumbing.**
- Scope: `orchestrate.yml` `engine` input plus label propagation, label in the
  contract, `ai_engine.sh` label resolution, wrapper template input.
- Done: a dispatched test project's sub-issue PR logs `model=claude-opus-5-5
  effort=high` in every role.
- Rollback: revert; the label is inert.

**7. Unblock judge.**
- Scope: scan step, `unblock_judge_dispatch.yml`, `scripts/unblock_judge.sh`,
  `prompts/mode-judge-unblock.txt`, the ledger, terminal handling, the
  `ai:operator-step` path, the hand-over from existing judges.
- Done: unit tests per verdict and cap; a fixture issue labelled `ai:blocked`
  gets one verdict and the action executes; a second identical block cannot get
  the same verdict.
- Rollback: revert, or set `UNBLOCK_JUDGE_ENABLED=false` (new var, default `true`).

**8. Ports** (four PRs):
- **8a** P1 single-issue security pass;
- **8b** P3 standalone clarify auto-decide plus AD log;
- **8c** P4 activation verification;
- **8d** P6 retarget.

P5 ships in Phase 3. Each has its own kill switch with default on:
`SINGLE_ISSUE_SECURITY_PASS_ENABLED`, `STANDALONE_AUTO_DECIDE_ENABLED`,
`ACTIVATION_VERIFY_ENABLED`, `RETARGET_MERGED_BASE_ENABLED`. Done = tests plus
one live run each; rollback = revert or the switch.

## Implementation Steps

### Phase 0 — freeze

1. Confirm with the operator that `CLAUDE_FIXER_ENABLED=false` is set on
   coding-workflows (the session cannot read Actions variables; ask once).
2. `list_triggers` (`include_completed: false`, all pages). Delete every
   Routine whose name matches the automation patterns already encoded in
   `.claude/scripts/stale_routines.py` (read it before Phase 2 deletes it):
   - `Claude issue pickup: …`
   - `PR #<n> status check-in…`
   - `PR #<n> hand-back`
   - `implement-plan <slug>: …`
   - `dispatch <owner>/<repo>#<n>: …`

   Print the deleted names. If the operator's second claude.ai account holds
   Routines (`docs/operations/master-session.md` "ownership"), list them in
   the report as an operator step for that account.
3. `list_sessions` (`mine: true`, all pages). Archive every un-archived
   session whose title matches the automation patterns in
   `scripts/claude_session_janitor.py` plus:
   - `PR #<n> status check-in`
   - `implement-plan <slug> — <stage>`
   - `Claude issue pickup`
   - `PR #<n> — fix …`

   Never archive the session running this plan, or any session whose title
   matches none of these. Print the list.
4. PR: `scripts/claude_issue_route.py` `normalize_implementer_var` default
   `claude` → `codex`; update its test; changelog fragment.

### Phase 1 — teardown

1. Re-query open PRs in coding-workflows. Close every `claude/*`-head PR
   (comment "Closed: the Claude session automation is retired, see
   docs/plans/replace-claude-sessions-with-cli-engine-plan.md"). Skip the PR of
   this plan's own work and any PR the operator names. Close `claude-workers`
   PR #1.
2. Re-query open issues. Classify each with the rules from the planning
   inventory:
   - queue / meta: `ai:claude-issue-queue*`, `ai:permission-prompt`;
   - automation-infra: an issue about the Claude automation, including
     security-audit findings against retired files;
   - real work: everything else carrying `ai:claude*`.

   Print the classification table before acting. Then:
   - close queue/meta and automation-infra as not planned, with the comment
     above;
   - for real work, remove the `ai:claude*` labels, add `ai:codex` (D2) and
     comment `/reclarify`.
3. Branches. For coding-workflows, `claude-workers` and the 13 consumers:
   1. list `claude/*` heads with `git ls-remote`;
   2. subtract heads with an open PR (keeps tele's 10 product PRs and this
      plan's branch);
   3. save the `ref sha` lines to the session scratchpad and post them as a
      collapsed block in the Phase 1 report comment on this plan's PR, so any
      branch can be restored;
   4. delete with `git push origin --delete` in batches of ≤ 100 refs per
      push.
4. Delete the retired labels in coding-workflows now (one `DELETE` per label;
   six labels). Consumers get it through Phase 2's retired-labels list.
5. Report final counts against G2.

### Phase 2 — forward removal

1. Delete every file in the "Retire" column of the retirement map (both the
   `.claude/` tree and its `workflow-templates/.claude/` twin), the Q39 docs
   and the listed `changelog.d/` fragments (re-check each against the tree).
2. `clarify.yml`: remove the Claude routing step and the hand-off (around
   420–560), so every standalone issue continues to plan as before #4443.
   Keep the trusted-author gate.
3. `review_autofix.yml`:
   - remove the Claude-fixer hand-off gate and steps;
   - keep the input `claude_fixer_converged_head` declared, with description
     "Deprecated; ignored", and drop its uses;
   - keep `CLAUDE_FIXER_ENABLED` read (Phase 5c gives it meaning; until then
     it is read and unused);
   - remove the `claude/*` auto-merge special cases (#4609 deterministic
     skip) so `claude/*` PRs behave like other PRs;
   - `wc -c` must drop.
4. `review_autofix_sweep.yml`: remove the `claude-pr-catch-all` job.
   `issue_pr_status.yml`: remove the lessons-ingestion step (538–570).
   `validate.yml`: remove the `claude/implement-plan-*` target authorisation
   added by #4734 / #4791, keeping any generic stable-target logic that
   predates them.
5. `.github/ai/label_contract.v1.json`: move the retired labels to a
   `"retired_labels"` array, which `scripts/ai_labels.py sync-labels` learns to
   delete in a consumer when present. Add `"retired_files"` handling to
   `update_workflows.yml`: a list of consumer paths it deletes when they exist
   and are byte-identical to any released version, or to a sha256 list in the
   manifest. Consumer-modified copies are left and logged. Put the manifest in
   `workflow-templates/retired_files.txt`. Its entries are the retired
   `.claude/` commands, scripts and hooks.
6. CLAUDE.md:
   - replace the bodies of §26, §28 and §23.I with a stub:
     `Retired on <date>. The session-based Claude automation was replaced by
     the Claude CLI engine in the Actions pipelines
     (docs/plans/replace-claude-sessions-with-cli-engine-plan.md). Section
     number kept per §6.`
   - update the cross-references listed in the retirement map;
   - remove §26 text from `.claude/settings.json` hook descriptions.
7. `.claude/settings.json` and the twin: remove the retired hooks and allow
   rules.
8. Rewrite `implement-plan-claude.md` and `implement-issue-claude.md` (and
   twins) as thin hand-offs, per "Approach — engine label". Strip the
   unattended mode from `verify-activation.md` and `deploy-activate.md`.
   Ensure `.claude/commands/claude-issue-pickup.md` is deleted (it has no twin).
9. Move `security_pass_skip.py` to `scripts/` with its test (the logic is
   reused in 8a).
10. Remove the retired tests and their registrations in `ci.yml`,
    `mark-stable.yml` (≈436–455) and `test-and-mark-stable.yml` (≈4451–4475).
11. README / agents.md edits per the retirement map. Add the agents.md
    Cloudflare row placeholder only in Phase 4.
12. Fragment `changelog.d/<pr>-retire-claude-session-automation.md` (section
    `removed`).

### Phase 3 — engine plumbing

1. `.github/actions/install-claude/action.yml` [new].
2. `.github/ai/claude_engine.json` [new], with these keys:
   - `cli_version`, `broker_url` (empty until Phase 4), `oidc_audience`;
   - `gate_utilization` 0.9, `probe_model`;
   - `role_defaults`: `{role: {engine, claude_model}}`, every engine
     `codex` in this phase;
   - `utility_roles`.
3. `scripts/ai_engine.sh` [new]: `ai_engine_for_role`, `ai_engine_model`,
   `ai_engine_effort`, `claude_run`, the fallback (D1), and the
   `AI_ENGINE_SELECTED` / `AI_ENGINE_FALLBACK` logs. Tabs (§9).
4. `scripts/claude_engine.py` [new]: `extract`, `classify`, `probe-parse`,
   `choose`, `settings` (renders `claude_settings.json.tmpl`). No API calls;
   docstrings state inputs and outputs.
5. `scripts/claude_settings.json.tmpl` [new] (P5).
6. `scripts/claude_anthropic_relay.py` [new]; `clarify_isolated_run.sh` and
   `review_untrusted_sandbox.sh` gain a `claude` branch (not yet selected).
7. `scripts/codex_stall_guard.sh`, `scripts/codex_heartbeat.sh`: `--engine`
   label flag. `scripts/cost_audit.py`: parse stream-json results.
8. `.github/workflows/claude-engine-smoke.yml` [new], `workflow_dispatch`
   only, matrix over tool profiles. It asserts:
   - the context gate (start-up tokens < 25,000; CLAUDE.md marker invisible);
   - the relay gate;
   - P5 denials (`gh pr merge` denied, `.github/workflows` edit denied);
   - the `classify` cases.
9. Tests: `tests/test_ai_engine.py`, `tests/test_claude_engine.py`,
   `tests/test_claude_settings_policy.py`, `tests/test_claude_anthropic_relay.py`
   [new], each in its own `ci.yml` step.
10. README "Claude engine" section and agents.md "Models in use" engine column.
    Changelog fragment.

### Phase 4 — token broker

1. `tools/claude-pool-broker/` [new]: `src/index.ts` (JWT verification with
   WebCrypto, allowlist fetch and cache, response), `wrangler.toml`
   (`name = "claude-pool-broker"`, workers.dev), `test/` (vitest with signed
   fixture JWTs: valid, wrong owner, wrong repo, wrong `job_workflow_ref`,
   expired, bad signature, allowlist fetch failure with and without a cached
   copy).
2. Deploy with the `FT_GAMES_CF` credential (split per §24.A). Record the
   Worker in agents.md "Cloudflare resources" (§24.F). Put the URL in
   `.github/ai/claude_engine.json` `broker_url`.
3. `.github/actions/claude-pool-token/action.yml` [new] (+ `scripts/claude_pool_token.sh`).
4. `id-token: write` on every reusable-workflow job that will host a Claude
   role, and in the `workflow-templates/ai-*.yml` wrappers that call them.
5. `claude-workers`: PR adding `.github/workflows/claude-pool-key-sync.yml`
   and a README section listing the activation steps. The operator merges it
   (§23.C default-branch merge of a repo without a review pipeline).
6. Tests: a test that no coding-workflows workflow or template references
   `CLAUDE_POOL_TOKEN`; action script unit tests with a stub broker; the Worker
   tests run in a new `ci.yml` step (`npx vitest run`, Node 22).
7. Changelog fragment.

### Phase 5a–5d — role cutovers

For each role in the phase:

1. Replace the role's codex invocation with
   `if [ "$(ai_engine_for_role <ROLE>)" = claude ]; then claude_run …; else
   <unchanged codex/OpenCode command>; fi`. In YAML, the step body moves to
   `scripts/` when that keeps workflows below §27's guard.
2. Add `claude-pool-token` to the job (guarded by the role resolving to
   `claude`).
3. Set the role's `role_defaults.<role>.engine` to `claude`.
4. Extend that role's existing tests with a Claude-branch case and an
   exact-string codex-branch case (G4).
5. 5c only: give `CLAUDE_FIXER_ENABLED` its Q35 meaning, and rewrite
   `tests/test_review_autofix_claude_fixer_mode.py` for it. After merge, ask the
   operator to set or delete the variable (`true` is the default).
6. Changelog fragment per PR.

### Phase 6 — engine label

1. Add `ai:engine-claude` to `label_contract.v1.json`.
2. `orchestrate.yml` and the `ai-orchestrate.yml` template: optional input
   `engine` (`''|claude|codex`). Label the tracking issue. Propagate the label
   to sub-issues and PRs in the poller's issue/PR creation paths.
3. `ai_engine.sh`: label resolution (D2), Opus 5.5 / high override. Read
   labels from the event payload already in each job; use one REST read only
   where a job lacks them (§15, documented in the function docstring).
4. `implement-plan-claude.md`: pass `engine=claude` in its dispatch.
5. Tests and fragment.

### Phase 7 — unblock judge

1. `scripts/orchestrate_poll_process.sh`: an `unblock_scan` step in the tick,
   behind `UNBLOCK_JUDGE_ENABLED`.
2. `.github/workflows/unblock_judge_dispatch.yml` [new] and its
   `workflow-templates/` wrapper.
3. `scripts/unblock_judge.sh`, `scripts/unblock_ledger.py` (port of
   `escalation_ledger.py` from PR #5164's head; GitHub keeps
   `refs/pull/5164/head` after the PR is closed and its branch deleted in
   Phase 1, so fetch it from there), and
   `prompts/mode-judge-unblock.txt` [new].
4. Hand-over hooks: the existing judges' give-up exits add the block label the
   scan reads (no behaviour change before the give-up).
5. Remove the human-only latches' "wait forever" branches. The stall-ladder
   `escalate_human` path becomes the block label. Defaults stay unless the
   judge acts.
6. Tests: `tests/test_unblock_judge.py` (verdict parsing, never-repeat ledger,
   caps, terminal, operator_step, forbidden overrides) and `tests/test_unblock_scan.py`
   (batching, age and marker filters). Register them in CI.
7. README "Unblock judge", agents.md markers and log prefixes, fragment.

### Phase 8a–8d — ports

- **8a:** `scripts/review_single_issue_security_pass.sh` [new], the step in
  `review_autofix.yml` (body in the script), skip rules from
  `scripts/security_pass_skip.py`, the cycle counter in a PR comment marker,
  tests.
- **8b:**
  - `clarify.yml`: standalone auto-answer through
    `orchestrate_parse_and_post_answer.sh`'s answer builder;
  - `scripts/auto_decisions.py` [new] for the AD comment;
  - the PR-body section in `implement.yml`'s PR body composer;
  - tests.
- **8c:** `prompts/mode-activation-verify.txt` [new],
  `scripts/activation_verify.sh` [new], the job in `issue_pr_status.yml` and
  the poller's completion path, the `ai:operator-step` issue writer shared with
  Phase 7, tests.
- **8d:** `scripts/retarget_merged_base.sh` [new], called from
  `implement.yml` and the `review_autofix.yml` gate, tests.

Each port gets its own changelog fragment and README/agents.md entry.

## Files & Modules

- Phase 0: `scripts/claude_issue_route.py`, `tests/test_claude_issue_route.py`
  (both deleted again in Phase 2 — harmless in any order), `changelog.d/` [new].
- Phase 2: every path in the retirement map [del] or [edit]; `CLAUDE.md`;
  `agents.md`; `README.md`; `.claude/settings.json`;
  `workflow-templates/.claude/**`; `workflow-templates/retired_files.txt` [new];
  `workflow-templates/ai-review.yml`; `workflow-templates/review_rb_judge_dispatch.yml`;
  `.github/workflows/{clarify,review_autofix,review_autofix_sweep,issue_pr_status,validate,ci,mark-stable,test-and-mark-stable,update_workflows,sync_ai_labels}.yml`;
  `.github/ai/label_contract.v1.json`; `scripts/ai_labels.py`;
  `scripts/security_pass_skip.py` [new, moved]; `tests/test_security_pass_skip.py` [moved];
  `tests/test_update_workflows_retired_files.py` [new]; `tests/test_ai_labels_retired.py` [new].
- Phase 3: `.github/actions/install-claude/action.yml` [new],
  `.github/ai/claude_engine.json` [new], `scripts/ai_engine.sh` [new],
  `scripts/claude_engine.py` [new], `scripts/claude_settings.json.tmpl` [new],
  `scripts/claude_anthropic_relay.py` [new], `scripts/clarify_isolated_run.sh`,
  `scripts/review_untrusted_sandbox.sh`, `scripts/codex_stall_guard.sh`,
  `scripts/codex_heartbeat.sh`, `scripts/cost_audit.py`,
  `.github/workflows/claude-engine-smoke.yml` [new], tests [new], `ci.yml`.
- Phase 4: `tools/claude-pool-broker/**` [new],
  `.github/actions/claude-pool-token/action.yml` [new],
  `scripts/claude_pool_token.sh` [new], reusable workflows (job permissions),
  `workflow-templates/ai-*.yml` (permissions),
  `claude-workers/.github/workflows/claude-pool-key-sync.yml` [new],
  `claude-workers/README.md`, `agents.md`.
- Phase 5: the call-site files named per sub-phase; `.github/ai/claude_engine.json`.
- Phase 6: `.github/workflows/orchestrate.yml`, `workflow-templates/ai-orchestrate.yml`,
  `scripts/orchestrate_poll_process.sh`, `scripts/ai_engine.sh`,
  `.claude/commands/implement-plan-claude.md` (+ twin), label contract.
- Phase 7: `scripts/orchestrate_poll_process.sh`,
  `.github/workflows/unblock_judge_dispatch.yml` [new],
  `workflow-templates/unblock_judge_dispatch.yml` [new],
  `scripts/unblock_judge.sh` [new], `scripts/unblock_ledger.py` [new],
  `prompts/mode-judge-unblock.txt` [new], existing judge scripts (give-up exits).
- Phase 8: files named in its steps.
- Every phase: `README.md`, `agents.md`, one `changelog.d/` fragment.

## Tests

- **Unit** (pytest, each new file in its own `ci.yml` step; the Worker tests
  with vitest):
  - engine resolution (labels, per-role vars, global, defaults, D2 precedence),
    model/effort mapping (D3), fallback reasons (D1);
  - `claude_engine.py` extract/classify using the spike transcripts (S7, S15) and a
    simulated `rejected` usage limit;
  - probe parsing and the least-used choice (ties, all gated, failed probes);
  - settings rendering and the P5 deny list;
  - relay token swap (the dummy token never leaves the relay);
  - broker JWT and allowlist cases;
  - retired-files deletion (identical, modified, absent);
  - retired-labels deletion;
  - unblock judge ledger, caps and terminals;
  - ports.
- **Exact-string regression (G4):** for every converted call site, the codex
  branch's command line equals the pre-plan string captured in the test
  fixture.
- **Static:**
  - `tests/test_workflow_file_size_limit.py` on every touched workflow;
  - the no-`CLAUDE_POOL_TOKEN` grep test;
  - the G1 grep as a test (`tests/test_no_session_automation.py`);
  - `tests/test_claude_md_section_numbers.py`.
- **Integration (live, automated):**
  - `claude-engine-smoke.yml` after Phases 3, 4 and each 5x;
  - the key-sync run after Phase 4;
  - one real run per role after its cutover (G3);
  - a fixture blocked issue after Phase 7.
- **End to end (activation):**
  - one standalone issue through clarify → plan → implement → review → merge
    on Claude, in coding-workflows;
  - one `/implement-plan-claude` project of two phases with `ai:engine-claude`;
  - one consumer issue after the next `@stable` sync.

## Risks & Mitigations

- **OIDC `job_workflow_ref` for reusable workflows.** ACCEPTED — pending the
  Phase 4 smoke run (Q32). If the claim names the caller instead, the broker
  falls back to checking `workflow_ref`'s repository against the allowlist plus
  `repository_owner`. The implementing session asks before loosening further.
- **A real usage-limit rejection has not been observed.** ACCEPTED — pending
  first production occurrence (Q32). The classifier treats `is_error` with
  `rate_limit_info.status: rejected` or utilisation ≥ 1.0 as `usage_limit`. Any
  other failure is `crashed`, which uses the role's retries, then codex (D1).
- **Terms of service for rotating personal subscription accounts.** ACCEPTED —
  operator decision (2026-09-30, reaffirmed Q32).
- **Fixed start-up context per run.** ACCEPTED — pending the Phase 3 context
  gate (Q32). Mitigations are in "Claude engine".
- **6-hour job limit.** ACCEPTED (Q32). Unchanged from codex. The roles keep
  their existing timeouts.
- **Sandbox relay may not authenticate OAuth through a base-URL proxy.** Gate in
  Phase 3. The session asks before shipping 5a/5c clarify/editor on Claude. The
  other roles are unaffected.
- **Usage concentration.** No per-account concurrency cap; the gate is read per
  job. Failover on `usage_limit` (D1); the operator adds accounts with one secret.
- **Codex review of this plan's own PRs (Q40).** The GPT editor may push "fixes"
  that re-add removed code. Mitigation: the session reviews each auto-merged
  diff in its Phase report; G1's grep test fails CI if session automation
  returns.
- **Consumers with stale syncs** keep old assets until they sync:
  `tele-funtoken-msg-scoring` has its update workflow disabled;
  `fun-token-multi-chain` update runs fail. The final report lists them as
  operator steps. D1 keeps their pipelines on codex until then.
- **Retired-file deletion could remove consumer-edited copies.** Only
  byte-identical copies are deleted; modified copies are logged.
- **Closing 104 PRs and deleting ~2,300 branches is bulk-destructive.**
  - The classification is printed before acting.
  - `ref sha` lists are saved for restore.
  - tele's 10 product PRs and their branches are excluded by rule.
- **Unblock judge over-reach.** Hard limits are enforced in code, not only in
  the prompt:
  - the never-repeat ledger;
  - no security waiver, no validation pass, no merge past failing checks;
  - no workflow/`.claude/`/`scripts/` overrides in coding-workflows.
- **Broad, non-expiring `GH_PAT`** unchanged from today; the broker adds no
  GitHub credential.
- **Second claude.ai account's Routines** are invisible to this session. Listed
  as an operator step in Phase 0's report.

## Rollout

**Activation gates (human, once):**

1. Before Phase 0: set `CLAUDE_FIXER_ENABLED=false` on coding-workflows (Q40).
2. Phase 4:
   - create a Cloudflare API token on the ft.games account with *Workers
     Scripts: Edit* only, and store it as `CF_BROKER_DEPLOY_TOKEN`
     (`<account_id>:<token>`) in `claude-workers`;
   - add production `CLAUDE_POOL_TOKEN_<NAME>` secrets (made with
     `npx -y @anthropic-ai/claude-code setup-token`) and delete `TEST1`/`TEST2`
     if those accounts are not pool members;
   - merge the `claude-workers` key-sync PR.
3. After 5c merges: set `CLAUDE_FIXER_ENABLED=true` (or delete the variable).
4. Optional clean-up: delete retired variables and secrets from repository
   settings.

**Order of execution:** 0 → 1 → 2 → 3 → 4 → 5a → 5b → 5c → 5d → 6 → 7 → 8a–8d.
Any other order is also safe (D1, D4).

**Propagation (§14):** consumers receive Phases 2–8 on the next `@stable`
promotion (daily `promote-main-to-stable.yml`), then the 04:00 UTC sync or the
release dispatch. Retired files and labels are removed by that sync (Q29).

**Rollback:**
- per role: `AI_ENGINE_<ROLE>=codex`;
- globally: `AI_ENGINE=codex`;
- review write roles: `CLAUDE_FIXER_ENABLED=false`;
- unblock judge and ports: their kill switches;
- any phase: revert its PR.

Session automation is not restorable by design.

## References

- Planning session audit (2026-10-03) and operator answers Q1–Q40.
- `docs/plans/claude-actions-worker-pool-plan.md` (spike evidence S1–S21;
  deleted by Phase 2, read from git history).
- PR #5164 head (`escalation-judge.md`, `escalation_ledger.py`) — source for
  the unblock ledger.
- `docs/completed/opencode-review-autofix-cutover-plan.md` — role-by-role
  harness cutover precedent.
- `docs/plans/confine-human-steps-to-activation-plan.md` — complementary to
  Q33's dormant-plus-operator-step rule.
- CLAUDE.md §6, §9, §14, §15, §18–§27.
- Issues/PRs: #4302, #4304, #4438, #4443, #4525, #4549, #4609, #4623, #4734,
  #4791, #5164, #6060, #6096, #6097, #6098, #6100.
- code.claude.com/docs (CLI flags, `setup-token`), GitHub OIDC docs
  (`job_workflow_ref`), Cloudflare Workers secrets API.
