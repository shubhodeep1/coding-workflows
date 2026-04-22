# Resilient Codex/OpenAI Failure Handling, Universal Label Auto-Creation, Per-Phase Resume Signals, and Poller Label-Repair Sweep

> Status: **READY — Q1–Q6 answered 2026-04-18 (see Appendix A).**
> Owner: orchestrator (implementation will be driven by the AI orchestrator pipeline).
> Source: resumed from a prior planning session; this document is the canonical plan of record.

---

## 1. Background

The AI pipeline (clarify → plan → implement → review → validate) is driven by `codex exec` calls against OpenAI/OpenRouter. These calls are prone to transient failures:

- Empty stdout despite `RC=0` ("blank completion").
- Non-zero RC after a watchdog timeout (e.g. `implement.yml` `RC=142` idle-kill at `.github/workflows/implement.yml:1022`).
- Output that parses but produces no file changes.
- HTTP 5xx / rate-limit / SSE disconnects mid-stream.

Today only the implement step has structured post-Codex fallbacks (`implement.yml` → `diagnose_post_codex_failure`, `ai:implementation-failed`, `ai:implement-fix-up`). Every other step (clarify, plan, orchestrate-clarify-respond, review_autofix, validate, integration-judge, final-merge judge) has no phase-scoped failure label and relies on `STALL_THRESHOLD_*_MINUTES` in `scripts/orchestrate_poll_process.sh` to eventually re-dispatch. Consequences:

- **Slow recovery** — default `STALL_THRESHOLD_MINUTES=120` (line 581) means a blank Codex return blocks the wave for up to 2 hours.
- **Wrong-step resume** — stall recovery uses only the current `ai:*` phase label, which is often already wrong, so it sometimes re-dispatches a later phase over an earlier-phase failure.
- **Double-labelled / mislabelled issues** — `ai_labels.py repair-labels` exists but is only invoked opportunistically; real issues in prod end up with e.g. `ai:planning` + `ai:implementing` simultaneously, or `ai:done` without a linked PR.
- **Label-creation gaps** — only `scripts/label_helpers.sh::ensure_label_exists` and a few call sites create labels on demand; workflows like `clarify.yml`, `plan.yml`, `validate.yml`, `ai-orchestrate-clarify-respond.yml`, `ai-update-workflows.yml`, and `ai-memory-maintenance.yml` apply labels directly via `gh issue edit --add-label` with no pre-create, so any new consumer repo that hasn't mirrored the label catalog fails silently (label silently dropped) — which then poisons every subsequent poller decision.
- **API-call pressure** — every extra verification call compounds the §15 (CLAUDE.md) rate-limit problem.

---

## 2. Non-Negotiable Constraints (from CLAUDE.md — re-read before starting)

1. **Prime Directive / Always-On Ask-First.** Before writing code, batch clarifying Q1/Q2/… using the mandatory format. Do not assume "reasonable defaults" for anything below marked **CONFIRM**.
2. **§6 Naming immutability.** Do not rename or repurpose any existing `ai:*` label, env var, or workflow input. All new labels are additive. Preserve every existing name. If you need a new variant, add alongside.
3. **§14 Consumer repo registry.** If you introduce a new label, it must ship via `.github/ai/label_contract.v1.json`, `scripts/label_helpers.sh` catalog, and be auto-created by every workflow that can apply it. No exceptions — consumer repos don't share our local env.
4. **§15 GitHub API hygiene.** Every new `gh api` / `gh_retry` / `_safe_gh_jq` call must be justified: either merged into an existing call, added as a field to an existing GraphQL alias in `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` / `orchestrate_poll_process.sh:1534` tracking-comments prefetch, or accompanied by a comment listing which existing calls were audited and why they were insufficient. **Net API calls must not increase** — the goal is to reduce them while adding functionality.
5. **§7 Output.** Final response must list every file changed with the line ranges of major logic, and update `README.md` / `agents.md` for any new env var, label, or operational contract.

