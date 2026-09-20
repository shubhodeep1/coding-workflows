# Security pass convergence: project-line scope, advisory routing, and rebinding

> **Refreshed 2026-09-19.** The original plan (2026-09-06) had three phases.
> Its budget, delta re-audit, exhaustion table, waiver, and follow-up pieces
> have shipped, partly under other PRs, and two later changes (the exhaustion
> judge and the keep_fixing round cap) changed how convergence is achieved.
> This refresh records what is live, restates the remaining scope so
> `/implement-plan-ai` can hand it to the orchestrator as-is, and replaces the
> original D2 with a stricter rule the operator chose on 2026-09-19 (Q8: A).

## Summary

Make the mandatory project security pass block only on defects the project
itself introduced. Today a finding blocks completion when its cited *file* is
in the audited range; since every fix cycle commits into the integration
branch, each fix widens the set of files in scope and exposes pre-existing
defects in them as new "project" blockers. The remaining work classifies
each finding by *line* ownership with `git blame` (D1), routes findings on
pre-existing lines to non-blocking `ai:security` follow-up issues filed
through the existing advisory path (D3), and rebinds a clean pass across a
sync merge that adds no project lines (D4). Every finding on a
project-written line keeps blocking, whenever that line was written (D2′).
Convergence is guaranteed by the keep_fixing round cap that already ships in
`scripts/orchestrate_poll_process.sh`, not by suppressing project-owned
findings.

## Automation & Wiring (§18.E)

- **New script vs extension:** no new standalone script. The change extends
  `scripts/security_audit.sh` (findings-json mode only), the poller
  `scripts/orchestrate_poll_process.sh`, and the audit prompt. Nothing
  requires manual invocation.
- **Scheduler entry point:** the existing orchestrator poller,
  `.github/workflows/orchestrate_poll.yml` (cron `*/5 * * * *` via
  `internal-orchestrate-poll.yml` here and the synced
  `workflow-templates/ai-orchestrate-poll.yml` wrapper in consumers). The
  security pass already runs inline in that tick (`run_security_pass_inline`,
  `scripts/orchestrate_poll_process.sh:6227`); every change below runs inside
  it on the same tick.
- **Long-running supervisor:** none. The poller is the supervisor.
- **DB operations:** none. No MongoDB collections, indexes, or contracts (§10
  not applicable).
- **Future-removal registry (§18.F):** no single-use or long-running scripts
  are introduced; no `docs/scripts-pending-removal.md` entry.

## Status against the code (2026-09-19)

