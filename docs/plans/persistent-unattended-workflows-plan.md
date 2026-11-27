# Keep unattended work going until the change is delivered and verified

## Summary

An interactive session keeps working until it can show that a change works. It knows what "done" looks like, checks it, remembers what it tried, fetches evidence when it needs it, and changes approach when one fails. The unattended pipelines stop at "PR merged" and start each stage fresh. This plan adds those behaviours to every unattended workflow in eleven independently mergeable phases, each behind a default-off repo variable:

- a done-check the plan writes;
- a multi-turn repair loop before the PR opens;
- a host-written attempt journal;
- evidence packs, and later an on-demand evidence broker;
- a check after merge that re-plans on failure;
- a budget and progress governor;
- orchestrator-level checks.

The workflows covered are standalone issues (clarify, plan, implement, review), orchestrator projects (the decomposer and wave items), review autofix, the merge-conflict resolver, and the workflow-failure heal.

## §18 Automation surface (CLAUDE.md §18.E)

| Question | Answer |
|---|---|
| New script, extended script, or code only? | New library scripts, all called only by existing workflow steps or the poller: `scripts/attempt_journal.py` (P1), `scripts/acceptance_check.py` (P2), `scripts/evidence_pack.py` (P4), `scripts/persist_postmerge_verify.py` (P5), `scripts/persist_budget.py` (P6), `scripts/persist_orchestrator.py` (P7–P10), `scripts/evidence_broker.py` (P11). Every other change extends existing scripts, workflows and prompts. No standalone manual script (§18.A). |
| Scheduler / PR-push entry points | No new triggers. **Per-issue stages:** `.github/workflows/clarify.yml`, `plan.yml`, `implement.yml` (issue-comment and dispatch triggers, unchanged). **PRs:** `.github/workflows/review_autofix.yml` (`pull_request` and poller dispatch; covers the review editor and the merge-conflict resolver). **After merge:** `.github/workflows/issue_pr_status.yml`, `sync-issue-status` job (merged-PR path). **Poller ticks:** `.github/workflows/orchestrate_poll.yml` → `scripts/orchestrate_poll_process.sh` (P5 checker, P6 governor, P7–P10). **Decomposer:** `.github/workflows/orchestrate.yml`, step "Build Codex prompt" (P7). |
| Long-running supervisor (§18.C)? | None new. The poller is the supervisor that re-enters work. P5, P6 and P8–P10 add bounded work to its existing tick. |
| DB work (§18.D)? | None. No MongoDB collections, indexes or contracts (§10 N/A). |
| `docs/scripts-pending-removal.md` (§18.F) | No entries. The new scripts are permanent library steps called by automation, not single-use, long-running or supervisor scripts. Precedent: `docs/plans/heal-evidence-reproduction-and-trends-plan.md` treats `heal_reproduce.sh` the same way. |

## Context

### Why now

