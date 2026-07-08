# Complete the Remaining Squad-Workflow Improvements

## Archived status

This file is the canonical completed-plan record for tracking issue `#3408`. The closeout summary below reflects a re-audit of the shipped repository state on 2026-07-08 UTC; the historical plan text that follows is preserved for context, and where it conflicts with the closeout summary, the closeout summary is authoritative.

## Closeout summary

All five phases shipped and wired, re-audited against `origin/main` on 2026-07-08 UTC:

- **N1 GitHub label-sync** — `scripts/ai_labels.py` `cmd_sync_labels` + `sync-labels` subcommand; `.github/workflows/sync_ai_labels.yml` and `workflow-templates/ai-sync-labels.yml`; CI dry-run smoke and `tests/test_ai_labels.py`.
- **P5a consumer drift-scan** — `scripts/audit_consumer_drift.py`, `.github/workflows/audit_consumer_drift.yml` (weekly cron), `tests/test_audit_consumer_drift.py`.
- **P5b shared shell extraction** — `codex_helpers.sh` / `watchdog_helpers.sh` / `tg_helpers.sh` / `memory_helpers.sh` with call-sites rewired and a CI anti-regression lint against inline `config.toml` assembly.
- **N2 per-repo learnings** — `repo_learnings` memory category, `memory_maintenance` extraction step gated by `MEMORY_LEARNINGS_EXTRACT_ENABLED`, `{{REPO_LEARNINGS}}` injection in clarify/plan.
- **N4 per-phase write guards** — `.github/ai/write_guards.v1.json`, `scripts/write_guard.sh`, wired into review / implement / validate.

Only deviation: the explicitly-optional `cmd_record_learning` convenience wrapper (N2 §4.2) was dropped, as the plan permitted.

---

## Summary

Implement the five remaining items from `docs/squad-workflow-improvements.md`
(N1, P5a, P5b, N2, N4) as four committed feature PRs plus one
evaluate-and-implement N4 PR. After this plan PR lands, the source report
(`docs/squad-workflow-improvements.md`) is deleted because the plan
supersedes it.

## Context

`docs/squad-workflow-improvements.md` (2026-04-27 refresh of the 2026-03-22
report) closed out P1–P4 and N3 was rolled into P5a. The following five
items remain:

| ID | Item | Original effort estimate | Status in report |
|---|---|---|---|
| N1 | GitHub label-sync workflow from `label_contract.v1.json` | Low | Closes the P1 gap |
| P5a | Consumer drift-scan audit workflow | Medium | Closes the P5 gap |
| P5b | Shared shell block extraction into helper scripts | Medium-High | P5 follow-up |
| N2 | Per-repo accumulated learnings in memory schema | Medium | New pattern |
| N4 | Governance-as-code per-phase file-write guards | Medium-High | Evaluate-then-implement |

The user has elected (Q2: B) to **implement** N4 rather than treat it as
evaluation-only. The user also elected (Q3: B) to **delete the report doc
in this plan PR**, so this plan must capture every constraint and detail
needed downstream — once the report is gone, this file is the only
record.

Verbatim quotes from the report are preserved in the per-item sections
below so nothing is lost. The 2026-03-22 source under
`research/squad-workflow-improvements-20260322T042436Z.md` (cited in
the report header) remains untouched.

## Goals

- Land each of N1, P5a, P5b, N2, N4 as its own merged PR on the `stable`
  release channel, so each item is independently reviewable and revertible.
- Close the P1 gap: a new consumer repo can onboard with zero manual label
  setup once N1 lands.
- Close the P5 gap: out-of-sync consumer wrappers are detected
  automatically and reported via Telegram once P5a lands.
- Reduce inline shell duplication in workflows (P5b): every duplicated
  block in scope MUST be sourced from a `*_helpers.sh` module after the
  P5b PR lands; lint enforces no regression.
- Persist repo-specific learnings across pipeline runs (N2): subsequent
  clarify/plan phases inject prior learnings into prompts.
- Establish per-phase file-write guards (N4): implement/plan/review/validate
  phases each enforce an allowlist; violations fail the commit step.
- After all five PRs are merged to `stable`, the
  `docs/squad-workflow-improvements.md` retirement (already executed in
  this plan PR) is consistent with the codebase state.

## Non-goals

- This plan is NOT a re-evaluation of P1–P4 or N3; those are settled per
  the report.
- This plan does NOT propose changes to the Squad branch-promotion model,
  triage routing, or agent-charter files (intentionally rejected — see
  the report's "Patterns Intentionally Rejected" section, preserved
  verbatim in the appendix below).
- This plan does NOT introduce a new model, prompt template, or pipeline
  phase. All work is on the workflow/automation surface.
- N4 implementation in this plan is the **minimum viable** per-phase
  guard (allowlist + post-commit validation); broader governance-as-code
  features (e.g. cross-agent reviewer lockout, per-file SLA tracking)
  remain out of scope.