---

## 3. Goals (in priority order)

1. **Resilient per-phase Codex failure handling.** When a Codex run at any phase returns empty output, non-zero RC, or output that fails its phase-specific validator, the workflow must:
	- Attempt in-workflow retry with exponential backoff (already present for implement at `implement.yml:1064-1067`; port the same pattern to every other Codex invocation, see §4 for the full list).
	- On final retry failure, apply a phase-specific terminal failure label and post a machine-readable fail comment (§5) so the poller can resume the exact failed step immediately — not wait for the stall threshold.
2. **Sound multi-source resume verification.** Poller must not trust the label alone. Before re-dispatching, it must corroborate using ≥2 of {latest machine-readable comment marker, presence/absence of linked PR, presence of approved plan comment, integration branch state} — and abort to `ai:needs-human` on contradictory signals.
3. **Universal label auto-creation.** Every workflow and script that applies any `ai:*` label must source `scripts/label_helpers.sh` and call `ensure_label_exists` (or its Python equivalent for scripts that don't shell out). No direct `gh issue edit --add-label` / `gh pr edit --add-label` without a pre-flight ensure.
4. **Poller label-repair sweep.** A new orchestrate-poller phase scans every tracked issue once per cycle, cross-references {labels, linked PR state, machine-readable comment history, state-file phase}, removes double-labels, corrects mismatches, and emits a structured audit comment.
5. **API-call reduction.** Net `gh api` calls must decrease or stay flat. Use the cycle-local caches (`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, `_candidate_details_json`). Extend the existing GraphQL prefetch queries with any new fields rather than adding per-item REST calls.

---

## 4. Scope — Every Codex Call Site to Harden

Port the attempt/backoff/empty-check pattern used at `.github/workflows/implement.yml:1032-1072` to all of these, with phase-specific failure labels and machine-readable fail comments:

| Workflow file | Codex step (line anchor) | New failure label | Resume action |
| --- | --- | --- | --- |
| `.github/workflows/clarify.yml` | `run_codex` (~L625, L811) | `ai:clarify-failed` | `retrigger_pipeline` → clarify |
| `.github/workflows/orchestrate_clarify_respond.yml` | `run_codex` (~L575, L627) | `ai:clarify-respond-failed` | `auto_respond_clarify` |
| `.github/workflows/plan.yml` | `run_codex` (~L932) | `ai:plan-failed` | `retrigger_plan` |
| `.github/workflows/implement.yml` | main codex (L1032-1072), diagnose (L2678), repair (L1291), summary (L2188) | keep `ai:implementation-failed`; add `ai:implement-diagnose-failed` | `retrigger_implement` |
| `.github/workflows/review_autofix.yml` | all `codex exec` invocations | `ai:review-autofix-failed` | `retrigger_review` |
| `.github/workflows/validate.yml` (+ `internal-validate.yml`) | all `codex exec` invocations | `ai:validate-failed` (distinct from existing `ai:validation-failed` — keep both; **decided Q1=A**) | `retrigger_validate` |
| `scripts/orchestrate_poll_process.sh` integration-conflict judge (~L2293) and post-validation judge | same pattern | `ai:integration-judge-failed` | internal retry only |
| `.github/workflows/workflow-log-analysis.yml` (L331, L628, L872) | same pattern | `ai:log-analysis-failed` | human only |
| `.github/workflows/ai-memory-maintenance.yml` | any codex call | `ai:memory-maintenance-failed` | human only |

The in-workflow retry must:

- Match `implement.yml`'s backoff (`10s * 2^(attempt-1)`, default `max_attempts=3`) — **do not** introduce a new variable name; reuse `MAX_CODEX_ATTEMPTS` as a single global `vars.MAX_CODEX_ATTEMPTS` (default `3`) used by every workflow (**decided Q2=A**, no per-phase overrides).
- Treat all three failure modes as "attempt failed": `RC≠0`, empty stdout, or phase-specific validator rejection (e.g. `plan.yml:1027` `NEEDS_CLARIFICATION`, `clarify.yml:891` `STATUS` parse, `implement.yml:1052` empty `git status --porcelain`).
- On final failure, skip the rest of the workflow cleanly (no half-applied labels / no half-written PRs), then execute the single "terminal failure" step defined in §5.

---

## 5. Machine-Readable Fail-Comment Contract (new)

On terminal failure, every workflow posts **exactly one** comment on the source issue, wrapped in stable markers so the poller can parse past-tense failures without ambiguity (mirrors the existing `IMPLEMENT_FIXUP_BLOCKERS_V1` / `AI_STANDALONE_STALL_STATE_V1` patterns in `orchestrate_poll_process.sh:3299,3834`).

```
<!-- AI_PHASE_FAILURE_V1
{
  "schema_version": 1,
  "phase": "plan",                              // clarify|clarify-respond|plan|implement|review-autofix|validate|integration-judge|log-analysis|memory-maintenance
  "failure_mode": "codex_empty_output",         // codex_empty_output|codex_rc_nonzero|codex_watchdog_kill|validator_rejected|harness_error
  "failed_step_name": "Run Codex (plan)",       // exact step name from the workflow (for state-machine correlation)
  "workflow_run_id": "123456789",
  "workflow_run_url": "https://…",
  "attempt_count": 3,
  "max_attempts": 3,
  "codex_rc": 142,
  "codex_stderr_tail_sha256": "…",              // 64-char hash of last 4KB of stderr to dedupe repeated identical failures
  "issue_number": 2591,
  "tracking_issue": 2500,
  "recommended_resume_action": "retrigger_plan",// must match one of the existing normalize_stall_recovery_action verbs
  "timestamp": "2026-04-18T…Z"
}
AI_PHASE_FAILURE_V1 -->
## ❌ <Phase> failed after <N> attempts
<short human-readable text, RUN_URL link, tail of codex_log.txt>
```

**Poller parsing rules** (add to `orchestrate_poll_process.sh`):

- Select **all** comments containing `AI_PHASE_FAILURE_V1`, not just the latest (per user instruction: *"check more than just the latest in case something else comes in after"*). Select the most recent by timestamp that still corresponds to the current `stall_recovery_count` / `workflow_run_id` not already observed. Persist observed `workflow_run_id`s in the standalone-stall-state comment (`STANDALONE_STATE_MARKER_OPEN`, line 3834) to avoid re-acting on the same failure.
- The fail comment alone is **not sufficient** to trigger re-dispatch. It must be corroborated (see §6).

---

## 6. Sound Resume Verification (non-regressing state machine)

Before executing any `retrigger_*` action, the poller must satisfy **all** of:

1. **Label ↔ phase agreement.** Current `ai:*` phase label is the group member of `phase_groups` that corresponds to the fail-comment's phase, OR is the specific `ai:<phase>-failed` label from §4. Mismatch → route to label-repair sweep (§7) first, then re-evaluate next tick.
2. **Artefact presence check (phase-specific):**
	- `plan-failed` / `retrigger_plan` — there must be no approved plan comment (existing `plan.yml` emits one) newer than the fail comment. If one exists, abort to `ai:needs-human`.
	- `implement-failed` / `retrigger_implement` — there must be no open or merged PR whose head branch matches `ai/issue-<N>`. If one exists, abort and reroute to review. (Re-use `close_linked_pr` / `list_linked_prs_by_branch` already in `orchestrate_poll_process.sh:1099-1160`; do not add a new query.)
	- `clarify-failed` / `auto_respond_clarify` — ensure there is no pending human-provided clarification answer comment newer than the fail comment.
	- `review-autofix-failed` / `retrigger_review` — confirm PR exists and is open.
	- `validate-failed` / `retrigger_validate` — confirm the integration branch/PR exists and the previous validation dispatch has no in-progress run (reuse `_ACTIONS_RUNS_BLOB_CACHE`, line 3421).
3. **Idempotency.** The `workflow_run_id` from the fail comment must be newer than any `last_observed_failure_run_id` stored in the standalone-stall-state comment. If equal, do nothing.
4. **No competing phase.** If another phase-fail marker appears between the one we're acting on and now, prefer the latest phase in the `phase_groups` ordering (same rule `ai_labels.py::cmd_repair_labels` already uses at line 131 — *"keep most-advanced phase"*). This prevents regressing from `ai:implement-failed` back to `ai:plan-failed` if both fired in the same cycle.

On any failed check, log the contradiction with a `::warning::` and **do nothing this tick** — let the label-repair sweep run, then re-evaluate next tick.

---

## 7. Poller Label-Repair Sweep (new phase)

Add a new top-level phase `label_repair_sweep` that runs **once per poller cycle**, *before* stall recovery and *before* candidate selection (so downstream phases see repaired labels). For each issue in `ACTIVE_WORKFLOW_ISSUES` (cache already populated):

1. **Fetch in one batched GraphQL call** (extend the existing `_fetch_candidate_issue_details_graphql`):
	- All `ai:*` labels.
	- Last 50 comments filtered to those containing any `AI_*_V1` marker.
	- Timeline PR cross-references (already fetched at line 1086).
2. **Compute `authoritative_phase`:**
	- Start from the most recent `AI_PHASE_FAILURE_V1` whose `workflow_run_id` post-dates the current `status_since_ts`.
	- Else fall back to `ai_labels.py repair-labels` with `issue_labels` input.
	- Else fall back to the `phase_groups[0].fallback` (`ai:clarification`).
3. **Derive `desired_label_set`:**
	- Exactly one phase-exclusive label from `phase_groups.issue_phase` (the one matching `authoritative_phase`).
	- Plus at most one of the new `ai:<phase>-failed` labels (mutually exclusive group; add a `failure_phase` phase_group to `label_contract.v1.json`).
	- Keep orthogonal tags (`ai:orchestrator-managed`, `ai:orchestrator-tracking`, `ai:orchestrator-validate-required`, `ai:destructive-blocked`, `ai:implement-fix-up`) untouched.
4. **Diff against actual labels** → apply add/remove in **one** `gh issue edit` call per issue (already the pattern at line 1248). Batch dry-run logs if `LABEL_REPAIR_DRY_RUN=true`.
5. **Emit one audit comment** per cycle, per issue, only if changes were made:

	```
	<!-- AI_LABEL_REPAIR_V1
	{ "cycle": "<cycle_id>", "added": [...], "removed": [...], "authoritative_phase": "…", "evidence": {...} }
	AI_LABEL_REPAIR_V1 -->
	```

	Evidence block must cite the comment ID / PR number / state-file wave that drove the decision, so future audits can reconstruct the reasoning.

6. **Contradiction handling (decided Q3=B):** if evidence genuinely conflicts (e.g. open PR + `ai:plan-failed` marker + `ai:implementing` label all simultaneously), apply the label matching the **most-advanced evidence** (open PR → `ai:implementing`/`ai:done`; merged PR → `ai:done`) and discard older fail markers. The audit comment must list every discarded marker (comment ID + phase + run ID) so the decision is reconstructable. Only escalate to `ai:needs-human` when no evidence source is conclusive.

This sweep must add **zero new per-issue API calls**: reuse the prefetch already on the hot path and the existing edit call.

---

## 8. Universal Label Auto-Creation

- Extend `.github/ai/label_contract.v1.json` with the new failure labels from §4 and register them in a new `phase_groups` entry named `failure_phase` so `ai_labels.py` understands exclusivity.
- Mirror them in `scripts/label_helpers.sh` (`_AI_LABEL_COLORS` + `_AI_LABEL_DESCS` arrays — currently ends at line 70).
- Add a one-liner `source scripts/label_helpers.sh && ensure_label_exists "<label>"` before every first use in:
	- Every workflow in §4 above.
	- `ai-update-workflows.yml`, `ai-memory-maintenance.yml`, `orchestrate_clarify_respond.yml`, `validation-improvements-intake.yml`, `review_autofix.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, `issue_pr_status.yml`, `cancel_on_pr_close.yml`.
	- `scripts/review_rb_judge.sh`, `scripts/validate_process.sh` (already use labels per earlier grep — verify they all call `ensure_label_exists` first).
- For the label-repair sweep in `orchestrate_poll_process.sh`, add a pre-flight that iterates the entire contract once **per poller cycle** (not per issue) and runs `ensure_label_exists` for every contract label against `GITHUB_REPOSITORY`. One 21-call burst on cold cache, 0 on warm (label-create is 409-idempotent and cheap).
- Add a test in `tests/` that asserts every workflow touching an `ai:*` label sources `scripts/label_helpers.sh` and calls `ensure_label_exists` for each label it applies (static-check test, parsed via `yq`/`ruamel.yaml`).

---

## 9. API-Call Reduction Checklist

While implementing, audit and remove/merge at minimum these suspected redundancies (grep evidence cited inline):

- `orchestrate_poll_process.sh:2709-2711` fetches `state`, `mergeable`, `merged_at` with three separate `_safe_gh_jq` calls on the same PR — merge into one `jq`-multi-field extract.
- Same pattern at L2760-2762, L2654-2655. Three call sites → consolidate into a helper `pr_state_merged_mergeable(pr_num)` that emits `state\tmergeable\tmerged` in one API hit.
- `_candidate_details_json` GraphQL (search for `_fetch_candidate_issue_details_graphql`) — extend to also return last 5 `AI_*_V1` marker comment IDs + bodies so the label-repair sweep and resume verification reuse the same payload. Document the new fields in the helper's docstring per §15.
- The label-repair sweep's per-issue label edit must piggyback on any edit that stall-recovery would have performed in the same tick (merge `--add-label` / `--remove-label` flags into a single `gh issue edit`).

List every call before/after in the final response's "API inventory" section.

---

## 10. Env Vars (additive only; all with defaults per CLAUDE.md §4)

Adopted (**decided Q4=A** — all six accepted with proposed defaults):

- `MAX_CODEX_ATTEMPTS` (default `3`) — per-workflow Codex retry cap.
- `CODEX_RETRY_BACKOFF_BASE_SECS` (default `10`).
- `ENABLE_PHASE_FAILURE_COMMENTS` (default `true`) — emit `AI_PHASE_FAILURE_V1`.
- `ENABLE_LABEL_REPAIR_SWEEP` (default `true`).
- `LABEL_REPAIR_DRY_RUN` (default `false`) — Q6=B says ship enabled out of the gate, relying on the test matrix in §11; flip this to `true` per-repo only if a regression is observed in audit comments.
- `LABEL_REPAIR_MAX_ISSUES_PER_CYCLE` (default `50`) — safety bound.

**Do not** reuse, repurpose, or rename `STALL_THRESHOLD_*_MINUTES`, `MAX_STALL_RECOVERIES_PER_ISSUE`, `MAX_RECOVERY_ATTEMPTS`, or `MAX_VALIDATION_RECOVERY_ATTEMPTS` (CLAUDE.md §6).

---

## 11. Tests You Must Add/Extend

- `tests/test_implement_post_codex_recovery.py` — extend to cover all new failure paths in plan/clarify/review/validate (mock codex via `MOCK_CODEX_MODE` already used at line 329).
- `tests/test_orchestrate_poll_process.py` — new cases for label-repair sweep, contradictory signals → `ai:needs-human`, multi-fail-comment selection, idempotent re-dispatch gating on `workflow_run_id`.
- `tests/test_ai_labels.py` (new) — assert `failure_phase` group in the contract, repair-labels output for every new label, roundtrip with `label_helpers.sh` catalog (parse the bash file and assert keys match JSON).
- Static test: every `.github/workflows/*.yml` that uses `--add-label` / `--remove-label` also sources `scripts/label_helpers.sh` and calls `ensure_label_exists` with the same label name.

---

## 12. Mandatory Clarifying Questions — ASK BEFORE CODING

Per CLAUDE.md §2, ask these in a single batch and wait.

> **Q1: Naming for the new validate-phase Codex-failure label** — `ai:validation-failed` already exists and means *"runtime validation rejected the implementation"* (semantic). We need a separate label for *"the validate workflow's Codex call itself failed."*
>
> Choices:
> - **A** — Add `ai:validate-failed` as the new Codex-workflow-failure label; keep `ai:validation-failed` semantic-rejection. (RECOMMENDED — maximal clarity)
> - **B** — Overload `ai:validation-failed` to cover both; distinguish via `failure_mode` in the machine-readable comment only.
> - **C** — Use `ai:validate-codex-failed` for explicitness.
>
> Reply: `Q1: A`

> **Q2: `MAX_CODEX_ATTEMPTS` scope.**
>
> Choices:
> - **A** — One global `vars.MAX_CODEX_ATTEMPTS` (default `3`) used by every workflow. (RECOMMENDED)
> - **B** — Per-phase overrides (`MAX_CLARIFY_CODEX_ATTEMPTS`, …) with global fallback.
> - **C** — Hard-code `3` in each workflow, no var.
>
> Reply: `Q2: A`

> **Q3: Label-repair sweep behaviour when evidence is genuinely contradictory** (e.g. open PR + `ai:plan-failed` marker + `ai:implementing` label).
>
> Choices:
> - **A** — Apply `ai:needs-human` and leave phase label untouched; audit comment explains. (RECOMMENDED — conservative)
> - **B** — Apply the label matching the most advanced evidence (open PR → `ai:implementing`/`ai:done`) and discard older fail markers.
> - **C** — No-op; log a warning only.
>
> Reply: `Q3: A`

> **Q4: New env var set from §10 — accept all six, or trim?**
>
> Choices:
> - **A** — Accept all six with the proposed defaults. (RECOMMENDED)
> - **B** — Only `MAX_CODEX_ATTEMPTS`, `ENABLE_LABEL_REPAIR_SWEEP`, `LABEL_REPAIR_DRY_RUN`.
> - **C** — Please list exact set in follow-up.
>
> Reply: `Q4: A`

> **Q5: Should the label-repair sweep also act on issues not currently managed by the orchestrator** (i.e. those without `ai:orchestrator-managed` but carrying `ai:*` labels from old runs)?
>
> Choices:
> - **A** — Only sweep issues with `ai:orchestrator-managed`. (RECOMMENDED — matches the existing `has_known_label` guard in `ai_labels.py:122`)
> - **B** — Sweep all issues with any `ai:*` label.
> - **C** — Sweep all open issues regardless.
>
> Reply: `Q5: A`

> **Q6: Rollout order for existing mislabelled/double-labelled issues in prod.**
>
> Choices:
> - **A** — Ship with `LABEL_REPAIR_DRY_RUN=true` for one release, review audit comments, then flip to `false`. (RECOMMENDED)
> - **B** — Ship enabled; rely on tests.
> - **C** — Ship disabled; enable manually per repo.
>
> Reply: `Q6: A`

---

## 13. Deliverables (final response must include)

1. List of every file changed with major-logic line ranges.
2. Updates to `README.md` and `agents.md` covering: new labels, new env vars, new fail-comment schema, new poller phase, rollout toggles.
3. **"API inventory" section**: before/after count of `gh api` / `gh_retry` / `_safe_gh_jq` calls per poller cycle, per workflow run. Net must be ≤ current.
4. Test matrix showing new/extended tests and what each asserts.
5. Operational runbook snippet: how to (a) manually trigger a label-repair sweep, (b) decode an `AI_PHASE_FAILURE_V1` comment, (c) force-resume a specific phase when the poller has decided to escalate.

---

## 14. Out of Scope

- Rewriting the stall-recovery state machine itself (keep `resolve_stall_recovery_action` / `execute_stall_recovery_action` semantics).
- Changing any existing label's name, color, or description.
- Adding new Codex model selection logic.
- Touching the `_locks` Mongo collection or any DB contract.

---

## 15. Done Definition

- Every workflow listed in §4 retries Codex up to `MAX_CODEX_ATTEMPTS`, emits `AI_PHASE_FAILURE_V1` on terminal failure, and tags the issue with the correct `ai:<phase>-failed` label (auto-created via `ensure_label_exists`).
- Poller resumes the exact failed step within one cycle (≤ poll interval), **not** `STALL_THRESHOLD_*_MINUTES`.
- Label-repair sweep runs every cycle, corrects double/mislabelled issues, emits `AI_LABEL_REPAIR_V1` audit comments.
- No new `ai:*` label can be applied anywhere without `ensure_label_exists` first (enforced by static test).
- Net `gh api` calls per poller cycle is ≤ baseline (see API inventory).
- All new/changed behaviour documented in `README.md` + `agents.md`.
- All CLAUDE.md clarifying Q1–Q6 answered before code lands.

> **Reminder:** Ask Q1–Q6 first, batch, wait for answers, then implement. No partial implementations. No silent refactors. If any step reveals new ambiguity, stop and ask again.

---

## Appendix A — Open Decisions Log (filled in as Q1–Q6 are answered)

| Q | Decision | Date | Source |
| --- | --- | --- | --- |
| Q1 | **A** — Add `ai:validate-failed` as Codex-workflow-failure label; keep `ai:validation-failed` for semantic-rejection. | 2026-04-18 | user reply |
| Q2 | **A** — Single global `vars.MAX_CODEX_ATTEMPTS` (default `3`) used by every workflow; no per-phase overrides. | 2026-04-18 | user reply |
| Q3 | **B** — On contradictory evidence, apply the label matching the most-advanced evidence (open PR → `ai:implementing`/`ai:done`) and discard older fail markers. Audit comment lists discarded markers. (Deviates from the recommended conservative `ai:needs-human` default.) | 2026-04-18 | user reply |
| Q4 | **A** — Accept all six env vars (`MAX_CODEX_ATTEMPTS`, `CODEX_RETRY_BACKOFF_BASE_SECS`, `ENABLE_PHASE_FAILURE_COMMENTS`, `ENABLE_LABEL_REPAIR_SWEEP`, `LABEL_REPAIR_DRY_RUN`, `LABEL_REPAIR_MAX_ISSUES_PER_CYCLE`) with proposed defaults. | 2026-04-18 | user reply |
| Q5 | **A** — Sweep only issues carrying `ai:orchestrator-managed`; matches existing `has_known_label` guard in `ai_labels.py:122`. | 2026-04-18 | user reply |
| Q6 | **B** — Ship label-repair sweep enabled (`LABEL_REPAIR_DRY_RUN=false` default); rely on the §11 test matrix; flip per-repo to dry-run only on observed regression. (Deviates from the recommended one-release dry-run rollout.) | 2026-04-18 | user reply |
| Q7 | **A** — Keep `docs/resilient-codex-failure-plan.md` as the canonical filename. | 2026-04-18 | user reply |
| Q8 | **A** — Single-source commit: answers folded into Appendix A before commit. (Original commit `aba881f` was made under stop-hook pressure with `_pending_` rows; this update finalises Appendix A.) | 2026-04-18 | user reply |
| Q9 | **A** — Push to `claude/document-resilience-plan-jarfu`. | 2026-04-18 | user reply |