From 2026-10-27 release gates failed six times in a row. The auto-heal filed partial fixes or held plans for a human, while an interactive session found the root cause in hours (#8382, #8410). The heal side is planned in #8409 (`docs/plans/heal-evidence-reproduction-and-trends-plan.md`, P1–P7). Its non-goals leave standalone and orchestrator issues out, which is this plan.

The discussion that led here (2026-10-29/30) settled these decisions:

| ID | Decision |
|---|---|
| Q6 A | Done means the done-check's signal after merge when one exists; otherwise CI green plus the done-check's tests. |
| Q7 A | Before the PR opens, repair up to 3 turns while the failing set shrinks. If still red, open the PR marked `ai:acceptance-unmet`. |
| Q8 A | Prefetched evidence packs first; an on-demand read-only broker later, as its own phase behind a switch. |
| Q9 A | Per-issue caps on attempts and wall-clock time (3 approaches, 5 rounds, 48 h) plus a progress rule. Escalation goes through #8291's path. |
| Q10 A | One new multi-phase plan, enabled after #8409 and #8291 and reusing their modules. |
| Q11 A | Covers all workflows. Heals reuse #8409 P2 (reproduction) and P7 (check after merge) as their done-check, with no duplication. |

### What the pipelines get today

All facts are from `main` at c4c4f74.

- **Evidence.**
  - Every stage's model runs in a credential-free, `--network none` container on a synthetic git: HEAD plus one `isolated-base` commit (`scripts/codex_isolated_workspace.py`, `seed-git`).
  - Host checkouts are `fetch-depth: 1` in clarify (`clarify.yml:343`), plan (`plan.yml:462`) and implement (`implement.yml:1319`). Review uses depth 100 with `filter: blob:none` (`review_autofix.yml:2038`).
  - No standalone stage sees open PRs, CI history or `git log`. `prompts/mode-plan.txt:143` asks the planner to spot open PRs that already deliver the work, but no list is fed in. The only open-PR lister is `activation_open_prs_json()` (`scripts/activation_verify.sh:270-296`).
  - The decomposer prompt (`orchestrate.yml` ~778-811) has the static context, memory, schema and project description, and nothing about other projects or recent commits.
- **Done.**
  - The plan (`plan.yml` "Post implementation plan", 2708-2759) has no machine-checkable done condition.
  - `sync-issue-status` closes the issue on merge (`issue_pr_status.yml:767-782`).
  - activation-verify (`issue_pr_status.yml:1047-1256`, `scripts/activation_verify.sh`) is a static code read. It files a new fix issue for gaps and never re-opens or re-plans the source.
  - The only "keep open and restart after merge" path is partial delivery (`handle_partial_delivery_merge`, `issue_pr_status.yml:259-317`).
- **Verification before the PR.**
  - Syntax checks block (`implement.yml:4692-5378`).
  - The pre-push gate (`implement.yml:5388`, `continue-on-error: true`) runs once, gets one repair turn (`implement_prepush_test_gate_main`, `scripts/implement_step_prepush_test_gate.sh:619-722`), re-runs once, and never blocks.
  - The same applies in review and the resolver (`scripts/review_autofix_step_prepush_test_gate.sh:579+`, `review_conflict_resolve.sh:969`).
  - The gate module has no explicit test list. `--tests-file` is planned by #8409 P2 and not yet on `main`.
- **Memory between stages.**
  - The issue thread is the only memory.
  - The gate's repair prompt (`review_prepush_test_gate_build_prompt`, `review_autofix_step_prepush_test_gate.sh:373-400`) has no record of what the editor removed and why. That is how PR #8371 looped: the editor removed a fixture in `tests/test_promote_main_cycle.py` and the repair turn added it back.
  - The review ledger (`.ai/review_issue_ledger/pr-N.txt`, `scripts/review_issue_ledger.sh`) keeps per-finding `EDITOR_OUTCOMES` in actions/cache only.
- **Changing approach.**
  - The stall judge (`invoke_stall_judge`, `scripts/orchestrate_poll_process.sh:19475`) gets the last 3 review-run conclusions and no logs. `prior_recovery_actions` is `[]` for standalone issues (OPP:19605-19620).
  - The unblock judge (`scripts/unblock_judge.sh:2169`) can `reissue` (a new issue), with `MAX_ROUNDS_PER_ITEM = 2` (`scripts/unblock_ledger.py:197`).
  - Standalone exhaustion closes the issue (`skip`, OPP:23731-23747).
  - #8291 (`docs/plans/remove-unattended-pipeline-stuck-states-plan.md`, in flight as PR #8307) adds a failure-fingerprint breaker and park-not-close escalation. The fingerprint *consumer* exists on that branch (`evaluate_failure_fingerprint_breaker`, `orchestrate_lib.py` there); the plan and clarify *producers* do not yet.
- **Orchestrator.**
  - Wave N+1 issue bodies are frozen at decomposition (OPP:27414-27440, 31720+).
  - A clean wave advances with no judge (`ENABLE_CLEAN_WAVE_JUDGE_SKIP`, default true, OPP:2914 and 30650-30669).
  - Nothing groups identical failures across items.
  - Nothing edits `pending_issue_defs` after seeding (only `del` at OPP:27396, 27472, 31772).
  - Merged PR numbers per wave issue are not persisted (derived per tick from `pr_states`, `orchestrate_lib.py` ~3600-3640).

## Goals

Every goal is behind its own default-off switch. With the switch off, behaviour and prompt bytes are identical to today.

- **G1 (P1, journal).** Every clarify, plan, implement, review-editor, resolver, post-merge, governor and wave-check run on an issue adds one host-written entry to that issue's `AI_ATTEMPT_JOURNAL_V1` comment. Every stage prompt and judge context receives the rendered journal.

  PR #8371's flip-flop shape is the test case. A gate repair turn that would restore content the review editor deleted in the same round sees that deletion and the rule against restoring it.
- **G2 (P2, done-check).** Plans emit `ACCEPTANCE:` lines. The host validates them against the planning ref and stores the result as an `AI_ACCEPTANCE_V1` marker in the plan comment. Implement and review read it. An invalid block never holds a plan; it is recorded and the issue falls back to "CI green".
- **G3 (P3, repair loop).** The pre-push gate repairs up to `PERSIST_REPAIR_MAX_TURNS` (default 3) turns.
  - It continues only while the PR-caused failure count strictly drops and the step budget allows.
  - It runs the done-check's tests explicitly, including red-on-base checks.
  - The PR opens either way. If any done-check test is unmet, it carries `ai:acceptance-unmet` and an `## Acceptance` section.
  - A later review round that meets the done-check removes the label.
- **G4 (P4, evidence packs).** Clarify, plan and implement prompts end with an untrusted `EVIDENCE PACK` block built on the host:
  - open same-repo PRs touching the same paths;
  - recent `git log` subjects for those paths;
  - the default branch's last 10 completed runs.

  Each stage spends at most 4 GitHub API calls.
- **G5 (P5, check after merge).**
  - When an issue's done-check names a post-merge workflow, its merge leaves an `ai:verify-pending` label and an `AI_POSTMERGE_VERIFY_V1` marker.
  - Within `PERSIST_POSTMERGE_WINDOW_HOURS` (default 24) the poller checks the first completed run of that workflow on the target branch containing the merge. A pass logs `held`.
  - A failure reopens the issue and re-plans it through the existing `retrigger_plan` action, with the run in the journal. No run by the deadline logs `unverified` and stops.
  - Heals are skipped while #8409 P7 (`WORKFLOW_HEAL_VERIFY_ENABLED`) owns them.
- **G6 (P6, governor).** Each poller tick evaluates every open issue that has a journal:
  - **Re-plan:** after `PERSIST_REPLAN_AFTER_FAILED_ROUNDS` (default 3) consecutive failed rounds on one approach, or after 2 consecutive rounds with no progress.
  - **Escalate:** apply `ai:needs-human`, which feeds the existing unblock judge, when any cap is reached:
    - `PERSIST_MAX_APPROACHES` (3);
    - `PERSIST_MAX_ROUNDS` (5 per approach);
    - `PERSIST_MAX_HOURS` (48).
  - Items carrying security labels are never touched.
- **G7 (P7, orchestrator context).**
  - Wave N+1 issues get a `## Since this plan was decomposed` section: earlier waves' merged items, their PR numbers, files and journal outcome. It costs zero new API calls.
  - The decomposer prompt gets other open orchestrator projects and, when P4's script is present, an evidence pack for the plan's paths.
- **G8 (P8, grouping).** When 2 or more open items in one project fail with the same cause key within `PERSIST_ORCH_GROUP_WINDOW_HOURS` (default 24):
  - one owner item keeps working;
  - the others pause, marked `grouped_under` in state;
  - siblings resume when the owner's key changes, its PR merges, or `PERSIST_ORCH_GROUP_MAX_HOURS` (default 12) passes.
- **G9 (P9, wave check).** Where the clean-wave skip would advance a wave, the poller first requires the integration PR's head check runs to be green.
  - Pending holds the wave, up to `PERSIST_ORCH_WAVE_CHECK_MAX_WAIT_MINUTES` (default 90).
  - Red, or still pending at the limit, runs the existing wave judge, with the failing checks and unmet item done-checks in its prompt.
- **G10 (P10, re-decompose).** The wave judge may return `revise_remaining: true`. The poller then re-decomposes only the not-yet-created `pending_issue_defs`, validated by the existing schema and partition guard, at most `PERSIST_ORCH_MAX_REDECOMPOSE` (default 2) times per project.
- **G11 (P11, broker).** A plan that ends `STATUS: NOT_CLEAR` with `EVIDENCE_REQUEST:` lines gets them fulfilled on the host and is re-run once with the results.
  - The menu is fixed and same-repo only.
  - Caps: 10 requests, 32 KiB in total.

## Non-goals

- **Giving any model sandbox a token, network access or a deeper git history.** Evidence is built on the host and passed in as untrusted data (§23, §1). History reaches models only as host-rendered subjects, paths and diffstats.
- **Duplicating #8409.** Heal evidence (P1), reproduction (P2), trends (P3), scope (P4), grouping (P5), branch routing (P6) and heal post-merge checks (P7) stay there. This plan reads their outputs where present.
- **Duplicating #8291.** Its fingerprint breaker, escalation policy (`UNBLOCK_MAX_ROUNDS_PER_ITEM`, digest parking) and producers stay there. P6 escalates by applying `ai:needs-human`, which `main` already routes to the unblock judge (`BLOCK_LABELS`, `scripts/unblock_ledger.py:133-153`).
- **Making the pre-push gate block pushes.** It stays `continue-on-error` (Q7 A).
- **Letting a done-check auto-approve a plan.** Only #8409's experimentally verified `REPRODUCIBLE` path auto-approves, and only for heals.
- **Weakening security handling.** Security findings and items with security labels still wait for a person.
- **A model-spend cap per issue** (Q9 A chose attempts and time only).
- **Changing the gate's test selection rules.**

## Constraints

- **§6 naming immutability.** No existing identifier is renamed or removed. New identifiers were checked against `origin/main` at c4c4f74 with `git grep -F`, and none collide:
  - **Repo variables:** `PERSIST_*` (every repo variable listed under Rollout).
  - **Markers and labels:** `AI_ATTEMPT_JOURNAL_V1`, `AI_ACCEPTANCE_V1`, `AI_POSTMERGE_VERIFY_V1`, `ai:acceptance-unmet`, `ai:verify-pending`.
  - **Model-output lines and state fields:** `EVIDENCE_REQUEST`, `grouped_under`, `revise_remaining`, `persist_merged_pr`, `redecompose_count`.
  - **Scripts and prompts:** `attempt_journal.py`, `acceptance_check.py`, `evidence_pack.py`, `evidence_broker.py`, `persist_budget.py`, `persist_postmerge_verify.py`, `persist_orchestrator.py`, `prompts/mode-plan-acceptance.txt`, `prompts/mode-persist-journal-rules.txt`, `prompts/mode-orchestrate-revise.txt`.
  - **Gate options:** `--tests-file`, `--expect-baseline-fail-file`.
  - **Avoided:** `merged_pr`, which has 315 existing uses; the new state field is `persist_merged_pr`.
  - Implementers re-check with `git grep -F` before adding any other name. The log prefix is `PERSIST` (`PERSIST <component> outcome=<o> ...`).
- **§4 env defaults.** Every variable has a default in the workflow expression (`${{ vars.X || '<default>' }}`) and in the script that reads it. A helper run from `@stable` may see an older workflow.
- **§9 style.** Tabs in Python and shell, 2-space YAML, braces on a new line where the language has them.
- **§13.** Nothing writes into `.git/`. Host history fetches go to the private ref `refs/persist/history`, never `refs/heads/*`.
- **§14 consumers.**
  - Reusable workflows and scripts reach all repos in `.github/ai/consumer_repos.json` on the next `@stable`. Every phase is default-off per repo, so consumers see no change until they set the variable.
  - `workflow-templates/.claude/hooks/prepush_affected_tests.py` (P3) changes are additive only.
  - The two new labels reach consumers through the label sync (`sync_ai_labels.yml` / `workflow-templates/ai-sync-labels.yml`). That is harmless while unused.
- **§15 GitHub API.** Each phase states its budget under Implementation Steps:
  - Poller work reads only from the per-tick caches: `_candidate_details_json` (comments and linked PR), `_load_actions_runs_cached` and `ACTIVE_WORKFLOW_ISSUES`.
  - New calls are batched and fail open.
  - New Python callers use `scripts/gh_api_retry.py` (`optional=True` honours the rate-limit breaker); new shell callers use `gh_retry`.
- **§19.** No new PR body or comment uses auto-close keywords. PR bodies still pass `scripts/lint_pr_body_auto_close.py`.
- **§20 / §30.** Exactly one `changelog.d/` and one `agents.d/` fragment per phase PR. Never edit `CHANGELOG.md` or `agents.md` directly.
- **§27 workflow size.** At c4c4f74:

  | Workflow | Size (bytes) |
  |---|---|
  | `implement.yml` | 425,273 |
  | `review_autofix.yml` | 373,864 |
  | `plan.yml` | 203,288 |
  | `orchestrate_poll.yml` | 132,590 |
  | `clarify.yml` | 148,086 |

  - All logic goes into `scripts/`. Workflow edits are env lines and thin step bodies.
  - The total budget across all phases is +6,000 bytes for `implement.yml` and +3,000 for `review_autofix.yml`. Every phase PR runs `wc -c` and `tests/test_workflow_file_size_limit.py`.
  - New scripts that workflows run from the trusted support dir are added to the staging lists: `clarify.yml:483`, `plan.yml:590`, `implement.yml:1474`, the orchestrate list, and `scripts/stage_workflow_support.sh:57` `REQUIRED_BOOTSTRAP_SCRIPTS` for review.
- **Prompt changes.**
  - The plan prompt exists in three copies: `prompts/_templates/mode-plan.txt`, the rendered `prompts/mode-plan.txt`, and the inline contract in `scripts/run_plan_codex.sh`.
  - To keep switch-off byte identity, no phase edits those. New rules ship as separate fragments, appended only when the phase's switch is on. This follows `mode-plan-heal-counterfactual.txt` (installed at `plan.yml:754-762`, appended at `run_plan_codex.sh:309`).
- **Security (§1).**
  - All new model inputs are host-built and appended after the cacheable static prefix and memory, inside `=== BEGIN UNTRUSTED <NAME> ===` / `=== END UNTRUSTED <NAME> ===` fences.
  - Inner fence forgeries are broken with the `sed` used by `scripts/workflow_failure_heal_evidence.sh` (~line 72).
  - Lines starting with `::` or `##[` are neutralised, and `<!--`, `#N` and `@name` are scrubbed, as `implement_step_prepush_test_gate.sh:82` (`neutralise`, `scrub`) does.
  - Journal fields written by a model (`note`) are capped at 300 characters and labelled untrusted. Every other journal field is computed by the host.
  - Marker comments are trusted only when authored by the pipeline login, as #8291 P3 specifies. If identity cannot be resolved, the comment is ignored.
  - Gate runs keep `--network none --read-only --cap-drop ALL --security-opt no-new-privileges` with no credentials (`scripts/prepush_test_gate_run.sh`).

## Approach

**One durable record per issue, and the existing poller as the supervisor.** "As persistent as a session" does not mean one long-running job; Actions caps jobs at 6 hours and runners are shared. It means three things:
- each issue carries its own state (the journal, the done-check, the post-merge marker);
- the poller that already ticks re-enters work at the right stage;
- every stage reads what earlier stages learned.

**Layers, each usable alone:**

- **Record:** P1 journal.
- **Define done:** P2 done-check.
- **Work toward done before the PR:** P3 repair loop.
- **Know more:** P4 packs, P11 broker.
- **Check done after merge:** P5.
- **Decide what to do next:** P6 governor.
- **Projects:** P7–P10 apply the same ideas at project level.

**Interfaces shared with #8409.**
- **`--tests-file`.** P3 and #8409 P2 both need an explicit test list in the gate module and wrapper. Both plans specify the same interface: `run --tests-file <file>`, one `path[::name]` per line, bypassing `select_tests`. Whichever phase merges first adds it, and the other reuses it.
- **`cause_key`.** P8 uses #8409 P5's `cause_key` when `scripts/workflow_failure_heal.py` provides it, and otherwise the journal's `failure_key`. That key is built from the existing `fingerprint()` and `error_signature()` (`workflow_failure_heal.py:3163`, `:3128`).

**Alternatives considered:**

- **Keep the issue open after merge until verified.** Rejected: `sync-issue-status` closing on merge is relied on by the merge train, activation-verify and dashboards. Reopening on failure matches #8409 P7 and keeps the common path unchanged.
- **Re-plan through the unblock judge's `reissue`.** Rejected: it creates a new issue and loses the journal and PR linkage. P5 and P6 reuse `retrigger_plan` (OPP:18624), which re-runs plan on the same issue via `/answer [auto-answered-by-orchestrator]` (already accepted by `plan.yml`'s `if:`).
- **Store the journal in actions/cache, as the review ledger does.** Rejected: caches are per-workflow and evictable, and the poller cannot read them. An issue comment is durable, visible, and already in the poller's per-tick comment cache.
- **Give the planner a read-only GitHub token or MCP server.** Rejected, as in #8409: the broker (P11) keeps credentials on the host and offers a fixed, validated menu.
- **Block the push until the done-check passes.** Rejected by Q7 A: the gate still produces false failures in some sandboxes, and a blocked push leaves no PR for review or the outer loop to act on.

