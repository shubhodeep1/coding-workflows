# Unattended Claude pipeline: completion plan

## Summary

Take the coding pipeline from its 2026-10-07 state (release frozen since
2026-10-03, 33 PRs queued behind one blocked head, every Claude-edited PR
failing its editor round, OpenRouter credit nearly spent) to the intended
end state: a fully unattended pipeline that runs every model role on the
Claude account pool by default, falls back to codex on OpenRouter only when
every pool account is at its usage gate (with an alert), blocks for a human
only after the automated judges have exhausted their rounds, and keeps the
codex pipeline one variable away (`AI_ENGINE=codex`).

Phases 1 and 2 are operational and run from the interactive session that
wrote this plan. Phases 3 to 5 are code work for the orchestrator
(`/implement-plan-claude`), with an Opus 5.5 interactive session as the
fallback executor if the orchestrator is not healthy when they start.

## Automation wiring (§18.E)

| Question | Answer |
|---|---|
| New script / extended script / code-only? | Code-only. Every change extends an existing script, workflow or prompt. No new standalone script. The one new helper, `scripts/ai_engine_fallback_report.sh`, already exists on PR #6615 and runs as a workflow job, never by hand. |
| Scheduler / PR-push entry points | Review runs: `.github/workflows/review_autofix.yml` (phases 1, 4). Phase jobs: `clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`, `validate.yml`, `security-audit.yml`, `workflow-failure-heal-intake.yml`, `check_failure_triage.yml`, `workflow-log-analysis.yml`, `validation-refresh.yml`, `unblock_judge.yml` (phase 3). Poller tick: `orchestrate_poll.yml` → `scripts/orchestrate_poll_process.sh` (phase 4). Release: `promote-main-to-stable.yml` → `test-and-mark-stable.yml` (phase 2, 5). |
| New long-running supervisor (§18.C)? | No. Everything rides the existing poller, sweep, promote and review crons. |
| DB work (§18.D)? | None. No MongoDB collections, indexes or contracts are touched (§10 N/A). |
| `docs/scripts-pending-removal.md` (§18.F) | No new entries. |

## Context

Findings from the 2026-10-07 audit (50 open PRs, 55 open issues, run logs,
run artifacts, OpenRouter management API):

