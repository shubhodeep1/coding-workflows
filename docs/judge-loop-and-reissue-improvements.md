# Plan: Judge-In-Loop, Sticky Findings, Typed Rejections, and Reissue Baseline

This plan describes a set of changes to the autofix / consolidator / judge /
reissue pipeline that close five gaps observed by a downstream consumer repo
during a multi-round autofix that ended in `close_and_reissue` over two
defects which earlier reviewer rounds had already raised.

The diagnosis is downstream-observed; the implementation is upstream. Every
phase below is flag-gated, fails open to today's behaviour on any error, and
adds new log prefixes rather than renaming any contractual ones (per
`agents.md` §Stable log prefixes).

This document is a **sibling** to `docs/review-pipeline-improvements.md`
(consolidator + ledger + floor rules). The two share file paths but never the
same line ranges; phases here can ship independently of that plan.

---

## Goals

1. Convert the per-PR judge from a one-shot end-of-loop gate into a corrective
   signal the autofix loop can act on, so reviewer findings dismissed in
   round N can be re-elevated in round N+1 without escalating to
   `close_and_reissue`.
2. Give the consolidator typed, machine-checkable rejection rationales so a
   "spec doesn't support" or "already fixed" dismissal cannot land without
   evidence of the right shape.
3. Track repeat findings across rounds so the same file:line:symptom flagged
   twice cannot silently fall off the must-fix list.
4. When the judge does close-and-reissue, preserve the prior diff as a
   baseline (when appropriate) so the next implement run does a surgical fix
   rather than re-deriving the entire change from zero.
5. Synthesise a tiny per-round behavioural smoke check from the judge's
   "remaining issues" list so behavioural defects (not just type / lint
   errors) get a per-round green/red signal.
6. Ship every change behind a feature flag with a fail-open path so a degraded
   verifier, judge-interim pass, or sticky-findings parser reduces the
   pipeline to today's behaviour, not worse.

---

## Non-Goals

- Webhook-driven autofix dedup on `pull_request_review_comment` events. The
  upstream autofix is `workflow_dispatch` / orchestrator-driven
  (`.github/workflows/review_autofix.yml`); cascading bot reviews on a
  consumer's PR are a consumer-side concern and out of scope here.
- Number of reviewers, reviewer model identities, or the two-pass reviewer
  architecture.
- `MAX_AUTOFIX_ITERATIONS` cap or hand-off conditions from autofix to the
  per-PR judge — the **end-of-loop** judge keeps its current role and budget;
  this plan adds a **mid-loop** judge alongside it.
- Orchestrator job flow (PR cadence, merge strategy, feature/main targeting).
- DB collections, indexes, or contracts (no `/db/contracts/*.yml` change).
- PR review mode (`@codex change`) semantics.
- Validation self-healing flow.

---

## Current-State Summary

Verified against the repo at HEAD of `claude/review-pr57-defects-Gx4k3`.

1. **Consolidator dismissal format is partially typed.** CLASSIFICATION is an
   enum (`must-fix | nice-to-have | unclassified | duplicate-of:<id> |
   non-actionable`) at `prompts/review-consolidator.txt:64-65`, parser-enforced
   at `scripts/review_parse_consolidator.sh:372-377`. The escape route is the
   **NOTES** field of `non-actionable`, which is free-form prose and not
   machine-checked.
2. **Judge runs once per PR, after autofix exhaustion only.** Confirmed at
   `.github/workflows/review_autofix.yml:2210-2230`. `MAX_AUTOFIX_ITERATIONS`
   defaults to 3. `skip_judge=false` is unconditional after
   exhaustion / `force_rb_judge`.
3. **No cross-round memory of reviewer findings.** `LAST_RUN_DIFF` (used by
   the editor to see what the prior round changed) and `OSCILLATION_GUARD`
   (intra-round diff stability) exist; no script compares round N's findings
   to round N-1's outcomes.
4. **Reissue creates a fresh issue from scratch.**
   `scripts/review_rb_judge.sh:636-682` closes the PR, calls `gh issue create`
   with `NEW_ISSUE_BODY` from the judge JSON. The closed PR's branch is not
   cherry-picked and the prior diff is not referenced as a baseline.
5. **Smoke / validation harness is a Docker Compose health probe plus a
   TAP-based shell-test runner; it does not itself run typecheck or lint.**
   `scripts/validate_driver.sh:75-114` is env / config defaults (compose
   file, app URL, health timeouts, `TEST_DIR`, `CANARY_PATTERN`,
   `HELPER_PATTERN`); `discover_tests()` at `:687` walks `${TEST_DIR}` and
   `run_tests()` at `:806` executes each script as a TAP test. Typecheck /
   lint, when run at all, run in the consumer repo's own CI, not in this
   driver. There is no synthesis of behavioural assertions from judge
   findings.
6. **Spec citations are required of the judge but never verified.**
   `prompts/mode-judge.txt:17-18` says "Cite specific files, functions, and
   line numbers inline next to each claim. Never fabricate" — instruction
   only, no verifier.

---

## Architecture Overview

