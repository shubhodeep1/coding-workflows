# Security pass convergence: project-line scope, incremental re-audit, advisory follow-ups, and operator waivers

## Summary

Make the orchestrator's mandatory security pass converge instead of looping:
a finding blocks a project only when it sits on a line the project itself
wrote, each fix-cycle re-audit verifies the previous cycle's findings and looks
for new ones only in code that arrived since the last audit, pre-existing-code
findings become non-blocking `ai:security` follow-ups instead of being dropped,
the cycle budget rises from 3 to 5, the exhaustion comment shows the surviving
findings table, and a `/security-pass-waive` command lets an operator accept a
finding once per project.

## Automation & Wiring (§18.E)

- **New script vs extension:** no new standalone script. The change extends
  `scripts/security_audit.sh` (findings-json mode only), the poller
  `scripts/orchestrate_poll_process.sh`, and the audit prompt. Nothing requires
  manual invocation.
- **Scheduler entry point:** the existing orchestrator poller,
  `.github/workflows/orchestrate_poll.yml` (cron `*/5 * * * *` via
  `internal-orchestrate-poll.yml` here and the synced
  `workflow-templates/ai-orchestrate-poll.yml` wrapper in consumers). The
  security pass already runs inline in that tick; every change below runs
  inside `run_security_pass_inline` or the tracking-issue command scan on the
  same tick.
- **Long-running supervisor:** none. The poller is the supervisor.
- **DB operations:** none. No MongoDB collections, indexes, or contracts (§10
  not applicable).
- **Future-removal registry (§18.F):** no single-use or long-running scripts
  are introduced; no `docs/scripts-pending-removal.md` entry.

## Context

The security-pass gate shipped via project #3933 (see
`docs/completed/orchestrator-security-pass-gate-plan.md`) and is default-on.
A review of every project that went through it since the Phase 4 flip shows
the gate works as designed but does not converge:

- **coding-workflows #3965** (a 913-line feature): 9 consolidated fix issues
  (#3976, #3987, #3990 → #3993 → #3996, #3998, #4000, #4005, #4017) carrying
  13 findings. `git blame` on every cited line at the audited head attributes
  5 to the project's own code, 4 to code an earlier fix cycle wrote, and 4 to
  code older than the project (for example `orchestrate_poll.yml:684` and
  `scripts/targeted_file_context.py:230`, both from the Aug 27 baseline). The
  integration branch grew from 913 to 4,137 insertions across 29 files while
  fixing them. The project passed at `75048a2c`, a routine `chore: sync main`
  merge advanced the head to `56f71c8f`, and the full re-audit found 2 more.
- **tele-funtoken-msg-scoring #3928**: cycles found 2, 2, 3, then 4 findings
  across four audits with no repeated finding, and exhausted 3/3. The terminal
  comment reported "4 blocking finding(s)" and nothing else; the operator had
  to `/re-security-pass` blind.
- **tele-funtoken-msg-scoring #3955** (Cricket Sixer): 3 of the 4 cycle-1
  findings are in shared bet-execution code dated Apr 18, Aug 26, and Feb 19
  (`ft.games/app.py:42741`, `ft.games/app.py:43188`,
  `ft.games/autobet.py:353`). They are real platform findings charged to the
  first project that touched those files.

Three mechanisms produce this:

1. `scripts/security_audit.sh` enforces incremental scope **per file**
   (`if audit_scope_mode == "incremental" and str(normalized_finding["file"])
   not in changed_files`, line 782). A one-line edit to a 55,000-line
   `ft.games/app.py` puts the whole file in scope, and
   `security_audit_append_prompt_context` (lines 118–135) tells the model it
   may cite any line in a changed file.
2. `run_security_pass_inline` (`scripts/orchestrate_poll_process.sh:4850`)
   re-audits `merge-base..head` in full on every cycle with no memory of the
   previous cycle's findings. A non-deterministic model returns a different
   handful each run.
3. Each fix widens the diff, which widens the per-file scope, which exposes
   more old code. Fixes also add new mechanisms that the next audit flags
   (#3998's HMAC state signing → #4000's rotation-lockout finding).

Binding rules: §5 (extend existing mechanisms), §6 (no renames; every new
identifier below was grepped and is unique), §14 (consumers receive
`scripts/`, `prompts/`, and the reusable `orchestrate_poll.yml` through the
`@stable` sync; no wrapper change), §15 (API hygiene), §18 (no manual
scripts), §19 (`Refs #N` only), §20 (changelog fragments).

Clarification answers that fixed the design (all `A`): blame-based line
ownership; 3-line context window; one capped `ai:security` follow-up per
pre-existing finding; cycle N+1 blocks only on lines newer than the previous
audit plus re-emitted prior findings; an unfixed prior finding survives and
counts toward the budget; a head advance with zero new project lines rebinds
the pass without a model run; `MAX_SECURITY_PASS_CYCLES` default 5;
`/security-pass-waive` accepted in failed and fixing states with a loop reset
in the failed state; OWNER/MEMBER/COLLABORATOR non-bot authors; exact plus
fuzzy waiver matching; three phases.

## Decisions

### D1 — Line ownership via `git blame`, not diff-hunk parsing

- **Chosen:** for each surviving finding, `git blame --porcelain -L <lo>,<hi>
  <head> -- <file>` over the cited line ±`SECURITY_AUDIT_OWNERSHIP_CONTEXT_LINES`
  (default 3). A line is project-owned when its blamed commit is **not** an
  ancestor of `SECURITY_AUDIT_DIFF_BASE` (the merge-base). The finding is
  project-owned when any line in the window is.
- **Alternatives considered:** parsing `git diff merge-base..head` into
  per-file line sets; asking the model to self-classify.
- **Why:** blame is exact per line, costs one local git call per finding
  (findings are a handful per run), and needs no hunk-offset bookkeeping.
  Sync merges from the default branch resolve to commits reachable from the
  merge-base, so their lines are never project-owned. Model self-classification
  is not trustworthy for a gate.

### D2 — Cycle N+1 blocks only on new project lines plus re-emitted prior findings

- **Chosen:** the poller passes `SECURITY_AUDIT_NEW_SINCE_SHA` (the last
  audited head) and `SECURITY_AUDIT_PRIOR_FINDINGS_IN` (the last blocked
  audit's blocking findings). The prompt asks the model to re-emit any prior
  finding that is still exploitable **with its exact `finding_id`**. The
  post-filter keeps a project-owned finding as blocking when its `finding_id`
  is a prior id, or when its owning commit is not an ancestor of
  `SECURITY_AUDIT_NEW_SINCE_SHA`. Prior ids not re-emitted are reported as
  `verified_fixed_finding_ids`.
- **Alternatives considered:** any project-owned finding blocks on every
  cycle (today's behaviour narrowed to lines); a separate verification-only
  model call before the new-findings call.
- **Why:** the first alternative cannot converge on a large diff (#3928 saw
  no repeated finding in four runs). A second model call doubles cost per
  cycle; re-emission with a stable id keeps the output contract unchanged.

### D3 — Pre-existing findings become capped `ai:security` follow-ups

- **Chosen:** findings that pass confidence and exclusions but sit on
  pre-existing lines land in `advisory_findings`; the poller files at most
  `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP` (default 5) `ai:security` issues per
  audit run, deduped on the weekly audit's `<!-- ai:security-finding:<id> -->`
  marker and on `security_pass_followup_issues` in state. They never gate
  completion.
- **Alternatives considered:** drop them (today's out-of-scope behaviour for
  other files); one consolidated advisory issue per project; uncapped.
- **Why:** the #3955 double-debit finding is real and platform-wide; dropping
  it loses value, blocking on it charges the wrong project. The weekly audit
  already owns the `ai:security` follow-up shape and marker, so reuse is
  §5-compliant; the cap protects §15 and issue-tracker noise.

### D4 — Rebind a clean pass when the head advance adds no project lines

- **Chosen:** when the prior status is `passed` and the head moved, the
  poller lists `git rev-list <head> ^<last_audited_sha> ^<merge_base>`; if
  every commit is a merge commit whose `git show --format= --cc` output is
  empty (no conflict-resolution edits), the pass is rebound to the new head
  without a model run (`SECURITY_PASS_REBOUND`).
- **Alternatives considered:** always re-run; compare patch-ids of the
  project diff before and after.
- **Why:** a routine `chore: sync main into orchestrator/project-N` is the
  most common invalidation and adds no project code. Patch-id comparison of a
  4,000-line diff is heavier and fails on any context drift. Any non-merge
  commit or an evil merge still triggers a real audit.

### D5 — Waivers persist in project state and match exact-or-fuzzy

- **Chosen:** `/security-pass-waive <finding_id> …` stores
  `{finding_id, file, line, owasp_or_stride_category}` (resolved from
  `security_pass_prior_findings` when known) in
  `security_pass_waived_findings`. The engine drops a finding when the id
  matches exactly, or when file and category match and the cited line is
  within the context window of the waived line; waived rows are also given to
  the model as accepted.
- **Alternatives considered:** editing `scripts/security_audit_fp_exclusions.json`
  (repo-wide, synced to every consumer, substring-based); exact-id only.
- **Why:** finding ids are model-generated and drift between runs; a
  project-scoped waiver with a location match survives that drift without
  touching the shared catalog.

### D6 — Three independently mergeable phases with fail-open seams

- **Chosen:** P1 engine (inert unless new env vars are set; findings JSON
  gains only additive fields under the unchanged
  `security_audit_findings.v1` schema), P2 poller (treats absent additive
  fields as empty, so an old engine behaves exactly as today), P3 waive
  command (only writes state P2 reads, and P2 treats an absent array as
  empty).
- **Alternatives considered:** single PR; two phases.
- **Why:** every merge lands in production. Additive JSON and default-off env
  vars mean any merge order leaves the weekly audit byte-identical and the
  poller no worse than today.

## Goals

- A finding whose cited line and 3-line window are all older than the
  project's merge-base never blocks completion; it is filed as an
  `ai:security` follow-up (up to 5 per audit run) and reported on the
  tracking issue.
- On fix cycle N+1 the poller passes the cycle-N findings for verification;
  a prior finding not re-emitted is logged as verified fixed, a re-emitted one
  blocks; new findings block only on project lines newer than the last audit.
- A `passed` pass invalidated by a head advance that adds no project lines is
  rebound with zero model cost.
- `MAX_SECURITY_PASS_CYCLES` defaults to 5 everywhere it is documented and
  defaulted.
- The exhaustion comment carries the same findings table the fix issue would
  have, plus the waive command usage.
- `/security-pass-waive` by an OWNER/MEMBER/COLLABORATOR human persists the
  waiver, acknowledges it, and in the failed state resets the loop.
- The weekly `security-audit.yml` path is byte-identical in behaviour.
- With `ENABLE_SECURITY_PASS=false` nothing here runs.

## Non-goals

- No change to decision D3 of the original plan: every surviving
  project-owned finding still blocks regardless of severity.
- No change to the weekly audit's cadence, tracker, or 3-per-week follow-up
  cap; the security pass's advisory follow-ups use the same label and marker
  but their own cap.
- No repo-wide exclusion-catalog changes and no deterministic scanners.
- No change to how the consolidated fix issue flows through
  clarify → plan → implement → review, and no new fix-issue prompt text
  beyond what the findings table already carries.
- No new consumer wrapper inputs; consumers set repo vars if they want
  non-default values.
- No MongoDB or data-model work.

## Constraints

- **§6 naming immutability:** nothing is renamed or removed. New identifiers,
  all verified unique by grep: env vars `SECURITY_AUDIT_LINE_OWNERSHIP`,
  `SECURITY_AUDIT_OWNERSHIP_CONTEXT_LINES`, `SECURITY_AUDIT_NEW_SINCE_SHA`,
  `SECURITY_AUDIT_PRIOR_FINDINGS_IN`, `SECURITY_AUDIT_WAIVED_FINDINGS_IN`,
  `SECURITY_PASS_LINE_OWNERSHIP`, `SECURITY_PASS_OWNERSHIP_CONTEXT_LINES`,
  `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP`; findings-JSON fields
  `advisory_findings`, `verified_fixed_finding_ids`, `counts.advisory`,
  `counts.suppressed_waived`, `counts.suppressed_older_than_base`; state
  fields `security_pass_last_audited_sha`, `security_pass_prior_findings`,
  `security_pass_waived_findings`, `security_pass_followup_issues`; shell
  functions `classify_security_pass_ownership` (engine-side python is inline),
  `security_pass_rebind_if_no_new_project_lines`,
  `create_security_pass_advisory_followups`,
  `render_security_pass_findings_table`; command `/security-pass-waive`;
  dedup marker `<!-- security-pass-waive-dedup:<id> -->`; log prefixes
  `SECURITY_PASS_REBOUND`, `SECURITY_PASS_VERIFIED_FIXED`,
  `SECURITY_PASS_ADVISORY_FOLLOWUP_CREATED`, `SECURITY_PASS_WAIVED`,
  `SECURITY_PASS_WAIVE_REJECTED`. `MAX_SECURITY_PASS_CYCLES` keeps its name;
  only its default changes.
- **Findings JSON schema:** `schema_version` stays
  `security_audit_findings.v1`; new top-level keys and `counts` keys are
  additive. The poller's jq validator (`orchestrate_poll_process.sh` ≈5000)
  is extended to type-check the new keys **only when present**.
- **§15 API hygiene:** per audit run the only new calls are, on the advisory
  path, one paginated `GET repos/<slug>/issues?state=open&labels=ai:security`
  for dedupe and at most `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP` issue creates.
  Rebinding, ownership classification, and prior-finding verification use
  local git only. The waive command reads the `COMMENTS` array the tick
  already fetched. Advisory creation is fail-open: a failed list or create
  logs a warning and never blocks the pass.
- **§19:** advisory issue bodies and all comments use `Refs #<tracking>`.
- **§20:** one `changelog.d/` fragment per phase; `CHANGELOG.md` untouched.
- **§14:** no `.github/ai/consumer_repos.json` change; consumers get the
  engine, prompt, and poller through the existing `@stable` sync of
  `scripts/`, `prompts/`, and the reusable `orchestrate_poll.yml`.
- **README anchor discipline:** the orchestrator pipeline-step doc under
  `anchor:orchestrator-pipeline-steps` is append-only; new prose is a suffixed
  bullet (`12f.`), never a renumber.
- **Prompt template mirrors:** `prompts/mode-security-audit.txt` and
  `prompts/_templates/mode-security-audit.txt` change in the same commit.

## Approach

```
run_security_pass_inline (poller, every completion route)
  │
  ├─ prior status passed & head moved?
  │     └─ rev-list head ^last_audited ^merge_base all clean merges?
  │            yes → SECURITY_PASS_REBOUND, passed at new head, return 0
  │            no  → budget reset (existing) + continue
  │
  ├─ codex_heartbeat → security_audit.sh findings-json with
  │     SECURITY_AUDIT_DIFF_BASE=merge_base  SECURITY_AUDIT_DIFF_HEAD=head
  │     SECURITY_AUDIT_LINE_OWNERSHIP=project-lines  CONTEXT_LINES=3
  │     SECURITY_AUDIT_NEW_SINCE_SHA=last_audited (if ancestor of head)
  │     SECURITY_AUDIT_PRIOR_FINDINGS_IN=<prior blocking findings>
  │     SECURITY_AUDIT_WAIVED_FINDINGS_IN=<waived rows>
  │
  │   engine post-filter per finding (after validity, file scope, confidence,
  │   exclusion catalog, in that order):
  │     waived (exact id | file+category+line±ctx)  → suppressed_waived
  │     blame window all ≤ merge_base               → advisory_findings
  │     id ∈ prior ids                              → findings (blocking)
  │     owning commit ≤ NEW_SINCE_SHA                → suppressed_older_than_base
  │     else                                         → findings (blocking)
  │   prior ids absent from output                   → verified_fixed_finding_ids
  │
  ├─ security_pass_last_audited_sha = head (always, on a valid result)
  ├─ advisory_findings → create_security_pass_advisory_followups (cap 5, fail-open)
  ├─ findings == 0 → passed (prior findings cleared)
  └─ findings > 0 → prior findings = findings
        cycle < MAX(5) → consolidated fix issue (existing)
        cycle ≥ MAX    → terminal failure comment WITH findings table + waive usage
```

`/security-pass-waive` is scanned on the same tick as `/re-security-pass`,
with the same boundary logic (newest command after the last state comment or
dedup marker). It persists waivers, drops them from
`security_pass_prior_findings`, and in the failed state resets the loop
exactly as `/re-security-pass` does.

## Phases & Merge Strategy

One PR per phase. Any merge order is safe: P1 is inert until P2 sets its env
vars; P2 treats an old engine's output (no additive fields) as "no advisory,
no verified" and still gains rebinding, the findings table, the new budget
default, and the poller-side plumbing; P3 only adds a command that writes a
state array P2 already tolerates as absent or empty. Reverting any single
phase leaves the others functional.

1. **Phase 1 — Audit engine: line ownership, prior-finding verification,
   waiver input.**
   Scope: `scripts/security_audit.sh` (findings-json mode only),
   `prompts/mode-security-audit.txt` and its template mirror, contract tests.
   Done when: with none of the new env vars set, every existing
   `tests/test_security_audit_workflow_contract.py` test passes unchanged and
   the findings JSON is byte-identical to today; with
   `SECURITY_AUDIT_LINE_OWNERSHIP=project-lines` in a sandbox git repo, a
   finding on a pre-existing line lands in `advisory_findings`, a finding on
   a new line stays in `findings`, `NEW_SINCE_SHA` suppresses an older
   project line unless its id is a prior id, and a waived row is suppressed
   by exact id and by fuzzy location.
   Rollback: revert the PR; the poller (if P2 is live) keeps passing env vars
   the old engine ignores and behaves as today.

2. **Phase 2 — Poller: incremental re-audit, rebinding, advisory follow-ups,
   exhaustion table, budget 5.**
   Scope: `scripts/orchestrate_poll_process.sh`,
   `.github/workflows/orchestrate_poll.yml` (default only), `README.md`,
   `agents.md`, `docs/how-it-works.md`, poller tests, changelog fragment.
   Done when: the full `tests/test_orchestrate_poll_process.py` suite passes;
   new tests prove rebinding on a clean sync merge, no rebinding on an evil
   merge or a non-merge commit, `security_pass_last_audited_sha` and
   `security_pass_prior_findings` are threaded into the engine env, advisory
   follow-ups are created at most 5 per run and deduped, an old-engine result
   without additive fields still blocks and passes exactly as today, and the
   exhaustion comment carries the findings table.
   Rollback: revert the PR, or set repo var `SECURITY_PASS_LINE_OWNERSHIP=file`
   to restore per-file scope while keeping everything else, or
   `MAX_SECURITY_PASS_CYCLES=3` to restore the old budget.

3. **Phase 3 — `/security-pass-waive` command.**
   Scope: `scripts/orchestrate_poll_process.sh` (command scan and
   acknowledgement), `docs/how-it-works.md` command table, `README.md`
   bullet, `agents.md`, poller tests, changelog fragment.
   Done when: tests prove a waive by an OWNER human in the failed state
   persists the rows, resets the loop, posts the dedup-marked ack, and the
   next audit env carries `SECURITY_AUDIT_WAIVED_FINDINGS_IN`; a waive in the
   fixing state persists without resetting; a bot author or a
   `CONTRIBUTOR`/`NONE` association is rejected with
   `SECURITY_PASS_WAIVE_REJECTED` and no state change; malformed ids are
   rejected; a repeated comment is deduped by the marker.
   Rollback: revert the PR; existing `security_pass_waived_findings` arrays
   stay in state and are ignored by a poller without the command (P2 still
   forwards them to the engine, which is the intended behaviour).

## Implementation Steps

### Phase 1 — engine

1. `scripts/security_audit.sh` (env block, ≈ lines 191–212): add
   `SECURITY_AUDIT_LINE_OWNERSHIP="${SECURITY_AUDIT_LINE_OWNERSHIP:-file}"`
   (accept `file` | `project-lines`, else exit 1 with the same message shape
   as the confidence-gate check),
   `SECURITY_AUDIT_OWNERSHIP_CONTEXT_LINES` (default `3`, integer 0–50),
   `SECURITY_AUDIT_NEW_SINCE_SHA` (default empty),
   `SECURITY_AUDIT_PRIOR_FINDINGS_IN` and `SECURITY_AUDIT_WAIVED_FINDINGS_IN`
   (default empty; when set, must be readable JSON arrays, validated in the
   preflight block near the existing `findings-output-preflight`). All four
   scoped vars are accepted only when `SECURITY_AUDIT_OUTPUT_MODE=findings-json`
   and `SECURITY_AUDIT_DIFF_BASE` is set; otherwise `project-lines` fails
   preflight with `line ownership requires findings-json mode and an explicit
   diff range` so the weekly path cannot pick it up by accident.
2. `scripts/security_audit.sh` scope resolution (≈ lines 411–442): when
   `SECURITY_AUDIT_NEW_SINCE_SHA` is set, resolve it with the same
   `rev-parse --verify --end-of-options` pattern and require
   `git merge-base --is-ancestor <new_since> <head>`; on failure, log a
   warning and clear it (fail open to full project-line scope, never to
   file scope).
3. `scripts/security_audit.sh` `security_audit_append_prompt_context`
   (≈ lines 118–135): in the incremental branch, when ownership is
   `project-lines`, append: "Only lines the project itself added or modified
   in the range are gated; findings on older lines in these files are
   reported as advisory." Then, when `SECURITY_AUDIT_PRIOR_FINDINGS_IN` is
   set, append a `=== BEGIN PRIOR BLOCKING FINDINGS TO VERIFY ===` block
   listing `finding_id`, `file:line`, category, and exploit scenario, with
   the instruction: "For each prior finding, re-emit it with the exact same
   `finding_id` only if it is still exploitable at the current head; omit it
   if fixed." When `SECURITY_AUDIT_WAIVED_FINDINGS_IN` is set, append a
   `=== BEGIN ACCEPTED FINDINGS (do not re-report) ===` block with the same
   columns. Both blocks are wrapped as untrusted evidence, matching the
   existing project-specification delimiters.
4. `prompts/mode-security-audit.txt` and
   `prompts/_templates/mode-security-audit.txt`: add one additive rule under
   `Rules:` — "When the runtime context lists prior blocking findings, re-emit
   a still-exploitable one under its exact `finding_id`; never re-emit an
   accepted finding." No existing line is reworded.
5. `scripts/security_audit.sh` post-filter python (≈ lines 549–830): add
   argv for ownership mode, context lines, new-since SHA, prior-findings
   path, waived-findings path, and the head SHA. After the existing
   confidence and exclusion checks, apply in order: waiver match
   (`suppressed_waived`), ownership classification via
   `git blame --porcelain -L` over `[line-ctx, line+ctx]` at the head with
   commit ancestry checked by `git merge-base --is-ancestor <commit>
   <base_sha>` (cache commit→bool per run; a blame failure classifies the
   finding as project-owned, fail closed), advisory routing, prior-id
   override, and new-since suppression. Emit `advisory_findings` (same row
   shape as `findings`), `verified_fixed_finding_ids`, and the three new
   counts in the summary file. Ownership mode `file` skips every new step
   and emits empty arrays and zero counts, so the summary shape is the same
   in both modes.
6. `scripts/security_audit.sh` findings packaging (≈ lines 831–900): carry
   the new arrays and counts into `SECURITY_AUDIT_FINDINGS_OUT`; keep
   `schema_version` at `security_audit_findings.v1`; extend `count_keys` with
   `advisory`, `suppressed_waived`, `suppressed_older_than_base`.
7. `tests/test_security_audit_workflow_contract.py`: add the sandbox tests
   listed under Tests; assert the default-mode output has no new behaviour by
   diffing against the existing fixture.
8. `changelog.d/security-pass-convergence-engine.md` (`changed`).

### Phase 2 — poller

9. `scripts/orchestrate_poll_process.sh` defaults (≈ lines 1284–1296):
   change the `MAX_SECURITY_PASS_CYCLES` fallback from `3` to `5` (both the
   default expansion and the warning path); add
   `SECURITY_PASS_LINE_OWNERSHIP="${SECURITY_PASS_LINE_OWNERSHIP:-project-lines}"`
   (accept `file` | `project-lines`, warn and default on anything else),
   `SECURITY_PASS_OWNERSHIP_CONTEXT_LINES` (default `3`),
   `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP` (default `5`, integer ≥ 0).
10. `.github/workflows/orchestrate_poll.yml` (≈ line 645): change the
    `MAX_SECURITY_PASS_CYCLES` fallback to `'5'`; add
    `SECURITY_PASS_LINE_OWNERSHIP: ${{ vars.SECURITY_PASS_LINE_OWNERSHIP || 'project-lines' }}`,
    `SECURITY_PASS_OWNERSHIP_CONTEXT_LINES: ${{ vars.SECURITY_PASS_OWNERSHIP_CONTEXT_LINES || '3' }}`,
    `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP: ${{ vars.SECURITY_PASS_ADVISORY_FOLLOWUP_CAP || '5' }}`
    next to the existing three security-pass vars.
    `tests/test_orchestrate_poll_workflow_contract.py` asserts the new env
    rows.
11. `ensure_security_pass_state_fields` (≈ lines 4428–4452): normalize
    `security_pass_last_audited_sha` (string, default `""`),
    `security_pass_prior_findings` (array of objects with the eight finding
    keys, default `[]`), `security_pass_waived_findings` (array of objects
    with `finding_id` string and optional `file`, `line`, category; default
    `[]`), `security_pass_followup_issues` (array of
    `{finding_id, issue}` with positive integer issue; default `[]`).
12. New `render_security_pass_findings_table <findings_file|json> <out_md>`
    (python, placed directly above `create_security_pass_fix_issue`):
    extracts the table rendering that `create_security_pass_fix_issue`
    (≈ lines 4689–4760) builds inline, unchanged in output.
    `create_security_pass_fix_issue` calls it; a byte-identical fix-issue
    body is asserted by the existing
    `test_security_pass_findings_create_one_consolidated_managed_fix_issue`.
13. `security_pass_terminal_failure` (≈ lines 4525–4556): accept the
    findings file as a fourth argument; the exhaustion comment appends the
    rendered table and one line: "To accept a finding as a known risk,
    comment `/security-pass-waive <finding_id> [<finding_id> …]`; to re-run
    after fixing, comment `/re-security-pass`." The Telegram text is
    unchanged.
14. New `security_pass_rebind_if_no_new_project_lines <head_sha>
    <merge_base_sha>` called from `run_security_pass_inline` immediately
    before the existing `prior_security_status = passed` budget reset
    (≈ line 4907): returns 0 and rebinds (state `security_pass_status =
    "passed"`, `security_pass_head_sha = head`, reconcile body,
    `post_state_comment`, log `SECURITY_PASS_REBOUND tracking_issue=…
    from=<last_audited> to=<head> reason=no_new_project_lines`) only when
    `security_pass_last_audited_sha` is non-empty and an ancestor of the head
    and `git rev-list <head> ^<last_audited> ^<merge_base>` is non-empty and
    every listed commit has two or more parents and empty
    `git show --format= --cc <commit>` output. Any git error returns 1
    (fall through to a real audit).
15. `run_security_pass_inline` engine invocation (≈ lines 4985–5000): pass
    `SECURITY_AUDIT_LINE_OWNERSHIP="${SECURITY_PASS_LINE_OWNERSHIP}"`,
    `SECURITY_AUDIT_OWNERSHIP_CONTEXT_LINES="${SECURITY_PASS_OWNERSHIP_CONTEXT_LINES}"`,
    `SECURITY_AUDIT_NEW_SINCE_SHA` set to `security_pass_last_audited_sha`
    when non-empty, an ancestor of the head, and the prior status is
    `blocked` (a `passed` prior status already went through the rebind check
    and, on fall-through, reset its budget; it also passes the last audited
    SHA so only the new commits are scanned), and the two `*_IN` files
    written to `${RUNTIME_DIR}/security_pass_prior_${TRACKING_NUM}.json` and
    `${RUNTIME_DIR}/security_pass_waived_${TRACKING_NUM}.json` from state
    when the arrays are non-empty.
16. `run_security_pass_inline` result validation (≈ lines 5000–5040): extend
    the jq validator so `advisory_findings`, `verified_fixed_finding_ids`,
    and the three new counts are optional but type-checked when present
    (same per-row checks as `findings`).
17. `run_security_pass_inline` after head recheck (≈ lines 5058–5070): set
    `security_pass_last_audited_sha = head`; on `findings == 0` set
    `security_pass_prior_findings = []`; on findings set
    `security_pass_prior_findings = .findings`; when
    `verified_fixed_finding_ids` is non-empty log
    `SECURITY_PASS_VERIFIED_FIXED tracking_issue=… ids=<comma list>`; call
    `create_security_pass_advisory_followups` when `advisory_findings` is
    non-empty and the cap is > 0.
18. New `create_security_pass_advisory_followups <findings_file>
    <integration_branch>`: one paginated
    `GET repos/${GITHUB_REPOSITORY}/issues?state=open&labels=ai:security&per_page=100`
    (same `gh_retry gh api --paginate` shape as
    `resolve_security_pass_fix_successor`, which is the audited existing
    call for managed issues; a list of `ai:security` issues is a different
    label scope and is not otherwise fetched on this tick); skip any finding
    whose `<!-- ai:security-finding:<id> -->` marker is present in an open
    body or whose id is in `security_pass_followup_issues`; create up to the
    cap with `ensure_label_exists "ai:security"`, title
    `[security-pass] advisory: <id>: <severity> <file>:<line>`, body =
    marker line, `Refs #<tracking>`, `Generated by the orchestrator security
    pass (advisory, non-blocking) for integration branch <branch> at <head>`,
    then the same Category / Severity / Confidence / Location / Exploit
    scenario / Recommendation sections the weekly audit writes; append
    `{finding_id, issue}` to `security_pass_followup_issues`; log
    `SECURITY_PASS_ADVISORY_FOLLOWUP_CREATED tracking_issue=… finding=… issue=…`.
    A list failure or create failure logs `::warning::` and returns 0.
19. Tracking comments: the existing "🔒 Project security pass blocked" and
    the clean-pass path gain one trailing sentence when advisories were filed
    this run: "N advisory finding(s) on pre-existing code were filed as
    non-blocking follow-ups: #a, #b." (No extra comment; the existing
    `post_tracking_comment` call carries it.)
20. Docs: `README.md` env tables (rows for the three new poller vars, budget
    default 5 in all three `MAX_SECURITY_PASS_CYCLES` rows), append bullet
    `12f.` under `anchor:orchestrator-pipeline-steps` describing project-line
    scope, incremental re-audit, rebinding, and advisory follow-ups;
    `agents.md` security-pass paragraph (new state fields, budget 5);
    `docs/how-it-works.md` state diagram note for rebinding.
21. `tests/test_orchestrate_poll_process.py`: new tests under Tests; update
    `test_security_pass_cycle_exhaustion_terminalizes_project` and
    `test_security_pass_exhaustion_still_terminalizes_from_blocked_status`
    for the new default of 5 (or pin `MAX_SECURITY_PASS_CYCLES=3` in the
    test env) and assert the table in the exhaustion comment.
22. `changelog.d/security-pass-convergence-poller.md` (`changed`).

### Phase 3 — waive command

23. `scripts/orchestrate_poll_process.sh`, directly after the
    `/re-security-pass` block (≈ lines 15332–15375): add a
    `/security-pass-waive` scan that runs when
    `PROJECT_STATUS = failed` with `ai:security-pass-failed`, or
    `PROJECT_STATUS = security-pass-fixing`. Boundary logic is copied from
    `/re-security-pass` with the marker `<!-- security-pass-waive-dedup:`
    added to the boundary set. The comment must match
    `^\s*/security-pass-waive(\s+[A-Za-z0-9][A-Za-z0-9._-]{0,120})+\s*$`,
    its `author_association` must be in `OWNER`, `MEMBER`, `COLLABORATOR`
    (same predicate as the existing OWNER/MEMBER/COLLABORATOR check at
    ≈ line 13874), and `user.type` must not be `Bot` (`COMMENTS` is the
    merged raw REST payload, so both fields are already present). Rejections
    log `SECURITY_PASS_WAIVE_REJECTED tracking_issue=…
    comment=<id> reason=<author|format>` and post a short dedup-marked
    comment so the same comment is not re-evaluated.
24. On acceptance: for each id, take `{finding_id, file, line,
    owasp_or_stride_category}` from `security_pass_prior_findings` when
    present, else `{finding_id}` alone (exact-match only; the ack says so);
    upsert into `security_pass_waived_findings` by id; remove the ids from
    `security_pass_prior_findings`. In the failed state also apply the same
    reset `/re-security-pass` applies (`status = security-pass`, `cycle = 0`,
    `security_pass_status = pending`, cleared head SHA and active issues) and
    run the audit on the same tick through the existing
    `run_security_pass_inline` call; in the fixing state only persist. Post
    `<!-- security-pass-waive-dedup:<comment-id> -->` + "## ✅ Security-pass
    findings waived" listing ids and their match mode, `tg_notify … WARNING`,
    log `SECURITY_PASS_WAIVED tracking_issue=… ids=… by=<login>`,
    `reconcile_tracking_body_after_security_pass_transition`,
    `post_state_comment`.
25. Tracking body: the `### Security pass` block gains a
    `- Waived findings: <n>` row (rendered only when non-zero; the render
    reads state only, so this is part of the existing
    `reconcile_tracking_issue_body_from_state` template).
26. Docs: `docs/how-it-works.md` command table row for
    `/security-pass-waive`; `README.md` bullet `12f.` sentence on waivers;
    `agents.md` state field note.
27. `tests/test_orchestrate_poll_process.py`: new tests under Tests.
28. `changelog.d/security-pass-convergence-waive.md` (`added`).

## Files & Modules

- `scripts/security_audit.sh` — P1
- `prompts/mode-security-audit.txt` — P1
- `prompts/_templates/mode-security-audit.txt` — P1
- `tests/test_security_audit_workflow_contract.py` — P1
- `changelog.d/security-pass-convergence-engine.md` `[new]` — P1
- `scripts/orchestrate_poll_process.sh` — P2, P3
- `.github/workflows/orchestrate_poll.yml` — P2
- `README.md` — P2, P3
- `agents.md` — P2, P3
- `docs/how-it-works.md` — P2, P3
- `tests/test_orchestrate_poll_process.py` — P2, P3
- `tests/test_orchestrate_poll_workflow_contract.py` — P2
- `changelog.d/security-pass-convergence-poller.md` `[new]` — P2
- `changelog.d/security-pass-convergence-waive.md` `[new]` — P3

## Tests

Unit / contract (sandbox git repos, fake `codex`, as the existing suites do):

- P1, engine:
  - default env: findings-json output byte-identical to the current fixture
    (regression guard for the weekly path and for P2-before-P1 ordering);
  - `project-lines`: pre-existing line → `advisory_findings`; new line →
    `findings`; line within 3 of a new line → `findings`; line 4 away →
    `advisory_findings`;
  - `NEW_SINCE_SHA`: older project line suppressed
    (`suppressed_older_than_base`) unless its id is in the prior list; prior
    id absent from model output → `verified_fixed_finding_ids`;
  - waivers: exact id suppressed; same file+category within window
    suppressed; different category not suppressed;
  - blame failure classifies as blocking (fail closed); `project-lines`
    without `DIFF_BASE` or in `issues` mode fails preflight side-effect-free;
  - prompt context contains the prior/accepted blocks only when the inputs
    are set.
- P2, poller:
  - clean sync merge after `passed` → `SECURITY_PASS_REBOUND`, no codex call,
    pass valid at the new head; evil merge or non-merge commit → real audit;
  - engine env carries `LINE_OWNERSHIP`, context, `NEW_SINCE_SHA` only when
    ancestor, and the prior/waived files only when non-empty;
  - old-engine output (no additive keys) still blocks/passes as today;
  - `security_pass_last_audited_sha` and `security_pass_prior_findings`
    persist and clear correctly across blocked → merged fix → re-audit →
    passed;
  - advisory follow-ups: created ≤ cap, deduped by marker and by state,
    label ensured, failure is fail-open, tracking comment names them;
  - exhaustion comment contains the findings table and the waive usage;
  - `MAX_SECURITY_PASS_CYCLES` default 5 in script and workflow env.
- P3, command:
  - OWNER human in failed state → persisted rows with file/line/category,
    loop reset, ack with marker, re-audit env includes the waived file;
  - MEMBER in fixing state → persisted, no reset, ack;
  - bot author / `CONTRIBUTOR` / malformed id → rejected log, no state
    change, dedup comment posted once;
  - unknown id → persisted as exact-match only, ack says so;
  - repeated identical comment after the marker → ignored.

End-to-end (manual, after P2 merges): watch the next project that reaches
the gate on a consumer repo; confirm `SECURITY_PASS_REBOUND` on the first
sync merge after a clean pass, and that a fix cycle's re-audit logs
`SECURITY_PASS_VERIFIED_FIXED` for the previous rows.

## Risks & Mitigations

- **A pre-existing vulnerability that the project makes newly reachable is
  advisory, not blocking.** ACCEPTED — the project's own lines still block,
  the advisory is filed as `ai:security`, and the weekly audit continues to
  cover the default branch; the operator can re-add strictness with
  `SECURITY_PASS_LINE_OWNERSHIP=file`.
- **The model ignores the re-emit instruction and renames a still-open prior
  finding.** The renamed finding is still on a project line; it blocks only if
  that line is newer than the last audit, otherwise it is suppressed as older
  than base. Mitigation: the prior block lists file:line and the rule is
  explicit; the fuzzy waiver logic is not applied to priors on purpose so a
  renamed-but-open finding at a new line still blocks. Residual risk ACCEPTED
  — it trades a possible miss for guaranteed convergence, which the operator
  chose (Q4: A).
- **Blame on very large files per finding.** `git blame -L` on a 55k-line
  file is sub-second; findings per run are single digits. No mitigation
  needed beyond the per-commit ancestry cache.
- **Rebind on a merge that carries conflict-resolution edits.** `--cc` output
  is non-empty for any evil merge, so it is audited. A merge with only
  whitespace resolution still shows in `--cc`; it is audited, which is the
  safe direction.
- **Advisory issue noise on consumer repos.** Cap of 5 per run, marker
  dedupe against open issues, and the repo var can be set to `0`.
- **Old poller + new engine.** The engine never enables ownership mode on
  its own, so nothing changes. **New poller + old engine.** Additive keys
  absent → today's behaviour. Covered by tests in both phases.
- **A waiver hides a finding that a later fix cycle reintroduces
  elsewhere.** Fuzzy matching is bounded to the same file, same category,
  and the context window; anything else surfaces normally.
- **Budget default change alters in-flight projects.** A project currently
  at cycle 3/3 gains two more cycles on its next audit; none is terminalized
  by the change. ACCEPTED.
- **Bot-authored waive comments.** The tick's `COMMENTS` array is the merged
  raw REST payload of `issues/<n>/comments` (pages joined with
  `jq -s 'add'`), so `user.type` and `author_association` are already
  present; no projection change and no new API call are needed.

## Rollout

- All three phases ship default-on for the security pass with per-feature
  kill switches: `SECURITY_PASS_LINE_OWNERSHIP=file` restores per-file
  scope, `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP=0` stops advisory issues,
  `MAX_SECURITY_PASS_CYCLES=3` restores the old budget, and
  `ENABLE_SECURITY_PASS=false` remains the global kill switch. The weekly
  audit is unaffected in every phase.
- Consumers receive each phase on the next `@stable` sync of `scripts/`,
  `prompts/`, and the reusable `orchestrate_poll.yml`; no wrapper or repo-var
  action is required. The `ai:security` label is created on demand by
  `ensure_label_exists`.
- Rollback per phase: revert the phase PR. State fields written by a later
  phase are tolerated (normalized to defaults) by an earlier one.
- No migration: `ensure_security_pass_state_fields` defaults the new fields
  on first read, so projects already in `security-pass-fixing` (#3965, #3955)
  and `ai:security-pass-failed` (#3928) pick up the new behaviour on their
  next tick. For those, the first post-upgrade audit has no
  `security_pass_last_audited_sha`, so it runs a full project-line audit and
  seeds the field.

## References

- `docs/completed/orchestrator-security-pass-gate-plan.md` (original gate
  design, decisions D1–D8)
- coding-workflows #3933 (gate implementation project), #3965 (looping
  project), #4003 (successor adoption), #4013 (budget reset + body render)
- tele-funtoken-msg-scoring #3928, #3955 (consumer projects that hit the
  loop), fix issues #4009, #4021, #4023, #4043
- `scripts/security_audit.sh` post-filter (≈ line 782) and
  `security_audit_append_prompt_context` (≈ line 118)
- `scripts/orchestrate_poll_process.sh` `run_security_pass_inline`
  (≈ line 4850), `security_pass_terminal_failure` (≈ line 4525),
  `create_security_pass_fix_issue` (≈ line 4689), `/re-security-pass`
  (≈ line 15332), OWNER/MEMBER/COLLABORATOR predicate (≈ line 13874)
- `scripts/security_audit.sh` weekly follow-up body and marker (≈ line 1075)