## Decisions

These are the recommended defaults. The reviewer can override any of them on this PR.

- **D1: every phase is default-off.** Rollout turns phases on one at a time. A follow-up PR per phase flips its default after a clean week, keeping the switch.
- **D2: close at merge, reopen on failure** (see Alternatives). `ai:verify-pending` marks issues awaiting their post-merge check.
- **D3: re-plan in place** through `retrigger_plan`. The new plan comment starts a new approach in the journal. The same `ai/issue-N` branch and PR are reused by implement's existing "PR already exists" path (`implement.yml` ~6273).
- **D4: an invalid done-check never holds a plan.** It is dropped, the plan proceeds with done meaning "CI green", and the journal records `acceptance=invalid` with reasons. A done-check never grants approval.
- **D5: `ai:acceptance-unmet` does not block merge by itself.** CI and review stay the merge gates. A done-check test that fails also fails CI.
- **D6: journal writes are best-effort read-merge-write.** A lost entry from two concurrent writers is accepted. Entries are keyed by `(stage, run_id, attempt)` and merged on every write.
- **D7: the broker serves the plan stage only.** Judges keep their prefetched context. Extending the broker is a later decision.
- **D8: orchestrator-managed items get no per-item post-merge check (P5).** Their merges land on the integration branch. P9's wave check covers them, and the project's final PR goes through activation-verify as today.

## Phases & Merge Strategy

Each phase is one PR, independently mergeable, production-safe with its switch off, and complete on its own. No phase requires another to have merged. Where a phase can use another's output (the journal, the done-check marker, `--tests-file`, `cause_key`, `evidence_pack.py`), it reads that output only when present. When the output is absent, it logs `PERSIST <component> outcome=skipped reason=<dependency>_absent` and keeps today's behaviour.

1. **P1: Attempt journal.**
   - Switch: `PERSIST_JOURNAL_ENABLED` (default `false`).
   - Scope:
     - `scripts/attempt_journal.py` (`append`, `render`, `parse`);
     - writes from clarify, plan, implement, review editor, resolver;
     - reads in plan, implement, review editor, gate repair prompts, the stall judge and the unblock judge;
     - the repair-turn rule fragment.
   - Done when:
     - with the switch on, a fixture issue run through plan, implement and two review rounds has one journal comment with 4 entries;
     - a replay of PR #8371's two rounds shows the repair prompt containing `EDITOR DELETIONS THIS ROUND` with the fixture;
     - with the switch off, every prompt is byte-identical.
   - Rollback: switch off. Existing journal comments are ignored.
2. **P2: Done-check (acceptance).**
   - Switch: `PERSIST_ACCEPTANCE_ENABLED` (default `false`).
   - Scope: `prompts/mode-plan-acceptance.txt` (template + rendered), `scripts/acceptance_check.py validate|parse|render`, the `plan.yml` validation step, the `AI_ACCEPTANCE_V1` marker in the plan comment, and the `## Acceptance` section in the implement PR body (status `not run` unless P3 is on).
   - Done when: a plan with valid lines gets a marker matching the fixture; a plan naming a missing test file or unknown check gets `acceptance=invalid` and proceeds; with the switch off, the plan comment is byte-identical.
   - Rollback: switch off. Markers are ignored.
3. **P3: Repair loop before the PR.**
   - Switches: `PERSIST_REPAIR_LOOP_ENABLED` (default `false`), `PERSIST_REPAIR_MAX_TURNS` (default `3`).
   - Scope:
     - the loop in both gate step scripts;
     - `--tests-file` and `--expect-baseline-fail-file` in the gate module and wrapper (additive);
     - the done-check result in the gate result;
     - the `ai:acceptance-unmet` label;
     - label removal in review when met.
   - Done when (fixture with fake docker):
     - failing counts 3, 1, 0 stop after 2 repairs with status `pass`;
     - counts 3, 3 stop after 1 repair;
     - a red-on-base test that passes on base is reported `unmet (passes on base)`;
     - the PR gets the label and section;
     - with the switch off, the gate result and PR body are byte-identical.
   - Rollback: switch off.
4. **P4: Evidence packs.**
   - Switches: `PERSIST_EVIDENCE_PACK_ENABLED` (default `false`), `PERSIST_EVIDENCE_PACK_MAX_BYTES` (default `12000`).
   - Scope: `scripts/evidence_pack.py`, wiring in clarify, plan and implement.
   - Done when: against a fake `gh` and a local repo, the pack lists overlapping open PRs, per-path `git log` subjects and default-branch run conclusions, within 4 API calls and the byte cap. Any failure gives `(evidence unavailable: <reason>)` and exit 0. With the switch off, prompts are byte-identical.
   - Rollback: switch off.