1. **Claude editor output rejected.** The review editor runs on Claude
   Opus 5.5 since Phase 5c (#6208). `scripts/review_apply_fixes.sh` only
   accepted the full audit labels (`total issues listed`, `issues applied`,
   `issues already applied`, `issues ignored`); Opus writes
   `total 5; applied 0; already applied 0; ignored 5`. All three attempts
   of every round were rejected, the run finalized as "editor no-op
   suspicious", and the third round tripped the identical-failure cap
   (#6605 run 37565800725; same fingerprint `29efdb47…` on #6606, #6614).
   Fixed by PR #6622.
2. **Conflict resolver fails closed on the manifest.** The deterministic
   union-merge for `.ai/.workspace_source_manifest.txt` in
   `scripts/review_conflict_prepare.sh` never matched (stage pattern), so
   every PR with a base conflict on that file died with
   `sandbox_path_unsupported` and was then capped. Blocks #6594 (forward
   merge stable→main), #6515 (merge-train fix), #6146 (train head), #6209
   (Phase 5d), #6509, #6593. Fixed by PR #6614.
3. **Release gate broken on main and stable.** Phase 0a of
   `test-and-mark-stable.yml` imports pytest before it is installed
   (runs 37554001238, 37584198174). `stable` is v1.29.15 (c05e521), 70
   commits behind main; consumers have none of the Claude engine work or
   the reviewer-panel cost cut. Fixed by PR #6606.
4. **Merge train serialized on the manifest.** 33 `ai/issue-*` PRs carry
   `ai:merge-queued` because every implementation commit touches the
   generated manifest (#6508 → PR #6515).
5. **OpenRouter.** $1.9k of credit left of $98k; $12k spent in the last 7
   days, $35k in 30 days, at $1k to $3.4k a day. Between 2026-10-04 and
   2026-10-06 all 129 implement jobs that selected Claude silently ran on
   codex (`cli_missing`, fixed by #6481) with no alert.
6. **Claude engine coverage.** Phases 0 to 4, 5a to 5c, 6, 7 and 8a to 8d
   of `replace-claude-sessions-with-cli-engine-plan.md` are on main. Phase
   5d (validate, security audit, heal, triage, log analysis, retro,
   summariser, materiality, behavioural smoke) is not: #6209 is 70 commits
   and 87 files, conflicting with main, with 33 identical review failures.
   #6610 (audit role only) overlaps it. `UNBLOCK_JUDGE` and
   `ACTIVATION_VERIFY` still default to codex.
7. **Pool.** Three accounts (FUNTOKEN1, FUNTOKEN2, PERSONAL1), probed live
   at every job start, gated at 90% of the 5-hour or 7-day window.
8. **Reviewer panel.** The glm-5.2 → mistral-small swap and the seeded size
   tiers (#6438) are live on main and correct; stable still runs glm-5.2
   and grok-4.20 until the next release.
9. **Security.** The per-PR pass is off by default since #6562. Five audit
   findings sit in `ai:clarification` (#6306, #6518, #6532, #6540, #6548).
   The convergence plan (line ownership) is unimplemented; the stuck-states
   plan (#6519) merged as a document only.

## Decisions (operator answers, 2026-10-07)

### D1 — Fallback policy: codex only for capacity

- **Chosen:** When Claude cannot run for a reason other than account
  limits (CLI missing, broker refused, relay dead, output-format defect),
  the step fails, a heal report is filed and a Telegram WARNING is sent.
  Codex on OpenRouter runs only when every pool account is at the usage
  gate (`all_gated`) or every tried account returned `usage_limit`, with
  one Telegram note per run. Switch: `AI_ENGINE_FALLBACK_POLICY`, default
  `capacity`; `always` restores today's behaviour.
- **Alternatives considered:** keep the silent fallback for every reason
  and only add alerts (PR #6615 as written).
- **Why:** Q1 = A. The 129 silent codex runs of 10-04 to 10-06 cost
  OpenRouter spend and hid a defect for two days.

### D2 — Bootstrap PRs merge by hand, once

- **Chosen:** #6622, #6606 and #6614 are merged by the operator after CI
  is green. Everything after them goes through the pipeline.
- **Alternatives considered:** everything through the pipeline (the
  pipeline is what they fix); one-time blanket merge approval.
- **Why:** Q2 = A.

### D3 — Phase 5d is re-cut

- **Chosen:** #6209 is closed. Its commits are re-cut onto current main as
  four PRs (3a validate roles, 3b security audit = #6610, 3c heal, triage,
  activation verify and unblock judge, 3d log analysis, retro and the
  review utility roles).
- **Alternatives considered:** rebase #6209 as one PR.
- **Why:** Q3 = A. 87 files and 33 identical failures; smaller PRs clear
  review in one round each.

### D4 — Security passes

- **Chosen:** The per-PR pass stays off. The weekly default-branch audit
  and the project-level pass stay, and the project pass converges: a
  finding blocks only when every cited line was written by the project
  (`git blame` against the integration base); other findings become
  non-blocking `ai:security` follow-ups.
- **Alternatives considered:** per-PR pass back on with the same rules;
  weekly audit only.
- **Why:** Q4 = A. The per-PR pass drove most OpenRouter spend and never
  converged (#6135: findings in 4 of 5 cycles).

### D5 — Waiting security clarifications

- **Chosen:** The implementing session answers #6306, #6518, #6532, #6540
  and #6548 with the best engineering decision and lets the pipeline
  continue.
- **Alternatives considered:** list them for the operator; close as not
  planned.
- **Why:** Q5 = A.

### D6 — Exhausted judge rounds park, never close

- **Chosen:** When every automated judge round is spent, the item is
  parked in one standing `ai:needs-human` digest issue with one CRITICAL
  alert. The item stays open. `ai:unblock-closed` is no longer applied by
  the unblock judge's exhaustion path.
- **Alternatives considered:** keep closing or marking failed.
- **Why:** Q6 = A; stuck-states plan P6.

### D7 — OpenRouter stays funded; the panel stays

- **Chosen:** The operator tops up OpenRouter. Spend is cut by D1 and by
  Phase 3, not by changing the six-vendor reviewer panel.
- **Alternatives considered:** treat OpenRouter as capacity-limited and
  move the panel too.
- **Why:** Q7 = A, plan Q18.

### D8 — Consumers follow the release

- **Chosen:** coding-workflows first. Consumers receive everything through
  the next green stable release and their daily sync. Their state is
  checked in Phase 5.
- **Alternatives considered:** audit each consumer's variables now.
- **Why:** Q9 = A.

## Goals

- **G1 — Pipeline unblocked.** #6605 merged; the forward merge of stable
  into main merged; the 33 queued PRs drained (merged, or closed with a
  reason); a green stable release carries the Claude engine work and the
  panel change to consumers.
- **G2 — Claude in every role by default.** With no variables set, every
  role in `.github/ai/claude_engine.json` except the six reviewer slots
  has `"engine": "claude"`, including `UNBLOCK_JUDGE` and
  `ACTIVATION_VERIFY`. Verified by `claude-engine-smoke.yml` and one real
  run per role logging `AI_ENGINE_SELECTED … engine=claude`.
- **G3 — Fallback only on capacity, never silent.** `AI_ENGINE_FALLBACK`
  with a non-capacity reason fails the step and opens one heal issue per
  role and reason; a capacity fallback sends one Telegram note per run.
  Verified by `tests/test_ai_engine.py` and the heal intake tests from
  #6615.
- **G4 — Codex one switch away.** `AI_ENGINE=codex` or
  `AI_ENGINE_<ROLE>=codex` produces the pre-plan command line. Unchanged
  from the engine plan's G4.
- **G5 — No human before the judges are spent.** Every terminal path in
  the poller, the review-blocked judge and the unblock judge ends in the
  needs-human digest, not in a close.
- **G6 — Security converges.** A project's security pass blocks only on
  project-written lines, and `MAX_SECURITY_PASS_CYCLES` is reached only
  when a project-written finding persists.
- **G7 — Spend visible.** The weekly retro reports OpenRouter spend per
  model from the management API beside the pool's per-account usage.

## Non-goals

- Changing the six-vendor reviewer panel or its editor (`openai/gpt-6-sol`
  stays the codex-path editor).
- The stuck-states plan items not named here (P2 single support-script
  source, P7 latch gaps, P8 dependency waits, P9 clarify snapshot, P11
  retired labels and zombies, P12 contract-list union). They stay on that
  plan.
- Buying or managing Claude accounts.

## Constraints

- **§6.** No existing identifier is renamed or removed. New identifiers
  (checked unique on 2026-10-07): variable `AI_ENGINE_FALLBACK_POLICY`,
  label `ai:needs-human`, marker `<!-- ai:needs-human:v1 … -->`, log
  prefix `NEEDS_HUMAN`.
- **§15.** The digest is one upserted issue; the unblock judge already
  scans once per label set per tick. No per-item API calls are added.
- **§19.** No auto-close keywords against tracking issues.
- **§20.** One `changelog.d/` fragment per PR.
- **§21.** Every commit checks its branch has no merged PR.
- **§23.C.** Workflow dispatches, PR merges and closes of PRs the session
  did not open are asked for once, as a list, per phase.
- **§25.** No PR-activity subscriptions.
- **§27.** `review_autofix.yml` is at 465,643 bytes after #6614. Phase 4
  changes to it move step bodies into `scripts/` first.

## Phases & Merge Strategy

| Phase | What | Executor | Depends on |
|---|---|---|---|
| 1 Bootstrap | Merge #6622, #6606, #6614. Re-dispatch review on #6605 and merge it. | interactive session + operator merges | — |
| 2 Drain | Re-dispatch #6594, #6515, #6146, #6509; close #6593 (superseded by #6606); merge main into #6615 and re-scope it to D1; close #6209 (D3); let the train drain; get the nightly promote cycle green; release. | interactive session | 1 |
| 3 Claude everywhere | 3a `VALIDATE`, `VALIDATE_SELF_HEAL`, `VALIDATION_REFRESH`; 3b `SECURITY_AUDIT` (= #6610); 3c `WORKFLOW_HEAL`, `CHECK_TRIAGE`, `ACTIVATION_VERIFY`, `UNBLOCK_JUDGE`; 3d `LOG_ANALYSIS`, `LOG_AUDIT`, `LOG_SUMMARY`, `RETRO`, `SUMMARISER`, `MATERIALITY`, `BEHAVIOURAL_SMOKE`; 3e fallback policy (D1, from #6615). | orchestrator (`/implement-plan-claude`), Opus session as fallback | 2 |
| 4 Blocker reduction | 4a needs-human digest (D6); 4b security convergence (D4); 4c answer the five clarification issues (D5); 4d operator-step issue drains itself when the promote cycle completes. | orchestrator | 3 |
| 5 Verify and release | Smoke matrix, panel check on stable, consumer sync check, docs, retro spend section (G7). | interactive session | 4 |

Each phase-3 and phase-4 item is one PR into `main`, independently
mergeable.

## Implementation Steps

### Phase 1 — Bootstrap

1. Operator merges #6622 (editor labels), #6606 (release gate), #6614
   (resolver and non-retryable cap) once their CI is green.
2. Re-dispatch `internal-review.yml` for #6605. The editor round completes
   (new support SHA on main), auto-merge lands it.
3. Verify with one review run that `AI_ENGINE_SELECTED role=REVIEW_EDITOR
   engine=claude` is followed by an accepted summary (no
   `expected exactly one entry` line).

### Phase 2 — Drain

1. Re-dispatch review on #6594. The manifest conflict resolves
   deterministically; the forward merge lands.
2. Re-dispatch #6515. Once merged, `MERGE_TRAIN_IGNORE_PATHS` stops the
   manifest from queueing PRs; the poller releases the train on its next
   tick.
3. Re-dispatch #6146 (train head). Then watch the queue drain; intervene
   only on heads that fail twice.
4. Close #6593 as superseded by #6606; #6591 closes itself when the gate
   passes on stable.
5. Merge main into #6615 and re-scope it to D1 (Phase 3e carries the code
   change if the rebase is too wide).
6. Close #6209 with a comment naming the Phase 3 PRs that replace it.
7. Let the nightly `promote-main-to-stable.yml` cycle run. If it does not
   start by itself, dispatch it (one §23.C ask).

### Phase 3 — Claude everywhere

- **3a validate roles.** From #6209: `validate.yml`, `validation-refresh.yml`,
  `scripts/validate_process.sh`, `scripts/self_heal_validation.sh`,
  `scripts/validation_discovery_bootstrap.py`, the trusted engine root
  under `${RUNNER_TEMP}/claude-engine-support`, `claude_run_selected` in
  `scripts/ai_engine.sh`. Role defaults flip to `claude`.
- **3b security audit.** #6610 as-is, rebased if needed.
- **3c heal, triage, activation, unblock.** From #6209:
  `workflow-failure-heal-intake.yml`, `check_failure_triage.yml`,
  `scripts/workflow_failure_heal_intake.sh`, `scripts/check_failure_triage.sh`,
  plus `issue_pr_status.yml` / `orchestrate_poll.yml` activation verify and
  `unblock_judge.yml` / `scripts/unblock_judge.sh`. Role defaults flip.
- **3d log analysis and utility roles.** From #6209:
  `workflow-log-analysis.yml`, `scripts/workflow_retro_fanout.sh`,
  `scripts/summarize_unselected_runs.py`, `scripts/summarize_reviewer_consensus.sh`,
  `scripts/review_synthesise_smoke.sh`, `scripts/review_agents_md_materiality.sh`,
  the implement-issue summary step. Utility roles on Sonnet 5.5.
- **3e fallback policy.** `ai_engine_fallback <role> <reason> [class]`
  records every fallback (from #6615). `claude_run` reads
  `AI_ENGINE_FALLBACK_POLICY` (default `capacity`): a non-capacity reason
  returns a new exit code 76 and the call site fails the step with
  `::error::AI_ENGINE_FALLBACK_REFUSED role= reason=`; the phase job's
  `engine-fallback-report` job files the heal report and the Telegram
  WARNING. `always` keeps exit 75 and the codex path. `tests/test_ai_engine.py`
  covers both policies for every call site.

### Phase 4 — Blocker reduction

- **4a needs-human digest.** `scripts/unblock_ledger.py` exhaustion path
  and `scripts/orchestrate_poll_process.sh` terminal paths call one helper
  in `scripts/operator_step_issue.py`'s module (extended, not new) that
  upserts the `ai:needs-human` digest issue, adds the item's line, labels
  the item `ai:needs-human`, and sends one CRITICAL. Nothing closes.
- **4b security convergence.** `scripts/security_audit.sh` gets
  `SECURITY_AUDIT_LINE_OWNERSHIP` (default `project`): for each finding it
  blames the cited lines against the integration base; findings with no
  project-written line are emitted with `"advisory": true`. The poller's
  security pass counts only blocking findings toward
  `MAX_SECURITY_PASS_CYCLES` and files advisories as `ai:security` issues
  that do not gate the project.
- **4c clarifications.** Answer #6306, #6518, #6532, #6540, #6548 in the
  issue thread (`/answer`), choosing the fix the finding recommends unless
  it conflicts with §6.
- **4d operator steps.** `prompts/mode-activation-verify.txt` stops
  classifying "promote and release" and "consumer sync" as operator steps;
  `scripts/operator_step_issue.py` ticks an entry when the promote cycle
  reports the stable tag past the merge.

### Phase 5 — Verify and release

1. Dispatch `claude-engine-smoke.yml` and confirm every role logs
   `engine=claude`.
2. Confirm the stable panel is `minimax-m3, mistral-small-2603,
   deepseek-v4-pro, gemini-3.1-flash-lite, qwen3.7-plus, gpt-6-luna`.
3. Read each consumer's last `AI Update Workflows` run; report any
   `ALLOW_WORKFLOW_EDITS=false` or failed sync.
4. Add the OpenRouter spend-per-model table to the weekly retro
   (`scripts/workflow_retro.py`) from `GET /api/v1/activity`, keyed by the
   existing `OPENROUTER_API_KEY`'s account (read-only; no new secret).

## Tests

- Phase 3: `tests/test_claude_engine_utility_roles.py` (from #6209),
  `tests/test_ai_engine.py`, `tests/test_claude_engine.py`,
  `tests/test_security_audit_workflow_contract.py` (from #6610),
  `tests/test_unblock_judge.py`.
- Phase 4: `tests/test_unblock_judge.py` (digest instead of close),
  `tests/test_orchestrate_poll_process.py -k security` (advisory
  findings), `tests/test_security_audit_workflow_contract.py` (line
  ownership), `tests/test_operator_step_issue.py`.
- Every PR runs `tests/test_workflow_file_size_limit.py`.

## Risks & Mitigations

- **Pool capacity.** Two usable accounts today. Mitigation: D1 keeps the
  codex path for `all_gated`; the operator can add accounts in
  claude-workers at any time.
- **Re-cut of #6209 drifts from the reviewed version.** Mitigation: each
  3x PR cites the #6209 commits it carries; the 5d tests travel with it.
- **Line ownership misclassifies a finding.** Mitigation: default to
  blocking when blame cannot be computed; advisory findings are still
  filed as issues.
- **Digest issue grows unbounded.** Mitigation: entries are removed when
  the item merges or closes; the issue body is capped at 200 entries with
  an overflow count.

## Rollout

Phases 1 and 2 are operational and reversible by closing a PR. Phase 3
PRs each have `AI_ENGINE_<ROLE>=codex` as rollback. Phase 3e has
`AI_ENGINE_FALLBACK_POLICY=always`. Phase 4a has `NEEDS_HUMAN_DIGEST_ENABLED`
(default `true`, from the stuck-states plan). Phase 4b has
`SECURITY_AUDIT_LINE_OWNERSHIP=off`.

## References

- `docs/plans/replace-claude-sessions-with-cli-engine-plan.md`
- `docs/plans/remove-unattended-pipeline-stuck-states-plan.md`
- `docs/plans/security-pass-convergence-plan.md`
- PRs #6622, #6606, #6614, #6615, #6209, #6610, #6515, #6594
