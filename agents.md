# agents.md — Repo Architecture Facts (coding-workflows)

This file contains **repo-specific architectural facts** for any AI agent
(interactive Claude session, codex-cli unattended pipeline, third-party
reviewer model). Global engineering rules live in `CLAUDE.md` (interactive)
or `unattended_system_instructions.md` (unattended) — do not duplicate them
here.

Consumer repos define their own `agents.md` with their own architectural
facts. The unattended pipeline loads this file as `agents_canonical.md` and
the consumer's `agents.md` separately; both are inlined into the prompt.

---

## Workflow architecture

Phases of the unattended pipeline (each is a separate workflow file under
`.github/workflows/`):

1. **clarify** (`clarify.yml`, `internal-clarify.yml`) — read the issue,
   decide whether clarifying questions are needed, emit `STATUS: CLEAR` or
   a `Q1`/`Q2` batch.
2. **clarify-respond** (`orchestrate_clarify_respond.yml`) — answer the
   clarifier's questions on behalf of an orchestrator-managed issue.
3. **plan** (`plan.yml`, `internal-plan.yml`) — read the clarified issue and
   emit a structured implementation plan with files-to-change and a
   per-issue ≤60-minute time budget.
4. **implement** (`implement.yml`, `internal-implement.yml`) — execute the
   plan with codex-cli; write the actual files.
5. **implement-diagnose** (`scripts/implement_diagnose_post_codex_failure.sh`,
   driven by `MODEL_DIAGNOSE`) — analyse a post-Codex validation failure and
   emit JSON fix-up issue proposals.
6. **implement-repair** (`prompts/mode-implement-repair.txt`,
   `mode-implement-repair-syntax.txt`) — narrow post-Codex repair runs.
7. **review autofix** (`review_autofix.yml`, `internal-review.yml`) — multi-
   model reviewer + consolidator + editor loop on PR changes.
8. **conflict resolver** (`prompts/conflict-resolver.txt`,
   `integration-sync-conflict-resolver.txt`) — merge-conflict resolution
   inside autofix.
9. **orchestrate** (`orchestrate.yml`, `orchestrate_poll.yml`) — issue
   decomposition + judge polling.
10. **judge** (`mode-judge.txt`, `mode-orchestrate-poll-judge.txt`,
    `mode-judge-review-blocked.txt`, `mode-judge-stall-recovery.txt`) —
    JSON-emitting evaluation of wave state.
11. **validate** (`validate.yml`, `mode-validate-*.txt`) — generate / fix /
    self-heal a validation harness for the implemented change.
12. **workflow log analysis** (`workflow-log-analysis.yml`,
    `mode-workflow-*.txt`) — periodic audit of workflow runs.

---

## Models in use (defaults; overridable via repo-vars)