5. **P5: Check after merge.**
   - Switches: `PERSIST_POSTMERGE_VERIFY_ENABLED` (default `false`), `PERSIST_POSTMERGE_WINDOW_HOURS` (default `24`), `PERSIST_POSTMERGE_MAX_PER_TICK` (default `3`).
   - Scope: marker and label at merge in `issue_pr_status.yml`; `scripts/persist_postmerge_verify.py`; the poller tick step; reopen and `retrigger_plan` on failure.
   - Done when: fixture issues whose next run passed, failed at the named job, or never ran end as `held`, reopened and re-planned, and `unverified`. Heals are skipped while `WORKFLOW_HEAL_VERIFY_ENABLED=true`. With the switch off, nothing is written at merge.
   - Rollback: switch off. Leftover `ai:verify-pending` labels are removed by the poller once their window has passed, whether or not the switch is on.
6. **P6: Budget and progress governor.**
   - Switches: `PERSIST_BUDGET_ENABLED` (default `false`), `PERSIST_MAX_APPROACHES` (`3`), `PERSIST_MAX_ROUNDS` (`5`), `PERSIST_MAX_HOURS` (`48`), `PERSIST_REPLAN_AFTER_FAILED_ROUNDS` (`3`).
   - Scope: pure `scripts/persist_budget.py decide`, and poller wiring for standalone and managed issues.
   - Done when: table-driven tests cover continue, re-plan after 3 failures, re-plan after 2 no-progress rounds, escalate at each cap, and a security-labelled item ignored. With the switch off, the poller makes no new decisions. Without a journal, it logs `skipped reason=journal_absent`.
   - Rollback: switch off.
7. **P7: Orchestrator context.**
   - Switch: `PERSIST_ORCH_CONTEXT_ENABLED` (default `false`).
   - Scope: the `persist_merged_pr` state field recorded when an item flips to merged; the `## Since this plan was decomposed` section on wave N+1 issues (both creation sites); the open-projects list and optional evidence pack in the decomposer prompt.
   - Done when: a fixture state with two merged wave-1 items renders a section naming both PRs and files on a wave-2 issue, with zero API calls. With the switch off, issue bodies and the decomposer prompt are byte-identical.
   - Rollback: switch off. The state field is ignored by everything else.
8. **P8: Grouping repeated failures across items.**
   - Switches: `PERSIST_ORCH_FAILURE_GROUPING_ENABLED` (default `false`), `PERSIST_ORCH_GROUP_WINDOW_HOURS` (`24`), `PERSIST_ORCH_GROUP_MAX_HOURS` (`12`).
   - Scope: grouping, pause and release in `scripts/persist_orchestrator.py group`; poller wiring; grouped evidence in the owner's stall-judge diagnostics.
   - Done when: a fixture project with three items sharing a key pauses two, keeps the owner, and releases both when the owner's key changes or after 12 h. Items with different keys are unaffected. Without journals, it logs `skipped reason=journal_absent`.
   - Rollback: switch off. Stale `grouped_under` fields are cleared on the next tick.
9. **P9: Check between waves.**
   - Switches: `PERSIST_ORCH_WAVE_CHECK_ENABLED` (default `false`), `PERSIST_ORCH_WAVE_CHECK_MAX_WAIT_MINUTES` (`90`).
   - Scope: a check-run gate on the integration PR head before the clean-wave skip; a judge prompt block with the failing checks and unmet done-checks.
   - Done when (fixture check runs): green advances, pending holds, red invokes the judge, and pending past 90 minutes invokes the judge. A missing `final_merge_pr` fails open to today's skip.
   - Rollback: switch off.
10. **P10: Re-decompose remaining waves.**
    - Switches: `PERSIST_ORCH_REDECOMPOSE_ENABLED` (default `false`), `PERSIST_ORCH_MAX_REDECOMPOSE` (`2`).
    - Scope: the optional `revise_remaining` judge output field; the `prompts/mode-orchestrate-revise.txt` judge call; validation and replacement of not-yet-created `pending_issue_defs`; a tracking comment.
    - Done when:
      - a fixture verdict with `revise_remaining: true` replaces only pending defs;
      - invalid output (a schema failure, a partition-guard failure, or touching created ids) leaves state unchanged;
      - the cap holds.
    - Rollback: switch off. `pending_issue_defs_previous` allows restoring the prior defs.
11. **P11: Evidence broker.**
    - Switches: `PERSIST_EVIDENCE_BROKER_ENABLED` (default `false`), `PERSIST_EVIDENCE_BROKER_MAX_REQUESTS` (`10`), `PERSIST_EVIDENCE_BROKER_MAX_BYTES` (`32768`).
    - Scope: `scripts/evidence_broker.py validate|fulfil`; the `EVIDENCE_REQUEST:` contract in the acceptance-independent fragment `prompts/mode-plan-evidence-request.txt`; one re-run of the planner with results.
    - Done when: valid requests are fulfilled within caps against a fake `gh` and a local repo. Requests for other repos, credential paths, malformed ids or more than the cap are refused per line. A plan without requests runs exactly once.
    - Rollback: switch off.

**Suggested enabling order (not a merge order):**
- Prerequisites first: #8410 (gate sandbox), #8291 / PR #8307, then #8409 P2, P5 and P7.
- Then this plan: P1, then P2, then P4, then P3, then P5, then P6, then P7, P9 and P8, then P10, then P11.

## Implementation Steps

### P1: Attempt journal

1. `scripts/attempt_journal.py` [new], pure functions plus a thin CLI.
   - **Entry schema `attempt_journal.v1`:**
     - `ts`
     - `stage`: `clarify|plan|implement|review|resolver|postmerge|governor|wave_check`
     - `run_id`, `attempt`, `head_sha`
     - `approach`: the plan comment id the entry works under; `0` before any plan
     - `outcome`: `ok|fail|partial|blocked|held|unverified`
     - `failure_key`: the first 12 hex of `workflow_failure_heal.fingerprint(<workflow>, <step>, error_signature(<failure text>))`, else `""`
     - `files_changed`: at most 20
     - `gate`: `{status, pr_caused, inherited, unverified}`, from the gate result
     - `acceptance`: `{state: met|unmet|invalid|none, unmet: [<=10]}`
     - `editor_deletions`: `[{file, lines}]`, at most 10
     - `evidence_hash`: sha256 of the evidence pack or broker results the stage saw, else `""`
     - `note`: at most 300 characters of model text, untrusted
   - **`parse(comments_json, trusted_login)`.** Uses the newest comment by `trusted_login` containing `<!-- AI_ATTEMPT_JOURNAL_V1`. The JSON sits between `<!-- AI_ATTEMPT_JOURNAL_V1` and `AI_ATTEMPT_JOURNAL_V1 -->`, the same shape as `AI_STANDALONE_STALL_STATE_V1`.
   - **`merge(entries, new)`.** Key `(stage, run_id, attempt)`; keeps the newest 40.
   - **`render(entries, max_bytes=6000)`.** Newest first, as a compact table. `note` is prefixed `UNTRUSTED_DATA:`. Output goes inside `=== BEGIN UNTRUSTED ATTEMPT JOURNAL (host-recorded; note fields are model text) ===`.
   - **`append --repo --issue --entry-file [--comments-file]`.**
     - It re-reads comments if no file is given (1 paginated `issues/<n>/comments` call), merges, and PATCHes the comment, or POSTs one. The comment's visible part is a short table; the marker holds the JSON.
     - Fail-open: any error logs `PERSIST journal outcome=error reason=<r>` and exits 0.
     - API: at most 1 read and 1 write per stage run.
2. **Writers.** One `attempt_journal.py append` call per stage end, each behind `PERSIST_JOURNAL_ENABLED`, at a thin step or inside an existing step script:
   - `clarify.yml` (after the clarify result is posted);
   - `plan.yml` (after "Post implementation plan", 2708-2759; `approach` = `PLAN_COMMENT_ID`);
   - `implement.yml` (after "Create Pull Request", ~6148; gate fields from `${RUNTIME_DIR}/prepush_gate/*/result.json`);
   - review: in `scripts/review_apply_fixes.sh` after the editor commit, with stage `review`, `files_changed`, and the gate fields;
   - resolver: in `scripts/review_conflict_resolve.sh` after each attempt;
   - the linked issue is found from the `ai/issue-N` head, or via `LINKED_ISSUE_NUMBERS` logic. With no linked issue, the journal lives on the PR itself.