```
Round N starts
  │
  ▼
Reviewers (existing two-pass) ── reviewer_bundle.txt
  │
  ▼
Sticky-Findings Annotator (Phase B)        ◄── reads round (N-1) consolidator output
  │                                            • marks file:line:rule_id as "sticky"
  │                                            • emits sticky_findings.json
  ▼
Consolidator (existing, prompt updated by Phase C)
  │   • emits CLASSIFICATION enum (existing)
  │   • emits REJECTION_KIND for non-actionable (new, typed)
  │   • emits typed evidence per REJECTION_KIND (new)
  ▼
Reject-Verifier (Phase C, gpt-5.4-mini)    ◄── only runs on non-actionable
  │   • re-fetches cited spec / diff hunk
  │   • grades whether evidence supports the rejection
  │   • on FAIL: reverses CLASSIFICATION to must-fix
  ▼
Editor (existing) → applies fixes → commits
  │
  ▼
Per-Round Smoke (Phase D)                  ◄── existing typecheck/lint
  │   • + behavioural assertions synthesised from judge-interim
  │     "remaining issues" once Phase A is on
  ▼
Judge-Interim (Phase A, gpt-5.4 low)       ◄── NEW per-round pass
  │   • cheap evidence-based pass over the latest commit
  │   • emits remaining_issues[] with file/line + spec/line citations
  │   • findings fed back into round (N+1) consolidator input
  │   • does NOT close the PR or escalate; advisory only
  ▼
Round N+1 starts ─────────────────────────► (loop)

After MAX_AUTOFIX_ITERATIONS or force_rb_judge:
  ▼
Per-PR Judge (existing) — unchanged role + budget
  │   • emits action ∈ {merge_ok, keep_iterating, close_and_reissue}
  │   • when close_and_reissue, NEW field: reissue_mode ∈ {spot-fix, redo}
  ▼
Reissue Path (Phase E)
  │   • spot-fix: cherry-pick prior PR head onto a new branch, file new issue
  │     with files_touched scoped to judge.remaining_issues[].file
  │   • redo (today's behaviour): create fresh issue, no baseline
```

---

## Phased Rollout