- Test propagation to consumer repos is out of scope; consumer-side
  validation comes from running the existing `update_workflows.yml`
  sync after each PR is merged to `stable`.

## Constraints

- **CLAUDE.md §6 (naming immutability)**: every new identifier introduced
  by this work (workflow filenames, env vars, log prefixes, helper
  function names, memory category values, label-contract fields) is
  brand new. No rename or removal of an existing identifier is required
  or permitted. Specifically, the stable log prefixes listed in
  `agents.md` (`LABEL_REPAIR`, `LABEL_REPAIR_DIFF`, `AUTOFIX_PEER_CHECK`,
  `AUTOFIX_DISPATCH_SKIPPED`, `AUTOFIX_DISPATCH_ISSUED`,
  `AI_PHASE_FAILURE_V1`, `SEMBLE_QUERY`, `SEMBLE_FALLBACK`,
  `SERENA_QUERY`, `SERENA_FALLBACK`, `SERENA_PROBE`) remain untouched.
- **CLAUDE.md §10 (MongoDB)**: not applicable. The repo has no
  `/db/contracts/` directory; the `ai-memory/schemas/*.json` files are
  JSON-schema validators for git-stored memory records, not MongoDB
  contracts. The N2 schema change is therefore a JSON-schema edit, not a
  Mongo contract update.
- **CLAUDE.md §14 (consumer-repo registry)**: N1 propagates a new wrapper
  template to all 11 repos in `.github/ai/consumer_repos.json`. P5a runs
  centrally in `coding-workflows` and reads `consumer_repos.json` to
  iterate targets; no new consumers are added. P5b, N2, N4 do not
  propagate new wrappers but DO modify workflow templates (P5b helpers,
  N2 memory-maintenance), which propagate via the existing
  `update_workflows.yml` flow.
- **CLAUDE.md §15 (GitHub API hygiene)**: P5a is the only item that adds
  bulk `gh api` calls (one per consumer repo per audit run). The plan
  specifies a single batched-fetch path with cycle-local caching and
  fail-open semantics. Other items do not add new GitHub API call
  surface beyond existing helpers.
- **CLAUDE.md §0 / §2**: per-item Implementation Steps below are written
  to be executable without further clarification. Any genuinely
  ambiguous sub-decision is listed in `Open Questions`.
- **`agents.md` stable log prefixes**: any new log line introduced by
  this work uses a new prefix (e.g. `LABEL_SYNC_*`, `DRIFT_SCAN_*`,
  `WRITE_GUARD_*`) — these become contractual once shipped per §6.

## Approach

Five PRs, landed in dependency order:

```
N1 (label-sync)        # standalone; no deps
  → P5a (drift-scan)   # depends on N1 only for `sync-ai-labels.yml` filename
       being in the template set the audit checks against
  → P5b (helper extraction)
       # standalone; can ship in parallel with P5a if helpful
  → N2 (repo learnings)
       # depends on memory schema; touches memory_maintenance.yml
  → N4 (write guards)
       # last because review-blocker risk; touches implement.yml,
       # validate.yml, review_apply_fixes.sh, review_commit_changes.sh
```

Each PR follows the standard contract: code change + tests + agents.md
log-prefix entry (if new prefixes are introduced) + workflow-template
update (if a consumer-facing wrapper changes) + README.md env-var entry
(if a new env var is introduced).

Alternatives considered:

- **One large PR covering everything**: rejected via Q4. Too hard to
  review; one failure stalls every other item.
- **Quick-wins-only plan (Q2: C)**: rejected via Q2. The user wants the
  full sweep.
- **Defer N4 to a follow-up plan**: rejected via Q2: B. The user opted
  to commit to N4 implementation now.

## Implementation Steps

### Phase 1 — N1: GitHub Label-Sync from Contract

Verbatim from the report (preserved here because the report is being
deleted in this plan PR):

> **Squad pattern**: `sync-squad-labels.yml` reads `.squad/team.md`,
> parses the team roster, and creates/updates GitHub labels (including
> colors and descriptions) via the GitHub API.
>
> **Current state**: We have `label_contract.v1.json` (the source of
> truth) and `ai_labels.py` (the enforcement engine), but no workflow
> that ensures the actual GitHub labels in a repository match the
> contract.
>
> **Proposed adaptation**: Add a `sync-ai-labels.yml` reusable workflow
> + template that reads `label_contract.v1.json` from
> `coding-workflows@stable`, creates/updates all `ai:*` labels in the
> consumer repo with correct colors and descriptions, triggers on
> `workflow_dispatch` and optionally on `repository_dispatch` from
> `update_workflows.yml`, and reports created/updated/unchanged counts.

#### 1.1 New CLI subcommand in `ai_labels.py`

Add `cmd_sync_labels` mirroring the existing `cmd_resolve_phase` /
`cmd_repair_labels` shape:

- Inputs: `--repo <owner/name>`, `--contract <path>`, `--dry-run`
- Output: JSON `{ "created": N, "updated": N, "unchanged": N, "errors": [] }`
- Implementation: for each entry in `labels` of the contract:
  1. `GET /repos/{repo}/labels/{name}`
  2. If 404 → `POST /repos/{repo}/labels` (create)
  3. If 200 + color/description differ → `PATCH /repos/{repo}/labels/{name}` (update)
  4. Else → unchanged
- Fail-open on per-label API errors; aggregate into `errors[]`; non-zero
  exit only if **every** label call failed.
- New log prefixes: `LABEL_SYNC_CREATED`, `LABEL_SYNC_UPDATED`,
  `LABEL_SYNC_UNCHANGED`, `LABEL_SYNC_ERROR`.

Files: `scripts/ai_labels.py` (~ +80 lines).

#### 1.2 New reusable workflow `sync_ai_labels.yml`

- Trigger: `workflow_call` (with `dry_run` boolean input) + standalone
  `workflow_dispatch` for testing inside `coding-workflows` itself.
- Single job, `ubuntu-latest`, ≤ 5 min timeout.
- Steps:
  1. `actions/checkout@v5` (no token needed; contract is in repo).
  2. Run `python3 scripts/ai_labels.py sync-labels --repo "${{ github.repository }}"`.
  3. Emit a `$GITHUB_STEP_SUMMARY` table with counts.
  4. Post a Telegram alert via `tg_send_msg` at `DEBUG` level on
     success, `WARNING` on partial errors, `ERROR` on full failure.
- Permissions: `contents: read`, `issues: write` (required for labels API).

File: `.github/workflows/sync_ai_labels.yml` `[new]`.

#### 1.3 New consumer wrapper template

`workflow-templates/ai-sync-labels.yml`:

```yaml
name: AI Sync Labels
on:
  workflow_dispatch: {}
  repository_dispatch:
    types: [coding-workflows-stable-released]
permissions:
  contents: read
  issues: write
jobs:
  sync:
    uses: shubhodeep1/coding-workflows/.github/workflows/sync_ai_labels.yml@stable
    secrets: inherit
```

File: `workflow-templates/ai-sync-labels.yml` `[new]`.

#### 1.4 Propagation

- The template will land in 11 consumer repos on the next stable
  release via the existing `update_workflows.yml` auto-sync. No
  change to `update_workflows.yml` itself is required because it
  copies every `ai-*.yml` template by glob.
- Onboarding docs: append a short note to `README.md`'s
  "Initial Setup" subsection (around line 466 reference area where the
  other `ai-*` wrappers are listed).

#### 1.5 Tests

- Unit test for `cmd_sync_labels` (mock GitHub API with `responses`
  library or shell stub).
- Integration smoke: add a step to `ci.yml` that invokes
  `ai_labels.py sync-labels --dry-run` and asserts non-error exit.

### Phase 2 — P5a: Consumer Drift-Scan Audit Workflow

Verbatim from the report:

> **Squad pattern**: Squad maintains `templates/workflows/*` as the
> canonical source and uses `squad upgrade` to sync installed copies.
> Any local modification is overwritten on upgrade.
>
> **Current state**: Our `update_workflows.yml` pushes template updates
> to consumer repos, but there is no reverse check. If a consumer repo
> manually edits their `ai-*.yml` wrappers, modifies permissions, or
> disables the auto-updater, we have no visibility.
>
> **Proposed adaptation**: A scheduled `audit-consumer-drift.yml`
> workflow that iterates `consumer_repos.json`, fetches
> `.github/workflows/ai-*.yml` via the GitHub API, diffs against
> `workflow-templates/*.yml` (allowing for expected variable
> differences), reports divergent repos via Telegram alert with
> specific file diffs, runs weekly on `schedule` + on
> `workflow_dispatch`.

#### 2.1 New workflow `audit_consumer_drift.yml`

- Trigger: `schedule: [cron: '0 8 * * 1']` (Monday 08:00 UTC) +
  `workflow_dispatch`.
- Permissions: `contents: read` (the workflow only reads, never
  writes).
- Runs only in `coding-workflows`. Not propagated as a wrapper.
- Reads `.github/ai/consumer_repos.json` for the target list.
- For each consumer repo:
  1. For each `workflow-templates/ai-*.yml`, fetch the same path from
     the consumer repo via `mcp__github__get_file_contents` or
     `gh api /repos/{owner}/{repo}/contents/.github/workflows/{file}`.
  2. Normalize: strip the canonical header comment, trim trailing
     whitespace.
  3. `diff -u` template vs fetched content.
  4. If non-empty diff: append `{ repo, file, diff }` to a results
     array.