3. **Editor deletions (the #8371 fix).**
   - `scripts/review_autofix_step_prepush_test_gate.sh` `review_prepush_test_gate_build_prompt` (373-400): when the switch is on, add `=== EDITOR DELETIONS THIS ROUND ===`. It holds the removed lines of `git diff HEAD -- <allow-list files>` (lines starting `-`, at most 4 KiB), plus the journal block.
   - Do the same in `implement_step_prepush_test_gate.sh`'s repair prompt for implement's own deletions.
   - Append `prompts/mode-persist-journal-rules.txt` [new]:
     - when a failure is caused by a deletion shown there, update the test to match rather than restoring the deleted code, unless the deletion breaks behaviour the issue or plan requires;
     - say which case applies in the repair summary;
     - never undo a change the journal records as a reviewer-requested fix.
4. **Readers.** Append the rendered block at the end of each prompt, after existing dynamic context, when the switch is on and a journal exists:
   - plan (`run_plan_codex.sh` after line ~300, before the heal append at 302);
   - implement (`implement.yml` after the implementation context concat, ~3177, via a staged script);
   - review editor (`review_apply_fixes.sh`, next to `ledger_status.txt`, ~1445).
5. **Judge context.**
   - `invoke_stall_judge` (OPP:19632-19660): add `attempt_journal` (the rendered text, at most 4 KiB) to the `diagnostics` object, from `_candidate_details_json` comments (no API call). Add the journal's newest entry key to the decision-cache key (OPP:19588-19600), so a new attempt re-asks the judge.
   - `scripts/unblock_judge.sh` `unblock_ask_model` (~2191-2217): add `attempt_journal` to `judge_context.json` from the comments it already reads.
6. Staging lists: add `scripts/attempt_journal.py` to `clarify.yml:483`, `plan.yml:590`, `implement.yml:1474` and `scripts/stage_workflow_support.sh:57`.
7. Env: `PERSIST_JOURNAL_ENABLED: ${{ vars.PERSIST_JOURNAL_ENABLED || 'false' }}` in `clarify.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`, `orchestrate_poll.yml` and `unblock_judge.yml`.
8. Fragments: `agents.d/<n>-persist-attempt-journal.md`, with the marker, the schema and the `PERSIST` prefix plus `LOG_PREFIX.name=PERSIST` under "Stable log prefixes (contractual)". Also `changelog.d/<n>-persist-attempt-journal.md`.

### P2: Done-check (acceptance)

1. `prompts/_templates/mode-plan-acceptance.txt` [new] and the rendered `prompts/mode-plan-acceptance.txt` [new]. Contract (unfenced lines, anywhere after `## Files likely to change`):
   - `ACCEPTANCE: test <repo-path>[::<name>] [red-on-base]`, at most 10. `red-on-base` means the test must fail on the merge-base, and is required for bug fixes and regressions.
   - `ACCEPTANCE: check <check-run name>`, at most 5. CI check runs that must be green on the PR head.
   - Exactly one of:
     - `ACCEPTANCE: post-merge workflow <.github/workflows/file.yml> [job <job name>]`;
     - `ACCEPTANCE: post-merge none: <reason>`.
   - At least one `test` or `check` line.
   - Prefer tests the change adds or edits. Never name a test that needs network or credentials.
2. Install the fragment as `plan.yml:754-762` does the heal counterfactual, and append it in `run_plan_codex.sh` next to line 309, only when `PERSIST_ACCEPTANCE_ENABLED=true`. Neither `mode-plan.txt` copy nor the `run_plan_codex.sh` inline contract changes.
3. `scripts/acceptance_check.py` [new]:
   - **`validate --plan-file --repo-root --out`.**
     - Strips fenced blocks, reusing `partial_delivery._unfenced_lines`.
     - Parses the lines.
     - Checks that test paths exist at the planning ref or appear in the plan's `## Files likely to change` (`targeted_file_context.extract_paths_from_plan`).
     - Checks that check names match a job `name:` (or a job id when no name is set) in `.github/workflows/*.yml` at the planning ref.
     - Checks that the post-merge workflow file exists, and that its job, when named, exists in it.
     - Writes `acceptance.v1` JSON `{state: valid|invalid|none, tests[], checks[], post_merge{workflow, job}|null, errors[]}`.
     - No network.
   - **`marker --in <json>`.** Prints `<!-- AI_ACCEPTANCE_V1 <compact json> -->`.
   - **`parse --plan-comment-file`.** Extracts the marker. Implement already verifies the plan comment's author (`implement.yml:2570-2601`).
   - **`render --in <json> [--gate-result <file>]`.** The `## Acceptance` markdown section.
4. `plan.yml`: a new step after "Parse planning output" (1971-2115), switch-gated, that runs `validate` on `${CODEX_OUTPUT_FILE}`. "Post implementation plan" (2708-2759) appends the marker line before `<!-- ai:plan-proposal:v1 -->`. `invalid` logs `PERSIST acceptance outcome=invalid errors=<n>` (D4). Size: at most 1 KB.
5. `implement.yml`: in the PR-body compose step, after the gate summary (6134-6137), append `acceptance_check.py render` output when a marker exists. Size: at most 0.5 KB.
6. Heals: when the plan carries `HEAL_COUNTERFACTUAL: REPRODUCIBLE: <ids>` (#8409 P2) and no `ACCEPTANCE:` lines, `validate` derives `test <id> red-on-base` lines from it and `post_merge = null`, because #8409 P7 owns the check after merge. Without #8409 P2, heals follow the normal contract.
7. API: none.
8. Fragments.

### P3: Repair loop before the PR

1. `workflow-templates/.claude/hooks/prepush_affected_tests.py` (additive, §14):
   - `run --tests-file <file>`: one `path[::name]` per line, bypassing `select_tests` (346). It is merged with path selection when both are given. This is the interface #8409 P2 specifies; skip it if it is already present.
   - `run --expect-baseline-fail-file <file>`: those units are also run on `--baseline-tree`.
   - `result.json` gains an optional `acceptance` key: `{"<unit>": {"head": "pass|fail|unverified", "base": "pass|fail|unverified|not_run"}}`. The `RESULT_SCHEMA` value stays `prepush_test_gate.v1` (additive key).
2. `scripts/prepush_test_gate_run.sh`: pass both options through, mounted read-only.
3. `scripts/implement_step_prepush_test_gate.sh` `implement_prepush_test_gate_main` (619-722), behind `PERSIST_REPAIR_LOOP_ENABLED`:
   - Replace the single repair (688-706) with a loop. While `turn < PERSIST_REPAIR_MAX_TURNS`, status is `fail`, `pr_caused > 0`, `pr_caused` is strictly below the previous run's (after turn 1), and `elapsed + editor_reserve + run_wall <= step_budget`: repair, then re-run into `run<k+1>`.
   - A reverted repair (syntax) ends the loop.
   - The existing `gate_repair` strings gain `ran xN`.
   - When an `AI_ACCEPTANCE_V1` marker exists in `${PLAN_FILE}`, write the tests to `tests.txt` and the red-on-base ones to `baseline_fail.txt`, and pass them on every run.
   - Done-check met means every test passes on head, and every red-on-base test fails on base. A red-on-base test that passes on base is `unmet (passes on base)`.
   - Unmet done-check tests count toward `pr_caused` for loop purposes only.
   - Write `${RUNTIME_DIR}/acceptance_result.json`.
4. Label: in the PR-body step, when the done-check is unmet, add `--label ai:acceptance-unmet` to `PR_CREATE_ARGS` (6171-6178), after `ensure_label_exists`. The `## Acceptance` section (P2's renderer, used inline when P2's script is absent) lists each test's state. The "PR already exists" path (~6273) applies the label with the same call.
5. `scripts/review_autofix_step_prepush_test_gate.sh` main (579+) and its `--phase resolver` use: the same loop over `rpg_*`.
   - The done-check comes from the linked issue's plan comment, read from the comments review already fetches.
   - When the done-check is met on the new head and the PR carries `ai:acceptance-unmet` (labels are in the PR meta already fetched), remove it (1 DELETE).
   - When newly unmet, add it (1 POST).
6. `.github/ai/label_contract.v1.json` and `scripts/label_helpers.sh` (`_AI_LABEL_COLORS`, `_AI_LABEL_DESCS`): add `ai:acceptance-unmet` ("The PR does not yet meet its plan's done-check").
7. Env: `implement.yml` and `review_autofix.yml`, two lines each.
8. API: at most 1 label write per review round, only on a state change.
9. Fragments.

### P4: Evidence packs

1. `scripts/evidence_pack.py` [new], host-only, using `gh_api_retry.py` with `optional=True`.
   - **Command:** `build --repo --default-branch --paths-file --repo-root --out --max-bytes`.
   - **Open PRs.**
     - One call: `GET repos/<r>/pulls?state=open&sort=updated&direction=desc&per_page=50`. Keep same-repo heads with trusted authors, using `activation_open_prs_json`'s filter.
     - One aliased GraphQL query per 25 PRs for `files(first:100){path}`, at most 2 queries.
     - Render `#N title(<=120) — overlapping paths`, at most 10 PRs, no bodies.
   - **History.**
     - `git fetch --no-tags --filter=blob:none --depth=200 origin +refs/heads/<default>:refs/persist/history`. No API call; a fetch failure gives `history unavailable`.
     - For at most 10 paths: `git log -n 8 --format='%h %as %s' refs/persist/history -- <path>`. Subjects are scrubbed.
   - **Default-branch health.**
     - One call: `GET repos/<r>/actions/runs?branch=<default>&status=completed&per_page=10`.
     - Render `name conclusion created_at`. No logs.
   - **Output and failure.**
     - Fenced `=== BEGIN UNTRUSTED EVIDENCE PACK ===`, neutralised and scrubbed, capped at `--max-bytes`.
     - Fail-open: write a stub and exit 0.
     - Log `PERSIST evidence_pack outcome=ok|partial|skipped calls=<n> bytes=<n>`.
   - **API:** at most 4 calls per build.
2. Paths:
   - **Clarify:** repo paths found in the issue body and comments (`targeted_file_context.extract_paths_from_plan` run over the issue text).
   - **Plan:** the same, plus the previous plan's `## Files likely to change` when re-planning.
   - **Implement:** the approved plan's `## Files likely to change`.
   - Cap: 10 paths.
3. Wiring: a host step before each prompt build, switch-gated, appended last in each prompt (the heal-evidence slot pattern):
   - `clarify.yml` (before the concat at 1624-1640);
   - `plan.yml` (before `run_plan_codex.sh`; the script appends it at ~300);
   - `implement.yml` (append after 3177).
   - Staging lists as in P1.
4. Fragments.

### P5: Check after merge

1. `issue_pr_status.yml` `sync-issue-status`, merged path, behind `PERSIST_POSTMERGE_VERIFY_ENABLED`, delegated to `scripts/persist_postmerge_verify.py arm`:
   - **Skip:**
     - orchestrator-managed issues (body has `**Orchestrator metadata**`, D8);
     - heals (`ai:workflow-heal`) while `WORKFLOW_HEAL_VERIFY_ENABLED=true`;
     - PRs labelled `ai:skip-activation-verify`.
   - **Read:** the linked issue's comments, 1 paginated call. Take the done-check marker from the latest trusted plan comment.
   - **Post-merge workflow named:** post `<!-- AI_POSTMERGE_VERIFY_V1 {"pr":N,"merge_sha":"…","workflow":"…","job":"…"|null,"branch":"<base>","merged_at":"…","state":"pending"} -->` and add `ai:verify-pending`. The existing close still happens (D2).
   - **`post-merge none`, or no marker:** journal entry `outcome=ok` (done means CI green, which the merge already required).
   - **API:** 1 read and at most 3 writes per merged PR.
2. `scripts/persist_postmerge_verify.py check`, called from a new function `run_persist_postmerge_verify` in OPP, once per tick after the standalone loop, behind the switch.
   - **Candidates.** One REST call: `GET repos/<r>/issues?labels=ai:verify-pending&state=all&per_page=50`. Process at most `PERSIST_POSTMERGE_MAX_PER_TICK`. Markers come from each candidate's comments (1 call each, so at most 3 per tick).
   - **Runs.** From `_load_actions_runs_cached` (0 calls): the first completed run of the named workflow `path` on `branch`, created after `merged_at`, whose `head_sha` contains `merge_sha`. Containment is checked with 1 `compare` call, only when the SHAs differ. With a job named, 1 `actions/runs/<id>/jobs` call.
   - **Pass:** remove the label; journal `postmerge held`; log `PERSIST postmerge outcome=held`.
   - **Fail** (the run or named job failed):
     - reopen the issue and remove the label;
     - comment `## Check after merge failed` with the run link and the failing job;
     - journal `postmerge fail` with `failure_key`;
     - call the existing `retrigger_plan` action for the issue (OPP:18624);
     - log `outcome=recurred`.
     - When P6 is on, P6 decides instead between re-plan and escalation.
     - Without P6, at most `PERSIST_MAX_APPROACHES` automatic re-plans per issue, counted from journal or marker history. Past the cap, apply `ai:needs-human`.
   - **Past the window with no qualifying run:** remove the label; comment `unverified (no run of <workflow> within <h> h)`; journal it; `outcome=unverified`.
   - **API per tick:** 1 list, plus at most 3 × (1 comments read, 1 compare, 1 jobs read, 3 writes).
3. `orchestrate_poll.yml`: env lines. `.github/ai/label_contract.v1.json` and `scripts/label_helpers.sh`: add `ai:verify-pending` ("Merged; waiting for the check after merge").
4. Fragments.

### P6: Budget and progress governor

1. `scripts/persist_budget.py` [new], pure: `decide --journal-json --now --labels --caps-json` returns `{action: continue|replan|escalate|ignore, reason}`.
   - **Ignore:** labels include any security label (`ai:security-pass-failed`, or a label containing `security`), `ai:needs-human`, or `ai:closed`.
   - **Approaches:** distinct non-zero `approach` values.
   - **Rounds:** entries with stage `implement|review|resolver|postmerge` under the current approach.
   - **Failed round:** `outcome` is `fail`, or acceptance is `unmet`.
   - **Progress between two consecutive failed rounds:** a different `failure_key`, fewer `acceptance.unmet`, fewer `gate.pr_caused`, or a different `evidence_hash`.
   - **Order of checks:**
     1. Elapsed since the first entry exceeds `PERSIST_MAX_HOURS`: escalate (`max_hours`).
     2. Rounds on the current approach are at least `PERSIST_MAX_ROUNDS`, or there were `PERSIST_REPLAN_AFTER_FAILED_ROUNDS` consecutive failed rounds, or 2 consecutive no-progress failed rounds: re-plan if approaches are below `PERSIST_MAX_APPROACHES`, else escalate.
     3. Otherwise continue.
2. OPP wiring, behind `PERSIST_BUDGET_ENABLED`:
   - **Where:**
     - standalone: in `run_standalone_stall_recovery` (22418), after the per-issue skips (22600-22740) and before the action resolution;
     - managed: in the managed issue loop next to the stall checks.
   - **Input:** the journal comes from `_candidate_details_json` comments (0 calls). When `comments_available` is false or there are 100 or more comments, fetch once (1 call).
   - **On `replan`:**
     - the existing `retrigger_plan` action, with the governor's reason and a one-paragraph journal summary in the `/answer` text;
     - journal `governor replan`;
     - a state flag so the same decision is not repeated within the tick window.
   - **On `escalate`:**
     - add `ai:needs-human`, plus a comment with the reason and journal summary;
     - journal `governor escalate`.
     - The existing unblock scan picks it up (`BLOCK_LABELS`), and #8291's digest parking applies where merged.
   - **API:** at most 2 writes per decision. Decisions are capped at 5 per tick.
3. Env in `orchestrate_poll.yml`. Fragments.

### P7: Orchestrator context

1. `scripts/orchestrate_lib.py`, where status flips to `merged` (~3600-3640): when `PERSIST_ORCH_CONTEXT_ENABLED`, record `persist_merged_pr` (the PR number from `pr_states`) on the wave issue entry. Additive; other readers ignore it.
2. `scripts/persist_orchestrator.py` [new] `since-note --state-file --current-wave`. Renders `## Since this plan was decomposed` from state only:
   - merged items in earlier waves (issue, `persist_merged_pr`, `files_touched`);
   - judge-added issues;
   - the newest journal outcome per item when its journal is in the per-tick comment cache.

   At most 4 KiB.
3. OPP deferred creation (27414-27440) and the post-gate creation (31720+): insert the section between `DEF_BODY` and the metadata footer when enabled.
4. `orchestrate.yml` "Build Codex prompt" (~778-811), when enabled:
   - append `=== BEGIN UNTRUSTED OTHER OPEN PROJECTS ===`, built with 1 REST call (`issues?labels=ai:orchestrator-tracking&state=open&per_page=50`), titles only, at most 20;
   - when `scripts/evidence_pack.py` is present in the support checkout, append its pack for the paths named in the plan file referenced by the description.
5. Fragments.

### P8: Grouping repeated failures across items

1. `scripts/persist_orchestrator.py group --state-file --journals-json --now`, pure. Returns `{groups: [{key, owner, members[]}], release: [issue]}`.
   - **Key:** #8409 P5's `cause_key` from `scripts/workflow_failure_heal.py` when it exists, applied to the journal entry's stored workflow, step and signature. Otherwise the journal's `failure_key`.
   - **Owner:** the member with the earliest failing entry.
   - **Window:** `PERSIST_ORCH_GROUP_WINDOW_HOURS`.
   - **Release:** when the owner's newest key differs, its PR merged, or `PERSIST_ORCH_GROUP_MAX_HOURS` passed.
2. OPP managed loop, behind `PERSIST_ORCH_FAILURE_GROUPING_ENABLED`:
   - set or clear `grouped_under` on wave issue entries;
   - skip stall recovery and re-triggers for grouped items;
   - post one comment per item when it is grouped;
   - add `grouped_items` (titles and keys) to the owner's stall-judge diagnostics;
   - on release, clear the field and let normal stall recovery resume.
   - API: at most 1 comment per newly grouped item.
3. Fragments.

### P9: Check between waves

1. OPP, before the clean-wave skip (30650-30669), behind `PERSIST_ORCH_WAVE_CHECK_ENABLED`, when the skip conditions hold and `final_merge_pr` is set:
   - read the PR head SHA from the PR state the poller already has;
   - one call: `GET repos/<r>/commits/<sha>/check-runs?per_page=100`. Ignore `skipped` and `neutral`.
   - **All success:** proceed with the skip. Log `PERSIST wave_check outcome=green`.
   - **Any pending:** do not advance this tick. Record `wave_check_wait_since`. Past `PERSIST_ORCH_WAVE_CHECK_MAX_WAIT_MINUTES`, treat it as red.
   - **Any failure:** set `INVOKE_JUDGE_FOR_STUCK=true` so the existing judge path runs, and add a `=== BEGIN UNTRUSTED WAVE CHECK ===` block to the judge prompt (30790-31007). It holds the failing check names, one log tail of at most 8 KiB from the first failing check (1 call), and the wave items whose journals show `acceptance.state=unmet`.
   - **No `final_merge_pr`:** fail open to today's skip, logging `reason=no_final_pr`.
2. Journal entry `wave_check` on the tracking issue.
3. API: at most 2 calls per wave boundary per tick.
4. Fragments.

### P10: Re-decompose remaining waves

1. `prompts/mode-judge.txt`: no edit. When the switch is on, append `prompts/mode-judge-revise-option.txt` [new]. It documents the optional output field `revise_remaining: true` and `revise_reason`, used only when the remaining waves' plan no longer fits what merged.
2. OPP judge-output handling (~31133): when enabled and `revise_remaining` is true and `redecompose_count < PERSIST_ORCH_MAX_REDECOMPOSE`, run `prompts/mode-orchestrate-revise.txt` [new] through the existing poller judge runner (`poller_claude_judge`, read-only sandbox). Its input:
   - `project_body_snapshot`;
   - current `pending_issue_defs`;
   - the P7 since-note, rendered here if P7 is off;
   - journals;
   - the judge's reason.

   Its output: `{pending_issue_defs: {...}, dependency_edges: [...]}`, limited to pending ids or new ids.
3. Validation in `scripts/persist_orchestrator.py revise-validate`, all of which must pass:
   - the `orchestrate_schema.v1.json` item shape;
   - ids do not collide with created issues;
   - `validate_wave_file_partition` and `compute_waves` succeed on the remaining graph;
   - `implementation_minutes_estimate` is at most 60.

   On success: store the old defs in `pending_issue_defs_previous`, replace them, increment `redecompose_count`, and post a tracking comment listing removed and added titles. On failure: leave state unchanged and log `PERSIST redecompose outcome=invalid`.
4. API: 1 comment. The model call reuses the judge runner's budget.
5. Fragments.

### P11: Evidence broker

1. `prompts/_templates/mode-plan-evidence-request.txt` and the rendered `prompts/mode-plan-evidence-request.txt` [new], appended when enabled. When the planner needs evidence it cannot see, it ends with `PLAN_SELF_CHECK: BLOCKER: evidence needed`, `STATUS: NOT_CLEAR`, and at most 10 lines from this menu:
   - `EVIDENCE_REQUEST: git_log <path>`
   - `EVIDENCE_REQUEST: git_show <40-hex>:<path>`
   - `EVIDENCE_REQUEST: git_diffstat <40-hex>`
   - `EVIDENCE_REQUEST: run_steps <run_id>`
   - `EVIDENCE_REQUEST: run_log_tail <run_id> <job_id>`
   - `EVIDENCE_REQUEST: pr_files <n>`
   - `EVIDENCE_REQUEST: pr_diff <n>`
   - `EVIDENCE_REQUEST: issue <n>`
2. `scripts/evidence_broker.py` [new]:
   - **`validate`.** Regexes per kind; numbers only; this repository only. Paths must be repo-relative, have no `..`, and pass `codex_isolated_workspace.py`'s credential-path filter. Unknown kinds are refused per line.
   - **`fulfil`.**
     - **Git kinds:** from `refs/persist/history`, fetching it as in P4 if absent. `git show` is capped at 4 KiB and refused for files the filter rejects.
     - **GitHub kinds:** 1 call each through `gh_api_retry.py`; `run_log_tail` takes the last 4 KiB.
     - Each answer is fenced as `=== BEGIN UNTRUSTED BROKER RESULT <kind> <arg> ===`, neutralised and scrubbed. Totals are capped at `PERSIST_EVIDENCE_BROKER_MAX_BYTES`.
     - Logs `PERSIST broker outcome=ok requests=<n> refused=<n> calls=<n>`.
3. `plan.yml` (via `run_plan_codex.sh`): after the first planner output, if it ends `NOT_CLEAR` with valid requests and the switch is on, fulfil them and re-run the planner once with the results appended (`PERSIST_EVIDENCE_BROKER_MAX_ROUNDS`, fixed at 1 in this phase). The second output goes through the normal paths. If it still asks, it is blocked as today.
4. API: at most 10 calls per plan run. The journal records `evidence_hash`.
5. Fragments.

## Files & Modules

- **New scripts:**
  - `scripts/attempt_journal.py` [new] (P1)
  - `scripts/acceptance_check.py` [new] (P2)
  - `scripts/evidence_pack.py` [new] (P4)
  - `scripts/persist_postmerge_verify.py` [new] (P5)
  - `scripts/persist_budget.py` [new] (P6)
  - `scripts/persist_orchestrator.py` [new] (P7, P8, P10)
  - `scripts/evidence_broker.py` [new] (P11)
- **New prompts:**
  - `prompts/mode-persist-journal-rules.txt` [new] (P1)
  - `prompts/_templates/mode-plan-acceptance.txt`, `prompts/mode-plan-acceptance.txt` [new] (P2)
  - `prompts/mode-judge-revise-option.txt`, `prompts/mode-orchestrate-revise.txt` [new] (P10)
  - `prompts/_templates/mode-plan-evidence-request.txt`, `prompts/mode-plan-evidence-request.txt` [new] (P11)
- **Existing scripts:**
  - `scripts/run_plan_codex.sh` (P1, P2, P4, P11: appends only)
  - `scripts/implement_step_prepush_test_gate.sh` (P1, P3)
  - `scripts/review_autofix_step_prepush_test_gate.sh` (P1, P3)
  - `scripts/review_apply_fixes.sh`, `scripts/review_conflict_resolve.sh` (P1)
  - `scripts/prepush_test_gate_run.sh` (P3)
  - `workflow-templates/.claude/hooks/prepush_affected_tests.py` (P3, additive; ships to consumers per §14)
  - `scripts/orchestrate_poll_process.sh` (P1, P5, P6, P7, P8, P9, P10)
  - `scripts/orchestrate_lib.py` (P7)
  - `scripts/unblock_judge.sh` (P1)
  - `scripts/stage_workflow_support.sh` (staging list, P1, P3, P4)
- **Workflows (env and thin steps only):**
  - `.github/workflows/clarify.yml` (P1, P4)
  - `.github/workflows/plan.yml` (P1, P2, P4, P11)
  - `.github/workflows/implement.yml` (P1, P2, P3, P4)
  - `.github/workflows/review_autofix.yml` (P1, P3)
  - `.github/workflows/issue_pr_status.yml` (P5)
  - `.github/workflows/orchestrate_poll.yml` (P1, P5–P10)
  - `.github/workflows/orchestrate.yml` (P7)
  - `.github/workflows/unblock_judge.yml` (P1)
- **Labels:** `.github/ai/label_contract.v1.json` and `scripts/label_helpers.sh` (P3, P5).
- **Tests:**
  - new: `tests/test_attempt_journal.py`, `tests/test_acceptance_check.py`, `tests/test_evidence_pack.py`, `tests/test_persist_postmerge_verify.py`, `tests/test_persist_budget.py`, `tests/test_persist_orchestrator.py`, `tests/test_evidence_broker.py`;
  - extended: `tests/test_prepush_affected_tests.py`, `tests/test_implement_prepush_test_gate.py`, `tests/test_review_prepush_test_gate.py`, `tests/test_orchestrate_poll_process.py`, `tests/test_workflow_untrusted_input_contract.py`, `tests/test_workflow_file_size_limit.py` (unchanged guard, re-run).
- **Docs:** one `agents.d/` and one `changelog.d/` fragment per phase. README sections arrive through the `agents.d/` fragments' text (§30). `docs/scripts-pending-removal.md`: no entries (§18.F, see the §18 table).

## Data Model / Index Changes

None. No MongoDB collections, indexes or contracts are involved (§10).

The new persisted shapes are GitHub-side only:
- the `AI_ATTEMPT_JOURNAL_V1` and `AI_POSTMERGE_VERIFY_V1` comments;
- the `AI_ACCEPTANCE_V1` marker inside the plan comment;
- the tracking-state fields `persist_merged_pr`, `grouped_under`, `wave_check_wait_since`, `redecompose_count` and `pending_issue_defs_previous`. They are additive, and existing state readers ignore unknown fields.

## Tests

- **Unit, per phase, pure functions with table-driven fixtures:**
  - journal parse, merge, render and trust filtering, including forged markers from non-pipeline authors;
  - done-check parse and validate, covering missing paths, unknown checks, both post-merge forms, fenced lines ignored, and the heal `REPRODUCIBLE` derivation;
  - repair-loop stop rules: shrinking, flat, reverted, budget exhausted, and the red-on-base passes-on-base case;
  - evidence pack rendering and caps;
  - post-merge outcomes: held, recurred, unverified, and the heal skip;
  - governor decisions for every rule and cap, plus the security ignore;
  - grouping and release;
  - wave-check states;
  - re-decompose validation rejects;
  - broker validation refusals: other repo, `..`, credential path, wrong kind, over the cap.
- **Switch-off byte identity.** For every phase, render the affected prompt, PR body, issue body or plan comment with the switch off and compare it to fixtures captured from `main` at c4c4f74.
- **API budget.** Each new call path runs against a fake `gh` that counts calls. Limits:

  | Path | Max calls |
  |---|---|
  | Journal | 2 per stage |
  | Evidence pack | 4 |
  | Arm at merge | 4 |
  | Post-merge check | 1 + 6 × `MAX_PER_TICK` |
  | Governor | 2 per decision |
  | Wave check | 2 |
  | Broker | 10 |
- **Replay.**
  - PR #8371's two review rounds: the editor's deletion of the fixture in `tests/test_promote_main_cycle.py`, then the gate failure. Assert that the repair prompt contains the deletion and the rule.
  - A recorded release-gate payload with P5's marker shows `recurred` for runs 40876853022 / 40932430968 and `held` for a later green run.
- **Docker end to end** (skipped without docker, following `tests/test_codex_isolated_exec_docker_e2e.py`): the gate with `--tests-file` and `--expect-baseline-fail-file` on a two-commit fixture repo, where one test fails on base and passes on head.
- **Untrusted-input contract.** Extend `tests/test_workflow_untrusted_input_contract.py`: every new block is fenced, fence-forgery-safe, and appended after memory.

## Risks & Mitigations

- **Loops that look like progress but aren't.**
  - The progress rule requires a measurable change.
  - The caps (3 approaches, 5 rounds, 48 h) are hard.
  - Escalation goes to the existing unblock judge and digest, not to more retries.
- **Runner minutes grow with the repair loop.**
  - The loop is bounded by the existing gate step budget (3300 s) and continues only while failures shrink.
  - It should be offset by fewer review rounds: review runs reached a median of about 81 minutes on 10-29.
  - Enable it after #8410 so the loop does not chase sandbox noise.
- **Done-checks that are too weak** (for example, an existing test that already passes).
  - `red-on-base` is required for bug fixes, and the gate verifies it.
  - Reviewers see the `## Acceptance` section.
  - A done-check never auto-approves (D4, Non-goals).
- **A post-merge workflow that rarely runs leaves issues in `ai:verify-pending`.** The 24-hour window ends as `unverified` and removes the label. Leftover labels are cleared even with the switch off.
- **A post-merge failure caused by something else re-plans the wrong issue.**
  - The check requires the run to contain the merge commit, and the named job to fail.
  - The re-plan prompt carries the run link, and the planner may conclude "not caused by this change". The governor caps re-plans.
  - `ACCEPTED`: some false reopens will happen; each costs one plan run.
- **Journal comment races** (two stages writing at once). `ACCEPTED` (D6): writes re-read and merge just before writing. A lost entry only weakens advice, never correctness.
- **Prompt injection through evidence, journal notes, broker results or other PR titles.**
  - All of it is fenced untrusted, neutralised and scrubbed.
  - Only same-repo, trusted-author PRs and titles are included, with no bodies.
  - Models stay credential-free and offline.
- **Broker data exposure.** Reads are limited to the same repository the pipeline already serves, through the credential-path filter. Logs are the same data heal evidence already uses. No cross-repo reads.
- **Re-decomposition produces worse waves.**
  - It is limited to pending defs, validated by the same guards as the first decomposition, and capped at 2.
  - The previous defs are kept for restore.
  - It is off until the earlier phases have run cleanly.
- **Grouping pauses an item whose failure only looks similar.** The pause is limited to 12 h, released on any owner change, and the owner's judge sees all members.
- **Wave check holds a project on a flaky check.** It is limited to 90 minutes, then the existing wave judge decides. It fails open when there is no integration PR.
- **API budget.** The worst case per poller tick across P5, P6, P8 and P9 is about 30 calls, all from batched or cached data where possible. All calls fail open and use the rate-limit breaker (`gh_api_retry.py` `optional=True`).
- **Workflow size (§27).** Logic is in scripts. Each PR checks `wc -c`. The total growth budget is stated under Constraints.
- **Interface overlap with #8409.**
  - `--tests-file` uses the same spec in both plans; the second implementer reuses it.
  - `cause_key` is optional input to P8.
  - If #8409 changes either interface in review, this plan follows it. `ACCEPTED — pending #8409's review outcome.`

## Rollout

1. Merge phases in any order, all with switches off. Each PR proves byte identity with its switch off.
2. Prerequisites before enabling anything:
   - #8410 merged;
   - #8291 / PR #8307 merged;
   - #8409 P2, P5 and P7 enabled for heals.
3. Enable in this order, watching at least 3 issues per step before the next:
   1. `PERSIST_JOURNAL_ENABLED` (observe only).
   2. `PERSIST_ACCEPTANCE_ENABLED`.
   3. `PERSIST_EVIDENCE_PACK_ENABLED`.
   4. `PERSIST_REPAIR_LOOP_ENABLED`.
   5. `PERSIST_POSTMERGE_VERIFY_ENABLED`.
   6. `PERSIST_BUDGET_ENABLED`.
   7. `PERSIST_ORCH_CONTEXT_ENABLED`, then `PERSIST_ORCH_WAVE_CHECK_ENABLED`, then `PERSIST_ORCH_FAILURE_GROUPING_ENABLED`.
   8. `PERSIST_ORCH_REDECOMPOSE_ENABLED`.
   9. `PERSIST_EVIDENCE_BROKER_ENABLED`.
4. After a clean week for a phase, open one follow-up PR that flips its default to `true`, keeping the switch (D1).
5. Rollback for any phase: set its variable to `false`. No migration is needed. Markers, labels and state fields written by a phase are ignored when it is off; `ai:verify-pending` is cleaned up regardless.
6. Consumers (§14): every phase is default-off per repository. The two labels and the additive gate options arrive on the next `@stable`, unused until a consumer sets the variables.

## References

- Discussion decisions Q6–Q11 (2026-10-29/30, interactive session).
- Plans:
  - `docs/plans/heal-evidence-reproduction-and-trends-plan.md` (#8409)
  - `docs/plans/remove-unattended-pipeline-stuck-states-plan.md` (#8291, PR #8307)
  - `docs/plans/prepush-affected-test-gate-plan.md` (#8192)
  - `docs/plans/heal-scope-linked-runs-script-closure-plan.md` (#8290)
  - `docs/plans/orchestrator-prerequisite-gate-automation-plan.md`
  - `docs/plans/heal-issues-self-resolve-plan.md` (#8200)
- PRs:
  - #8410 (gate sandbox: `jq`, exec `/tmp`)
  - #8382 (gate baseline timeout)
  - #8371 (review/gate flip-flop)
  - #8335 (open-PR duplicate rule)
- Release runs: 40608957733, 40831286935, 40876853022, 40932430968.