Phases A–E are independent enough to ship in separate PRs. Order matters only
where a phase depends on output of an earlier one (D depends on A; E benefits
from A's `remaining_issues` shape but works without it).

| Phase | Proposal # | Depends on | New LLM cost / PR | Risk |
|---|---|---|---|---|
| A. Judge-In-Loop | #2 | — | +2 cheap judge calls | Medium |
| B. Sticky Findings | #3 | — (script-only) | 0 | Low |
| C. Typed Rejections + Verifier | #1+#4 | — | +1 cheap verifier (only on rejects) | Low |
| D. Behavioural Smoke Synthesis | #5 | A | +1 synthesis call | Medium |
| E. Reissue Baseline | #6 | (benefits from A) | 0 (git ops) | Medium |

Recommended ship order: **A → C → B → E → D** (A first because it unlocks D
and gives E better signal; C next because it's a low-risk prompt+parser change
that pays off independently; B is script-only and can land in parallel with C;
E and D are higher-risk and benefit from production data from A/B/C).

---

## Phase A — Judge-In-Loop (per-round judge)

### Motivation

End-of-loop judge runs only when autofix has exhausted its iteration budget,
so any reviewer finding the consolidator dismissed in round 1 has no
corrective signal until round 3 finishes and the judge either closes the PR
or it merges. This converts a binary close/keep escalation into a per-round
advisory that the next-round consolidator must consider.

### Design

A new "judge-interim" mode runs after the editor commits in each round
(rounds 1 .. MAX_AUTOFIX_ITERATIONS - 1). It is a stripped-down, low-reasoning
version of the existing judge: same evidence-based output shape, same citation
requirement, smaller scope (only the latest commit's diff, not the whole PR),
no escalation authority.

The output JSON's `remaining_issues[]` array is persisted to
`reviewer_artifacts/judge_interim_round_<N>.json` and merged into round (N+1)'s
consolidator input as a new `<judge_interim_priors>` block.

### Interfaces

**New prompt:** `prompts/mode-judge-interim.txt`
- Inherits citation rules from `mode-judge.txt:17-23` verbatim.
- Hard constraints: must NOT emit `action`, must NOT recommend
  `close_and_reissue`, output is advisory only.
- Output JSON schema (subset of mode-judge.txt):

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
- Inputs: PR number, round number, head SHA.
- Calls the LLM at `medium` for the diff context but `low` reasoning for the
  judge body (parameterised by `JUDGE_INTERIM_REASONING`, default `low`).
- Writes `reviewer_artifacts/judge_interim_round_<N>.json` and emits
  `JUDGE_INTERIM_PASS_OK` / `JUDGE_INTERIM_PASS_FAIL` log lines.
- Fail-open: any non-zero exit, malformed JSON, or LLM failure logs
  `JUDGE_INTERIM_PASS_FAIL` and the loop continues without it.

**Workflow change:** `.github/workflows/review_autofix.yml`
- Insert a new step after the editor commit step in the per-round loop body,
  guarded by `if: env.JUDGE_INTERIM_ENABLED == 'true' && <not last round>`.
- Step calls `scripts/review_run_judge_interim.sh`, captures the artifact,
  and exposes `judge_interim_priors_path` as an output for the next round's
  consolidator step to consume.

**Consolidator input change:** `scripts/review_apply_fixes.sh`
- Detect `judge_interim_priors_path` env / file path.
- If present, prepend a `<judge_interim_priors>` block (formatted plain text,
  not JSON) to the consolidator prompt context. The consolidator prompt
  (`prompts/review-consolidator.txt`) gets a small additive block instructing
  it to treat priors as **advisory carry-over from the prior round's
  judge**, not as new reviewer findings.

### Files Changed / Created

| Path | Change |
|---|---|
| `prompts/mode-judge-interim.txt` | NEW — derived from `prompts/mode-judge.txt` |
| `scripts/review_run_judge_interim.sh` | NEW |
| `.github/workflows/review_autofix.yml` | INSERT 1 step around line 2210 (mid-loop, before the existing exhaustion check) |
| `scripts/review_apply_fixes.sh` | ADD prior-merge logic (script-level, no schema change) |
| `prompts/review-consolidator.txt` | APPEND ~10 lines explaining the new `<judge_interim_priors>` block |
| `agents.md` | ADD new log prefixes to §Stable log prefixes |

### Env Vars (with defaults)

| Var | Default | Meaning |
|---|---|---|
| `JUDGE_INTERIM_ENABLED` | `false` (Phase A merge); `true` (Phase A bake-out PR) | Master flag |
| `JUDGE_INTERIM_REASONING` | `low` | Reasoning effort for the per-round judge body |
| `JUDGE_INTERIM_TIMEOUT_S` | `120` | Hard timeout per round; over → fail-open |

### Log Prefixes (additive)

- `JUDGE_INTERIM_PASS_OK`
- `JUDGE_INTERIM_PASS_FAIL`
- `JUDGE_INTERIM_PRIORS_MERGED`

### Fail-Open Behaviour

| Failure | Effect |
|---|---|
| Script timeout / LLM error | Skip judge-interim for this round; loop continues |
| Malformed JSON | Same as above; logged; consolidator sees no priors |
| `JUDGE_INTERIM_ENABLED=false` | Phase entirely inert; pipeline behaves exactly like today |

### Acceptance Criteria

- With flag on, every non-final autofix round emits a `judge_interim_round_<N>.json` artifact OR a `JUDGE_INTERIM_PASS_FAIL` log line.
- Consolidator prompt context for round N+1 contains the `<judge_interim_priors>` block when round N produced one.
- With flag off, no new artifacts, no new log lines, no behavioural delta vs. today.
- End-of-loop per-PR judge invocation, budget, and prompt are unchanged (verified by diffing against `scripts/review_rb_judge.sh:636-690`).

### Cost

`MAX_AUTOFIX_ITERATIONS = 3` ⇒ at most 2 judge-interim calls per PR (rounds 1
and 2; round 3 is followed by the existing end-of-loop judge). At `low`
reasoning these are roughly 30–40% of the cost of an end-of-loop judge call,
so total LLM cost increase per PR is on the order of one judge call.

---

## Phase B — Sticky Findings (cross-round memory)

### Motivation

Reviewers in round N often re-flag issues that round (N-1)'s consolidator
dismissed as `non-actionable`. Today the consolidator sees no signal that
this is a repeat hit and is free to dismiss it again. Sticky annotation
forces the consolidator to either (a) classify as `must-fix` or (b) cite
why the prior dismissal is still correct, with the prior dismissal's NOTES
in scope.

### Design

A script-only post-processor (no LLM) runs **before** the consolidator in
each round (rounds ≥ 2):

1. Load the prior round's parsed consolidator output
   (`reviewer_artifacts/consolidator_parsed_round_<N-1>.json`).
2. Load the current round's reviewer bundle.
3. For each reviewer finding, compute a sticky **identity key** that excludes
   the line number, then match by line-range overlap separately:

	```
	identity_key = sha1(file + ":" + normalize(symptom))[:12]
	```

	`normalize(symptom)` lowercases and strips known boilerplate prefixes
	(e.g. "Issue: ", "Bug: "). The line number is **not** part of the hash.

4. A finding in round N matches a prior round's entry when both:
   - `identity_key` is equal, **and**
   - `|prior.line - current.line| <= STICKY_LINE_BUCKET` (default 5).

   Earlier drafts hashed `bucket(line, ±5)` (rounding to the nearest 5)
   into the key. That scheme was rejected because it is unstable at bucket
   boundaries — e.g. a 1-line drift from line 4 to line 5 hashes to
   different buckets (0 vs 5) and silently fails to match. The
   range-overlap match above is symmetric and absorbs uniform ±5 drift.

5. If a match is found in round (N-1) with CLASSIFICATION ∈
   {`non-actionable`, `nice-to-have`, `unclassified`}, mark the current
   finding as `sticky=true` and attach the prior NOTES.
6. Emit `reviewer_artifacts/sticky_findings_round_<N>.json` and inject a
   `<sticky_findings_priors>` block into the consolidator prompt.

The consolidator prompt is updated to require: when a finding is `sticky`
and the consolidator wishes to dismiss it again, the rejection must use the
`already-rejected-with-evidence` REJECTION_KIND (introduced in Phase C) and
include the prior round's evidence verbatim. Otherwise the finding must be
classified `must-fix`.

### Interfaces

**New script:** `scripts/review_annotate_sticky.sh`
- Inputs: prior consolidator JSON, current reviewer bundle.
- Output: `reviewer_artifacts/sticky_findings_round_<N>.json`.
- Pure shell + `jq` + a small Python helper for the sha1 identity key and
  the range-overlap match.

**Consolidator prompt change:** `prompts/review-consolidator.txt`
- Add a new section "Repeat findings (sticky)" that defines what `sticky=true`
  means and the constrained dismissal path.

### Files Changed / Created

| Path | Change |
|---|---|
| `scripts/review_annotate_sticky.sh` | NEW |
| `scripts/review_apply_fixes.sh` | INSERT call to sticky annotator before consolidator step |
| `prompts/review-consolidator.txt` | APPEND "Repeat findings (sticky)" section |
| `agents.md` | ADD `STICKY_FINDING_DETECTED`, `STICKY_FINDING_PROMOTED`, `STICKY_ANNOTATOR_NOOP`, `STICKY_FALSE_POS` |

### Env Vars

| Var | Default | Meaning |
|---|---|---|
| `STICKY_FINDINGS_ENABLED` | `false` initially; `true` after Phase B bake-out | Master flag |
| `STICKY_LINE_BUCKET` | `5` | ± line tolerance for sticky key matching |

### Log Prefixes (additive)

- `STICKY_FINDING_DETECTED` (per finding, on detection)
- `STICKY_FINDING_PROMOTED` (when consolidator's classification was
  upgraded `non-actionable → must-fix` because of sticky rules; emitted by
  the parser, not the consolidator)
- `STICKY_ANNOTATOR_NOOP` (annotator skipped due to missing or unreadable
  prior round artifact; fail-open path)
- `STICKY_FALSE_POS` (identity key and line range matched but the current
  finding's symptom text diverged significantly from the prior round's
  NOTES; logged for offline tuning of `STICKY_LINE_BUCKET`, no behavioural
  effect)

### Fail-Open Behaviour

- Missing prior JSON / unreadable bundle → no annotation, log
  `STICKY_ANNOTATOR_NOOP`, consolidator runs as today.
- Sticky annotator non-zero exit → same.

### Acceptance Criteria

- A reviewer finding flagged at round 1 (line ±5) and re-flagged at round 2
  produces a `STICKY_FINDING_DETECTED` log line in round 2.
- Consolidator prompt for round 2 contains the prior NOTES verbatim under the
  matching finding.
- Test: a fixture in `tests/` simulates two-round bundle inputs and asserts
  the round-2 consolidator input contains the sticky block.

### Risk

The range-overlap match (`|Δline| ≤ STICKY_LINE_BUCKET`) produces false
positives if the editor moved unrelated code around enough that an
unrelated finding now lands inside the tolerance window. Mitigation: false
positive rate is bounded by the consolidator's freedom to still dismiss
with the typed REJECTION_KIND from Phase C. We also log candidate matches
separately (`STICKY_FALSE_POS`) when the symptom text diverges
significantly from the prior round's NOTES despite the identity key + line
range overlap, for offline tuning of `STICKY_LINE_BUCKET`.

---

## Phase C — Typed Rejection Schema + Reject-Verifier

(Bundles proposals #1 and #4.)

### Motivation

The CLASSIFICATION enum is typed but the **NOTES** of `non-actionable` is
free-form prose. A consolidator can today dismiss a real defect by writing
"spec says X" without any machine-checkable evidence. Typing the rejection
schema and adding a cheap verifier closes that loophole.

### Design

#### C-1: Typed REJECTION_KIND

When CLASSIFICATION is `non-actionable`, the consolidator must additionally
emit `REJECTION_KIND` ∈ one of:

| KIND | Required typed evidence |
|---|---|
| `already-fixed` | `EVIDENCE_DIFF_HUNK`: file path + line range that fixed it, present in PR diff |
| `out-of-scope` | `EVIDENCE_FILES_TOUCHED`: cite the issue body's `files_touched` block; the cited path must NOT be in it |
| `reviewer-wrong` | `EVIDENCE_RUNTIME_PATH`: a function:line citing why the reviewer's claimed runtime path doesn't apply |
| `spec-doesnt-support` | `EVIDENCE_SPEC_QUOTE`: ≥1 verbatim quoted block (≤500 chars) from the cited spec section |
| `already-rejected-with-evidence` | `EVIDENCE_PRIOR_ROUND`: prior round's REJECTION_KIND + evidence; only valid when `sticky=true` |

`EVIDENCE_*` must be machine-extractable (delimited blocks the parser can
slice). The parser (`scripts/review_parse_consolidator.sh`) is updated to
require the matching evidence shape; missing or malformed evidence
demotes CLASSIFICATION to `unclassified` (existing failure mode preserves
backward compat — see line 372-377).

#### C-2: Reject-Verifier (LLM pass on non-actionable rejections)

After the consolidator emits and the parser accepts the typed rejection,
a small verifier runs **only on `non-actionable` items** to check:

- `already-fixed`: does the PR diff actually contain the cited hunk fixing
  the cited symptom? (Script-only — `git diff` + grep, no LLM needed.)
- `out-of-scope`: is the cited file actually absent from
  `files_touched`? (Script-only.)
- `reviewer-wrong`: does the cited function:line in the codebase exist and
  contradict the reviewer's claim? (LLM verifier — small.)
- `spec-doesnt-support`: does the quoted spec passage actually support the
  rejection? (LLM verifier — the higher-leverage case from the diagnosis.)
- `already-rejected-with-evidence`: does the prior round's evidence still
  apply (cited file/line still in the same shape)? (Script-only.)

Each verifier returns `support | does-not-support | inconclusive`. On
`does-not-support`, the parser **reverses CLASSIFICATION to `must-fix`** and
attaches the verifier's reasoning as `REVERSAL_REASON`. On `inconclusive`,
the rejection stands but logs `CONSOLIDATOR_REJECT_VERIFIER_INCONCLUSIVE`
for offline review.

The LLM verifier is `gpt-5.4-mini` at `low` reasoning, single-shot, with
explicit prompt limits (≤2k input tokens, ≤200 output tokens) so cost stays
roughly proportional to the number of `non-actionable` rejections (typically
0–3 per round).

### Interfaces

**Prompt update:** `prompts/review-consolidator.txt:64-65` and adjacent
- Replace single CLASSIFICATION line with the joint
  CLASSIFICATION + REJECTION_KIND + EVIDENCE_* schema.
- Add an examples block showing each REJECTION_KIND with its evidence shape.

**Parser update:** `scripts/review_parse_consolidator.sh:372-377`
- Extend the existing fail-open path: when `non-actionable` lacks
  REJECTION_KIND or its required EVIDENCE_*, demote to `unclassified` (today's
  fallback). When evidence shape is malformed but present, log
  `CONSOLIDATOR_REJECT_EVIDENCE_MALFORMED` and demote.

**New script:** `scripts/review_reject_verify.sh`
- Inputs: parsed consolidator JSON, PR diff, repo root.
- Routes by REJECTION_KIND; runs script-only verifiers inline; offloads
  `reviewer-wrong` and `spec-doesnt-support` to a single batched LLM call
  (one prompt, multiple items).
- Output: writes a `verified_rejections.json` artifact and, for any
  `does-not-support`, mutates the parsed consolidator JSON in place to
  re-classify and emit `CONSOLIDATOR_REJECT_REVERSED`.

**New prompt:** `prompts/consolidator-reject-verifier.txt`
- Single-purpose prompt for the LLM half of the verifier. Inputs: list of
  rejections with REJECTION_KIND + EVIDENCE_*. Output: per-item JSON with
  `verdict` and a one-sentence reason.

### Files Changed / Created

| Path | Change |
|---|---|
| `prompts/review-consolidator.txt` | UPDATE rejection schema, ADD examples block |
| `scripts/review_parse_consolidator.sh` | EXTEND fail-open at line 372-377, ADD evidence parser |
| `scripts/review_reject_verify.sh` | NEW |
| `prompts/consolidator-reject-verifier.txt` | NEW |
| `scripts/review_apply_fixes.sh` | INSERT verifier call between parser and editor steps |
| `agents.md` | ADD log prefixes |

### Env Vars

| Var | Default | Meaning |
|---|---|---|
| `CONSOLIDATOR_REJECT_VERIFIER_ENABLED` | `false` initially; `true` after bake-out | Master flag |
| `CONSOLIDATOR_REJECT_VERIFIER_REASONING` | `low` | LLM reasoning effort |
| `CONSOLIDATOR_REJECT_VERIFIER_BATCH_MAX` | `8` | Max rejections per LLM call |

### Log Prefixes (additive)

- `CONSOLIDATOR_REJECT_TYPED` — rejection passed evidence-shape check
- `CONSOLIDATOR_REJECT_EVIDENCE_MALFORMED` — demoted to `unclassified`
- `CONSOLIDATOR_REJECT_VERIFIED` — LLM/script verdict = support
- `CONSOLIDATOR_REJECT_REVERSED` — LLM/script verdict = does-not-support;
  classification reversed to `must-fix`
- `CONSOLIDATOR_REJECT_VERIFIER_INCONCLUSIVE`
- `CONSOLIDATOR_REJECT_VERIFIER_FAIL` — verifier script timeout / LLM error /
  malformed output; classifications left as-is (fail-open)

### Fail-Open Behaviour

- Verifier script timeout / LLM error / malformed verifier output → log
  `CONSOLIDATOR_REJECT_VERIFIER_FAIL`, leave classifications as-is, continue.
- `CONSOLIDATOR_REJECT_VERIFIER_ENABLED=false` → script-level evidence-shape
  check still runs (cheap, no LLM); LLM pass skipped. This degrades to a
  schema-only enforcement.

### Acceptance Criteria

- A consolidator output that rejects with `non-actionable` and no
  `REJECTION_KIND` is demoted to `unclassified` by the parser.
- A `spec-doesnt-support` rejection citing a passage that does not in fact
  support the rejection is reversed to `must-fix` with `REVERSAL_REASON`
  populated.
- An `already-fixed` rejection citing a diff hunk not present in the PR diff
  is reversed (script-only path).
- With flag off, today's `non-actionable` + free-form NOTES rejections still
  pass through (backward compat).

---

## Phase D — Behavioural Smoke Synthesis from Judge Findings

### Motivation

Per-round smoke today is the consumer's CI (typecheck / lint, when wired)
plus the upstream Docker Compose health probe and TAP shell tests run by
`scripts/validate_driver.sh`. Both downstream defects in the diagnosis were
behavioural (URL fallback shape, never-settling Promise) and would have been
green on every typecheck / lint pass and would not have been exercised by
the existing TAP harness. Synthesising a tiny behavioural assertion per
remaining issue gives the loop a per-round red signal for behavioural defects.

### Design

When Phase A is on, each round's `judge_interim_round_<N>.json` contains
`remaining_issues[]` with file/line/symptom/evidence_quote. Phase D adds a
synthesis step that turns each remaining issue into a small assertion
(target language: shell-runnable test, JS test, or Python test, depending on
the consumer repo's `validation/validate.env` setting).

The synthesised assertions are stored at the **top level** of
`${TEST_DIR}` (default `validation/tests/`) with a deterministic prefixed
filename (`synth_round_<N>_<issue_id>.sh`) and run alongside existing
smoke tests in the next round. The flat layout is required because
`discover_tests()` at `scripts/validate_driver.sh:698` uses
`find ... -maxdepth 1`; subdirectories (e.g. `synthesised/from_judge_round_<N>/`)
would not be discovered without a driver change. The `synth_round_` prefix
is chosen so it does NOT begin with `_` (which would be excluded by
`HELPER_PATTERN` at `:114`, default `_*.sh`).

Synthesis is one LLM call per round that emits all assertions in a batch
(prompt budget: ≤4k input, ≤2k output, gpt-5.4-mini at `low`). The LLM is
instructed to emit assertions that are **conservative**: pass when the issue
is fixed, fail when the issue is present, never block on infrastructure
flakiness (no network, no clock).

### Interfaces

**New prompt:** `prompts/behavioural-smoke-synthesise.txt`
- Inputs: `remaining_issues[]` from judge-interim, repo language hint.
- Outputs: array of `{path, content, expected_to_fail_until_fixed: bool}`.

**New script:** `scripts/review_synthesise_smoke.sh`
- Reads judge-interim artifact, calls LLM, writes test files directly into
  `${TEST_DIR}` (e.g. `validation/tests/synth_round_<N>_<issue_id>.sh`) —
  flat, top-level, prefix-discriminated.
- Also writes a manifest `validation/tests/synth_round_<N>_manifest.json`
  (`.json` is naturally ignored by `discover_tests()` since the discovery
  glob is `*.sh`).

**Workflow change:** `.github/workflows/review_autofix.yml`
- Insert a step **after** judge-interim and **before** the next round's
  reviewer pass.
- Existing `discover_tests()` at `scripts/validate_driver.sh:687` walks
  `${TEST_DIR}` (default `validation/tests/`, set at `:105`) with
  `find ... -maxdepth 1` at `:698` and `run_tests()` at `:806` executes
  each match. No driver change is needed **provided synthesised tests are
  placed flat** at the top level of `${TEST_DIR}`. Add a config knob
  (`VALIDATION_INCLUDE_SYNTHESISED`, default `true` when Phase D is on) to
  allow opt-out by skipping write of the synthesised files.
- **Marker-regex sync caveat.** The existing autofix loop's editor-fallback
  detection regex (`'editor failed before producing|unavailable \(editor
  fallback\)'`) is duplicated at `review_autofix.yml:2934` (in-step retry
  decision) and `:3344` (disposition step that sets
  `EDITOR_NOOP_SUSPICIOUS`). The new synthesise step must NOT write to
  or rotate `EDITOR_SUMMARY_FILE`, and any new failure marker the step
  introduces must use the same regex everywhere it is checked — drift
  between sites would let a fallback summary slip past the
  `EDITOR_NOOP_SUSPICIOUS` gate (`review_autofix.yml:3691`) and trigger
  unnecessary merge-conflict resolver runs.

### Files Changed / Created

| Path | Change |
|---|---|
| `prompts/behavioural-smoke-synthesise.txt` | NEW |
| `scripts/review_synthesise_smoke.sh` | NEW |
| `.github/workflows/review_autofix.yml` | INSERT 1 step after Phase A's judge-interim step |
| `scripts/validate_driver.sh` | OPTIONAL: respect `VALIDATION_INCLUDE_SYNTHESISED=false` to skip synthesised tests |
| `agents.md` | ADD log prefixes |

### Env Vars

| Var | Default | Meaning |
|---|---|---|
| `BEHAVIOURAL_SMOKE_FROM_JUDGE_ENABLED` | `false` initially | Master flag |
| `VALIDATION_INCLUDE_SYNTHESISED` | `true` | Whether the validator includes synthesised tests |
| `BEHAVIOURAL_SMOKE_LANG` | (auto-detect) | Override target language |

### Log Prefixes (additive)

- `BEHAVIOURAL_SMOKE_SYNTHESISED` (per round, with count)
- `BEHAVIOURAL_SMOKE_PRESENT_FAILED` (synthesised assertion failed → defect
  still present)
- `BEHAVIOURAL_SMOKE_PRESENT_PASSED` (synthesised assertion passed → defect
  cleared)
- `BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL`

### Fail-Open Behaviour

- LLM synthesis fails → no synthesised tests added, log
  `BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL`, validator runs as today.
- Synthesised test errors out (not a clean pass/fail) → treated as inconclusive
  with a log; never blocks the round.

### Acceptance Criteria

- With flag on, each round emits a `BEHAVIOURAL_SMOKE_SYNTHESISED count=<n>`
  log line where `n` matches `len(judge_interim_round_<N>.remaining_issues)`.
- A round whose synthesised assertion goes from FAIL → PASS between rounds N
  and N+1 emits `BEHAVIOURAL_SMOKE_PRESENT_PASSED` and is recorded as a
  resolved item in the next consolidator's input.

### Risk

LLM-synthesised assertions can be wrong in either direction (false-pass or
false-fail). Both modes are bounded:
- **False-pass**: assertion never fails even when defect is present. Effect
  is at-most-zero — we already had no behavioural smoke. We don't worsen.
- **False-fail**: assertion fails on correct code. Effect is a louder per-
  round signal — but the **editor still controls the diff**, and the
  consolidator's classification of the underlying issue is unchanged. The
  per-round red signal is advisory.

---

## Phase E — Reissue Baseline Preservation

### Motivation

Today `scripts/review_rb_judge.sh:636-682` closes the PR and creates a new
issue from `NEW_ISSUE_BODY` with no link to the prior PR's branch. The next
implement run re-derives the entire shell from zero, so the same defects can
recur in the same shape. Preserving the prior diff as a baseline turns
"redo from scratch" into "surgical fix on top of prior work" when the judge
believes the approach was right.

### Design

Add a new field to the judge JSON output: `reissue_mode` ∈ {`spot-fix`,
`redo`}.

- `spot-fix` (new): the implementation is mostly correct; reissue should
  cherry-pick the closed PR's HEAD onto a fresh branch and the new issue
  body must scope `files_touched` to only the files in
  `judge.remaining_issues[].file`.
- `redo` (today's behaviour): the implementation is wrong / scope was
  misunderstood; reissue creates a fresh issue with no baseline.

Default when the field is absent (e.g. older judge runs): `redo`. This
preserves current behaviour exactly when Phase E is off or the judge prompt
hasn't been updated.

### Interfaces

**Prompt update:** `prompts/mode-judge.txt`
- Add the `reissue_mode` field to the JSON output spec near the existing
  `action` definition.
- Add ~6 lines of guidance: choose `spot-fix` when remaining_issues count is
  small relative to PR diff size and the issues are localised; otherwise
  `redo`.

**Script update:** `scripts/review_rb_judge.sh:636-690`
- Branch on `reissue_mode`:
  - `redo` → existing path (`gh issue create` with `NEW_ISSUE_BODY`).
  - `spot-fix` → 
    - Read closed PR's HEAD SHA via `gh pr view --json headRefOid`.
    - In a fresh worktree (per `git worktree`, never destructive on the
      caller's checkout), create a new branch off the closed PR's HEAD.
    - Push that branch.
    - Create the new issue with a `prior_pr_baseline_branch:` field in
      the issue body and `files_touched:` scoped to the judge's remaining
      issues. The implement phase already respects `files_touched`.
- On any failure during the spot-fix path (worktree creation, push, branch
  ref missing), fall back to `redo` and log `REISSUE_BASELINE_DISCARDED`.

**Implement-phase respect:** the implement workflow already reads
`files_touched` from the issue body. The new `prior_pr_baseline_branch`
field needs a small addition in `.github/workflows/implement.yml` (or its
internal counterpart) to checkout that branch as the starting point when
present. When absent, behaviour is unchanged.

### Files Changed / Created

| Path | Change |
|---|---|
| `prompts/mode-judge.txt` | ADD `reissue_mode` field, guidance lines |
| `scripts/review_rb_judge.sh` | EXTEND `close_and_reissue` action with the spot-fix path (lines 636-690) |
| `.github/workflows/implement.yml` (and `internal-implement.yml`) | ADD optional `prior_pr_baseline_branch` checkout step |
| `agents.md` | ADD log prefixes |

### Env Vars

| Var | Default | Meaning |
|---|---|---|
| `REISSUE_PRESERVE_BASELINE_ENABLED` | `false` initially; `true` after bake-out | Master flag; when `false` the judge's `reissue_mode` is ignored and `redo` always wins |

### Log Prefixes (additive)

- `REISSUE_BASELINE_PRESERVED` (when spot-fix path completed and pushed
  baseline branch)
- `REISSUE_BASELINE_DISCARDED` (when spot-fix attempted but fell back to
  redo)
- `REISSUE_MODE` (`spot-fix` or `redo`, emitted whenever close_and_reissue
  runs)

### Fail-Open Behaviour

- Judge omits `reissue_mode` → treat as `redo` (today's behaviour).
- Spot-fix fails at any step → fall back to `redo`, log
  `REISSUE_BASELINE_DISCARDED`.
- `REISSUE_PRESERVE_BASELINE_ENABLED=false` → ignore `reissue_mode`, always
  `redo`.

### Acceptance Criteria

- With flag on and judge emits `reissue_mode: spot-fix`: a new branch is
  pushed off the closed PR's HEAD, and the new issue body contains
  `prior_pr_baseline_branch: <branch>` and `files_touched:` scoped to
  `remaining_issues[].file`.
- With flag on and judge emits `reissue_mode: redo`: behaviour identical to
  today.
- With flag off: behaviour identical to today.
- `git worktree` use is non-destructive on the caller's checkout (verified by
  a test that runs the spot-fix path and asserts no changes to the original
  working tree).

### Risk

Cherry-picking the prior diff is wrong when the judge mis-assesses the
approach. Mitigations:
- Default to `redo` when `reissue_mode` is absent or invalid.
- Judge prompt explicitly biases toward `redo` when in doubt.
- Small `files_touched` scope on the new issue prevents the next implement
  from drifting beyond the surgical fix.

---

## Cost Summary

Per-PR LLM cost delta when all phases are on, against today's baseline of
"reviewers × MAX_AUTOFIX_ITERATIONS + consolidator × N + end-of-loop judge":

| Phase | Added calls per PR | Cost weight (vs end-of-loop judge = 1.0) |
|---|---|---|
| A | up to 2 (rounds 1, 2) | ≈ 0.3 each → ≈ 0.6 |
| B | 0 | 0 |
| C | up to 1 per round, only if `non-actionable` rejections exist | ≈ 0.1 |
| D | up to 1 per round | ≈ 0.15 each → ≈ 0.45 |
| E | 0 | 0 |
| **Total** | | **≈ 1.15× one judge call per PR** |

In exchange: PRs that today escalate to `close_and_reissue` (full restart,
3 autofix rounds wasted) instead converge in fewer rounds or do a surgical
fix on baseline. Even one avoided full restart per ~10 PRs pays for the
added cost an order of magnitude over.

---

## Backward Compatibility

Per `CLAUDE.md` §6 (naming immutability) and `agents.md` §Stable log
prefixes:

- **No existing identifier is renamed.** Every change is additive.
- **Every contractual log prefix is preserved.** New prefixes are added to
  `agents.md` §Stable log prefixes when they cross the contract surface
  (workflow-log-analysis or API-hygiene reporting).
- **All defaults preserve today's behaviour.** Each phase defaults to
  flag-off at first ship; bake-out PRs flip individual flags to `true` after
  observing logs over a 1–2 week window.
- **Judge end-of-loop role unchanged.** `scripts/review_rb_judge.sh` keeps
  its budget, prompt, and authority. Phase E adds a new field but tolerates
  its absence.
- **Consolidator fail-open path preserved.** Phase C extends the existing
  `unclassified` demotion (line 372-377), it doesn't replace it.
- **No DB contract change.** No `/db/contracts/*.yml` is touched. No new
  collections, indexes, or unique constraints.

---

## Rollout & Rollback

### Phased ship plan (per phase)

1. **Land code with flag default `false`.** Phase merges to `main` inert.
2. **Bake-out PR flips the flag to `true`.** Observe one weekly orchestrator
   cycle (≈ 20–40 PRs) for new log prefixes, error rates, and the
   `*_FAIL` prefixes.
3. **Lock the flag.** Once stable, the env var stays as a kill-switch; the
   default is now `true`.

### Rollback

Each phase has an instant kill-switch via its `*_ENABLED` env var. Setting
the var to `false` returns the pipeline to today's behaviour with no code
revert needed. Code revert is a single PR per phase (no inter-phase
coupling beyond D depending on A's artifact format).

---

## Test Plan

### Unit / fixture tests

- **Phase A:** fixture with 2 rounds of editor commits; assert
  `judge_interim_round_<N>.json` is emitted and merged into round N+1's
  consolidator input. Assert end-of-loop judge invocation is unchanged.
- **Phase B:** fixture with 2 rounds of reviewer bundles where round 2 has a
  finding at the same file:line ±5 as a round-1 `non-actionable`. Assert
  `STICKY_FINDING_DETECTED` and prior NOTES inclusion in round 2's
  consolidator input.
- **Phase C:** fixture matrix — one consolidator output per REJECTION_KIND,
  half with valid evidence and half with malformed evidence. Assert parser
  reverses or demotes correctly. Add a fixture where the spec quote does
  not in fact support the claim; assert `CONSOLIDATOR_REJECT_REVERSED`.
- **Phase D:** fixture with a fixed `judge_interim_round_<N>.json`; assert
  the synthesis step produces N test files and the manifest is well-formed.
  Run synthesised tests against a deliberately broken sandbox and assert
  `BEHAVIOURAL_SMOKE_PRESENT_FAILED`; fix the sandbox and assert
  `BEHAVIOURAL_SMOKE_PRESENT_PASSED`.
- **Phase E:** fixture judge JSON with `reissue_mode: spot-fix` and
  `reissue_mode: redo`. Assert spot-fix creates the baseline branch via
  `git worktree` without mutating the caller checkout, and that absent /
  invalid `reissue_mode` falls back to `redo` cleanly.

### Integration tests

- A synthetic end-to-end run on a small fixture repo with all flags on:
  3 autofix rounds, judge-interim each round, sticky promotion in round 2,
  one consolidator rejection reversed by Phase C, behavioural smoke
  synthesised each round, and a final judge action of `merge_ok`.
- A synthetic end-to-end where the final action is `close_and_reissue`
  with `reissue_mode: spot-fix`; assert the new issue's body contains
  the baseline branch reference.

### Existing test impact

- `tests/test_review_autofix_last_run_diff_oscillation_guard.py` — verify
  the LAST_RUN_DIFF semantics are unchanged (Phase A adds adjacent logic,
  not in-flow).
- Existing consolidator parser tests — extend with the typed-rejection
  fixtures from Phase C; pre-existing tests must still pass.

---

## Documentation Updates

- `agents.md` §Stable log prefixes — add the new prefixes from each phase
  (additive only).
- `agents.md` §Workflow architecture — add a one-line mention of judge-
  interim alongside the existing judge entry.
- `unattended_system_instructions.md` — append a note on each new phase's
  contract (the unattended pipeline reads this file; per `CLAUDE.md` it does
  not see `CLAUDE.md`).
- `probably_unnecessary_but_read_if_stuck.md` (operator runbook) — add
  rollback / kill-switch section per phase.
- This document (`docs/judge-loop-and-reissue-improvements.md`) is the
  authoritative design reference; per-phase implementation PRs link to it
  in the PR description.

---

## Open Questions (for sign-off before implementation)

> **Q1: Default phase flag at first merge.**
>
> Choices:
> - **A** — Land all phases with their `*_ENABLED` flag default `false`,
>   then a separate small PR per phase flips the default to `true` after
>   bake-out (1–2 weeks of observation per phase). (RECOMMENDED — aligns
>   with `CLAUDE.md` §1 priority order: safety > speed.)
> - **B** — Land each phase with the flag default `true` so it takes effect
>   on merge, with the flag kept as a kill-switch.
> - **C** — Mixed: land phases B (script-only, no LLM cost) and C
>   (script-only path; LLM verifier behind its own flag) with default
>   `true`; keep A, D, E default `false` until bake-out.
>
> Reply: `Q1: A` (or B/C).

> **Q2: Phase ordering / parallelism.**
>
> Choices:
> - **A** — Ship serially in the order **A → C → B → E → D**, one PR per
>   phase, each merging before the next starts. Slowest, safest.
>   (RECOMMENDED.)
> - **B** — Ship A first, then B and C in parallel PRs (independent),
>   then E, then D.
> - **C** — Bundle A + C + B into one PR (all share consolidator-prompt
>   surface area), then E, then D.
>
> Reply: `Q2: A` (or B/C).

> **Q3: Phase D scope.**
>
> Choices:
> - **A** — Synthesise behavioural assertions every round (depends on
>   Phase A's per-round judge-interim). (RECOMMENDED if A is on.)
> - **B** — Synthesise only at reissue time, using the end-of-loop judge's
>   `remaining_issues`. Lower cost, less per-round signal. Works without
>   Phase A.
> - **C** — Skip Phase D for now; revisit after Phases A–C–B–E ship and
>   we have data on whether behavioural defects are still slipping
>   through.
>
> Reply: `Q3: A` (or B/C).

> **Q4: Reject-Verifier (Phase C) — script-only vs LLM-extended.**
>
> Choices:
> - **A** — Ship the script-only verifier first (handles `already-fixed`,
>   `out-of-scope`, `already-rejected-with-evidence`), defer the LLM
>   verifier (handles `reviewer-wrong`, `spec-doesnt-support`) to a
>   second PR. (RECOMMENDED — script-only path is risk-free and pays off
>   immediately.)
> - **B** — Ship script-only and LLM verifier together in one PR.
> - **C** — Skip the LLM verifier entirely; rely on schema enforcement
>   only. (Loses the highest-leverage check from the diagnosis —
>   `spec-doesnt-support` verification.)
>
> Reply: `Q4: A` (or B/C).

> **Q5: Phase E spot-fix `files_touched` scoping.**
>
> Choices:
> - **A** — Scope new issue's `files_touched` strictly to
>   `judge.remaining_issues[].file` (the smallest possible scope).
>   (RECOMMENDED — biases the next implement run toward a surgical fix.)
> - **B** — Scope to `remaining_issues[].file` ∪ `closed_pr.files_changed`
>   (gives the implement phase room to refactor adjacent code).
> - **C** — Don't scope; let the next implement run see the full repo.
>
> Reply: `Q5: A` (or B/C).
