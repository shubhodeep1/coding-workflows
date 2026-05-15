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
| implement (main editor) | `openai/gpt-5.4` | `xhigh` (smoke: no override — see `.github/workflows/implement.yml:597-606`) | `low` |
| implement-repair, implement-repair-syntax | `openai/gpt-5.4` | `xhigh` | `low` |
| implement-diagnose | `openai/gpt-5.4` | `xhigh` | `low` |
| review autofix editor | `openai/gpt-5.4` | `xhigh` (smoke: `medium`) | `low` |
| review autofix reviewers (pass 1) | `openai/gpt-5.4` | `xhigh` (hardcoded at the `run_reviewer_pass ... "xhigh"` callsite in `scripts/review_run_reviewers.sh:1173`; not affected by the smoke `REVIEWER_REASONING_EFFORT=low` override in two-pass mode) | `low` |
| review autofix reviewers (pass 2) | `openai/gpt-5.4` | `xhigh` (LOC gate collapsed: both `REVIEWER_PASS2_REASONING_SMALL` and `REVIEWER_PASS2_REASONING_LARGE` default to `xhigh`); smoke: `low`; operator override wins | `low` |
| review consolidator | `openai/gpt-5.4` | `xhigh` | `low` |
| conflict resolver | `openai/gpt-5.4` | `high` (decoupled from smoke; `scripts/review_conflict_resolve.sh` validates `xhigh`, `high`, `medium`, `none` only — `low` is rejected; default lowered from `xhigh` after runs `25627236793` / `25627316961` hit `timeout`-killed retries on degenerate orchestrator-stack integrations; override per-repo via `vars.THINKING_LEVEL_CONFLICT_RESOLVER`) | `low` |
| validate generate, diagnose | `openai/gpt-5.4` | `xhigh` | `low` |
| validate discover | `openai/gpt-5.4` | `xhigh` (per-phase override via `MODEL_REASONING_EFFORT_DISCOVER`) | `low` |
| validate fix-harness, self-heal | `openai/gpt-5.4` | `xhigh` | `low` |
| workflow log analyze | `openai/gpt-5.4` | `xhigh` | `low` |
| workflow audit | `openai/gpt-5.4` | `xhigh` (hardcoded in `.github/workflows/workflow-log-analysis.yml:716-717`) | `low` |
| workflow api-redundancy | `openai/gpt-5.4` | `xhigh` (default of `THINKING_LEVEL_ANALYSIS`) | `low` |
| workflow log summary | `openai/gpt-5.4-mini` | default | `low` |
| reviewer consensus summariser | `openai/gpt-5.4-mini` | `medium` (`XPOLL_SUMMARISER_REASONING`) | `low` |

All gpt-5.4 phases now resolve to `low` verbosity at every layer: the per-phase
`MODEL_VERBOSITY` env-var default in `.github/workflows/*.yml` (`VERBOSITY_*`
repo-vars), the `-c model_verbosity=low` CLI flag on every `codex exec`
callsite (≈20 sites across `scripts/*.sh` and `.github/workflows/*.yml`),
the `model_verbosity = "low"` line that `scripts/write_codex_config.sh:242`
writes into `config.toml`, and the `"default_verbosity": "low"` for
`openai/gpt-5.4` in `scripts/codex_model_catalog.json:354`. Third-party
reviewer models (`minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`,
`deepseek/deepseek-v4-pro`, `qwen/qwen3.6-plus`, `x-ai/grok-4.1-fast`)
carry `support_verbosity = false` in the catalog — codex CLI logs
`model_verbosity is set but ignored as the model does not support verbosity`
and continues; the value is operationally moot for those rows. The
historical `high` value across every layer was a workaround for the
openai/codex#11151 announce-without-emit failure mode (implement /
review_autofix smoke runs at 2026-05-07 12:41 / 12:42, where the model
emitted a reasoning trace and exited without a tool call); the workaround
now relies on `include_apply_patch_tool = true` as the primary
belt-and-suspenders. If the announce-without-emit pattern recurs at `low`,
raise verbosity at the layer that needs it (start with the editor /
implement callsites, since those are the original 11151 reproducers).

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
- `SEMBLE_QUERY`
- `SEMBLE_FALLBACK`
- `SERENA_QUERY`
- `SERENA_FALLBACK`
- `SERENA_PROBE`

---

## Label-repair contradiction policy (current branch)

The active poller loop uses `reconcile_managed_issue_labels` for current-wave
managed issues and logs `LABEL_REPAIR*` diagnostics. The richer
contradiction-evidence helpers in `scripts/orchestrate_lib.py`
(`parse_phase_failure_markers`, `choose_most_advanced_conclusive_evidence`,
`resolve_label_repair_evidence`) are contract/reserved and not yet wired
into poller reconciliation.

---

## Review pipeline consolidator + ledger contract

The four-stage chain inserted between the reviewer panel and the editor in
`scripts/review_apply_fixes.sh` — floor rules, consolidator, parser, ledger
— operates under the following invariants. Full design lives in
`docs/review-pipeline-improvements.md`; the rules below are the load-bearing
ones that downstream code and humans tuning the pipeline depend on.

- **Fail-open at every stage.** A missing artefact, a model timeout, a
  parser error, or any `REVIEW_*_ENABLED=0` flip reduces the pipeline to
  today's "raw reviewer bundle goes to the editor" behaviour. No new
  stage may make the pipeline strictly worse than baseline.
