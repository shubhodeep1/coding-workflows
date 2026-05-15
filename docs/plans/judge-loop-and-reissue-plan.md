# Judge-in-Loop, Sticky Findings, Typed Rejections, Behavioural Smoke, and Reissue Baseline

## Summary

Land five flag-gated, fail-open improvements (Phases A–E) to the
`review_autofix` pipeline so that PRs which today escalate to
`close_and_reissue` after dismissing reviewer findings instead converge in
fewer rounds — or, when reissue is unavoidable, retain the prior diff as a
baseline. Every phase ships behind a master env-var flag defaulting to
`false`; a follow-up bake-out PR per phase flips the default once production
logs are clean.

## Context

The design reference is `docs/judge-loop-and-reissue-improvements.md`,
authored after a downstream consumer-repo run hit `close_and_reissue` on
two defects that earlier reviewer rounds had already raised. The
diagnosis is downstream-observed, the fix is upstream. This plan
re-grounds that design against HEAD of branch
`claude/judge-loop-implementation-plan-9uxkH` (commit `cf1f992`), corrects
drifted file/line references with searchable anchors, and folds in the
architectural fact that the autofix "round" is a per-workflow-run notion,
not an in-workflow loop.

The plan is a sibling to `docs/review-pipeline-improvements.md`
(consolidator + ledger + floor rules). The two share file paths but
never the same line ranges; phases here can ship independently of that
plan.

**Source-doc retirement.** Per the user's election (Q3, this plan's
clarification batch), `docs/judge-loop-and-reissue-improvements.md` is
**deleted in this same plan PR** — the new plan supersedes it and
captures every constraint needed downstream.

### Iteration model (current state)

This plan calls out an architectural fact that the source design doc
left implicit:

- The `review_autofix.yml` workflow is **single-run per push**. Each
  invocation runs the reviewer pass → consolidator → editor → commit
  sequence at most once.
- "Round N" in the design doc maps to "workflow run N" — there is no
  in-workflow loop over rounds.
- The iteration count is derived inside the `Count autofix iterations`
  step (`steps.retrigger_guard` in `review_autofix.yml`) by walking
  HEAD backwards and counting consecutive `[ai-autofix]` commits. When
  that count reaches `MAX_AUTOFIX_ITERATIONS` (default `3`),
  `max_iterations_reached=true` is set and the per-PR review-blocked
  judge fires instead of the reviewer/editor/commit steps.
- Cross-run state today is persisted via the **review-issue ledger**:
  `.ai/review_issue_ledger/pr-<PR_NUMBER>.txt`, restored via
  `actions/cache/restore@v4` keyed on `PR_NUMBER` and tolerant of
  missing prior cache (fail-open). The path is gitignored.

Every "per-round artifact" in this plan (judge-interim output, sticky
findings, verifier verdicts, synthesised behavioural assertions) follows
the **same persistence pattern** as the existing ledger: written under
`.ai/review_runtime/pr-<PR>/round-<N>/`, saved/restored via the same
`actions/cache@v4` mechanism, gitignored.

## Goals

1. **Phase A (judge-in-loop).** Convert the per-PR judge from a one-shot
   end-of-loop gate into a per-run corrective signal the autofix loop
   can act on, so reviewer findings dismissed in round N can be
   re-elevated in round N+1 without escalating to `close_and_reissue`.
2. **Phase B (sticky findings).** Track repeat findings across rounds
   so the same `file:line:symptom` flagged twice cannot silently fall
   off the must-fix list.
3. **Phase C (typed rejections + reject-verifier).** Give the
   consolidator typed, machine-checkable rejection rationales so a
   "spec doesn't support" or "already fixed" dismissal cannot land
   without evidence of the right shape; verify the highest-leverage
   rejections with a cheap LLM pass.
4. **Phase D (behavioural smoke synthesis).** Synthesise a tiny
   per-round behavioural smoke check from the judge-interim
   `remaining_issues[]` so behavioural defects (not just type / lint
   errors) get a per-round green/red signal.
5. **Phase E (reissue baseline).** When the end-of-loop judge does
   `close_and_reissue`, preserve the prior diff as a baseline (when
   appropriate) so the next implement run does a surgical fix rather
   than re-deriving the entire change from zero.
6. **Cross-phase invariant.** Every phase ships behind a feature flag
   with a fail-open path so a degraded verifier, judge-interim pass,
   or sticky-findings parser reduces the pipeline to today's
   behaviour, not worse.

## Non-goals

- Webhook-driven autofix dedup on `pull_request_review_comment` events
  (upstream autofix is `workflow_dispatch` / orchestrator-driven; bot
  cascading on a consumer PR is a consumer-side concern).
- Number of reviewers, reviewer model identities, or the two-pass
  reviewer architecture.
- `MAX_AUTOFIX_ITERATIONS` cap or the hand-off conditions from autofix
  to the per-PR judge. The end-of-loop judge keeps its current role
  and budget; this plan adds a **mid-loop** judge alongside it.
- Orchestrator job flow (PR cadence, merge strategy, feature/main
  targeting).
- DB collections, indexes, or contracts (per CLAUDE.md §10 — no
  `/db/contracts/*.yml` change; the repo has no `db/contracts/`
  directory).
- PR review mode (`@codex change`) semantics.
- Validation self-healing flow.

## Constraints

- **CLAUDE.md §6 (naming immutability).** No existing identifier is
  renamed or removed. Every new field, env var, log prefix, and
  artifact filename in this plan is brand-new. The stable log
  prefixes listed in `agents.md` (`LABEL_REPAIR`, `LABEL_REPAIR_DIFF`,
  `AUTOFIX_PEER_CHECK`, `AUTOFIX_DISPATCH_SKIPPED`,
  `AUTOFIX_DISPATCH_ISSUED`, `AI_PHASE_FAILURE_V1`, `SEMBLE_QUERY`,
  `SEMBLE_FALLBACK`, `SERENA_QUERY`, `SERENA_FALLBACK`,
  `SERENA_PROBE`) are untouched. New log prefixes are added alongside
  per the additive contract — see each phase's "Log prefixes
  (additive)" subsection.
- **CLAUDE.md §10 (MongoDB).** No collection, index, or contract
  change. This plan does not touch the database.
- **CLAUDE.md §14 (consumer-repo registry).** The phases touch
  `.github/workflows/review_autofix.yml`, `scripts/review_*.sh`,
  `scripts/validate_driver.sh`, `prompts/*.txt`, and
  `.github/workflows/implement.yml` / `internal-implement.yml` — all
  reached by consumer repos via the
  `workflow-templates/ai-review.yml` and `workflow-templates/ai-*.yml`
  wrappers which pin `shubhodeep1/coding-workflows/.github/workflows/*@stable`.
  No consumer-repo registry change is required — the consumer set in
  `.github/ai/consumer_repos.json` already covers every downstream
  recipient, and the workflow surface only updates when a new
  `@stable` tag is cut. The plan documents the propagation surface so
  the bake-out PR per phase can call out which consumers must be
  observed in the next stable release.
- **CLAUDE.md §15 (GitHub API hygiene).** New `gh` / MCP calls are
  scoped: Phase E adds one `gh pr view --json headRefOid` and one
  `gh issue create`; both fit inside the existing
  `close_and_reissue` block in `scripts/review_rb_judge.sh` and do
  not run in a poll loop. No new `gh api` is introduced in Phases
  A–D. The plan reuses the existing `gh_retry`, `_safe_gh_jq`, and
  `ensure_label_exists` helpers; the `headRefOid` fetch is alongside
  the existing `gh pr view --json` calls in the close path. No
  per-iteration `gh api` calls are added.
- **CLAUDE.md §13 (repo hygiene).** New persisted artifacts live
  under `.ai/review_runtime/`; the path is added to the existing
  gitignore rule (`.ai/` is already partly ignored — extend with
  `.ai/review_runtime/`). Nothing under `.git/**` is written.
- **`agents.md` §Stable log prefixes.** All new prefixes documented
  per phase land in `agents.md` §Stable log prefixes in the same PR
  that ships the phase (additive only).

## Approach

Ship Phases A–E as five independent, flag-gated PRs sequenced as
**A → C → B → E → D** per the source-doc Q2 recommendation
(pre-answered — see "Pre-resolved questions" below). Each PR lands
with its master env-var flag default `false`. After each merge a
small bake-out PR flips the default to `true` once the first
production cycle (~20–40 PRs over 1–2 weeks) shows clean logs and no
`*_FAIL` spikes.