- Emit `$GITHUB_STEP_SUMMARY` table grouped by repo.
- Telegram alert:
  - Zero drift → `DEBUG`-level "All consumer wrappers match
    `@stable`".
  - Drift detected → `WARNING`-level with one bullet per (repo, file)
    pair, capped at 25 lines per file diff (truncate beyond that).

File: `.github/workflows/audit_consumer_drift.yml` `[new]`.

#### 2.2 Helper script `scripts/audit_consumer_drift.py`

To keep the workflow YAML small and testable, the diff logic lives in
a Python script:

- Inputs: `--consumer-repos <path>`, `--templates-dir <path>`,
  `--output <json-path>`, `--max-diff-lines <int>`
- Output: JSON `{ "drift_count": N, "items": [{ "repo", "file",
  "diff_preview", "diff_lines_total" }] }`
- GitHub API hygiene (CLAUDE.md §15): one prefetch per consumer repo
  fetches the full `.github/workflows/` directory listing with one
  call, then per-file content fetches use that listing to skip
  non-existent files (no 404 round-trip). Cycle-local cache:
  `_consumer_workflow_cache` keyed by `(repo, file)`.
- Fail-open: per-repo API failures are recorded as `{ "repo": X,
  "error": "fetch_failed" }` and do not abort the loop.

File: `scripts/audit_consumer_drift.py` `[new]` (~ +200 lines).

#### 2.3 Log prefixes

New stable prefixes (contractual after merge, per `agents.md`):
`DRIFT_SCAN_START`, `DRIFT_SCAN_DIFF`, `DRIFT_SCAN_OK`,
`DRIFT_SCAN_ERROR`. Add to `agents.md`'s "Stable log prefixes" list.

#### 2.4 Tests

- Unit tests for `audit_consumer_drift.py` with mocked GitHub API
  responses: clean-match, single-file drift, multi-repo drift,
  per-repo fetch error.
- Manual smoke: run `workflow_dispatch` against current consumer set
  and verify the Telegram alert renders correctly.

### Phase 3 — P5b: Shared Shell Block Extraction

Verbatim from the report:

> Factor common shell patterns (editor watchdog setup, Telegram
> notification blocks, memory bootstrap, Codex config assembly) into
> sourced helper scripts. Several of these already exist
> (`memory_helpers.sh`, `tg_helpers.sh`, `gh_helpers.sh`,
> `label_helpers.sh`) — the gap is that some workflow inline steps
> still duplicate logic that could be delegated to these helpers.

#### 3.1 Survey + duplication map (preliminary step in the PR)

Before extraction, generate a duplication report via `grep` + manual
audit:

- Search for inline blocks ≥ 8 lines that appear in 2+ workflow files.
- Candidates (from prior analysis):
  - Codex config setup before `codex exec` (≈ 15 lines, duplicated in
    `implement.yml`, `clarify.yml`, `plan.yml`, `validate.yml`,
    `review_autofix.yml`).
  - Telegram "phase failure" alert block (≈ 12 lines, duplicated in
    every phase workflow).
  - Memory bootstrap (clone memory branch, set up sparse checkout,
    configure git creds) (≈ 20 lines, duplicated across memory-using
    phases).
  - Editor watchdog timer setup (≈ 18 lines, in `implement.yml`,
    `review_autofix.yml`).

#### 3.2 New helper functions

Extend the existing helpers — DO NOT create new helper files unless a
genuinely new domain emerges:

- `scripts/tg_helpers.sh`: add `tg_send_phase_failure <phase> <reason>`
  that wraps the 12-line block.
- `scripts/memory_helpers.sh`: add `memory_bootstrap <branch>` for the
  20-line clone + sparse-checkout block.
- `scripts/codex_helpers.sh` `[new]`: new file owning Codex config
  assembly. The function `codex_config_assemble <model> <reasoning>
  <verbosity>` replaces the duplicated 15-line block. Calling sites
  use it via `source` then function call.
- `scripts/watchdog_helpers.sh` `[new]`: new file for the editor
  watchdog block.

#### 3.3 Workflow call-site updates

For each workflow listed in §3.1, replace the inline block with
`source scripts/<helper>.sh && <fn> <args>`. Diff each replacement
**byte-for-byte equivalent** in observable behavior (env vars set,
files written, log lines emitted).

#### 3.4 Lint enforcement

Add a `ci.yml` check that rejects re-introduction of the extracted
blocks:

```bash
# Fail if any workflow YAML re-introduces an inline `cat > config.toml << EOF`
# Codex config block (extracted to codex_helpers.sh).
if grep -rn "cat > .*config.toml << " .github/workflows/ scripts/*.sh; then
  echo "::error::Inline Codex config block detected; use codex_helpers.sh"
  exit 1
fi
```

One such check per extracted block.

#### 3.5 Tests

- Bash unit tests under `scripts/dev/` for each new helper function,
  using existing test patterns.