- **The consolidator never gates.** `reviewer_bundle.txt` is the
  authoritative input to the editor. `review_issues.txt` (consolidator
  output, parsed) is advisory only. The editor reads the raw bundle in
  full and may override any consolidator recommendation by emitting a
  line of the form `CONSOLIDATOR_OVERRIDDEN: <issue_id> — <reason>` in
  its summary. The metrics step greps this line and reports the override
  count — a stable contract; the prefix `CONSOLIDATOR_OVERRIDDEN:` is
  covered by CLAUDE.md §6 and must not be renamed without an alongside
  shim.
- **Floor tags are non-skippable.** `floor_tags.txt` lists findings that
  hit any of three deterministic rules: ≥2-reviewer agreement on the
  same `(file, line)` anchor, a severity-keyword match, or a
  `ISSUE_CONFIDENCE: 5` reviewer signal. The editor prompt instructs the
  editor that every line must be addressed, rejected with reason, or
  deferred with reason. Floor tags override `non-actionable`
  classifications from the consolidator and survive the ledger's
  `accepted-residual` promotion.
- **Ledger `issue_id` is content-anchored, not line-anchored.** The
  stable identifier is `iss_<16-hex>` derived from `SHA-256(file path
  || anchor-line ±2 normalised code || lens || severity-keyword
  category)`. Whitespace / comment churn near the anchor does not
  change the hash; a genuine code edit at the anchor does, at which
  point the prior id is marked `FIXED` and any new finding gets a fresh
  id. Collisions are disambiguated with a `:<n>` suffix and logged via
  `hash_collision=1`.
- **`PERSIST_LIMIT → accepted-residual` is the retry brake.** After
  `REVIEW_LEDGER_PERSIST_LIMIT` (default `2`) iterations of unsuccessful
  editor attempts on the same `issue_id`, the ledger marks it
  `accepted-residual` and strips it from the editor's `review_issues.txt`
  input. The same anchor in `floor_tags.txt` still flows through —
  giving up on retry never overrides the floor.
- **Per-PR ledger isolation.** `.ai/review_issue_ledger/pr-<N>.txt` is
  the canonical path. The file is gitignored; cross-iteration persistence
  is via `actions/cache` keyed on `review-ledger-<repo>-pr-<N>-`. No
  concurrent PR writes to the same file. Legacy single-file overrides
  are still honoured if `REVIEW_LEDGER_PATH` is set explicitly.
- **Iteration scoping is a soft narrow, not a hard filter.** When
  `REVIEW_REVIEWER_ITERATION_SCOPING=1` and the iteration is N>1, the
  reviewer prompt receives an additional "ITERATION SCOPE FILES" block
  listing (a) files touched by the last `[ai-autofix]` commit and (b)
  files anchoring open ledger entries. Reviewers may still consult code
  outside the list to understand interactions; the block only directs
  effort allocation. Iteration 1 always sees the full diff. An empty
  union or a missing ledger drops the block entirely (fail-open).
- **Reviewer checklist preserves the seven-lens structure.** When
  `REVIEW_REVIEWER_CHECKLIST_ENABLED=1`, every reviewer files findings
  under seven explicit lens headings: `SECURITY & INPUT VALIDATION`,
  `CORRECTNESS & LOGIC`, `CONCURRENCY / RACES / IDEMPOTENCY`, `ERROR
  PATHS & EDGE CASES`, `PERFORMANCE & RESOURCE USE`, `INDEX-CONTRACT /
  DB RULES`, `NAMING / BACKWARD COMPATIBILITY`. Empty lenses emit
  `NONE` literally so consolidator / floor-rules can see the gap. The
  `ISSUE_CONFIDENCE: 1–5` scale is preserved exactly.

### Env var contract (review pipeline)

| Variable | Default | Description |
|---|---|---|
| `REVIEW_CONSOLIDATOR_ENABLED` | `1` | Master switch for the consolidator stage. |
| `REVIEW_CONSOLIDATOR_MODEL` | `openai/gpt-5.4` | OpenAI-compatible model id. |
| `REVIEW_CONSOLIDATOR_REASONING` | `xhigh` | Codex reasoning level (`xhigh`/`high`/`medium`/`low`/`none`). |
| `REVIEW_CONSOLIDATOR_TIMEOUT_SECS` | `300` | Hard wall-clock on the codex call. |
| `REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT` | `16000` | Output byte cap; truncation marker is parser-safe. |
| `REVIEW_PARSER_FAILOPEN` | `1` | When `1`, parser errors yield empty `review_issues.txt` and proceed; `0` for debug. |
| `REVIEW_FLOOR_RULES_ENABLED` | `1` | Master switch for the floor-rule scanner. |
| `REVIEW_FLOOR_KEYWORDS_FILE` | (built-in) | Optional override of the built-in keyword catalogue. |
| `REVIEW_LEDGER_ENABLED` | `1` | Master switch for ledger lifecycle tracking. |
| `REVIEW_LEDGER_PERSIST_LIMIT` | `2` | Persist count before `accepted-residual` transition. |
| `REVIEW_LEDGER_PATH` | `.ai/review_issue_ledger/pr-${PR_NUMBER}.txt` | Per-PR ledger path; per-iteration via `actions/cache`. |
| `REVIEW_REVIEWER_CHECKLIST_ENABLED` | `1` | Append seven-lens checklist to reviewer prompts. |
| `REVIEW_REVIEWER_ITERATION_SCOPING` | `1` | Soft-narrow reviewer focus on iteration N>1. |

---

## Reference

Operator runbooks (env var reference, autofix retrigger/dedup internals,
orchestrator integration-sync auto-heal, validation self-healing, workflow
log analysis pipeline, semantic cache scope, wrapper pin policy) live in
`./probably_unnecessary_but_read_if_stuck.md`. Read it only when needed —
it is intentionally large.