| Phase | Default model | Default reasoning | Verbosity |
|---|---|---|---|
| clarify, clarify-respond | `openai/gpt-5.4` | `xhigh` (smoke: `low` — `clarify.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` |
| plan | `openai/gpt-5.4` | `xhigh` (smoke: `low` — `plan.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` |
| orchestrate (decompose), judge | `openai/gpt-5.4` | `xhigh` | `low` |
| implement (main editor) | `openai/gpt-5.4` | `xhigh` (smoke: no override — see `.github/workflows/implement.yml:597-606`) | default |
| implement-repair, implement-repair-syntax | `openai/gpt-5.4` | `xhigh` | default |
| implement-diagnose | `openai/gpt-5.4` | `xhigh` | `low` |
| review autofix editor | `openai/gpt-5.4` | `xhigh` (smoke: `medium`) | default |
| review autofix reviewers (pass 1) | `openai/gpt-5.4` | `xhigh` (hardcoded at the `run_reviewer_pass ... "xhigh"` callsite in `scripts/review_run_reviewers.sh:1173`; not affected by the smoke `REVIEWER_REASONING_EFFORT=low` override in two-pass mode) | `low` |
| review autofix reviewers (pass 2) | `openai/gpt-5.4` | `xhigh` (LOC gate collapsed: both `REVIEWER_PASS2_REASONING_SMALL` and `REVIEWER_PASS2_REASONING_LARGE` default to `xhigh`); smoke: `low`; operator override wins | `low` |
| review consolidator | `openai/gpt-5.4` | `xhigh` | `low` |
| conflict resolver | `openai/gpt-5.4` | `xhigh` (decoupled from smoke; `scripts/review_conflict_resolve.sh` validates `xhigh\|high\|medium\|none` only — `low` is rejected) | default |
| validate generate, diagnose | `openai/gpt-5.4` | `xhigh` | `low` |
| validate discover | `openai/gpt-5.4` | `xhigh` (per-phase override via `MODEL_REASONING_EFFORT_DISCOVER`) | `low` |
| validate fix-harness, self-heal | `openai/gpt-5.4` | `xhigh` | default |
| workflow log analyze | `openai/gpt-5.4` | `xhigh` | `low` |
| workflow audit | `openai/gpt-5.4` | `xhigh` (hardcoded in `.github/workflows/workflow-log-analysis.yml:716-717`) | `low` |
| workflow api-redundancy | `openai/gpt-5.4` | `xhigh` (default of `THINKING_LEVEL_ANALYSIS`) | `low` |
| workflow log summary | `openai/gpt-5.4-mini` | default | `low` |
| reviewer consensus summariser | `openai/gpt-5.4-mini` | `medium` (`XPOLL_SUMMARISER_REASONING`) | `low` |

Every editor / reviewer / resolver phase now defaults to `openai/gpt-5.4`.
The previous legacy editor split (patch-heavy phases on a separate older
slug) was retired after the announce-without-emit regression
(openai/codex#11151) drove repeat no-edit failures. The 2026-05-07
ablation suite then identified the underlying root cause as
`apply_patch_tool_type: "freeform"` on the OpenRouter Responses path
(see the `openai/gpt-5.4` catalog entry — `apply_patch_tool_type` is now
`function`).

The reviewer-only multi-model run (claude-branch-review) uses third-party
models (`minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`,
`deepseek/deepseek-v4-pro`, `qwen/qwen3.6-plus`, `x-ai/grok-4.1-fast`) plus
`unattended_system_instructions.md` as system context.

---

## Repo-specific batching helpers

The following helpers are the canonical batched GraphQL paths for the
GitHub API hygiene rules in `unattended_system_instructions.md` §14:

- `_fetch_candidate_issue_details_graphql` (in `scripts/orchestrate_poll_process.sh`)
- `_fetch_linked_pr_status_graphql` (in `scripts/orchestrate_poll_process.sh`)

Both return a dict keyed by issue number so the caller can drop the result
into a cycle-local cache.

Cycle-local caches that must not be re-fetched per iteration:
`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`,
`_candidate_details_json`.

---

## Stable log prefixes (contractual)

Workflow-log-analysis and API-hygiene reporting depend on these stable log
prefixes. Renames are breaking unless an alongside-old shim is documented
and shipped:

- `LABEL_REPAIR`
- `LABEL_REPAIR_DIFF`
- `AUTOFIX_PEER_CHECK`
- `AUTOFIX_DISPATCH_SKIPPED`
- `AUTOFIX_DISPATCH_ISSUED`
- `AI_PHASE_FAILURE_V1`

---

## Label-repair contradiction policy (current branch)

The active poller loop uses `reconcile_managed_issue_labels` for current-wave
managed issues and logs `LABEL_REPAIR*` diagnostics. The richer
contradiction-evidence helpers in `scripts/orchestrate_lib.py`
(`parse_phase_failure_markers`, `choose_most_advanced_conclusive_evidence`,
`resolve_label_repair_evidence`) are contract/reserved and not yet wired
into poller reconciliation.

---

## Reference

Operator runbooks (env var reference, autofix retrigger/dedup internals,
orchestrator integration-sync auto-heal, validation self-healing, workflow
log analysis pipeline, semantic cache scope, wrapper pin policy) live in
`./probably_unnecessary_but_read_if_stuck.md`. Read it only when needed —
it is intentionally large.
