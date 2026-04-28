# Squad Workflow Improvements Report

> **Original report**: `research/squad-workflow-improvements-20260322T042436Z.md` (2026-03-22)
> **Updated**: 2026-04-27 — refreshed against current repo state and latest Squad codebase.
>
> All five original recommendations (P1–P5) have been re-evaluated.
> New patterns discovered in the latest Squad analysis are appended as N1–N4.

## External Repository

- Repo: `https://github.com/bradygaster/squad`
- Original commit: `1446050f43471b111aa6210eb9825449651f2b64` (2026-03-20)
- Latest review: HEAD as of 2026-04-27 (v0.9.1+)

## Scope and Methodology

Scope was restricted to workflow automation assets only.

Local assets reviewed:
- `.github/workflows/*` (32 workflows)
- `scripts/*` (35 scripts)
- `prompts/*` (23 prompt files)
- `.github/ai/label_contract.v1.json`
- `ai-memory/schemas/*`
- `workflow-templates/*` (12 templates)
- `CLAUDE.md`, `README.md`, `agents.md`

External assets reviewed:
- `.github/workflows/*` (15 workflow files)
- `templates/workflows/*` (11 template files)
- `.squad/` directory structure (team state, agent charters, decisions log)
- `.squad/templates/workflows/*`
- `squad.config.ts`, `package.json`, CLI commands
- Squad documentation site and GitHub blog post

Method:
- Mapped workflow patterns in `squad` to this repository's AI issue pipeline.
- Classified each pattern as `adopt`, `adapt`, or `reject`.
- Compared against the March 2026 baseline to mark patterns as `done`, `partial`, or `pending`.

---

## Current-State Baseline (This Repository)

### Strengths (in place since original report)

- End-to-end multi-phase issue pipeline with command gating and phase labels.
- PR autofix and review hardening pipeline with runtime context capture.
- Shared prompt fragments for phase modes under `prompts/`.
- AI memory infrastructure for persistence and retrieval.

### Improvements implemented since original report (March 2026)

- **Label contract + enforcement**: `.github/ai/label_contract.v1.json` (21 labels, phase groups with fallback logic), `scripts/ai_labels.py` (resolve-phase, repair-labels, contract-validate), CI validation in `ci.yml`, runtime integration in `orchestrate_poll_process.sh` and `validate_process.sh`.
- **Processed-command idempotency ledger**: Full schema (`processed_command_entry.v1.json`), CLI (claim/complete/check/list), library functions in `ai_memory_lib.py`, shell helpers in `memory_helpers.sh`, wired end-to-end in `plan.yml`, `implement.yml`, `orchestrate_clarify_respond.yml` with check → claim → complete lifecycle.
- **Prompt single-source**: All 23 prompts externalized to `prompts/`. Dynamic rendering via `scripts/render_prompt.sh`. CI validation (non-empty, no inline blocks, all placeholders resolved). Zero inline prompt duplicates remain.
- **Manual replay workflows**: 10+ workflows support `workflow_dispatch` (review_autofix with `pr_number` input, mark-stable with version tag + dry-run, internal-* wrappers for orchestrate/validate/review).
- **Template sync to consumer repos**: 12 templates in `workflow-templates/`, `update_workflows.yml` auto-sync via `repository_dispatch`, `consumer_repos.json` registry with 11 consumer repos.
- **Stall detection + orphan repair**: Embedded in orchestrator poller — `close_merged_issues_sweep`, standalone stall detection, label repair on every poll tick. Functionally equivalent to Squad's `squad-heartbeat.yml`.
- **CI drift guards**: `yamllint` + `actionlint` on all workflow YAML, `check_workflow_script_refs.py` for script reference validation.

### Remaining gaps

- No GitHub label-sync workflow (ensuring actual repo labels match the contract).
- No explicit deployed-vs-template drift-scan workflow for consumer repos.
- No template-generation tooling for shared shell blocks across workflows.

---