| Original item | Status | Evidence |
| --- | --- | --- |
| `MAX_SECURITY_PASS_CYCLES` default 5 | shipped | `scripts/orchestrate_poll_process.sh:1287`; README variable tables |
| Exhaustion comment carries the findings table (`render_security_pass_findings_table`) | shipped | PR #4018; `scripts/orchestrate_poll_process.sh` `security_pass_terminal_failure` (≈4721) |
| Delta re-audit: files changed since the last audited commit plus files of prior findings; prior findings re-emitted by id | shipped | `SECURITY_AUDIT_DIFF_SINCE`, `SECURITY_AUDIT_PRIOR_FINDINGS`, state `security_pass_last_audited_sha` and `security_pass_reported_findings` (the shipped name for the plan's `security_pass_prior_findings`); log `SECURITY_PASS_SCOPE ... mode=full\|delta`; fragment `changelog.d/3928-security-pass-delta-reaudit.md` |
| Previous fix cycle's own hunks audited as fresh attack surface | shipped (not in the original plan) | PR #4089; `SECURITY_AUDIT_FIX_CYCLE_DIFFS`, state `security_pass_fix_touched_files` |
| Waivers travel to the engine and match by id or by file+category within a window | shipped | `SECURITY_AUDIT_WAIVED_FINDINGS`, `SECURITY_AUDIT_WAIVER_LINE_WINDOW` (default 40), `security_pass_apply_waivers_to_findings` (≈5403) |
| `/security-pass-waive` command | shipped | `docs/how-it-works.md` command table; `security_pass_waived_findings`, `<!-- security-pass-waive-dedup:<id> -->` |
| Advisory `ai:security` follow-up creation with weekly-audit marker and state dedupe | shipped, but only for findings the exhaustion judge or a waive accepted | `create_security_pass_advisory_followup` (singular), `security_pass_followup_issues` |
| Exhaustion judge instead of terminal failure | shipped (not in the original plan) | PR #4074; `prompts/mode-judge-security-pass-exhaustion.txt`, `MAX_SECURITY_PASS_JUDGE_ROUNDS` |
| Advisory follow-ups deferred until the integration branch merges | shipped (not in the original plan) | PR #4119; `SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED`, `security_pass_file_deferred_advisory_followups` |
| keep_fixing round cap; merge-time `/answer` for follow-ups parked in `ai:blocked` | in PR #4135 (pending merge) | `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` (default 2), `security_pass_unblock_filed_advisory_followups`, state `security_pass_followups_merge_checked` |
| **D1** line ownership via `git blame` (`SECURITY_AUDIT_LINE_OWNERSHIP`, `advisory_findings`) | **not implemented** | no identifier exists in `scripts/security_audit.sh` or the poller |
| **D2** per-line "older than last audit" suppression, `verified_fixed_finding_ids`, `SECURITY_PASS_VERIFIED_FIXED` | **not implemented; suppression dropped by this refresh (D2′)** | prior-id re-emission is live through `SECURITY_AUDIT_PRIOR_FINDINGS`; the verified-fixed log is kept as remaining work |
| **D3** pre-existing findings routed to advisories at audit time (`SECURITY_PASS_ADVISORY_FOLLOWUP_CAP`) | **not implemented** | advisories today come only from the judge or a waive, after the budget is spent |
| **D4** rebind a clean pass across a clean sync merge (`SECURITY_PASS_REBOUND`) | **not implemented** | the budget-reset path (PR #4013) re-audits instead |

## Context

The security-pass gate shipped via project #3933 (see
`docs/completed/orchestrator-security-pass-gate-plan.md`) and is default-on.
The gate works as designed but charged pre-existing defects to the first
project that touched their files, and each fix widened the audited surface:

- **coding-workflows #3965** (a 913-line, one-issue project, merged into its
  integration branch on 2026-09-03): thirteen consolidated fix issues over
  sixteen days across three `/re-security-pass` resets (#3976, #3987, #3990,
  #3998, #4000, #4005, #4017, #4049, #4056, #4070, #4076, #4092, #4113). The
  exhaustion judge granted "one more cycle" in both of its rounds (cycles 6
  and 7 on a 5-cycle budget); nothing bounded the sequence until PR #4135.
  The integration PR #3968 grew to +16,359/−2,956 across 126 files.
- **The cycle-7 mechanism, in one example.** Cycle 6 changed one line of
  `.github/workflows/clarify.yml` (an action pin:
  `git diff f9a615c..ebf29af -- .github/workflows/clarify.yml` is 1 insertion,
  1 deletion). That put the whole file into the delta scope, and cycle 7
  reported `clarify.yml:215` — the support-staging list that omits
  `emit_event.sh` — as a critical project finding. That list is unchanged
  since the merge-base and identical on `main`
  (`scripts/gh_helpers.sh:28-35` on `main` sources `scripts/emit_event.sh`
  from the checkout the same way). The other nine findings cite the same two
  patterns (support-staging lists that omit a transitive helper, and agents
  launched under the runner UID), both present on `main` before the project:
  real hardening gaps in this repository's own workflows, none introduced by
  the project.
- **tele-funtoken-msg-scoring #3928** and **#3955** (from the original plan):
  findings in shared bet-execution code dated months before the project,
  charged to the first project that touched those files; four audits with no
  repeated finding exhausted a 3-cycle budget.

The mechanism is unchanged since the original plan: `scripts/security_audit.sh`
enforces incremental scope **per file** (`if audit_scope_mode == "incremental"
and str(normalized_finding["file"]) not in changed_files`, line 1423) and
`security_audit_append_prompt_context` (lines 118–137) tells the model
"every finding MUST cite one of these files", so any line of a changed file
is a valid blocking citation. The delta re-audit and the fix-cycle
attack-surface rule narrowed *which files* are re-read; neither distinguishes
project-written lines from pre-existing ones.

Binding rules: §5 (extend existing mechanisms), §6 (no renames; every new
identifier below was grepped and is unique on 2026-09-19), §14 (consumers
receive `scripts/`, `prompts/`, and the reusable `orchestrate_poll.yml`
through the `@stable` sync; no wrapper change), §15 (API hygiene), §18 (no
manual scripts), §19 (`Refs #N` only), §20 (changelog fragments).

Clarification answers that fixed the design: original round (2026-09-06, all
`A`): blame-based line ownership; 3-line context window; one capped
`ai:security` follow-up per pre-existing finding; head advance with zero new
project lines rebinds without a model run; three phases. Refresh round
(2026-09-19): Q5: A — route pre-existing findings to advisories rather than
narrowing the audit to hunks, because that keeps coverage identical while
moving the fix budget to real regressions; Q6 — `ai:security` issues stay
the pipeline entry point, no docs mirror; Q7: A — refresh this plan rather
than write a new one; Q8: A — a first-time finding on an older
project-written line blocks (replaces the original D2).

## Decisions

### D1 — Line ownership via `git blame`, not diff-hunk parsing

- **Chosen:** for each surviving finding, `git blame --porcelain -L <lo>,<hi>
  <head> -- <file>` over the cited line ±`SECURITY_AUDIT_OWNERSHIP_CONTEXT_LINES`
  (default 3). A line is project-owned when its blamed commit is **not** an
  ancestor of `SECURITY_AUDIT_DIFF_BASE` (the merge-base). The finding is
  project-owned when any line in the window is. A blame failure classifies
  the finding as project-owned (fail closed).
- **Alternatives considered:** parsing `git diff merge-base..head` into
  per-file line sets; asking the model to self-classify.
- **Why:** blame is exact per line, costs one local git call per finding
  (findings are a handful per run), and needs no hunk-offset bookkeeping.
  Sync merges from the default branch resolve to commits reachable from the
  merge-base, so their lines are never project-owned. Model
  self-classification is not trustworthy for a gate. The poller checkout has
  full history (`fetch-depth: 0`, `.github/workflows/orchestrate_poll.yml:238`),
  so ancestry checks are local.

### D2′ — Every project-owned finding blocks; convergence comes from the cap

- **Chosen (replaces the original D2):** the engine does not suppress a
  project-owned finding because its line is older than the last audited
  commit. Prior findings are still re-emitted by exact id (already live
  through `SECURITY_AUDIT_PRIOR_FINDINGS`), and prior ids absent from the
  output are logged as `verified_fixed_finding_ids` /
  `SECURITY_PASS_VERIFIED_FIXED`. No `SECURITY_AUDIT_NEW_SINCE_SHA`, no
  `suppressed_older_than_base`.
- **Alternatives considered:** the original D2 (suppress first-time findings
  on project lines older than the last audit unless their id is a prior id).
- **Why:** the original D2 traded a possible miss on the project's own code
  for convergence, at a time when nothing else bounded the loop. Since then
  the exhaustion judge and `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` (PR #4135)
  bound the number of fix cycles deterministically, so the coverage loss buys
  nothing. Security ranks first (§1). Operator decision Q8: A.
- **Interaction with the fix-cycle attack-surface rule (PR #4089):** the
  hunks of the previous fix cycle are project-owned by construction (their
  commits are not ancestors of the merge-base), so a finding inside them
  blocks under D1 with no special case. The concern raised in the original
  D2 note is moot under D2′.

### D3 — Pre-existing findings become capped `ai:security` follow-ups at audit time

- **Chosen:** findings that pass validity, file scope, confidence, exclusions,
  and waivers but whose blame window is entirely older than the merge-base
  land in `advisory_findings`. On every audit run the poller files at most
  `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP` (default 5) of them through the
  existing `create_security_pass_advisory_followup` (source `preexisting`),
  **immediately**, not deferred: the cited code is older than the merge-base,
  so it already exists on the default branch and the standalone pipeline can
  plan it there today. Rows above the cap are carried in state
  (`security_pass_advisory_backlog`) and filed on later ticks, capped the same
  way. They never gate completion, never count toward the fix-cycle budget,
  and are handed to the engine on the next audit as waived rows so they are
  not re-reported as blocking.
- **Alternatives considered:** drop them (the pre-2026-09 out-of-scope
  behaviour); one consolidated advisory issue per project; defer them until
  the final merge like judge-time advisories; uncapped.
- **Why:** the findings are real (the cycle-7 list is a genuine hardening
  backlog for this repository) and dropping them loses value, but blocking on
  them charges the wrong project. Deferral exists (PR #4119) because
  judge-accepted findings may cite code that exists only on the integration
  branch; a pre-existing finding by definition does not. The weekly audit
  already owns the `ai:security` follow-up shape and the
  `<!-- ai:security-finding:<id> -->` marker, so reuse is §5-compliant; the
  cap protects §15 and issue-tracker noise. `ai:security` issues enter
  clarify → plan → implement on their own, so no docs mirror or
  `/audit-plans` change is needed (Q6).

### D4 — Rebind a clean pass when the head advance adds no project lines

- **Chosen:** when the prior status is `passed` and the head moved, the
  poller lists `git rev-list <head> ^<last_audited_sha> ^<merge_base>`; if
  every commit is a merge commit whose `git show --format= --cc` output is
  empty (no conflict-resolution edits), the pass is rebound to the new head
  without a model run (`SECURITY_PASS_REBOUND`). Any non-merge commit, any
  evil merge, or any git error falls through to the existing budget-reset
  re-audit (PR #4013).
- **Alternatives considered:** always re-run (today); compare patch-ids of the
  project diff before and after.
- **Why:** a routine `chore: sync main into orchestrator/project-N` is the
  most common invalidation and adds no project code. Patch-id comparison of a
  multi-thousand-line diff is heavier and fails on any context drift.

### D5 — Waivers persist in project state and match exact-or-fuzzy

Shipped as designed (`security_pass_waived_findings`,
`SECURITY_AUDIT_WAIVED_FINDINGS`, `SECURITY_AUDIT_WAIVER_LINE_WINDOW`,
`/security-pass-waive`). No remaining work. Advisory rows filed under D3 are
appended to the same waiver list with `source: "preexisting"` so the existing
suppression covers them.

### D6 — Two independently mergeable phases with fail-open seams

- **Chosen:** Phase A engine (inert unless `SECURITY_AUDIT_LINE_OWNERSHIP=project-lines`
  is set; the findings JSON gains only additive fields under the unchanged
  `security_audit_findings.v1` schema), Phase B poller (treats absent
  additive fields as empty, so an old engine behaves exactly as today; adds
  rebinding, advisory routing, and the kill switch).
- **Why:** every merge lands in production. Additive JSON and a default-off
  engine flag mean any merge order leaves the weekly audit byte-identical and
  the poller no worse than today.

### D7 — Interaction with the exhaustion judge and the keep_fixing cap

- **Chosen:** no change to `security_pass_exhaustion_judge`. With D1 and D3
  live, only project-owned findings reach the budget, the judge, and the cap.
  `MAX_SECURITY_PASS_CYCLES`, `MAX_SECURITY_PASS_JUDGE_ROUNDS`, and
  `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` keep their meanings.
- **Why:** the judge and the cap decide *how many* cycles a project's own
  regressions get; D1 and D3 decide *what counts* as the project's. The two
  compose without new state.

## Goals

- A finding whose cited line and 3-line window are all older than the
  project's merge-base never blocks completion; it is filed as an
  `ai:security` follow-up (up to 5 per audit run, backlog carried in state)
  and reported on the tracking issue.
- A finding on any project-written line blocks, on every cycle, regardless
  of when the line was written (D2′).
- On fix cycle N+1 a prior finding not re-emitted is logged as verified
  fixed (`SECURITY_PASS_VERIFIED_FIXED`); a re-emitted one blocks (live today).
- A `passed` pass invalidated by a head advance that adds no project lines is
  rebound with zero model cost (`SECURITY_PASS_REBOUND`).
- `SECURITY_PASS_LINE_OWNERSHIP=file` restores today's per-file gate with no
  other behaviour change.
- The weekly `security-audit.yml` path is byte-identical in behaviour.
- With `ENABLE_SECURITY_PASS=false` nothing here runs.

## Non-goals

- No change to the weekly audit's cadence, tracker, or 3-per-week follow-up
  cap; the security pass's advisory follow-ups use the same label and marker
  but their own cap.
- No suppression of project-owned findings by age (the original D2 is
  withdrawn).
- No change to the exhaustion judge, the keep_fixing cap, or deferred
  judge-time advisories (PR #4119, #4135).
- No docs mirror of security follow-ups and no `/audit-plans` change: open
  `ai:security` issues already enter the automated pipeline (Q6).
- No narrowing of the audit prompt to diff hunks: the auditor keeps reading
  whole in-scope files; only the *routing* of a finding changes (Q5: A).
- No repo-wide exclusion-catalog changes and no deterministic scanners.
- No new consumer wrapper inputs; consumers set repo vars if they want
  non-default values.
- No MongoDB or data-model work.

## Constraints

- **§6 naming immutability:** nothing is renamed or removed. New identifiers,
  all verified unique by grep on 2026-09-19: env vars
  `SECURITY_AUDIT_LINE_OWNERSHIP`, `SECURITY_AUDIT_OWNERSHIP_CONTEXT_LINES`,
  `SECURITY_PASS_LINE_OWNERSHIP`, `SECURITY_PASS_OWNERSHIP_CONTEXT_LINES`,
  `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP`; findings-JSON fields
  `advisory_findings`, `verified_fixed_finding_ids`, `counts.advisory`;
  state fields `security_pass_advisory_backlog`; shell functions
  `security_pass_rebind_if_no_new_project_lines`,
  `security_pass_file_advisory_findings`; log prefixes
  `SECURITY_PASS_REBOUND`, `SECURITY_PASS_VERIFIED_FIXED`,
  `SECURITY_PASS_ADVISORY_ROUTED`. Existing identifiers are reused as they
  are: `SECURITY_AUDIT_PRIOR_FINDINGS`, `SECURITY_AUDIT_WAIVED_FINDINGS`,
  `security_pass_reported_findings`, `security_pass_waived_findings`,
  `security_pass_followup_issues`, `create_security_pass_advisory_followup`,
  `SECURITY_PASS_ADVISORY_FOLLOWUP_CREATED`. The original plan's
  `SECURITY_AUDIT_NEW_SINCE_SHA`, `SECURITY_AUDIT_PRIOR_FINDINGS_IN`,
  `SECURITY_AUDIT_WAIVED_FINDINGS_IN`, `counts.suppressed_older_than_base`,
  and `create_security_pass_advisory_followups` are withdrawn (never
  introduced).
- **Findings JSON schema:** `schema_version` stays
  `security_audit_findings.v1`; new top-level keys and `counts` keys are
  additive. The poller's jq validator (`run_security_pass_inline`, after the
  engine call at ≈6360) is extended to type-check the new keys **only when
  present**.
- **§15 API hygiene:** per audit run the only new calls are, on the advisory
  path, at most `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP` issue creates through
  `create_security_pass_advisory_followup`, which already dedupes on state
  and on one cached marker search per tracking issue per poller process.
  Rebinding, ownership classification, and verified-fixed logging use local
  git only. Advisory creation is fail-open: a failed create keeps the row in
  `security_pass_advisory_backlog` and never blocks the pass.
- **§19:** advisory issue bodies and all comments use `Refs #<tracking>`.
- **§20:** one `changelog.d/` fragment per phase; `CHANGELOG.md` untouched.
- **§14:** no `.github/ai/consumer_repos.json` change; consumers get the
  engine, prompt, and poller through the existing `@stable` sync.
- **README anchor discipline:** the orchestrator pipeline-step doc under
  `anchor:orchestrator-pipeline-steps` is append-only; `12e` and `12g` gain
  sentences, no bullet is renumbered.
- **Prompt template mirrors:** `prompts/mode-security-audit.txt` and
  `prompts/_templates/mode-security-audit.txt` change in the same commit
  (`tests/test_assemble_prompt.py` enforces parity).

## Approach

```
run_security_pass_inline (poller, every completion route; ≈6227)
  │
  ├─ prior status passed & head moved?
  │     └─ rev-list head ^last_audited ^merge_base all clean merges?
  │            yes → SECURITY_PASS_REBOUND, passed at new head, return 0
  │            no  → budget reset (existing, PR #4013) + continue
  │
  ├─ codex_heartbeat → security_audit.sh findings-json with (≈6345)
  │     SECURITY_AUDIT_DIFF_BASE / DIFF_HEAD / DIFF_SINCE            (existing)
  │     SECURITY_AUDIT_PRIOR_FINDINGS / WAIVED_FINDINGS / FIX_CYCLE_DIFFS (existing)
  │     SECURITY_AUDIT_LINE_OWNERSHIP=project-lines  CONTEXT_LINES=3  (new)
  │
  │   engine post-filter per finding (after validity, file scope, confidence,
  │   exclusion catalog, waivers — the existing order, line ≈1423):
  │     blame window all ≤ merge_base   → advisory_findings
  │     else                            → findings (blocking)
  │   prior ids absent from output      → verified_fixed_finding_ids
  │
  ├─ security_pass_last_audited_sha = head (existing)
  ├─ advisory_findings → waiver rows (source preexisting) + backlog
  │     → security_pass_file_advisory_findings (cap 5/tick, fail-open,
  │       create_security_pass_advisory_followup, filed immediately)
  ├─ findings == 0 → passed (existing)
  └─ findings > 0 → security_pass_reported_findings = findings (existing)
        cycle < MAX(5) → consolidated fix issue (existing)
        cycle ≥ MAX    → exhaustion judge → keep_fixing cap (existing)
```

## Phases & Merge Strategy

One PR per phase. Any merge order is safe: Phase A is inert until Phase B
sets `SECURITY_AUDIT_LINE_OWNERSHIP`; Phase B treats an old engine's output
(no additive fields) as "no advisory, no verified" and still gains rebinding
and the kill switch plumbing. Reverting either phase leaves the other
functional.

1. **Phase A — Audit engine: line ownership and verified-fixed reporting.**
   Scope: `scripts/security_audit.sh` (findings-json mode only),
   `prompts/mode-security-audit.txt` and its template mirror,
   `tests/test_security_audit_workflow_contract.py`, changelog fragment.
   Done when: with `SECURITY_AUDIT_LINE_OWNERSHIP` unset, every existing
   contract test passes unchanged and the findings JSON is byte-identical to
   today; with `project-lines` in a sandbox git repo, a finding on a
   pre-existing line lands in `advisory_findings`, a finding on a new line
   stays in `findings`, a finding within 3 lines of a new line stays in
   `findings`, a blame failure stays in `findings`, and a prior id absent
   from the model output appears in `verified_fixed_finding_ids`.
   Rollback: revert the PR; a live Phase B keeps passing env vars the old
   engine ignores and behaves as today.

2. **Phase B — Poller: kill switch, advisory routing, rebinding.**
   Scope: `scripts/orchestrate_poll_process.sh`,
   `.github/workflows/orchestrate_poll.yml` (defaults only), `README.md`,
   `agents.md`, `docs/how-it-works.md`, `tests/test_orchestrate_poll_process.py`,
   `tests/test_orchestrate_poll_workflow_contract.py`, changelog fragment.
   Done when: the full `tests/test_orchestrate_poll_process.py` suite passes;
   new tests prove rebinding on a clean sync merge and no rebinding on an
   evil merge or a non-merge commit; the engine env carries the ownership
   vars; `advisory_findings` become waiver rows plus advisory issues (≤ cap
   per tick, backlog carried, deduped, fail-open, tracking comment names
   them) and never block; an old-engine result without additive fields still
   blocks and passes exactly as today; `SECURITY_PASS_LINE_OWNERSHIP=file`
   restores per-file scope.
   Rollback: revert the PR, or set repo var `SECURITY_PASS_LINE_OWNERSHIP=file`
   to restore per-file scope while keeping rebinding, or
   `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP=0` to stop advisory issues (rows still
   accumulate in the backlog and are filed when the cap is raised).

## Implementation Steps

### Phase A — engine

1. `scripts/security_audit.sh` (env block, ≈ lines 191–300): add
   `SECURITY_AUDIT_LINE_OWNERSHIP="${SECURITY_AUDIT_LINE_OWNERSHIP:-file}"`
   (accept `file` | `project-lines`, else exit 1 with the same message shape
   as the confidence-gate check) and `SECURITY_AUDIT_OWNERSHIP_CONTEXT_LINES`
   (default `3`, integer 0–50). `project-lines` is accepted only when
   `SECURITY_AUDIT_OUTPUT_MODE=findings-json` and `SECURITY_AUDIT_DIFF_BASE`
   is set; otherwise preflight fails with `line ownership requires
   findings-json mode and an explicit diff range` so the weekly path cannot
   pick it up by accident.
2. `scripts/security_audit.sh` `security_audit_append_prompt_context`
   (≈ lines 118–137): in the incremental branch, when ownership is
   `project-lines`, append after the file list: "Only lines the project
   itself added or modified in the range block completion; a finding on an
   older line in these files is still worth reporting and is routed as an
   advisory. Cite the exact line where the defect is, not the nearest
   project-written line." The existing prior-findings and fix-cycle blocks
   are unchanged.
3. `prompts/mode-security-audit.txt` and
   `prompts/_templates/mode-security-audit.txt`: add one additive rule under
   `Rules:` — "Cite the line where the defect lives. The runtime routes
   findings by line ownership; a finding attributed to the wrong line is
   routed wrongly." No existing line is reworded.
4. `scripts/security_audit.sh` post-filter python (≈ lines 1185–1520): add
   argv for ownership mode, context lines, base SHA, and head SHA. After the
   existing scope, confidence, exclusion, and waiver checks, classify each
   surviving finding: `git blame --porcelain -L <lo>,<hi> <head> -- <file>`,
   collect the distinct commits, and for each commit check
   `git merge-base --is-ancestor <commit> <base_sha>` with a per-run cache.
   All-ancestor → `advisory_findings`; otherwise → `findings`. Any blame or
   ancestry error → `findings` (fail closed), with one warning per file.
   Compute `verified_fixed_finding_ids` = prior finding ids (from
   `SECURITY_AUDIT_PRIOR_FINDINGS`) that appear in neither `findings` nor
   `advisory_findings`. Emit `counts.advisory`. Ownership mode `file` skips
   the classification and emits an empty `advisory_findings`, so the summary
   shape is the same in both modes.
5. `scripts/security_audit.sh` findings packaging (≈ lines 1440–1520): carry
   `advisory_findings` (same row shape as `findings`),
   `verified_fixed_finding_ids`, and `counts.advisory` into
   `SECURITY_AUDIT_FINDINGS_OUT`; keep `schema_version` at
   `security_audit_findings.v1`; extend `count_keys` (≈ line 1493) with
   `advisory`. Add the advisory count to the human summary lines (≈ line
   1700).
6. `tests/test_security_audit_workflow_contract.py`: the sandbox tests under
   Tests; assert the default-mode output has no new behaviour by diffing
   against the existing fixture.
7. `changelog.d/security-pass-line-ownership-engine.md` (`changed`).

### Phase B — poller

8. `scripts/orchestrate_poll_process.sh` defaults (≈ lines 1284–1345, next
   to `MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS`): add
   `SECURITY_PASS_LINE_OWNERSHIP="${SECURITY_PASS_LINE_OWNERSHIP:-project-lines}"`
   (accept `file` | `project-lines`, warn and default on anything else),
   `SECURITY_PASS_OWNERSHIP_CONTEXT_LINES` (default `3`),
   `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP` (default `5`, integer ≥ 0).
9. `.github/workflows/orchestrate_poll.yml` (≈ line 690, next to
   `SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED`): add the three env rows with
   the same defaults; `tests/test_orchestrate_poll_workflow_contract.py`
   asserts them.
10. `ensure_security_pass_state_fields` (≈ line 4498): normalize
    `security_pass_advisory_backlog` (array of finding objects with the eight
    finding keys plus `audited_head_sha`; default `[]`, last 100 kept).
11. New `security_pass_rebind_if_no_new_project_lines <head_sha>
    <merge_base_sha>` called from `run_security_pass_inline` immediately
    before the existing `prior_security_status = passed` budget reset
    (`SECURITY_PASS_CYCLE_BUDGET_RESET`, ≈ line 6119): returns 0 and rebinds
    (state `security_pass_status = "passed"`, `security_pass_head_sha = head`,
    `security_pass_last_audited_sha = head`,
    `reconcile_tracking_body_after_security_pass_transition`,
    `post_state_comment`, log `SECURITY_PASS_REBOUND tracking_issue=…
    from=<last_audited> to=<head> reason=no_new_project_lines`) only when
    `security_pass_last_audited_sha` is non-empty and an ancestor of the head
    and `git rev-list <head> ^<last_audited> ^<merge_base>` is non-empty and
    every listed commit has two or more parents and empty
    `git show --format= --cc <commit>` output. Any git error returns 1.
12. `run_security_pass_inline` engine invocation (≈ lines 6345–6360): pass
    `SECURITY_AUDIT_LINE_OWNERSHIP="${SECURITY_PASS_LINE_OWNERSHIP}"` and
    `SECURITY_AUDIT_OWNERSHIP_CONTEXT_LINES="${SECURITY_PASS_OWNERSHIP_CONTEXT_LINES}"`.
    Log both in the existing `SECURITY_PASS_SCOPE` line (≈ 6317) as
    `ownership=<mode> context=<n>`.
13. `run_security_pass_inline` result validation (≈ lines 6360–6400): extend
    the jq validator so `advisory_findings`, `verified_fixed_finding_ids`,
    and `counts.advisory` are optional but type-checked when present (same
    per-row checks as `findings`).
14. `run_security_pass_inline` after the head recheck and before the
    blocked/passed decision: when `verified_fixed_finding_ids` is non-empty
    log `SECURITY_PASS_VERIFIED_FIXED tracking_issue=… ids=<comma list>`;
    when `advisory_findings` is non-empty, append each row to
    `security_pass_waived_findings` via `security_pass_record_waivers` with
    `source: "preexisting"`, `waived_by: "line-ownership"`,
    `waived_at_cycle`, `issue: null`, and to `security_pass_advisory_backlog`
    with `audited_head_sha`; log `SECURITY_PASS_ADVISORY_ROUTED
    tracking_issue=… head_sha=… count=<n> ids=<comma list>`. Then call
    `security_pass_file_advisory_findings`.
15. New `security_pass_file_advisory_findings <integration_branch> <head_sha>`:
    for at most `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP` rows of
    `security_pass_advisory_backlog` (oldest first), call
    `create_security_pass_advisory_followup "<finding>" "<integration_branch>"
    "<audited_head_sha>" "<justification>" "preexisting"` (sixth argument
    empty: not deferred) where the justification reads "The cited line
    predates this project's merge-base and already exists on the default
    branch; routed as a non-blocking advisory by line ownership." On an issue
    number, drop the row from the backlog (the create path already records
    `security_pass_followup_issues` and sets `issue` on the waiver row). A
    failed create leaves the row for the next tick. Log per row through the
    existing `SECURITY_PASS_ADVISORY_FOLLOWUP_CREATED`. Return 0 always.
    Add each filed issue to `security_pass_followups_merge_checked` (PR
    #4135) so the merge-time re-answer never reads it: it was planned against
    the default branch from the start.
16. Tracking comments: the existing "🔒 Project security pass blocked" and
    the clean-pass path gain one trailing sentence when advisories were
    routed this run: "N finding(s) on pre-existing code were routed as
    non-blocking `ai:security` follow-ups: #a, #b (M still queued)." No extra
    comment; the existing `post_tracking_comment` call carries it.
17. Advisory issue body: `create_security_pass_advisory_followup` gains a
    body line when `source` is `preexisting`: "This code predates the
    project (older than merge-base `<sha>`); the finding was routed as an
    advisory by line ownership, not accepted by a judge." The existing
    mitigation-policy section, markers, and `Refs #<tracking>` are unchanged.
18. Docs: `README.md` env tables (rows for the three new poller vars),
    sentences in `12e` (line ownership, advisory routing, rebinding) and
    `12g` (advisories now also come from line ownership, before the budget);
    `agents.md` security-pass paragraph (new state field, new log prefixes in
    both registries); `docs/how-it-works.md` state-diagram note for
    rebinding and the advisory edge.
19. `tests/test_orchestrate_poll_process.py`: the tests under Tests.
20. `changelog.d/security-pass-line-ownership-poller.md` (`changed`).

## Files & Modules

- `scripts/security_audit.sh` — A
- `prompts/mode-security-audit.txt` — A
- `prompts/_templates/mode-security-audit.txt` — A
- `tests/test_security_audit_workflow_contract.py` — A
- `changelog.d/security-pass-line-ownership-engine.md` `[new]` — A
- `scripts/orchestrate_poll_process.sh` — B
- `.github/workflows/orchestrate_poll.yml` — B
- `README.md` — B
- `agents.md` — B
- `docs/how-it-works.md` — B
- `tests/test_orchestrate_poll_process.py` — B
- `tests/test_orchestrate_poll_workflow_contract.py` — B
- `changelog.d/security-pass-line-ownership-poller.md` `[new]` — B

## Tests

Unit / contract (sandbox git repos, fake `codex`, as the existing suites do):

- Phase A, engine:
  - default env: findings-json output byte-identical to the current fixture
    (regression guard for the weekly path and for B-before-A ordering);
  - `project-lines`: pre-existing line → `advisory_findings`; new line →
    `findings`; line within 3 of a new line → `findings`; line 4 away →
    `advisory_findings`; a line rewritten by a sync merge from the default
    branch (commit reachable from the base) → `advisory_findings`;
  - prior id absent from model output → `verified_fixed_finding_ids`; prior
    id re-emitted → `findings`, not in the verified list;
  - a finding inside a previous fix cycle's hunk → `findings` (D2′);
  - blame failure classifies as blocking (fail closed); `project-lines`
    without `DIFF_BASE` or in `issues` mode fails preflight side-effect-free;
  - prompt context contains the ownership sentence only in `project-lines`.
- Phase B, poller:
  - clean sync merge after `passed` → `SECURITY_PASS_REBOUND`, no codex call,
    pass valid at the new head; evil merge or non-merge commit → real audit
    with the existing budget reset;
  - engine env carries the ownership vars; `SECURITY_PASS_SCOPE` logs them;
  - old-engine output (no additive keys) still blocks/passes as today;
  - advisories: routed rows become waiver rows with `source: "preexisting"`
    and backlog rows; ≤ cap filed per tick through the existing create path,
    filed immediately (no `followup_pending`), deduped by marker and state,
    added to `security_pass_followups_merge_checked`; the rest filed on the
    next tick; a create failure keeps the row; the tracking comment names
    them; `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP=0` files nothing and blocks
    nothing;
  - a project whose only findings are advisory passes on the first audit and
    completes;
  - `SECURITY_PASS_LINE_OWNERSHIP=file` reproduces today's blocking result on
    the same fixture;
  - `SECURITY_PASS_VERIFIED_FIXED` logged when the engine reports verified ids.

End-to-end (manual, after Phase B merges): watch the next project that
reaches the gate on a consumer repo; confirm `SECURITY_PASS_REBOUND` on the
first sync merge after a clean pass, `SECURITY_PASS_ADVISORY_ROUTED` for
findings in pre-existing code, and that the fix-cycle count stays at or
below the number of genuinely project-owned findings.

## Risks & Mitigations

- **A pre-existing vulnerability that the project makes newly reachable is
  advisory, not blocking.** ACCEPTED (original round, reaffirmed 2026-09-19)
  — the project's own lines still block, the advisory is filed immediately as
  `ai:security` and enters the pipeline on its own, the weekly audit continues
  to cover the default branch, and the operator can re-add strictness with
  `SECURITY_PASS_LINE_OWNERSHIP=file`.
- **The model cites a project-written line for a pre-existing defect (or the
  reverse).** The prompt rule in step 3 asks for the exact defect line; the
  3-line window absorbs off-by-a-few citations; a wrong classification in the
  blocking direction costs one fix cycle, in the advisory direction it costs
  nothing (the issue is still filed). Residual risk ACCEPTED.
- **Blame on very large files per finding.** `git blame -L` on a 55k-line
  file is sub-second; findings per run are single digits. No mitigation
  needed beyond the per-commit ancestry cache.
- **Rebind on a merge that carries conflict-resolution edits.** `--cc` output
  is non-empty for any evil merge, so it is audited. A merge with only
  whitespace resolution still shows in `--cc`; it is audited, which is the
  safe direction.
- **Advisory issue volume.** Cap of 5 per tick, backlog carried in state
  (last 100), marker and state dedupe, and the repo var can be set to `0`.
  For #3965 the first post-upgrade audit would route the current cycle-7
  class of findings, which is the intended outcome.
- **Old poller + new engine.** The engine never enables ownership mode on
  its own, so nothing changes. **New poller + old engine.** Additive keys
  absent → today's behaviour. Covered by tests in both phases.
- **Advisory rows as waivers suppress a later project regression at the same
  location.** Suppression matches exact id, or same file and category within
  `SECURITY_AUDIT_WAIVER_LINE_WINDOW` (40 lines). A project edit that
  introduces a *new* defect class in the same window is still reported (the
  category differs); the same class at the same place is by construction the
  advisory already filed. ACCEPTED, same as for judge waivers today.

## Rollout

- Both phases ship default-on for the security pass with per-feature kill
  switches: `SECURITY_PASS_LINE_OWNERSHIP=file` restores per-file scope,
  `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP=0` stops advisory issues, and
  `ENABLE_SECURITY_PASS=false` remains the global kill switch. The weekly
  audit is unaffected in every phase.
- Consumers receive each phase on the next `@stable` sync of `scripts/`,
  `prompts/`, and the reusable `orchestrate_poll.yml`; no wrapper or repo-var
  action is required. The `ai:security` label is created on demand by
  `ensure_label_exists`.
- Rollback per phase: revert the phase PR. State fields written by Phase B
  are tolerated (normalized to defaults) by an earlier poller.
- No migration: `ensure_security_pass_state_fields` defaults the new field on
  first read, so projects already in `security-pass-fixing` (#3965) pick up
  the new behaviour on their next audit; a project at the exhaustion judge
  sees only project-owned findings from then on.

## References

- `docs/completed/orchestrator-security-pass-gate-plan.md` (original gate
  design, decisions D1–D8)
- coding-workflows #3933 (gate implementation project), #3965 (looping
  project; cycle 7 fix issue #4113, integration PR #3968), #4013 (budget
  reset + body render), #4018 (exhaustion findings table), #4074 (exhaustion
  judge), #4089 (fix-cycle attack surface), #4119 (deferred advisories,
  staged-support editor fix), #4135 (keep_fixing cap, merge-time re-answer)
- tele-funtoken-msg-scoring #3928, #3955 (consumer projects that hit the
  loop), #4281 (delta re-audit and judge motivation)
- `scripts/security_audit.sh` per-file scope check (line 1423), prompt
  context (`security_audit_append_prompt_context`, lines 118–137),
  post-filter and packaging (≈1185–1520)
- `scripts/orchestrate_poll_process.sh` `ensure_security_pass_state_fields`
  (≈4498), `security_pass_terminal_failure` (≈4721),
  `create_security_pass_fix_issue` (≈5198),
  `security_pass_apply_waivers_to_findings` (≈5403),
  `create_security_pass_advisory_followup` (≈5493),
  `security_pass_exhaustion_judge` (≈5725), `run_security_pass_inline`
  (≈6227; engine env at ≈6345)