Within each phase, the design uses **searchable anchors** (function
names, step IDs, unique comment markers) instead of line numbers
because the source doc's line refs have already drifted by hundreds
of lines on `main` since the doc was written. The implementer
re-resolves the anchor against HEAD at implementation time. Every
anchor below has been spot-checked against
`claude/judge-loop-implementation-plan-9uxkH` at commit `cf1f992`.

### Pre-resolved questions

The source doc carries five open questions (Q1–Q5 in
`docs/judge-loop-and-reissue-improvements.md#Open Questions`). Per
the user's election (clarification batch on this plan, Q "Q1-Q5 in
source"), each is **pre-answered with the source doc's RECOMMENDED
choice** so implementation can start as soon as this plan PR merges.
Each pre-resolved decision is recorded below; reviewers must object
on this plan PR if any decision is wrong.

| Source-doc Q | Decision (RECOMMENDED) | Why |
|---|---|---|
| Q1 (default flag at first merge) | A — land each phase with `*_ENABLED` default `false`, follow-up bake-out PR flips it `true` | Aligns with CLAUDE.md §1 priority order: safety > speed |
| Q2 (phase ordering) | A — ship serially **A → C → B → E → D**, one PR per phase | Slowest, safest; A unlocks D, C is low-risk and pays off independently |
| Q3 (Phase D scope) | A — synthesise every round, depending on Phase A's `remaining_issues[]` | Best per-round signal once A is on |
| Q4 (Phase C verifier — script-only vs LLM) | A — script-only first PR; LLM verifier (handles `reviewer-wrong`, `spec-doesnt-support`) second PR | Script-only path is risk-free and pays off immediately |
| Q5 (Phase E `files_touched` scoping) | A — scope strictly to `judge.remaining_issues[].file` | Biases the next implement run toward a surgical fix |

Reviewers who want a different answer for any of Q1–Q5 must say so on
this plan PR; otherwise the implementer treats the RECOMMENDED choice
above as approved.

## Architecture Overview

```
Round N starts (= workflow run N on the PR head)
  │
  ▼
Reviewers (existing two-pass) ── reviewer_bundle.txt
  │
  ▼
Sticky-Findings Annotator (Phase B)        ◄── reads round (N-1) parsed
  │                                            consolidator output from the
  │                                            .ai/review_runtime cache
  │                                            • marks file:line:rule_id as "sticky"
  │                                            • emits sticky_findings.json
  ▼
Consolidator (existing, prompt updated by Phase C)
  │   • emits CLASSIFICATION enum (existing)
  │   • emits REJECTION_KIND for non-actionable (new, typed)
  │   • emits typed evidence per REJECTION_KIND (new)
  ▼
Reject-Verifier (Phase C)                  ◄── runs only on non-actionable
  │   • script-only verifiers for already-fixed / out-of-scope /
  │     already-rejected-with-evidence (Phase C PR-1)
  │   • LLM verifier for reviewer-wrong / spec-doesnt-support
  │     (Phase C PR-2 — gpt-5.4-mini)
  │   • on FAIL: reverses CLASSIFICATION to must-fix and writes
  │     REVERSAL_REASON
  ▼
Editor (existing) → applies fixes → commits
  │
  ▼
Per-Round Smoke (Phase D)                  ◄── existing typecheck/lint
  │   • + behavioural assertions synthesised from judge-interim
  │     remaining_issues[] (only when Phase A is on)
  ▼
Judge-Interim (Phase A, gpt-5.4 low)       ◄── NEW per-round pass
  │   • cheap evidence-based pass over the latest commit
  │   • emits remaining_issues[] with file/line + spec/line citations
  │   • findings fed back into round (N+1) consolidator input via cache
  │   • does NOT close the PR or escalate; advisory only
  ▼
Round N+1 starts (= workflow run N+1) ─────► (loop)

After MAX_AUTOFIX_ITERATIONS or force_rb_judge:
  ▼
Review-Blocked Judge (existing — rendered by review_rb_judge.sh from
  prompts/mode-judge-review-blocked.txt)
  │   • role + budget unchanged
  │   • today: action ∈ {merge, fix, merge_with_followup, close_and_reissue},
  │     remaining_issues_summary (string), new_issue (singular, nullable),
  │     followup_issue (singular, nullable)
  │   • Phase E adds: reissue_mode ∈ {spot-fix, redo}, AND a structured
  │     remaining_issues[] (file + line range + symptom) as a sibling to
  │     the existing remaining_issues_summary string — both kept for
  │     backward compat
  ▼
Reissue Path (Phase E)
  │   • spot-fix: cherry-pick prior PR head onto a new branch (via
  │     git worktree, non-destructive), file new issue with files_touched
  │     scoped to remaining_issues[].file
  │   • redo (today's behaviour): create fresh issue, no baseline
```

Note: the **orchestrator judge** — `prompts/mode-judge.txt`, invoked
by `orchestrate_poll_process.sh` — uses a different, status-based
schema with `new_issues[]` and is **not relevant to this plan**.
Phases A and E touch only the **review-autofix** loop's prompts and
scripts (`prompts/mode-judge-review-blocked.txt`,
`scripts/review_rb_judge.sh`,
`.github/workflows/review_autofix.yml`).

## Current-State Summary (verified against HEAD)

Re-verified against
`claude/judge-loop-implementation-plan-9uxkH @ cf1f992`. Anchors
below are grep-able strings, not line numbers.

1. **Consolidator dismissal format is partially typed.**
   `CLASSIFICATION` is an enum (`must-fix | nice-to-have |
   unclassified | duplicate-of:<id> | non-actionable`) declared in
   `prompts/review-consolidator.txt` (the line beginning
   `- CLASSIFICATION:`). Parser-enforced in
   `scripts/review_parse_consolidator.sh` at the `if [ -z
   "${classification}" ]` and `if [ "${classification}" =
   "non-actionable" ] && [ -z "${notes}" ]` fail-open blocks (both
   demote to `unclassified`). The escape route is the free-form
   **NOTES** field of `non-actionable` — not machine-checked.
2. **Judge runs once per PR, after autofix exhaustion only.**
   Confirmed in `.github/workflows/review_autofix.yml` — search for
   the `id: rb_judge` step and the `if: success() &&
   steps.retrigger_guard.outputs.max_iterations_reached == 'true'
   && steps.retrigger_guard.outputs.skip_judge != 'true' && ...`
   gate. `MAX_AUTOFIX_ITERATIONS` defaults to `3` (env at the
   `id: retrigger_guard` step). `skip_judge=false` is unconditional
   after exhaustion / `force_rb_judge`.
3. **No cross-round memory of reviewer findings.** `LAST_RUN_DIFF`
   (editor sees what the prior round changed) and `OSCILLATION_GUARD`
   (intra-round diff stability) exist. No script today compares round
   N's findings to round (N-1)'s outcomes. The review-issue ledger
   (`.ai/review_issue_ledger/pr-<PR>.txt`, restored via
   `actions/cache/restore@v4` with key
   `review-ledger-${{ github.repository }}-pr-${{ env.PR_NUMBER }}-...`)
   is the only existing cross-run persistence mechanism. New
   per-round artifacts in this plan reuse the same `actions/cache@v4`
   pattern, keyed on PR number and scoped under
   `.ai/review_runtime/pr-<PR>/round-<N>/`.
4. **Reissue creates a fresh issue from scratch.** Confirmed in
   `scripts/review_rb_judge.sh` inside the `close_and_reissue)` case
   of the action `case` block — search for `close_and_reissue)` and
   `gh_retry gh issue create`. The closed PR's branch is not
   cherry-picked and the prior diff is not referenced as a baseline.
   `NEW_ISSUE_BODY` is read from `.new_issue.body` of the judge
   JSON.
5. **Smoke / validation harness is a Docker Compose health probe
   plus a TAP-based shell-test runner; it does not itself run
   typecheck or lint.** `scripts/validate_driver.sh` env / config
   defaults are in the top section (`TEST_DIR`, `CANARY_PATTERN`,
   `HELPER_PATTERN` definitions). `discover_tests()` walks
   `${TEST_DIR}` (default `validation/tests`) with `find ...
   -maxdepth 1 -type f -name '*.sh'`; `run_tests()` executes each as
   a TAP test. Typecheck / lint runs in the consumer repo's own CI,
   not in this driver. There is no synthesis of behavioural
   assertions from judge findings.