## Imported Pattern Matrix (Squad → This Repo)

| Pattern from `squad` | Source evidence | Local analog | Status | Disposition |
| --- | --- | --- | --- | --- |
| Label catalog sync from team metadata | `sync-squad-labels.yml` | `.github/ai/label_contract.v1.json` + `scripts/ai_labels.py` + CI validation | **Partial** | Contract + enforcement done; GitHub label-sync workflow pending |
| Namespace mutual exclusivity enforcement | `squad-label-enforce.yml` | `ai_labels.py resolve-phase` + `repair-labels` in poller | **Done** | Phase exclusivity enforced inline on every poll tick |
| Scheduled orphan-state repair heartbeat | `squad-heartbeat.yml` | `close_merged_issues_sweep` + stall detection in orchestrator poller | **Done** | Poller subsumes heartbeat pattern |
| Manual rerun workflow with explicit input and status | `ci-rerun.yml` | 10+ `workflow_dispatch` entry points | **Done** | Multiple manual replay paths exist |
| Template-first workflow maintenance | `templates/workflows/*` + `squad upgrade` | `workflow-templates/*` + `update_workflows.yml` auto-sync | **Done** | Template sync operational for 11 consumer repos |
| Branch promotion + release lane automation | `squad-promote.yml`, `squad-release.yml` | `test-and-mark-stable.yml` + `mark-stable.yml` | **Done** | Different model (tag-based) but same intent |
| Triage routing with capability matching | `squad-triage.yml` | `clarify.yml` (readiness check, not capability routing) | **N/A** | Not applicable — single-agent-per-issue model |
| Decisions log / shared knowledge base | `.squad/decisions.md` | `ai-memory/` branch with structured records | **Done** | More structured than Squad's append-only markdown |
| Agent persistent learning / history | `.squad/agents/*/history.md` | Run events + task lineage in memory schemas | **Partial** | Per-repo accumulated learnings not captured |
| Governance-as-code file-write guards | SDK `ensureSquadPath*` + per-agent write guards | `ALLOW_WORKFLOW_EDITS` + `BULK_DELETE_THRESHOLD` + destructive-commit guard | **Partial** | Coarser-grained than Squad's per-agent model |

---

## Original Recommendations — Updated Status

### P1: Label Sync + Mutual-Exclusivity Enforcement — **DONE (one gap)**

**Original recommendation**: Introduce a single AI label contract and enforce it centrally.

**Implementation status**: ✅ Complete

| Component | Status | Evidence |
| --- | --- | --- |
| Label contract schema | ✅ Done | `.github/ai/label_contract.v1.json` — 21 labels, phase groups, fallback logic |
| Label enforcement script | ✅ Done | `scripts/ai_labels.py` — `resolve-phase`, `repair-labels`, `contract-validate` |
| CI validation | ✅ Done | `ci.yml` runs `ai_labels.py contract-validate` on every build |
| Workflow-level phase transitions | ✅ Done | `orchestrate_poll_process.sh` uses `resolve-phase` for transitions |
| Workflow-level label repair | ✅ Done | `orchestrate_poll_process.sh` uses `repair-labels` on stalled issues |
| GitHub label-sync workflow | ⚠️ Gap | No workflow ensures actual GitHub repo labels match the contract |