- Manual smoke: trigger one each of `implement.yml`, `review_autofix.yml`,
  `validate.yml` via `workflow_dispatch` on a smoke issue and verify
  no regression.

#### 3.6 Risk mitigation

This phase touches many workflows. To limit blast radius:

- Land the helpers + the lint check FIRST in a no-op commit (helpers
  exist but no workflow calls them yet).
- Then land call-site replacements ONE workflow at a time, each in a
  separate commit inside the same PR.
- Each commit pushes a smoke test via `workflow_dispatch` before the
  next commit lands.

### Phase 4 — N2: Per-Repo Accumulated Learnings

Verbatim from the report:

> **Squad pattern**: Each agent has a `history.md` file in
> `.squad/agents/{name}/` where project-specific learnings accumulate
> across sessions.
>
> **Proposed adaptation**: Add a `repo_learnings` record type to the
> memory schema. After each successful pipeline completion (implement
> → review → merge), the memory maintenance workflow could extract
> and persist key patterns observed during the run. Subsequent
> clarify/plan/implement phases would inject these learnings into
> prompts.

#### 4.1 Schema extension

Edit `ai-memory/schemas/memory_record.v1.json`:

- Add `"repo_learnings"` to the `category` enum (line 35-42 currently).
- Existing values remain: `decisions`, `constraints`, `patterns`,
  `incidents`, `run_events`, `task_summaries`.
- No `additionalProperties` change; the existing envelope (record_id,
  scope, summary, details, confidence, fingerprint, provenance,
  lineage, timestamps) is sufficient.

Per §6: this is an additive change. No existing identifier is renamed
or removed. Tests that depend on `category` matching one of the
historical values continue to pass; tests that enumerate the full
enum need an update (see §4.5).

#### 4.2 CLI surface

Reuse the existing `record-candidate` / `promote` / `retrieve`
commands in `ai_memory.py`. No new commands needed; `--category
repo_learnings` is now valid input.

Optionally, add a thin convenience wrapper `cmd_record_learning` that
defaults `--scope-level=global`, `--status=active` (skipping the
candidate step), and `--confidence=0.7`. Keep it optional; if it
complicates the CLI it can be dropped.

#### 4.3 Extraction in `memory_maintenance.yml`

The memory-maintenance workflow runs nightly (existing schedule). Add
a new step `extract-repo-learnings`:

- Inputs: last 24h of `run_events` and `task_summaries` for runs
  whose outcome is `merged`.
- Logic: heuristic extraction (LLM call to `openai/gpt-5.4-mini` with
  a new prompt `prompts/mode-extract-learnings.txt`). The prompt
  examines run metadata (files touched, env vars set, commands run,
  validation outcomes) and emits 0–5 candidate learnings per repo.
- Each candidate goes through standard candidate → active promotion
  via existing `ai_memory.py` flow.
- New env var: `MEMORY_LEARNINGS_EXTRACT_ENABLED` (default `true`,
  documented in `README.md`).

#### 4.4 Injection in clarify / plan prompts

- `scripts/render_prompt.sh`: add a new placeholder
  `{{REPO_LEARNINGS}}` rendered before the existing memory-context
  injection.
- `prompts/header.txt`: reference the placeholder.
- Retrieval: `ai_memory.py retrieve --category=repo_learnings
  --scope-level=global --max=10`.

#### 4.5 Tests

- Unit test: schema validation accepts `repo_learnings` as a valid
  category.
- Unit test: existing fixtures continue to validate.
- Integration: stub a memory-maintenance run with a fake merged-PR
  payload; assert at least one `repo_learnings` record is written.
- Prompt-render test: assert `{{REPO_LEARNINGS}}` resolves correctly
  on empty (no learnings yet) and populated (≥ 1 record) states.

#### 4.6 Fail-open guarantee

If learning extraction fails (LLM error, schema mismatch), the
memory-maintenance run continues; subsequent clarify/plan phases
receive an empty `{{REPO_LEARNINGS}}` block. This matches the
existing memory-system fail-open contract.

### Phase 5 — N4: Per-Phase File-Write Guards

Verbatim from the report:

> **Squad pattern**: Per-agent file-write guards enforced
> programmatically — agents can only write to paths explicitly allowed
> (e.g., `src/**`, `.squad/**`, `docs/**`).
>
> **Proposed adaptation**: Investigate whether path-based write guards
> per pipeline phase would reduce operational risk (e.g., implement
> can write `src/**` but not `.github/**` unless
> `ALLOW_WORKFLOW_EDITS=true`; review editor can only modify files
> flagged in the review findings).

The user (Q2: B) opted to implement rather than only evaluate. The
implementation below is the **minimum viable** version, conservative
to avoid blocking legitimate work.

#### 5.1 Allowlist configuration file

`.github/ai/write_guards.v1.json` `[new]`:

```json
{
  "schema_version": "write_guards.v1",
  "phases": {
    "implement": {
      "allowed_globs": ["**"],
      "blocked_globs": [".git/**"],
      "conditional_blocked_globs": {
        ".github/workflows/**": "ALLOW_WORKFLOW_EDITS"
      }
    },
    "review_editor": {
      "allowed_globs": ["**"],
      "blocked_globs": [".git/**"],
      "conditional_blocked_globs": {
        ".github/workflows/**": "ALLOW_WORKFLOW_EDITS"
      }
    },
    "validate_fix_harness": {
      "allowed_globs": ["validation/**", "scripts/**"],
      "blocked_globs": [".git/**", ".github/**", "ai-memory/**"]
    }
  }
}
```

The initial allowlist is deliberately permissive (`**` allowed, only
`.git` and conditionally `.github/workflows` blocked). This codifies
the current behavior; future tightening is a separate exercise.

#### 5.2 Enforcement helper `scripts/write_guard.sh`

New helper sourced in commit-time scripts:

- Function `write_guard_check <phase> <staged-files-list>`:
  - Loads `write_guards.v1.json`.
  - For each staged file, evaluates allowed/blocked globs and
    conditional gates.
  - Returns 0 if all files pass; non-zero (and logs `WRITE_GUARD_BLOCK
    phase=<p> file=<f> reason=<r>`) if any file fails.
- Fail-open on JSON parse error → log `WRITE_GUARD_CONFIG_ERROR` and
  return 0.

#### 5.3 Call-site wiring

- `scripts/review_commit_changes.sh`: insert `write_guard_check
  review_editor "$(git diff --cached --name-only)"` before the
  existing `ALLOW_WORKFLOW_EDITS` check (which becomes a sub-rule of
  the guard config). DO NOT remove the existing check — keep both
  enforced so disabling the new guard still leaves the old guard in
  place. Per §6.
- `.github/workflows/implement.yml`: add a `write_guard_check implement
  …` step before the commit step.
- `.github/workflows/validate.yml`: add `write_guard_check
  validate_fix_harness …`.

#### 5.4 Log prefixes

New stable prefixes (add to `agents.md`):
`WRITE_GUARD_BLOCK`, `WRITE_GUARD_CONFIG_ERROR`,
`WRITE_GUARD_BYPASS_ENV`.

#### 5.5 Tests

- Unit: per-phase + per-file table tests covering allowlist,
  blocklist, conditional-with-env-on, conditional-with-env-off, JSON
  parse error.
- Integration smoke: trigger `implement.yml` on a smoke issue that
  tries to write `.git/foo`; assert the commit step fails with
  `WRITE_GUARD_BLOCK`.

#### 5.6 Rollout

Default `WRITE_GUARDS_ENABLED=true`. Operators can set
`WRITE_GUARDS_ENABLED=false` repo-var (documented in `README.md`) for
emergency bypass. The variable is logged on every guard invocation
(`WRITE_GUARD_BYPASS_ENV`) so any disablement is auditable.

### Phase 6 — Doc Retirement (executed in THIS plan PR per Q3: B)

This plan PR includes the deletion of `docs/squad-workflow-improvements.md`.
All verbatim content from that report (proposals, intentionally-rejected
patterns) is preserved in this plan file (see "Implementation Steps" and
"Appendix: Patterns Intentionally Rejected"). No information loss.

## Files & Modules

### This plan PR (`claude/write-plan-complete-squad-improvements`)

- `docs/plans/complete-squad-improvements-plan.md` `[new]`
- `docs/squad-workflow-improvements.md` `[del]` (per Q3: B)

### Phase 1 PR (N1) — `claude/implement-n1-label-sync`

- `scripts/ai_labels.py` (edit: add `cmd_sync_labels`)
- `.github/workflows/sync_ai_labels.yml` `[new]`
- `workflow-templates/ai-sync-labels.yml` `[new]`
- `README.md` (edit: onboarding note)
- `agents.md` (edit: new log prefixes)
- `tests/test_ai_labels.py` (edit: add sync-labels unit tests)
- `.github/workflows/ci.yml` (edit: dry-run smoke step)

### Phase 2 PR (P5a) — `claude/implement-p5a-drift-scan`

- `scripts/audit_consumer_drift.py` `[new]`
- `.github/workflows/audit_consumer_drift.yml` `[new]`
- `agents.md` (edit: new log prefixes)
- `tests/test_audit_consumer_drift.py` `[new]`
- `README.md` (edit: drift-scan note)

### Phase 3 PR (P5b) — `claude/implement-p5b-shell-extraction`