6. **Spec citations are required of both judges but never
   verified.** The orchestrator judge
   (`prompts/mode-judge.txt`) says "Cite specific files, functions,
   and line numbers inline next to each claim. Never fabricate file
   paths, line numbers, or commit SHAs." The review-blocked judge
   (`prompts/mode-judge-review-blocked.txt`) likewise says "Be
   precise. Cite specific files and line numbers." Both are
   instructions only; no script verifies that the cited passages
   actually support the claim. (The two judges have distinct JSON
   schemas — see Architecture Overview — and Phases A and E touch
   different files.)

## Phased Rollout (table)

Phases A–E are independent enough to ship in separate PRs. Order
matters only where a phase depends on output of an earlier one (D
depends on A; E benefits from A's `remaining_issues` shape but works
without it).

| Phase | Proposal # | Depends on | New LLM cost / PR | Risk |
|---|---|---|---|---|
| A. Judge-In-Loop | #2 | — | +2 cheap judge calls | Medium |
| B. Sticky Findings | #3 | — (script-only) | 0 | Low |
| C. Typed Rejections + Verifier | #1+#4 | — | +1 cheap verifier (only on rejects) | Low |
| D. Behavioural Smoke Synthesis | #5 | A | +1 synthesis call | Medium |
| E. Reissue Baseline | #6 | (benefits from A) | 0 (git ops) | Medium |

Recommended ship order (decided per Q2 above): **A → C → B → E → D**.

## Phase A — Judge-In-Loop (per-round judge)

### Motivation

End-of-loop judge runs only when autofix has exhausted its iteration
budget, so any reviewer finding the consolidator dismissed in round 1
has no corrective signal until round 3 finishes and the judge either
closes the PR or it merges. This converts a binary close/keep
escalation into a per-round advisory that the next-round consolidator
must consider.

### Design