**Remaining work**: A lightweight `workflow_dispatch` workflow (similar to Squad's `sync-squad-labels.yml`) that reads `label_contract.v1.json` and creates/updates GitHub labels via `actions/github-script`. This would ensure consumer repos have all `ai:*` labels with correct colors/descriptions without manual setup. Estimated effort: Low.

### P2: Processed-Command Idempotency Ledger — **DONE**

**Original recommendation**: Track processed issue comment IDs across all phases.

**Implementation status**: ✅ Complete — fully wired end-to-end.

| Component | Status | Evidence |
| --- | --- | --- |
| Schema definition | ✅ Done | `ai-memory/schemas/processed_command_entry.v1.json` |
| CLI commands | ✅ Done | `claim-processed-command`, `complete-processed-command`, `get-processed-command`, `list-processed-commands` in `ai_memory.py` |
| Library functions | ✅ Done | 7 functions in `ai_memory_lib.py` |
| Shell helpers | ✅ Done | `memory_processed_command_check`, `_claim`, `_complete`, `_list` in `memory_helpers.sh` |
| plan.yml integration | ✅ Done | `/answer` command check → claim → skip guard |
| implement.yml integration | ✅ Done | `/approved` command check → claim → complete with PR metadata |
| orchestrate_clarify_respond.yml | ✅ Done | Clarify loop guard with cycle counting |

**No remaining work.** This recommendation is fully implemented.

### P3: Prompt Single-Source Refactor — **DONE**

**Original recommendation**: Use `prompts/header.txt` + `mode-*.txt` as the only prompt source.

**Implementation status**: ✅ Complete.

| Component | Status | Evidence |
| --- | --- | --- |
| Externalized prompt files | ✅ Done | 23 files in `prompts/` covering all phases |
| Dynamic rendering | ✅ Done | `scripts/render_prompt.sh` with Serena block injection |
| CI validation | ✅ Done | Non-empty check, required prompts exist, no inline Serena blocks, all placeholders resolved |
| Inline prompt elimination | ✅ Done | Zero inline prompt heredocs remain in workflows |

**No remaining work.** This recommendation is fully implemented.

### P4: Manual Phase Replay Workflow — **DONE**

**Original recommendation**: Add a `workflow_dispatch` replay that accepts issue_number + phase.

**Implementation status**: ✅ Complete — exceeded the original scope.

Implemented replay paths:
- `review_autofix.yml` — `workflow_dispatch` with `pr_number` input
- `internal-orchestrate-poll.yml` — manual poller trigger
- `internal-orchestrate.yml` — manual orchestrate trigger
- `internal-review.yml` — manual review trigger
- `internal-validate.yml` — manual validate trigger
- `mark-stable.yml` — manual release with `version_tag` + `dry_run`
- `test-and-mark-stable.yml` — combined test + release
- `workflow-log-analysis.yml` — manual log analysis

**No remaining work.** This recommendation is fully implemented.

### P5: Workflow Template Generation + Drift Checks — **PARTIAL**

**Original recommendation**: Establish workflow snippet/template generation for shared blocks and add drift checks in CI.

**Implementation status**: ⚠️ Partially complete.

| Component | Status | Evidence |
| --- | --- | --- |
| Template directory | ✅ Done | `workflow-templates/` with 12 templates |
| Consumer auto-sync | ✅ Done | `update_workflows.yml` via `repository_dispatch` |
| Consumer registry | ✅ Done | `.github/ai/consumer_repos.json` (11 repos) |
| CI YAML linting | ✅ Done | `yamllint` + `actionlint` in `ci.yml` |
| Script reference validation | ✅ Done | `check_workflow_script_refs.py` in CI |
| Deployed-vs-template drift scan | ❌ Pending | No workflow compares consumer deployments to templates |
| Shared shell block templating | ❌ Pending | Common patterns (Codex config, Telegram, guards) still manually copied |

**Remaining work**:

1. **Consumer drift-scan workflow** (Medium effort): A scheduled or `workflow_dispatch` workflow that checks out each consumer repo in `consumer_repos.json`, diffs their `ai-*.yml` wrappers against `workflow-templates/`, and reports divergence via Telegram/issue. Squad's `templates/workflows/*` + `squad upgrade` pattern is the inspiration — our `update_workflows.yml` auto-sync partially addresses this, but a periodic audit would catch repos that disabled auto-updates or have manual modifications.

2. **Shared shell block extraction** (Medium-High effort): Factor common shell patterns (editor watchdog setup, Telegram notification blocks, memory bootstrap, Codex config assembly) into sourced helper scripts. Several of these already exist (`memory_helpers.sh`, `tg_helpers.sh`, `gh_helpers.sh`, `label_helpers.sh`) — the gap is that some workflow inline steps still duplicate logic that could be delegated to these helpers.

---

## New Patterns from Latest Squad Analysis (N1–N4)

These patterns were not covered in the March 2026 report. They emerged from the latest Squad codebase review (v0.9.1+) and represent capabilities that have evolved since the original analysis.

### N1: GitHub Label-Sync from Contract — **ADOPT** (priority: P1-addendum)

**Squad pattern**: `sync-squad-labels.yml` reads `.squad/team.md`, parses the team roster, and creates/updates GitHub labels (including colors and descriptions) via the GitHub API. Triggers on changes to `team.md` or via `workflow_dispatch`.

**Current state**: We have `label_contract.v1.json` (the source of truth) and `ai_labels.py` (the enforcement engine), but no workflow that ensures the actual GitHub labels in a repository match the contract. Consumer repos must create `ai:*` labels manually during onboarding.

**Proposed adaptation**: Add a `sync-ai-labels.yml` reusable workflow + template that:
- Reads `label_contract.v1.json` from `coding-workflows@stable`
- Creates/updates all `ai:*` labels in the consumer repo with correct colors and descriptions
- Triggers on `workflow_dispatch` (manual) and optionally on `repository_dispatch` from `update_workflows.yml`
- Reports created/updated/unchanged counts

**Expected impact**: Eliminates manual label setup for new consumer repos. Prevents label typos and color inconsistencies. Pairs naturally with `update_workflows.yml` onboarding.

**Effort**: Low — the contract schema already defines everything needed; the workflow is a straightforward GitHub API loop.

### N2: Per-Repo Accumulated Learnings — **ADAPT** (priority: P6)

**Squad pattern**: Each agent has a `history.md` file in `.squad/agents/{name}/` where project-specific learnings accumulate across sessions. After 2-3 sessions, agents "know your conventions, preferences, and architecture" and stop asking questions they have already answered.

**Current state**: Our AI memory system captures run events, task lineage, decisions, and processed commands — all structured and schema-validated. However, these are transactional records. There is no accumulated "learnings" record type that distills cross-session patterns specific to a consumer repo (e.g., "this repo uses Prisma ORM", "tests must pass with `npm run test:ci`", "PRs targeting `develop` not `main`").

**Proposed adaptation**: Add a `repo_learnings` record type to the memory schema. After each successful pipeline completion (implement → review → merge), the memory maintenance workflow could extract and persist key patterns observed during the run. Subsequent clarify/plan/implement phases would inject these learnings into prompts, reducing unnecessary clarification questions and improving plan quality.

**Expected impact**: Reduced clarification cycles on repeat operations. Better first-attempt plan quality for repos with non-standard conventions.

**Effort**: Medium — requires schema extension, maintenance-phase extraction logic, and prompt injection wiring.

**Risk**: Low — fail-open by design (missing learnings = current behavior).

### N3: Consumer Drift-Scan Audit — **ADOPT** (priority: P5-addendum)

**Squad pattern**: Squad maintains `templates/workflows/*` as the canonical source and uses `squad upgrade` to sync installed copies. Any local modification is overwritten on upgrade.

**Current state**: Our `update_workflows.yml` pushes template updates to consumer repos, but there is no reverse check. If a consumer repo manually edits their `ai-*.yml` wrappers, modifies permissions, or disables the auto-updater, we have no visibility.

**Proposed adaptation**: A scheduled `audit-consumer-drift.yml` workflow that:
- Iterates `consumer_repos.json`
- For each repo, fetches `.github/workflows/ai-*.yml` via the GitHub API
- Diffs against `workflow-templates/*.yml` (allowing for expected variable differences)
- Reports divergent repos via Telegram alert with specific file diffs
- Runs weekly on `schedule` + on `workflow_dispatch`

**Expected impact**: Early detection of consumer repos that fall out of sync, especially those that disabled auto-updates or made local modifications that could break on the next template push.

**Effort**: Medium — primarily GitHub API reads + diff logic.

### N4: Governance-as-Code Write Guards — **EVALUATE** (priority: P7)

**Squad pattern**: Per-agent file-write guards enforced programmatically — agents can only write to paths explicitly allowed (e.g., `src/**`, `.squad/**`, `docs/**`). Reviewer lockout prevents self-review (if agent A wrote code, agent A cannot review it).

**Current state**: We have coarser-grained guards:
- `ALLOW_WORKFLOW_EDITS` — controls whether AI can modify `.github/workflows/` files
- `BULK_DELETE_THRESHOLD` — blocks commits that delete more than N files
- Destructive-commit guard — prevents bulk file deletions
- Canonical workflow-source file guard — blocks deletion of workflow source files

**Gap**: No per-phase path restrictions. The implement phase could theoretically modify any file in the repo. The review phase uses separate reviewer/editor models but there is no programmatic lockout preventing the same model context from reviewing its own output.

**Proposed adaptation**: This is an evaluation item, not an immediate adoption. Investigate whether path-based write guards per pipeline phase would reduce operational risk (e.g., implement can write `src/**` but not `.github/**` unless `ALLOW_WORKFLOW_EDITS=true`; review editor can only modify files flagged in the review findings). The current `ALLOW_WORKFLOW_EDITS` flag is the right granularity for the most dangerous path — extending it to other paths would add complexity without proportional risk reduction for most consumer repos.

**Expected impact**: Marginal for well-behaved LLMs; significant as a safety net against hallucinated file modifications.

**Effort**: Medium-High — requires per-phase allowlist configuration and post-commit validation.

**Risk**: Medium — overly restrictive guards could block legitimate cross-cutting changes.

---

## Adoption Sequencing (Updated)

### Completed (since March 2026)

| ID | Item | Completed |
| --- | --- | --- |
| P1 | AI label contract + enforcement | ✅ Full (contract, script, CI, workflow integration) |
| P2 | Processed-command idempotency ledger | ✅ Full (schema, CLI, library, workflow wiring) |
| P3 | Prompt single-source refactor | ✅ Full (23 files, rendering, CI validation) |
| P4 | Manual phase replay workflows | ✅ Full (10+ `workflow_dispatch` entry points) |

### Remaining quick wins (low risk, high leverage)

1. **N1** — GitHub label-sync workflow from contract (Low effort, closes the P1 gap)
2. **P5a** — Consumer drift-scan audit workflow (Medium effort, closes P5 gap)

### Follow-up items

3. **P5b** — Shared shell block extraction into helper scripts (Medium-High effort)
4. **N2** — Per-repo accumulated learnings in memory system (Medium effort)

### Evaluate later

5. **N4** — Per-phase file-write guards (Medium-High effort, needs cost/benefit analysis)

---

## Patterns Intentionally Rejected

| Pattern | Source | Reason |
| --- | --- | --- |
| Branch promotion pipeline (dev → preview → main) | `squad-promote.yml` | Our tag-based release model (`@stable` channel) serves the same purpose with less branch management overhead. |
| Triage routing with capability matching | `squad-triage.yml` | Not applicable — our pipeline uses a single-agent-per-issue model, not a multi-specialist team. |
| Agent identity / charter files | `.squad/agents/*/charter.md` | Our pipeline phases are defined by prompt files, not by agent identity documents. The prompt-per-phase model is more maintainable for our use case. |
| Dual-root mode (shared team across repos) | `squad init --mode remote` | Our consumer-repo model (reusable workflows + template sync) already provides centralized control without requiring a shared team root. |
| Scribe / session log archive | `.squad/scribe/`, `.squad/log/` | Our AI memory system with structured schemas, run events, and task lineage provides equivalent or better auditability. |
| Watch mode continuous polling | Ralph (`squad triage --interval`) | Our `orchestrate_poll.yml` with self-retrigger is functionally equivalent. |