- `scripts/tg_helpers.sh` (edit: add `tg_send_phase_failure`)
- `scripts/memory_helpers.sh` (edit: add `memory_bootstrap`)
- `scripts/codex_helpers.sh` `[new]`
- `scripts/watchdog_helpers.sh` `[new]`
- `.github/workflows/implement.yml` (edit: call-site replacement)
- `.github/workflows/clarify.yml` (edit: call-site replacement)
- `.github/workflows/plan.yml` (edit: call-site replacement)
- `.github/workflows/validate.yml` (edit: call-site replacement)
- `.github/workflows/review_autofix.yml` (edit: call-site replacement)
- `.github/workflows/ci.yml` (edit: anti-regression grep checks)
- `scripts/dev/test_codex_helpers.sh` `[new]`
- `scripts/dev/test_watchdog_helpers.sh` `[new]`

### Phase 4 PR (N2) — `claude/implement-n2-repo-learnings`

- `ai-memory/schemas/memory_record.v1.json` (edit: add enum value)
- `scripts/ai_memory.py` (edit: optional `cmd_record_learning`)
- `scripts/render_prompt.sh` (edit: `{{REPO_LEARNINGS}}` placeholder)
- `prompts/header.txt` (edit: reference placeholder)
- `prompts/mode-extract-learnings.txt` `[new]`
- `.github/workflows/memory_maintenance.yml` (edit: extraction step)
- `README.md` (edit: `MEMORY_LEARNINGS_EXTRACT_ENABLED` env var)
- `tests/test_memory_record_schema.py` (edit: new-category test)

### Phase 5 PR (N4) — `claude/implement-n4-write-guards`

- `.github/ai/write_guards.v1.json` `[new]`
- `scripts/write_guard.sh` `[new]`
- `scripts/review_commit_changes.sh` (edit: wire in guard)
- `.github/workflows/implement.yml` (edit: pre-commit guard step)
- `.github/workflows/validate.yml` (edit: pre-commit guard step)
- `agents.md` (edit: new log prefixes)
- `README.md` (edit: `WRITE_GUARDS_ENABLED` env var)
- `scripts/dev/test_write_guard.sh` `[new]`

## Data Model / Index Changes

Not applicable to MongoDB (no `/db/contracts/` in this repo).

JSON-schema change (N2 only):

- `ai-memory/schemas/memory_record.v1.json` — additive enum extension
  to `category`. The schema URI (`$id`) and `schema_version` const
  (`memory_record.v1`) remain unchanged because the change is additive
  and backward-compatible with existing records. If a future change is
  non-additive, bump to `memory_record.v2` per the existing
  convention.

## Tests

Per-PR test additions are listed in each Implementation Steps section.
Cross-cutting acceptance tests after all five PRs merge:

- Run the existing `ci.yml` suite (yamllint, actionlint, shellcheck,
  ruff, prompt validation, script-ref check) — all must pass.
- Dispatch `sync_ai_labels.yml` manually in a fresh consumer repo and
  verify all 21 `ai:*` labels appear with correct colors.
- Dispatch `audit_consumer_drift.yml` manually and verify the Telegram
  alert (or step summary) lists current drift accurately.
- Run a full smoke implement → review → validate → merge cycle and
  verify the `WRITE_GUARD_*` log lines appear at the expected
  step boundaries.
- Verify a `repo_learnings` record is created by
  `memory_maintenance.yml`'s nightly run after at least one
  successful implementation cycle.

## Risks & Mitigations

- **N1 — label collision with existing manually-created labels** —
  Color/description differences trigger a `PATCH`. Operator-customized
  colors will be overwritten. Mitigation: dry-run mode reports
  pending changes; default behavior is non-destructive update; no
  delete. Document in `README.md` that operators should not manually
  edit `ai:*` label colors.
- **P5a — false-positive drift on workflow header comment churn** —
  The canonical-header comment in each `ai-*.yml` differs between
  repos (timestamp, repo-specific text). Mitigation: the normalizer
  strips the leading comment block before diffing.
- **P5a — bulk GitHub API usage on each weekly run** — 11 repos × ~9
  templates = ~99 `GET` calls per week. Well under any rate limit;
  CLAUDE.md §15 satisfied via cycle-local caching and pre-listing the
  directory once per repo.
- **P5b — behavior regression from helper extraction** —
  Extracted helpers must be byte-for-byte equivalent. Mitigation:
  per-commit smoke-test contract inside the PR; CI grep checks block
  re-introduction.
- **N2 — extraction LLM cost / latency** — Adds one model call per
  nightly memory-maintenance run. Cost negligible at `gpt-5.4-mini`.
  Mitigation: gated by `MEMORY_LEARNINGS_EXTRACT_ENABLED`.
- **N2 — prompt-injection from learning content into clarify/plan** —
  Untrusted commit messages could theoretically poison learnings.
  Mitigation: extracted learnings are LLM-summaries of metadata
  (files touched, env vars), not raw issue/PR text. Confidence
  threshold ≥ 0.7 to promote to active.