A new "judge-interim" mode runs at the **end of each workflow run**
(rounds `1..MAX_AUTOFIX_ITERATIONS - 1`, where round N = workflow
run N — see Iteration Model). It is a stripped-down,
low-reasoning version of the existing judge: same evidence-based
output shape, same citation requirement, smaller scope (only the
latest commit's diff, not the whole PR), no escalation authority.

The output JSON's `remaining_issues[]` array is persisted to
`.ai/review_runtime/pr-${PR}/round-${N}/judge_interim.json` and
saved via `actions/cache/save@v4` keyed identically to the existing
review-ledger cache. On round N+1, the workflow's
`actions/cache/restore@v4` step restores the prior round's runtime
dir; `scripts/review_apply_fixes.sh` reads
`.ai/review_runtime/pr-${PR}/round-$((N-1))/judge_interim.json` and
merges its `remaining_issues[]` into the consolidator input as a new
`<judge_interim_priors>` block.

### Interfaces

**New prompt:** `prompts/mode-judge-interim.txt`
- Inherits the citation rule and empty-lookup fallback rule from
  `prompts/mode-judge.txt` (search for "Cite specific files,
  functions, and line numbers" and "Fallback on empty lookups")
  verbatim. The orchestrator judge has the cleanest citation
  discipline; the review-blocked judge prompt has its own citation
  rule but is action-oriented, not findings-oriented.
- Hard constraints: must NOT emit `action` (an RB-judge field) or
  `status` (an orchestrator-judge field), must NOT recommend
  `close_and_reissue`, output is advisory only.
- Output JSON schema (NEW — standalone; not a subset of either
  existing judge prompt):

  ```
  {
    "round": <int>,
    "head_sha": "<sha>",
    "remaining_issues": [
      {
        "id": "<file>:<start_line>:<rule_or_symptom>",
        "file": "<repo-relative path>",
        "line_start": <int>, "line_end": <int>,
        "symptom": "<short string>",
        "evidence_quote": "<≤200 chars from the cited file>",
        "severity": "must-fix | nice-to-have"
      }
    ]
  }
  ```

**New script:** `scripts/review_run_judge_interim.sh`
- Inputs (env): `PR_NUMBER`, `ROUND_NUMBER` (derived from the
  existing `autofix_iteration` output of the
  `id: retrigger_guard` step), `HEAD_SHA`.
- Calls the LLM at `medium` for the diff context but `low`
  reasoning for the judge body (parameterised by
  `JUDGE_INTERIM_REASONING`, default `low`).
- Writes `.ai/review_runtime/pr-${PR}/round-${N}/judge_interim.json`
  and emits `JUDGE_INTERIM_PASS_OK` /
  `JUDGE_INTERIM_PASS_FAIL` log lines.
- Fail-open: any non-zero exit, malformed JSON, or LLM failure logs
  `JUDGE_INTERIM_PASS_FAIL` and the loop continues without it.

**Workflow change:** `.github/workflows/review_autofix.yml`
- Insert a new step after the editor's commit step (anchor: after
  the `id: commit_changes` step) in the per-run sequence,
  guarded by:

  ```yaml
  if: env.JUDGE_INTERIM_ENABLED == 'true' &&
      steps.retrigger_guard.outputs.max_iterations_reached != 'true' &&
      steps.commit_changes.outputs.did_commit == 'true' &&
      env.PR_CLOSED != 'true' &&
      env.AUTOFIX_STALE_BASE_SKIP != 'true' &&
      env.CLAUDE_BRANCH_REVIEW_MODE != 'true'
  ```

- Step calls `scripts/review_run_judge_interim.sh` and writes its
  artifact under the cached `.ai/review_runtime/pr-${PR}/round-${N}/`
  directory.
- Extend the **existing** `actions/cache/save@v4` step that already
  caches the review-issue ledger (search the workflow for
  `path: .ai/review_issue_ledger/`) to also cache
  `.ai/review_runtime/`. Same cache key (PR + run_id + run_attempt);
  same restore-keys fallback (PR only). One cache, two paths.

**Consolidator input change:** `scripts/review_apply_fixes.sh`
- Detect
  `.ai/review_runtime/pr-${PR}/round-$((N-1))/judge_interim.json`
  on disk (cache restore happens before this script runs — see the
  `Restore review-issue ledger` step in the workflow).
- If present, prepend a `<judge_interim_priors>` block (formatted
  plain text, not JSON) to the consolidator prompt context. Place
  it adjacent to the existing `=== BEGIN ${RUNTIME_DIR}/review_issues.txt`
  embedding block (search `_embed_input_file`).
- The consolidator prompt (`prompts/review-consolidator.txt`) gets a
  small additive block instructing it to treat priors as
  **advisory carry-over from the prior round's judge**, not as new
  reviewer findings.

### Files changed / created

| Path | Change |
|---|---|
| `prompts/mode-judge-interim.txt` | NEW — standalone schema; citation rules borrowed from `prompts/mode-judge.txt` |
| `scripts/review_run_judge_interim.sh` | NEW |
| `.github/workflows/review_autofix.yml` | INSERT 1 step after `id: commit_changes`; EXTEND existing `actions/cache/*` step path lists to include `.ai/review_runtime/` |
| `scripts/review_apply_fixes.sh` | ADD prior-merge logic at the consolidator-input-construction site (script-level, no schema change) |
| `prompts/review-consolidator.txt` | APPEND ~10 lines explaining the new `<judge_interim_priors>` block |
| `agents.md` | ADD new log prefixes to §Stable log prefixes |
| `.gitignore` | EXTEND `.ai/` rule to cover `.ai/review_runtime/` |

### Env vars (with defaults)

| Var | Default | Meaning |
|---|---|---|
| `JUDGE_INTERIM_ENABLED` | `false` (Phase A merge); `true` (Phase A bake-out PR) | Master flag |
| `JUDGE_INTERIM_REASONING` | `low` | Reasoning effort for the per-round judge body |
| `JUDGE_INTERIM_TIMEOUT_S` | `120` | Hard timeout per round; over → fail-open |

### Log prefixes (additive — must land in `agents.md`)

- `JUDGE_INTERIM_PASS_OK`
- `JUDGE_INTERIM_PASS_FAIL`
- `JUDGE_INTERIM_PRIORS_MERGED`

### Fail-open behaviour

| Failure | Effect |
|---|---|
| Script timeout / LLM error | Skip judge-interim for this round; loop continues |
| Malformed JSON | Same as above; logged; consolidator sees no priors |
| Cache restore miss (first run on PR, or eviction) | Annotator skipped, log `JUDGE_INTERIM_PRIORS_MERGED count=0` — consolidator runs as today |
| `JUDGE_INTERIM_ENABLED=false` | Phase entirely inert; pipeline behaves exactly like today |

### Acceptance criteria

- With flag on, every non-final autofix round emits a
  `judge_interim.json` artifact under
  `.ai/review_runtime/pr-${PR}/round-${N}/` OR a
  `JUDGE_INTERIM_PASS_FAIL` log line.
- Consolidator prompt context for round N+1 contains the
  `<judge_interim_priors>` block when round N produced one
  (verified by the unit fixture in Tests).
- With flag off, no new artifacts, no new log lines, no behavioural
  delta vs. today.
- End-of-loop per-PR judge invocation, budget, and prompt are
  unchanged (verified by diffing against the existing `id: rb_judge`
  step gate).

### Cost

`MAX_AUTOFIX_ITERATIONS = 3` ⇒ at most 2 judge-interim calls per PR
(rounds 1 and 2; round 3 is followed by the existing end-of-loop
judge). At `low` reasoning these are roughly 30–40% of the cost of
an end-of-loop judge call, so total LLM cost increase per PR is on
the order of one judge call.

## Phase B — Sticky Findings (cross-round memory)

### Motivation

Reviewers in round N often re-flag issues that round (N-1)'s
consolidator dismissed as `non-actionable`. Today the consolidator
sees no signal that this is a repeat hit and is free to dismiss it
again. Sticky annotation forces the consolidator to either (a)
classify as `must-fix` or (b) cite why the prior dismissal is still
correct, with the prior dismissal's NOTES in scope.

### Design

A script-only post-processor (no LLM) runs **before** the
consolidator in each round (rounds ≥ 2):

1. Load the prior round's parsed consolidator output (copied into
   the cache by Phase B's writer step in round N-1):
   `.ai/review_runtime/pr-${PR}/round-$((N-1))/consolidator_parsed.txt`
   (the parser already writes the equivalent file to `RUNTIME_DIR`
   as `review_issues.txt` — Phase B adds a single `cp` after the
   parser so the file is in the cached path).
2. Load the current round's reviewer bundle
   (`${RUNTIME_DIR}/reviewer_bundle.txt`).
3. For each reviewer finding, compute a sticky **identity key** that
   excludes the line number, then match by line-range overlap
   separately:

   ```
   identity_key = sha1(file + ":" + normalize(symptom))[:12]
   ```

   `normalize(symptom)` lowercases and strips known boilerplate
   prefixes (`Issue: `, `Bug: `). The line number is **not** part of
   the hash.

4. A finding in round N matches a prior round's entry when both:
   - `identity_key` is equal, **and**
   - `|prior.line - current.line| <= STICKY_LINE_BUCKET` (default 5).

   Earlier drafts hashed `bucket(line, ±5)` (rounding to the
   nearest 5) into the key. That scheme was rejected because it is
   unstable at bucket boundaries — e.g. a 1-line drift from line 4
   to line 5 hashes to different buckets (0 vs 5) and silently
   fails to match. The range-overlap match above is symmetric and
   absorbs uniform ±5 drift.

5. If a match is found in round (N-1) with `CLASSIFICATION` ∈
   {`non-actionable`, `nice-to-have`, `unclassified`}, mark the
   current finding as `sticky=true` and attach the prior NOTES.
6. Emit
   `.ai/review_runtime/pr-${PR}/round-${N}/sticky_findings.json` and
   inject a `<sticky_findings_priors>` block into the consolidator
   prompt (same insertion site as Phase A's
   `<judge_interim_priors>`).

The consolidator prompt is updated to require: when a finding is
`sticky` and the consolidator wishes to dismiss it again, the
rejection must use the `already-rejected-with-evidence` `REJECTION_KIND`
(introduced in Phase C) and include the prior round's evidence
verbatim. Otherwise the finding must be classified `must-fix`.

### Interfaces

**New script:** `scripts/review_annotate_sticky.sh`
- Inputs (env): prior consolidator-parsed file path (from cache),
  current reviewer bundle path, `STICKY_LINE_BUCKET` (default 5).
- Output:
  `.ai/review_runtime/pr-${PR}/round-${N}/sticky_findings.json`.
- Pure shell + `jq` + a small Python helper for the sha1 identity
  key and the range-overlap match. The Python helper exits 0 if
  Python is unavailable in the runtime (fail-open).

**Consolidator prompt change:** `prompts/review-consolidator.txt`
- Add a new section "Repeat findings (sticky)" that defines what
  `sticky=true` means and the constrained dismissal path.

**Workflow change:** `.github/workflows/review_autofix.yml`
- Insert a step calling `scripts/review_annotate_sticky.sh` **before**
  the consolidator step (anchor: before `review_consolidate.sh` is
  invoked inside `review_apply_fixes.sh`, OR as a separate
  workflow step inserted between the reviewer step and the
  apply_fixes step — implementer's call). Either path needs the
  cache restore step to have run first.
- The "save consolidator output to cache" step runs at the end of
  the run (anchor: after the existing parser step in
  `review_apply_fixes.sh`); add a `cp "${REVIEW_ISSUES_FILE}"
  .ai/review_runtime/pr-${PR}/round-${N}/consolidator_parsed.txt`
  before the cache-save step picks it up.

### Files changed / created

| Path | Change |
|---|---|
| `scripts/review_annotate_sticky.sh` | NEW |
| `scripts/review_apply_fixes.sh` | INSERT call to sticky annotator before consolidator step; INSERT `cp` of parser output to the cached round dir at end |
| `prompts/review-consolidator.txt` | APPEND "Repeat findings (sticky)" section |
| `agents.md` | ADD `STICKY_FINDING_DETECTED`, `STICKY_FINDING_PROMOTED`, `STICKY_ANNOTATOR_NOOP`, `STICKY_FALSE_POS` |

### Env vars

| Var | Default | Meaning |
|---|---|---|
| `STICKY_FINDINGS_ENABLED` | `false` initially; `true` after Phase B bake-out | Master flag |
| `STICKY_LINE_BUCKET` | `5` | ± line tolerance for sticky key matching |

### Log prefixes (additive)

- `STICKY_FINDING_DETECTED` (per finding, on detection)
- `STICKY_FINDING_PROMOTED` (when consolidator's classification was
  upgraded `non-actionable → must-fix` because of sticky rules;
  emitted by the parser, not the consolidator)
- `STICKY_ANNOTATOR_NOOP` (annotator skipped due to missing or
  unreadable prior round artifact; fail-open path)
- `STICKY_FALSE_POS` (identity key and line range matched but the
  current finding's symptom text diverged significantly from the
  prior round's NOTES; logged for offline tuning of
  `STICKY_LINE_BUCKET`, no behavioural effect)

### Fail-open behaviour

- Missing prior JSON / unreadable bundle → no annotation, log
  `STICKY_ANNOTATOR_NOOP`, consolidator runs as today.
- Sticky annotator non-zero exit → same.

### Acceptance criteria

- A reviewer finding flagged at round 1 (line ±5) and re-flagged at
  round 2 produces a `STICKY_FINDING_DETECTED` log line in round 2.
- Consolidator prompt for round 2 contains the prior NOTES
  verbatim under the matching finding.
- A fixture in `tests/` simulates two-round bundle inputs and
  asserts the round-2 consolidator input contains the sticky block.

### Risk

The range-overlap match (`|Δline| ≤ STICKY_LINE_BUCKET`) produces
false positives if the editor moved unrelated code around enough
that an unrelated finding now lands inside the tolerance window.
Mitigation: false-positive rate is bounded by the consolidator's
freedom to still dismiss with the typed `REJECTION_KIND` from Phase
C. We also log candidate matches separately (`STICKY_FALSE_POS`)
when the symptom text diverges significantly from the prior round's
NOTES despite the identity key + line range overlap, for offline
tuning of `STICKY_LINE_BUCKET`.

## Phase C — Typed Rejection Schema + Reject-Verifier

(Bundles source-doc proposals #1 and #4.)

Per Q4 above: ship as **two PRs** under one phase umbrella —
**Phase C PR-1** lands the typed schema + parser enforcement +
script-only verifiers; **Phase C PR-2** adds the LLM verifier for
`reviewer-wrong` and `spec-doesnt-support`.

### Motivation

The `CLASSIFICATION` enum is typed but the **NOTES** field of
`non-actionable` is free-form prose. A consolidator can today
dismiss a real defect by writing "spec says X" without any
machine-checkable evidence. Typing the rejection schema and adding a
cheap verifier closes that loophole.

### Design

#### C-1: Typed `REJECTION_KIND` (Phase C PR-1)

When `CLASSIFICATION` is `non-actionable`, the consolidator must
additionally emit `REJECTION_KIND` ∈ one of:

| KIND | Required typed evidence |
|---|---|
| `already-fixed` | `EVIDENCE_DIFF_HUNK`: file path + line range that fixed it, present in PR diff |
| `out-of-scope` | `EVIDENCE_FILES_TOUCHED`: cite the issue body's `files_touched` block; the cited path must NOT be in it |
| `reviewer-wrong` | `EVIDENCE_RUNTIME_PATH`: a function:line citing why the reviewer's claimed runtime path doesn't apply |
| `spec-doesnt-support` | `EVIDENCE_SPEC_QUOTE`: ≥1 verbatim quoted block (≤500 chars) from the cited spec section |
| `already-rejected-with-evidence` | `EVIDENCE_PRIOR_ROUND`: prior round's `REJECTION_KIND` + evidence; only valid when `sticky=true` |

`EVIDENCE_*` must be machine-extractable (delimited blocks the
parser can slice). The parser
(`scripts/review_parse_consolidator.sh`) is updated to require the
matching evidence shape; missing or malformed evidence demotes
`CLASSIFICATION` to `unclassified` (existing failure mode preserves
backward compat — see the `if [ "${classification}" =
"non-actionable" ] && [ -z "${notes}" ]` block).

#### C-2: Reject-Verifier (Phase C PR-1 for script-only KINDs; Phase C PR-2 for LLM KINDs)

After the consolidator emits and the parser accepts the typed
rejection, a small verifier runs **only on `non-actionable`
items** to check:

- `already-fixed`: does the PR diff actually contain the cited
  hunk fixing the cited symptom? (Script-only — `git diff` + grep,
  no LLM needed.) **Phase C PR-1.**
- `out-of-scope`: is the cited file actually absent from
  `files_touched`? (Script-only.) **Phase C PR-1.**
- `reviewer-wrong`: does the cited function:line in the codebase
  exist and contradict the reviewer's claim? (LLM verifier —
  small.) **Phase C PR-2.**
- `spec-doesnt-support`: does the quoted spec passage actually
  support the rejection? (LLM verifier — the higher-leverage case
  from the diagnosis.) **Phase C PR-2.**
- `already-rejected-with-evidence`: does the prior round's
  evidence still apply (cited file/line still in the same shape)?
  (Script-only.) **Phase C PR-1.**

Each verifier returns `support | does-not-support | inconclusive`.
On `does-not-support`, the parser **reverses `CLASSIFICATION` to
`must-fix`** and attaches the verifier's reasoning as
`REVERSAL_REASON`. On `inconclusive`, the rejection stands but
logs `CONSOLIDATOR_REJECT_VERIFIER_INCONCLUSIVE` for offline
review.

The LLM verifier is `gpt-5.4-mini` at `low` reasoning, single-shot,
with explicit prompt limits (≤2k input tokens, ≤200 output tokens)
so cost stays roughly proportional to the number of
`non-actionable` rejections (typically 0–3 per round).

### Interfaces

**Prompt update:** `prompts/review-consolidator.txt`
- Replace the single `CLASSIFICATION:` line with the joint
  `CLASSIFICATION + REJECTION_KIND + EVIDENCE_*` schema.
- Add an examples block showing each `REJECTION_KIND` with its
  evidence shape.

**Parser update:** `scripts/review_parse_consolidator.sh`
- Extend the existing fail-open path: when `non-actionable` lacks
  `REJECTION_KIND` or its required `EVIDENCE_*`, demote to
  `unclassified` (today's fallback). When evidence shape is
  malformed but present, log
  `CONSOLIDATOR_REJECT_EVIDENCE_MALFORMED` and demote.

**New script:** `scripts/review_reject_verify.sh`
- Inputs: parsed consolidator JSON, PR diff, repo root.
- Routes by `REJECTION_KIND`; runs script-only verifiers inline;
  offloads `reviewer-wrong` and `spec-doesnt-support` to a single
  batched LLM call (one prompt, multiple items).
- Output: writes a `verified_rejections.json` artifact under
  `.ai/review_runtime/pr-${PR}/round-${N}/` and, for any
  `does-not-support`, mutates the parsed consolidator output
  (`${RUNTIME_DIR}/review_issues.txt`) in place to re-classify and
  emit `CONSOLIDATOR_REJECT_REVERSED`.

**New prompt:** `prompts/consolidator-reject-verifier.txt`
- Single-purpose prompt for the LLM half of the verifier. Inputs:
  list of rejections with `REJECTION_KIND` + `EVIDENCE_*`. Output:
  per-item JSON with `verdict` and a one-sentence reason.

### Files changed / created

| Path | Change |
|---|---|
| `prompts/review-consolidator.txt` | UPDATE rejection schema, ADD examples block |
| `scripts/review_parse_consolidator.sh` | EXTEND fail-open path; ADD evidence parser at the existing block-finalisation site |
| `scripts/review_reject_verify.sh` | NEW |
| `prompts/consolidator-reject-verifier.txt` | NEW (Phase C PR-2 only) |
| `scripts/review_apply_fixes.sh` | INSERT verifier call between parser and editor steps |
| `agents.md` | ADD log prefixes |

### Env vars

| Var | Default | Meaning |
|---|---|---|
| `CONSOLIDATOR_REJECT_VERIFIER_ENABLED` | `false` initially; `true` after bake-out | Master flag for the LLM verifier |
| `CONSOLIDATOR_REJECT_VERIFIER_REASONING` | `low` | LLM reasoning effort |
| `CONSOLIDATOR_REJECT_VERIFIER_BATCH_MAX` | `8` | Max rejections per LLM call |
| `CONSOLIDATOR_REJECT_SCHEMA_ENABLED` | `false` (Phase C PR-1); `true` (Phase C PR-1 bake-out) | Schema-only enforcement (script-only path) |

Phase C PR-1 ships the schema flag and script-only verifiers. Phase
C PR-2 ships the LLM verifier flag.

### Log prefixes (additive)

- `CONSOLIDATOR_REJECT_TYPED` — rejection passed evidence-shape
  check
- `CONSOLIDATOR_REJECT_EVIDENCE_MALFORMED` — demoted to
  `unclassified`
- `CONSOLIDATOR_REJECT_VERIFIED` — LLM/script verdict = support
- `CONSOLIDATOR_REJECT_REVERSED` — LLM/script verdict =
  does-not-support; classification reversed to `must-fix`
- `CONSOLIDATOR_REJECT_VERIFIER_INCONCLUSIVE`
- `CONSOLIDATOR_REJECT_VERIFIER_FAIL` — verifier script timeout /
  LLM error / malformed output; classifications left as-is
  (fail-open)

### Fail-open behaviour

- Verifier script timeout / LLM error / malformed verifier output
  → log `CONSOLIDATOR_REJECT_VERIFIER_FAIL`, leave classifications
  as-is, continue.
- `CONSOLIDATOR_REJECT_VERIFIER_ENABLED=false` → script-level
  evidence-shape check still runs (cheap, no LLM); LLM pass
  skipped. This degrades to schema-only enforcement.
- `CONSOLIDATOR_REJECT_SCHEMA_ENABLED=false` → today's
  `non-actionable` + free-form NOTES rejections still pass through
  (backward compat).

### Acceptance criteria

- A consolidator output that rejects with `non-actionable` and no
  `REJECTION_KIND` is demoted to `unclassified` by the parser
  (Phase C PR-1).
- A `spec-doesnt-support` rejection citing a passage that does
  not in fact support the rejection is reversed to `must-fix` with
  `REVERSAL_REASON` populated (Phase C PR-2).
- An `already-fixed` rejection citing a diff hunk not present in
  the PR diff is reversed (script-only path, Phase C PR-1).
- With both flags off, today's `non-actionable` + free-form NOTES
  rejections still pass through (backward compat).

## Phase D — Behavioural Smoke Synthesis from Judge Findings

### Motivation

Per-round smoke today is the consumer's CI (typecheck / lint, when
wired) plus the upstream Docker Compose health probe and TAP shell
tests run by `scripts/validate_driver.sh`. Both downstream defects in
the source-doc diagnosis were behavioural (URL fallback shape,
never-settling Promise) and would have been green on every
typecheck / lint pass and would not have been exercised by the
existing TAP harness. Synthesising a tiny behavioural assertion per
remaining issue gives the loop a per-round red signal for
behavioural defects.

### Design

When Phase A is on, each round's
`.ai/review_runtime/pr-${PR}/round-${N}/judge_interim.json`
contains `remaining_issues[]` with file/line/symptom/evidence_quote.
Phase D adds a synthesis step that turns each remaining issue into a
small assertion (target language: shell-runnable test, JS test, or
Python test, depending on the consumer repo's `validation/validate.env`
setting).

The synthesised assertions are stored at the **top level** of
`${TEST_DIR}` (default `validation/tests/`) with a deterministic
prefixed filename (`synth_round_<N>_<issue_id>.sh`) and run alongside
existing smoke tests in the next round. The flat layout is required
because `discover_tests()` in `scripts/validate_driver.sh` (anchor:
`find "${TEST_DIR}" -maxdepth 1 -type f -name '*.sh'`) limits depth
to 1; subdirectories (e.g. `synthesised/from_judge_round_<N>/`)
would not be discovered without a driver change. The `synth_round_`
prefix is chosen so it does NOT begin with `_` (which would be
excluded by `HELPER_PATTERN` default `_*.sh`).

Synthesis is one LLM call per round that emits all assertions in a
batch (prompt budget: ≤4k input, ≤2k output, gpt-5.4-mini at
`low`). The LLM is instructed to emit assertions that are
**conservative**: pass when the issue is fixed, fail when the issue
is present, never block on infrastructure flakiness (no network, no
clock).

### Interfaces

**New prompt:** `prompts/behavioural-smoke-synthesise.txt`
- Inputs: `remaining_issues[]` from judge-interim, repo language hint.
- Outputs: array of `{path, content,
  expected_to_fail_until_fixed: bool}`.

**New script:** `scripts/review_synthesise_smoke.sh`
- Reads judge-interim artifact, calls LLM, writes test files
  directly into `${TEST_DIR}` (e.g.
  `validation/tests/synth_round_<N>_<issue_id>.sh`) — flat,
  top-level, prefix-discriminated.
- Also writes a manifest
  `validation/tests/synth_round_<N>_manifest.json` (`.json` is
  naturally ignored by `discover_tests()` since the discovery glob
  is `*.sh`).
- Mirrors the file into the cached round dir
  (`.ai/review_runtime/pr-${PR}/round-${N}/synth/`) for diagnostic
  trace.

**Workflow change:** `.github/workflows/review_autofix.yml`
- Insert a step **after** Phase A's judge-interim step and
  **before** the next round's reviewer pass. (Because the autofix
  workflow runs once per push, "before the next round's reviewer
  pass" means "before the run ends"; the next push picks up the
  synthesised tests via the consumer repo's checkout. The
  synthesised tests land under `validation/tests/` in the consumer
  repo's working copy — implementation must commit them under the
  existing `[ai-autofix]` commit OR write them to a path the
  consumer's CI re-discovers on the next push. See Risk below.)
- The existing `discover_tests()` walks `${TEST_DIR}` (default
  `validation/tests/`) with `find ... -maxdepth 1` and
  `run_tests()` executes each match. No driver change is needed
  **provided synthesised tests are placed flat** at the top level
  of `${TEST_DIR}`. Add a config knob
  (`VALIDATION_INCLUDE_SYNTHESISED`, default `true` when Phase D is
  on) to allow opt-out by skipping write of the synthesised files.
- **Marker-regex sync caveat.** The existing autofix loop's
  editor-fallback detection regex (`'editor failed before
  producing|unavailable \(editor fallback\)'`) is duplicated at
  multiple sites in `review_autofix.yml` (anchors: the
  `grep -qiE 'editor failed before producing|unavailable \(editor
  fallback\)'` block inside the disposition step that sets
  `EDITOR_NOOP_SUSPICIOUS`, and the matching block earlier in the
  in-step retry decision). The new synthesise step must NOT write
  to or rotate `EDITOR_SUMMARY_FILE`, and any new failure marker
  the step introduces must use the same regex everywhere it is
  checked — drift between sites would let a fallback summary slip
  past the `EDITOR_NOOP_SUSPICIOUS` gate and trigger unnecessary
  merge-conflict resolver runs.

### Files changed / created

| Path | Change |
|---|---|
| `prompts/behavioural-smoke-synthesise.txt` | NEW |
| `scripts/review_synthesise_smoke.sh` | NEW |
| `.github/workflows/review_autofix.yml` | INSERT 1 step after Phase A's judge-interim step |
| `scripts/validate_driver.sh` | OPTIONAL: respect `VALIDATION_INCLUDE_SYNTHESISED=false` to skip synthesised tests |
| `agents.md` | ADD log prefixes |

### Env vars

| Var | Default | Meaning |
|---|---|---|
| `BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED` | `false` initially | Master flag |
| `VALIDATION_INCLUDE_SYNTHESISED` | `true` | Whether the validator includes synthesised tests |
| `BEHAVIOURAL_SMOKE_LANG` | (auto-detect) | Override target language |

### Log prefixes (additive)

- `BEHAVIOURAL_SMOKE_SYNTHESISED` (per round, with count)
- `BEHAVIOURAL_SMOKE_PRESENT_FAILED` (synthesised assertion failed →
  defect still present)
- `BEHAVIOURAL_SMOKE_PRESENT_PASSED` (synthesised assertion passed →
  defect cleared)
- `BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL`

### Fail-open behaviour

- LLM synthesis fails → no synthesised tests added, log
  `BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL`, validator runs as today.
- Synthesised test errors out (not a clean pass/fail) → treated as
  inconclusive with a log; never blocks the round.

### Acceptance criteria

- With flag on, each round emits a `BEHAVIOURAL_SMOKE_SYNTHESISED
  count=<n>` log line where `n` matches
  `len(judge_interim_round_<N>.remaining_issues)`.
- A round whose synthesised assertion goes from FAIL → PASS between
  rounds N and N+1 emits `BEHAVIOURAL_SMOKE_PRESENT_PASSED` and is
  recorded as a resolved item in the next consolidator's input.

### Risk

LLM-synthesised assertions can be wrong in either direction
(false-pass or false-fail). Both modes are bounded:
- **False-pass**: assertion never fails even when defect is
  present. Effect is at-most-zero — we already had no behavioural
  smoke. We don't worsen.
- **False-fail**: assertion fails on correct code. Effect is a
  louder per-round signal — but the **editor still controls the
  diff**, and the consolidator's classification of the underlying
  issue is unchanged. The per-round red signal is advisory.

A **second risk** specific to this repo's per-workflow-run model:
synthesised tests written under `validation/tests/` need to
propagate to the next workflow run. Either (a) commit them under
the existing `[ai-autofix]` commit, or (b) write them only to the
cached round dir and have a small step on the next run copy them
into `${TEST_DIR}` before validation. The implementer must pick one
explicitly — committing under `[ai-autofix]` is simpler but
pollutes the PR diff; the cache-copy approach is cleaner but
brittle to cache eviction. **Plan recommendation:** start with the
cache-copy approach behind `BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED`,
and add a `--commit-synthesised` flag later if cache eviction
proves a problem in production.

## Phase E — Reissue Baseline Preservation

### Motivation

Today the `close_and_reissue` action in `scripts/review_rb_judge.sh`
(anchor: the `close_and_reissue)` case of the action `case` block,
which calls `gh_retry gh pr close` then `gh_retry gh issue create`)
closes the PR and creates a new issue from `NEW_ISSUE_BODY` with no
link to the prior PR's branch. The next implement run re-derives the
entire shell from zero, so the same defects can recur in the same
shape. Preserving the prior diff as a baseline turns "redo from
scratch" into "surgical fix on top of prior work" when the judge
believes the approach was right.

### Design

Add a new field to the judge JSON output: `reissue_mode` ∈
{`spot-fix`, `redo`}.

- `spot-fix` (new): the implementation is mostly correct; reissue
  should cherry-pick the closed PR's HEAD onto a fresh branch and
  the new issue body must scope `files_touched` to only the files in
  `judge.remaining_issues[].file`.
- `redo` (today's behaviour): the implementation is wrong / scope
  was misunderstood; reissue creates a fresh issue with no
  baseline.

Default when the field is absent (e.g. older judge runs): `redo`.
This preserves current behaviour exactly when Phase E is off or the
judge prompt hasn't been updated.

### Interfaces

**Prompt update:** `prompts/mode-judge-review-blocked.txt`
(this is the prompt rendered by `scripts/review_rb_judge.sh` whose
`action` field drives `close_and_reissue`; the orchestrator judge
prompt `mode-judge.txt` uses a different schema with `status` and
`new_issues[]` and is not relevant to this phase)
- Add the `reissue_mode` field to the JSON output schema (anchor:
  the `Schema:` block under `Output contract:`, near the line
  `"action": "merge" | "fix" | "merge_with_followup" |
  "close_and_reissue"`). Place `reissue_mode` adjacent to
  `action`; only meaningful when `action == "close_and_reissue"`.
- Add a structured `remaining_issues` array as a **sibling** to
  the existing `remaining_issues_summary` string (do NOT replace
  the string — keep both for backward compat). Shape:

  ```
  "remaining_issues": [
    {
      "file": "<repo-relative path>",
      "line_start": <int>, "line_end": <int>,
      "symptom": "<short string>"
    }
  ]
  ```

  This shape is deliberately a strict subset of the Phase A interim
  judge's `remaining_issues[]` so the reissue path can consume
  either source.
- Add ~6 lines of guidance: choose `spot-fix` when
  `remaining_issues` count is small relative to PR diff size and
  the issues are localised; otherwise `redo`. Default when omitted:
  `redo` (preserves current behaviour).

**Script update:** `scripts/review_rb_judge.sh`
(anchor: the `close_and_reissue)` case of the action `case` block)
- Branch on `reissue_mode`:
  - `redo` → existing path (`gh_retry gh issue create` with
    `NEW_ISSUE_BODY`).
  - `spot-fix` →
    - Read closed PR's HEAD SHA via
      `gh_retry gh pr view "${PR_NUMBER}" --json headRefOid --jq
      .headRefOid` (alongside the existing `gh pr view` calls in
      this script).
    - In a fresh worktree (per `git worktree`, **never destructive
      on the caller's checkout**), create a new branch off the
      closed PR's HEAD.
    - Push that branch with `git push -u origin <branch>` (retry
      up to 4 times with exponential backoff per the project's
      git policy).
    - Create the new issue with a `prior_pr_baseline_branch:`
      field in the issue body and `files_touched:` sourced from
      the new structured `remaining_issues[].file` field added to
      the RB judge schema by this same prompt update. The
      implement phase already respects `files_touched`. If
      `remaining_issues[]` is absent (older judge runs predating
      this schema change) or empty, fall back to `redo` rather
      than producing an unscoped or empty `files_touched`.
- On any failure during the spot-fix path (worktree creation, push,
  branch ref missing), fall back to `redo` and log
  `REISSUE_BASELINE_DISCARDED`.

**Implement-phase respect:** the implement workflow already reads
`files_touched` from the issue body. The new
`prior_pr_baseline_branch` field needs a small addition in
`.github/workflows/implement.yml` (and the internal counterpart
`internal-implement.yml`) to checkout that branch as the starting
point when present. When absent, behaviour is unchanged.

### Files changed / created

| Path | Change |
|---|---|
| `prompts/mode-judge-review-blocked.txt` | ADD `reissue_mode` field, structured `remaining_issues[]` array (sibling to existing `remaining_issues_summary` string), guidance lines |
| `scripts/review_rb_judge.sh` | EXTEND `close_and_reissue` action with the spot-fix path (anchor: `close_and_reissue)` case) |
| `.github/workflows/implement.yml`, `.github/workflows/internal-implement.yml` | ADD optional `prior_pr_baseline_branch` checkout step |
| `agents.md` | ADD log prefixes |

### Env vars

| Var | Default | Meaning |
|---|---|---|
| `REISSUE_PRESERVE_BASELINE_ENABLED` | `false` initially; `true` after bake-out | Master flag; when `false` the judge's `reissue_mode` is ignored and `redo` always wins |

### Log prefixes (additive)

- `REISSUE_BASELINE_PRESERVED` (when spot-fix path completed and
  pushed baseline branch)
- `REISSUE_BASELINE_DISCARDED` (when spot-fix attempted but fell
  back to redo)
- `REISSUE_MODE` (`spot-fix` or `redo`, emitted whenever
  close_and_reissue runs)

### Fail-open behaviour

- Judge omits `reissue_mode` → treat as `redo` (today's
  behaviour).
- Spot-fix fails at any step → fall back to `redo`, log
  `REISSUE_BASELINE_DISCARDED`.
- `REISSUE_PRESERVE_BASELINE_ENABLED=false` → ignore
  `reissue_mode`, always `redo`.

### Acceptance criteria

- With flag on and judge emits `reissue_mode: spot-fix`: a new
  branch is pushed off the closed PR's HEAD, and the new issue body
  contains `prior_pr_baseline_branch: <branch>` and
  `files_touched:` scoped to `remaining_issues[].file`.
- With flag on and judge emits `reissue_mode: redo`: behaviour
  identical to today.
- With flag off: behaviour identical to today.
- `git worktree` use is non-destructive on the caller's checkout
  (verified by a test that runs the spot-fix path and asserts no
  changes to the original working tree).

### Risk

Cherry-picking the prior diff is wrong when the judge mis-assesses
the approach. Mitigations:
- Default to `redo` when `reissue_mode` is absent or invalid.
- Judge prompt explicitly biases toward `redo` when in doubt.
- Small `files_touched` scope on the new issue prevents the next
  implement from drifting beyond the surgical fix.

## Implementation Steps

Sequenced per the **A → C → B → E → D** order decided in
Pre-resolved Questions (Q2). Each phase ships as 1–2 PRs landing
flag-default-`false`; a bake-out PR per phase flips the default to
`true` after 1–2 weeks of clean logs.

1. **Phase A PR-1.** Land `prompts/mode-judge-interim.txt`,
   `scripts/review_run_judge_interim.sh`, the workflow insertion in
   `review_autofix.yml` (one step after `commit_changes`),
   `scripts/review_apply_fixes.sh` consolidator-input merge,
   `prompts/review-consolidator.txt` priors-block append, `agents.md`
   log-prefix entries, and `.gitignore` extension. Flag
   `JUDGE_INTERIM_ENABLED=false` by default.
2. **Phase A PR-2 (bake-out).** Flip `JUDGE_INTERIM_ENABLED=true`
   after observing one orchestrator weekly cycle's logs.
3. **Phase C PR-1.** Land typed `REJECTION_KIND` schema in
   `prompts/review-consolidator.txt`, parser enforcement in
   `scripts/review_parse_consolidator.sh`, script-only verifiers in
   `scripts/review_reject_verify.sh`, apply-fixes insertion,
   log-prefix entries. Flag `CONSOLIDATOR_REJECT_SCHEMA_ENABLED=false`
   by default.
4. **Phase C PR-2.** Add `prompts/consolidator-reject-verifier.txt`,
   wire the LLM verifier (`reviewer-wrong`, `spec-doesnt-support`)
   into `scripts/review_reject_verify.sh`. Flag
   `CONSOLIDATOR_REJECT_VERIFIER_ENABLED=false` by default.
5. **Phase C bake-out.** Flip both Phase C flags `true` after 1–2
   weeks.
6. **Phase B PR-1.** Land `scripts/review_annotate_sticky.sh`,
   apply-fixes insertion, sticky section in
   `prompts/review-consolidator.txt`, log-prefix entries. Flag
   `STICKY_FINDINGS_ENABLED=false` by default.
7. **Phase B bake-out.** Flip `STICKY_FINDINGS_ENABLED=true`.
8. **Phase E PR-1.** Land `reissue_mode` + structured
   `remaining_issues[]` in `prompts/mode-judge-review-blocked.txt`,
   spot-fix branch in `scripts/review_rb_judge.sh`,
   `prior_pr_baseline_branch` checkout step in
   `.github/workflows/implement.yml` and `internal-implement.yml`,
   log-prefix entries. Flag
   `REISSUE_PRESERVE_BASELINE_ENABLED=false` by default.
9. **Phase E bake-out.** Flip
   `REISSUE_PRESERVE_BASELINE_ENABLED=true`.
10. **Phase D PR-1.** Land `prompts/behavioural-smoke-synthesise.txt`,
    `scripts/review_synthesise_smoke.sh`, workflow insertion after
    Phase A's judge-interim step, `VALIDATION_INCLUDE_SYNTHESISED`
    knob in `scripts/validate_driver.sh`, log-prefix entries. Flag
    `BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED=false` by default.
11. **Phase D bake-out.** Flip
    `BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED=true`.

Each PR enumerates which `agents.md` log prefixes are added and links
back to this plan in its PR description.

## Files & Modules

New files across all phases:
- `prompts/mode-judge-interim.txt` `[new]` (Phase A)
- `scripts/review_run_judge_interim.sh` `[new]` (Phase A)
- `scripts/review_annotate_sticky.sh` `[new]` (Phase B)
- `scripts/review_reject_verify.sh` `[new]` (Phase C PR-1)
- `prompts/consolidator-reject-verifier.txt` `[new]` (Phase C PR-2)
- `prompts/behavioural-smoke-synthesise.txt` `[new]` (Phase D)
- `scripts/review_synthesise_smoke.sh` `[new]` (Phase D)

Edited files:
- `.github/workflows/review_autofix.yml` (Phases A, B-step, D)
- `.github/workflows/implement.yml` (Phase E)
- `.github/workflows/internal-implement.yml` (Phase E)
- `scripts/review_apply_fixes.sh` (Phases A, B, C)
- `scripts/review_parse_consolidator.sh` (Phase C)
- `scripts/review_rb_judge.sh` (Phase E)
- `scripts/validate_driver.sh` (Phase D — optional knob)
- `prompts/review-consolidator.txt` (Phases A, B, C)
- `prompts/mode-judge-review-blocked.txt` (Phase E)
- `agents.md` (every phase — additive log-prefix entries)
- `.gitignore` (Phase A — extend `.ai/` ignore to cover
  `.ai/review_runtime/`)
- `docs/judge-loop-and-reissue-improvements.md` `[del]` (this plan
  PR — source doc retired per Q3)

## Data Model / Index Changes

None. Per CLAUDE.md §10 explicitly enumerated as non-goal. No
`/db/contracts/*.yml` exists in this repo and none is created.

## Tests

### Unit / fixture tests

- **Phase A:** fixture with 2 rounds of editor commits; assert
  `.ai/review_runtime/pr-${PR}/round-${N}/judge_interim.json` is
  emitted and merged into round N+1's consolidator input. Assert
  end-of-loop judge invocation is unchanged.
- **Phase B:** fixture with 2 rounds of reviewer bundles where
  round 2 has a finding at the same file:line ±5 as a round-1
  `non-actionable`. Assert `STICKY_FINDING_DETECTED` and prior NOTES
  inclusion in round 2's consolidator input.
- **Phase C:** fixture matrix — one consolidator output per
  `REJECTION_KIND`, half with valid evidence and half with malformed
  evidence. Assert parser reverses or demotes correctly. Add a
  fixture where the spec quote does not in fact support the claim;
  assert `CONSOLIDATOR_REJECT_REVERSED`.
- **Phase D:** fixture with a fixed
  `judge_interim_round_<N>.json`; assert the synthesis step
  produces N test files and the manifest is well-formed. Run
  synthesised tests against a deliberately broken sandbox and
  assert `BEHAVIOURAL_SMOKE_PRESENT_FAILED`; fix the sandbox and
  assert `BEHAVIOURAL_SMOKE_PRESENT_PASSED`.
- **Phase E:** fixture judge JSON with `reissue_mode: spot-fix` and
  `reissue_mode: redo`. Assert spot-fix creates the baseline branch
  via `git worktree` without mutating the caller checkout, and that
  absent / invalid `reissue_mode` falls back to `redo` cleanly.

### Integration tests

- A synthetic end-to-end run on a small fixture repo with all flags
  on: 3 autofix rounds, judge-interim each round, sticky promotion
  in round 2, one consolidator rejection reversed by Phase C,
  behavioural smoke synthesised each round, and a final RB-judge
  action of `merge`.
- A synthetic end-to-end where the final action is
  `close_and_reissue` with `reissue_mode: spot-fix`; assert the new
  issue's body contains the baseline branch reference.

### Existing test impact

- `tests/test_review_autofix_last_run_diff_oscillation_guard.py` —
  verify the `LAST_RUN_DIFF` / `OSCILLATION_GUARD` semantics are
  unchanged (Phase A adds adjacent logic, not in-flow).
- Existing consolidator parser tests — extend with the
  typed-rejection fixtures from Phase C; pre-existing tests must
  still pass.

## Risks & Mitigations

- **Cache eviction loses round (N-1) artifacts.** Mitigation: every
  Phase that depends on the cache (A, B, C, D) has a fail-open path
  that treats a missing prior round as "no priors", which collapses
  to today's behaviour.
- **Anchors drift after this plan lands.** Mitigation: every
  anchor in this plan is a grep-able unique string (function name,
  step id, comment marker), not a line number. Drift only causes
  the implementer to re-grep, not to misplace the change.
- **Phase C `REJECTION_KIND` schema doesn't compose with a
  multi-cause rejection** (one finding rejected for two reasons —
  e.g. `out-of-scope` AND `spec-doesnt-support`). Mitigation: keep
  `REJECTION_KIND` single-valued for v1; record secondary reasons
  in the existing free-form `NOTES` field. If multi-cause becomes
  common, add a `SECONDARY_REJECTION_KINDS[]` array in a v2 PR.
- **Phase D synthesised tests pollute consumer-repo `validation/tests/`
  diff.** Mitigation: cache-copy approach by default (see Phase D
  Risk section), with a future `--commit-synthesised` flag if
  cache eviction proves a problem.
- **Phase E `git worktree` leaves stray worktrees on disk.**
  Mitigation: use `git worktree remove --force` in a `trap EXIT`
  in `scripts/review_rb_judge.sh`'s spot-fix block. Document in
  the script header.
- **Judge prompt change (Phase E) drifts the JSON schema in a way
  that breaks existing JSON parsing in `scripts/review_rb_judge.sh`**.
  Mitigation: both new fields (`reissue_mode`, structured
  `remaining_issues[]`) are additive; the parser uses `jq -r
  '.<field> // empty'` so absent fields are handled. Test the
  no-field path explicitly.

## Rollout

### Per-phase ship plan

1. **Land code with flag default `false`.** Phase merges to `main`
   inert.
2. **Bake-out PR flips the flag to `true`.** Observe one weekly
   orchestrator cycle (~20–40 PRs) for new log prefixes, error
   rates, and the `*_FAIL` prefixes.
3. **Lock the flag.** Once stable, the env var stays as a
   kill-switch; the default is now `true`.

### Consumer-repo propagation (CLAUDE.md §14)

Every consumer in `.github/ai/consumer_repos.json` pulls workflow
templates via the `update_workflows.yml` dispatch on a new `@stable`
release tag. Each phase's PR landing on `stable` propagates the
workflow / script changes; the env-var defaults are inherited by
the consumer wrappers automatically (the wrappers do not override
flags). Recommend cutting a `@stable` tag only after each phase's
bake-out PR has flipped its flag.

### Rollback

Each phase has an instant kill-switch via its `*_ENABLED` env var.
Setting the var to `false` returns the pipeline to today's
behaviour with no code revert needed. Code revert is a single PR
per phase (no inter-phase coupling beyond D depending on A's
artifact format — reverting A while leaving D enabled would log
`BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL` per round but not break
anything).

## Open Questions

The five source-doc questions (Q1–Q5) are pre-resolved to the
RECOMMENDED answer in the "Pre-resolved questions" table above;
reviewers who want a different answer must say so on this plan PR.

Remaining open questions for the implementer:

- **OQ-1.** Phase D: cache-copy vs commit-under-`[ai-autofix]` for
  synthesised tests. Plan recommends cache-copy first;
  `--commit-synthesised` later if cache eviction proves a problem.
  **Decide at Phase D implementation time.**
- **OQ-2.** Phase E: should the spot-fix baseline branch be named
  `<closed_pr_branch>-spotfix-<short_sha>` or
  `claude/reissue-spotfix-<issue_number>`? Plan does not pin a
  scheme; pick at implementation time based on branch-naming
  conventions visible in the orchestrator stall poller.
- **OQ-3.** Phase A: should the cache write also persist the raw
  consolidator output (in addition to the parsed
  `review_issues.txt`) for Phase C's verifier to re-examine in the
  next round? Plan defaults to **no** — round N's verification
  runs in round N, not retroactively in round N+1 — but call it
  out at implementation time if a use case emerges.

## References

- Source design doc (retired with this plan PR):
  `docs/judge-loop-and-reissue-improvements.md`
- Sibling plan (consolidator + ledger + floor rules):
  `docs/review-pipeline-improvements.md`
- Existing related plan style:
  `docs/plans/complete-squad-improvements-plan.md`
- Project rules: `CLAUDE.md` (§1, §6, §10, §13, §14, §15)
- Repo architecture: `agents.md` (§Workflow architecture, §Stable
  log prefixes)
- Consumer-repo registry: `.github/ai/consumer_repos.json`
- Operator runbook (rollback / kill-switch details):
  `probably_unnecessary_but_read_if_stuck.md`