- **N4 — false-positive blocks** — Misconfigured globs could block
  legitimate writes. Mitigation: initial allowlist is `**` (permissive);
  `WRITE_GUARDS_ENABLED=false` repo-var provides emergency disable;
  every block emits a `WRITE_GUARD_BLOCK` audit line.
- **N4 — backward compatibility with `ALLOW_WORKFLOW_EDITS`** — The
  existing flag is preserved verbatim; the new guard layers on top
  but does not replace it. Per §6, no rename or removal.
- **Order-of-merge risk** — If P5a merges before N1, the audit will
  find an `ai-sync-labels.yml` template missing from every consumer.
  Mitigation: the audit's allowlist of expected templates is read
  from `workflow-templates/` at runtime, so missing templates in any
  consumer correctly flag as drift (the desired behavior). The
  Telegram alert on the first run after N1 ships will list every
  consumer until `update_workflows.yml` propagates.
- **Doc retirement in plan PR before implementation lands** — Anyone
  reading the repo between this plan PR landing and Phase 1 PR landing
  will not see the source report. Mitigation: this plan file preserves
  every verbatim quote from the report (see Implementation Steps and
  Appendix).

## Rollout

- **Plan PR (this one)**: merges to `main`. No runtime impact; doc-only
  change (new plan file + deleted report).
- **Phase 1–5 PRs**: each follows the existing convention:
  1. PR merges to `main`.
  2. `promote-main-to-stable.yml` (manual `workflow_dispatch` after
     verification) promotes to `stable`.
  3. `update_workflows.yml` runs in each consumer on the next
     scheduled tick (or via `repository_dispatch`), propagating
     template changes.
- **Phase 1 (N1) propagation timing**: the new `ai-sync-labels.yml`
  template reaches all 11 consumers within ≤ 24 hours of `stable`
  promotion. Operators may run `workflow_dispatch` to sync labels
  on-demand.
- **Phase 5 (N4) propagation timing**: N4 touches workflow YAML in
  the source repo only; the consumer wrappers do not need to change.
- **Rollback**: any PR can be reverted in isolation because each is
  scoped to its own files. The doc-retirement is reversible by
  restoring `docs/squad-workflow-improvements.md` from `git revert`
  on this plan PR.
- **Feature-flag defaults**: N2 and N4 default `enabled=true` so
  operators can immediately benefit. To opt out: set the relevant env
  var to `false` (`MEMORY_LEARNINGS_EXTRACT_ENABLED`,
  `WRITE_GUARDS_ENABLED`).

## Open Questions

None — every blocking ambiguity was resolved in the clarification
round (Q1–Q6).

Non-blocking items that the implementation PRs may refine:

- Exact field set extracted into `repo_learnings` records (N2).
  Decision deferred to the prompt design step inside Phase 4.
- The set of files initially allowed/blocked by the N4 guard config.
  The initial proposal is `**` with `.git/**` blocked; tightening is
  a follow-up exercise tracked as a separate item if needed.
- Whether to split P5b extraction into N smaller PRs (one per
  helper). Q4: A specifies one PR per top-level item; this is left to
  the implementer's discretion if P5b grows too large to review.

## References

- Source report being retired: `docs/squad-workflow-improvements.md`
  (deleted in this plan PR per Q3: B). Verbatim content preserved in
  the per-phase sections above and the appendix below.
- Original 2026-03-22 research:
  `research/squad-workflow-improvements-20260322T042436Z.md`.
- External: `https://github.com/bradygaster/squad` (HEAD ref
  v0.9.1+).
- CLAUDE.md §§ 0, 2, 6, 10, 14, 15 (governing constraints).
- `agents.md` "Stable log prefixes (contractual)" — new prefixes
  introduced by this work (`LABEL_SYNC_*`, `DRIFT_SCAN_*`,
  `WRITE_GUARD_*`) become contractual on merge.

---

## Appendix: Patterns Intentionally Rejected (preserved verbatim)

| Pattern | Source | Reason |
| --- | --- | --- |
| Branch promotion pipeline (dev → preview → main) | `squad-promote.yml` | Our tag-based release model (`@stable` channel) serves the same purpose with less branch management overhead. |
| Triage routing with capability matching | `squad-triage.yml` | Not applicable — our pipeline uses a single-agent-per-issue model, not a multi-specialist team. |
| Agent identity / charter files | `.squad/agents/*/charter.md` | Our pipeline phases are defined by prompt files, not by agent identity documents. The prompt-per-phase model is more maintainable for our use case. |
| Dual-root mode (shared team across repos) | `squad init --mode remote` | Our consumer-repo model (reusable workflows + template sync) already provides centralized control without requiring a shared team root. |
| Scribe / session log archive | `.squad/scribe/`, `.squad/log/` | Our AI memory system with structured schemas, run events, and task lineage provides equivalent or better auditability. |
| Watch mode continuous polling | Ralph (`squad triage --interval`) | Our `orchestrate_poll.yml` with self-retrigger is functionally equivalent. |
