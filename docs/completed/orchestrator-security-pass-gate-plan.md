# Mandatory Orchestrator Security Pass Gate

## Summary

Add a mandatory, non-skippable end-of-project security/bugfix pass to the AI
orchestrator: after the judge declares a project `complete` and before any
route can reach `status=complete` or the final integration→default squash
merge, the poller runs an inline LLM security audit over the whole composed
integration-branch diff, and every surviving finding blocks completion until
fixed through a bounded fix loop cloned from the proven
`validating`/`validation-fixing` mechanism. Alongside the gate, the plan
hardens the shift-left prompts (`mode-plan.txt`, `mode-implement.txt`,
`mode-judge.txt`, `review-consolidator.txt`) with security instructions and a
shared money-handling lens, because many consumer projects move money and
security issues currently leak through to the default branch.

## Automation & Wiring (§18.E)

- **New script vs extension:** no new standalone script. The pass extends
  `scripts/security_audit.sh` (new machine-readable project-pass mode) and
  `scripts/orchestrate_poll_process.sh` (new state-machine phase). No script
  requires manual invocation at any point.
- **Scheduler entry point:** the existing orchestrator poller —
  `.github/workflows/orchestrate_poll.yml`, driven by
  `internal-orchestrate-poll.yml` (cron `*/5 * * * *`) in this repo and by the
  synced `workflow-templates/ai-orchestrate-poll.yml` wrapper in consumer
  repos. The pass runs inline inside the existing poll tick, judge-style
  (`codex exec`), exactly where the judge already runs. No new workflow file,
  no new cron surface, no new consumer wrapper.
- **Long-running supervisor:** none required. The poller is the existing
  supervisor-equivalent; this plan only adds states to its state machine.
- **DB operations:** none. No MongoDB collections, indexes, or contracts are
  touched (§10 not applicable).
- **Future-removal registry (§18.F):** no single-use or long-running scripts
  are introduced, so no `docs/scripts-pending-removal.md` entries are needed.

## Context

Research at the current HEAD established the gap this plan closes:

- An orchestrator project's happy path reaches `status=complete` with **zero
  security review of the composed result**. The end-of-project judge is
  skipped entirely on clean projects when `ENABLE_CLEAN_WAVE_JUDGE_SKIP=true`
  (the default): `scripts/orchestrate_poll_process.sh` synthesizes
  `JUDGE_STATUS="complete"` (`clean_project_completion_skip`, near lines
  17243–17258) without any LLM call. Even when the judge runs,
  `prompts/mode-judge.txt`'s only security-adjacent criteria (6–7) cover
  implicit-execution/capability drift, not application security.
- The validate stage (`validate.yml`, `scripts/validate_process.sh`,
  `scripts/validate_driver.sh`) is a runtime behavioural harness (docker
  compose + TAP) and contains no security content at all.
- The weekly `security-audit.yml` never sees a project before it ships: the
  orchestrator never dispatches it, it runs Sundays against the default
  branch (so up to 7 days post-merge), and its output is capped follow-up
  issues, never a gate. Its consumer wrapper `ai-security-audit.yml` exists
  only in the `full` install profile — `standard`-profile repos that run the
  orchestrator have no security audit wrapper at all.
- Per-PR review does have security as lens #1
  (`prompts/review-reviewer-checklist.txt`), but each wave PR is reviewed in
  isolation; vulnerabilities composed across waves are invisible to it. The
  integration branch at end-of-project is the only place the whole project
  diff exists as one reviewable unit.
- Shift-left is near-empty: `prompts/mode-implement.txt` contains no security
  guidance (its one "security" match is a changelog category name);
  `prompts/mode-plan.txt` mentions security once, incidentally; the
  consolidator's lens list drops the reviewer checklist's
  `IMPLICIT-EXECUTION & TRUST-BOUNDARY RISKS` lens, so such reviewer findings
  have no consolidator lens to land under.
- Placement subtlety that shapes the design: `mark_validation_complete()`
  performs the integration→default squash merge
  (`finalize_integration_merge_if_needed`, near line 8308) **before** writing
  `status=complete` (near line 8370). A pass inserted at the `complete` write
  would audit code already live on the default branch. The gate therefore
  sits earlier — between judge-`complete` and validation dispatch — while the
  integration branch is still unmerged.

Binding project rules: §5 (extend existing mechanisms), §6 (no renames; all
new identifiers verified unique), §14 (consumer propagation via `@stable`
sync), §15 (GitHub API call hygiene), §18 (no manual scripts; wire into the
existing scheduler), §19 (no auto-close keywords against
`ai:orchestrator-tracking` issues — all linkage below uses `Refs #N`), §20
(changelog fragments, never `CHANGELOG.md` directly).

## Decisions

### D1 — Execution vehicle: inline in the poller, judge-style

- **Chosen:** the pass runs as a `codex exec` call inside
  `scripts/orchestrate_poll_process.sh`, on the poller's existing
  integration-branch checkout, reusing `scripts/security_audit.sh`'s
  finding-validation, FP-exclusion, and confidence-gate logic through a new
  machine-readable mode.
- **Alternatives considered:** (b) a dispatched reusable workflow like
  validation, with a new `ai-security-pass` consumer wrapper added to the
  `standard` and `full` profiles; (c) dispatching the existing weekly
  `ai-security-audit.yml` wrapper.
- **Why:** (b) adds a workflow + wrapper + profile change and a propagation
  race for every existing consumer; (c) silently provides no pass on
  `standard`-profile repos, where the wrapper does not exist. Inline
  execution works everywhere the poller works (support scripts and prompts
  are staged from `@stable`), mirrors how the judge already executes, and
  needs no dispatch plumbing.

### D2 — Position: between judge-`complete` and validation dispatch

- **Chosen:** judge `complete` → `security-pass` → (fix loop if findings) →
  clean → `validating` → `ai:validated` → final integration→default merge →
  `complete`.
- **Alternatives considered:** after validation passes but before the final
  merge; in parallel with validation.
- **Why:** security fixes are code changes; running the pass first means
  runtime validation exercises the post-fix code instead of security-fix
  commits landing unvalidated. Parallel execution complicates the state
  machine for no wall-clock benefit (both loops can create fix issues).

### D3 — Blocking threshold: every surviving finding blocks

- **Chosen:** every finding that survives strict validation, the
  false-positive exclusion catalog, and the confidence gate blocks project
  completion, regardless of severity. No severity carve-out; no non-blocking
  follow-up lane for `medium`/`low`.
- **Alternatives considered:** critical+high block with medium/low filed as
  non-blocking `ai:security` follow-ups; critical-only blocking.
- **Why:** operator decision (clarification `Q3: B`) — the consumer projects
  handle money, and the confidence gate plus FP catalog are the intended
  noise filters, not severity.

### D4 — Confidence gate: dedicated env var, default 8

- **Chosen:** `SECURITY_PASS_CONFIDENCE_GATE` (default `8`), sharing the
  semantics of the weekly audit's `SECURITY_AUDIT_CONFIDENCE_GATE` and the
  same `scripts/security_audit_fp_exclusions.json` catalog, but tunable
  independently.
- **Alternatives considered:** reusing `SECURITY_AUDIT_CONFIDENCE_GATE`
  directly; lowering the gate to 6; severity-dependent gates.
- **Why:** consistency with a filter that is already tuned, while decoupling
  the blocking gate's knob from the weekly reporting audit's knob so one can
  move without the other.

### D5 — Fix loop: mirror validation exactly

- **Chosen:** one consolidated fix issue per cycle (labeled
  `ai:clarification` + `ai:orchestrator-managed`, carrying the standard
  orchestrator metadata block), flowing through the normal
  clarify→plan→implement→review pipeline; cycles bounded by
  `MAX_SECURITY_PASS_CYCLES` (default `3`); on exhaustion the project goes
  terminal `ai:security-pass-failed` and waits for a human
  `/re-security-pass` comment, mirroring `/revalidate`.
- **Alternatives considered:** one issue per finding; judge-style direct
  `[orchestrator-fix]` commits to the integration branch.
- **Why:** the validating/validation-fixing loop is the proven,
  already-tested pattern in this exact codebase, including its
  stale-redispatch guards, cycle budgets, and recovery command; per-finding
  issues multiply API calls (§15) and wave bookkeeping; direct commits skip
  the review pipeline that is itself a security control.

### D6 — Rollout: four phases, dark launch, final flip

- **Chosen:** Phase 1 prompt hardening; Phase 2 audit-engine project-pass
  mode (inert); Phase 3 poller state machine behind `ENABLE_SECURITY_PASS`
  default `false`; Phase 4 flips the default to `true` and finishes docs.
- **Alternatives considered:** three phases with the flag default `true`
  from the state-machine phase.
- **Why:** every merge in this pipeline lands in production directly; a
  default-off state-machine phase lets the pass be exercised on a chosen
  project (repo var override) before it gates every project. The flip phase
  carries a documented precondition (see Risks).

### D7 — Money lens: shared reference file

- **Chosen:** new `prompts/references/security-money-lens.txt`, interpolated
  as `{{REFERENCE_SECURITY_MONEY_LENS}}` into the pass prompt,
  `mode-plan.txt`, and `mode-implement.txt`.
- **Alternatives considered:** duplicating the lens text inline in each
  prompt; omitting the money lens.
- **Why:** one source of truth, additive (§6-safe), and the
  `prompts/references/` + render-contract mechanism already exists for
  exactly this shape (`severity-classification.txt` precedent).

### D8 — Harden `unattended_system_instructions.md` too

- **Chosen:** add a short additive security/money subsection near its Core
  Priorities section, since every unattended phase loads that file as system
  context.
- **Alternatives considered:** touching only the four mode/review prompts.
- **Why:** operator decision (`Q8: A`); a single additive paragraph reaches
  all codex-driven phases at once without renumbering or restructuring
  existing sections.

## Goals

- No orchestrator project can reach `status=complete` — via any of the four
  completion routes — without a security pass over the composed
  integration-branch diff having returned zero surviving findings, when
  `ENABLE_SECURITY_PASS=true`.
- The pass has **no clean-skip analogue**: the
  `clean_project_completion_skip` path routes into the pass rather than
  around it.
- Surviving findings feed a bounded fix loop (consolidated fix issue →
  normal pipeline → re-pass), with terminal `ai:security-pass-failed` +
  Telegram alert + `/re-security-pass` human recovery on budget exhaustion.
- `mode-plan.txt` requires a "Security considerations" plan section;
  `mode-implement.txt` carries a concrete security checklist;
  `mode-judge.txt` gains an application-security criterion; the consolidator
  gains a trust-boundary lens matching the reviewer checklist.
- A shared money-handling lens reaches plan, implement, and the pass prompt.
- The whole mechanism runs from the existing poller cron with no operator
  action, no new workflow, and no new consumer wrapper (§18).

## Non-goals

- No change to the weekly `security-audit.yml` cadence, its tracker issue,
  or its 3-per-week follow-up cap — it remains the drift safety net for
  non-orchestrator changes.
- No change to per-PR review reviewer rosters, tiers, or autofix budgets.
- No deterministic scanners (semgrep/bandit/trivy/CodeQL) — this plan gates
  on the LLM audit engine that already exists; scanner integration is a
  separate future decision.
- No change to `project_complete` semantics in
  `orchestrate_lib.py::cmd_check_wave_status` — the gate lives in the
  poller's status transitions, not in the wave-completion predicate (avoids
  regressing the `ahead_by` deadlock class fixed previously).
- No change to the per-issue (non-orchestrator) pipeline: single-issue flows
  outside orchestrator projects keep their current review-only posture.
- No MongoDB or data-model work.

## Constraints

- **§6 naming immutability:** nothing is renamed or removed. All new
  identifiers were checked against the codebase for collisions and are
  unique: env vars `ENABLE_SECURITY_PASS`, `MAX_SECURITY_PASS_CYCLES`,
  `SECURITY_PASS_CONFIDENCE_GATE`, `SECURITY_AUDIT_OUTPUT_MODE`,
  `SECURITY_AUDIT_DIFF_BASE`, `SECURITY_AUDIT_DIFF_HEAD`,
  `SECURITY_AUDIT_FINDINGS_OUT`; state statuses `security-pass`,
  `security-pass-fixing`; state fields `security_pass_cycle`,
  `security_pass_status`, `security_pass_active_fix_issues`; labels
  `ai:security-pass`, `ai:security-pass-fixing`, `ai:security-pass-failed`;
  command `/re-security-pass`; template var
  `REFERENCE_SECURITY_MONEY_LENS`; log prefixes `SECURITY_PASS_STARTED`,
  `SECURITY_PASS_CLEAN`, `SECURITY_PASS_BLOCKED`,
  `SECURITY_PASS_FIX_ISSUE_CREATED`, `SECURITY_PASS_FAILED`,
  `SECURITY_PASS_SKIPPED_DISABLED`. The existing `SECURITY_AUDIT_*` family
  is extended additively, never repurposed.
- **§15 API hygiene:** the pass reads code from the local checkout (zero API
  calls for analysis). Per cycle it adds at most: one issue-create call for
  the consolidated fix issue, label writes through the existing
  `set_tracking_phase_label` / label helpers, and state persistence through
  the existing `post_state_comment` path. Fix-issue status during
  `security-pass-fixing` is read through the existing batched helpers /
  cycle-local caches (`_fetch_candidate_issue_details_graphql` pattern), not
  new per-iteration calls.
- **§19:** the consolidated fix issue body and all comments reference the
  tracking issue as `Refs #N` only. This plan document itself contains no
  auto-close keyword against any issue.
- **§20:** each implementation phase ships its own
  `changelog.d/<pr>-<slug>.md` fragment; `CHANGELOG.md` is never edited.
- **§14:** no new consumer wrapper and no `.github/ai/consumer_repos.json`
  change. Consumers receive everything through the existing `@stable` sync
  of `scripts/`, `prompts/`, and the reusable `orchestrate_poll.yml`.
- **README anchor discipline:** orchestrator pipeline-step docs are
  append-only under the `anchor:orchestrator-pipeline-steps` comment —
  new prose is added as suffixed bullets, never by renumbering.
- **Prompt template mirrors:** where a prompt has an include-based source in
  `prompts/_templates/`, both the runtime body and the template mirror are
  updated in the same commit.

## Approach

One new phase pair in the poller state machine, one new output mode in the
existing audit engine, and additive prompt hardening:

```
waves … → judge complete ─┐
  (clean-skip also lands here — no skip analogue for the pass)
                          ▼
              status = security-pass
       inline codex exec audit of merge-base..integration-head
                          │
        ┌── findings ─────┴──── zero findings ──┐
        ▼                                       ▼
status = security-pass-fixing         existing path unchanged:
one consolidated fix issue            ENABLE_VALIDATION=true →
(clarify→plan→implement→review)         status = validating → … →
fix batch merged → re-run pass          mark_validation_complete →
(≤ MAX_SECURITY_PASS_CYCLES,            final squash merge →
 else ai:security-pass-failed           status = complete
 + Telegram + /re-security-pass)      ENABLE_VALIDATION=false →
                                        finalize merge → complete
```

The audit engine is `scripts/security_audit.sh` in a new additive mode:
`SECURITY_AUDIT_OUTPUT_MODE=findings-json` suppresses all tracker-issue and
follow-up-issue bookkeeping and instead writes the validated, FP-filtered,
confidence-gated findings array to `SECURITY_AUDIT_FINDINGS_OUT`. Diff scope
comes from `SECURITY_AUDIT_DIFF_BASE`/`SECURITY_AUDIT_DIFF_HEAD`
(merge-base of the integration branch and the default branch → integration
head), reusing the existing incremental-scope machinery and its
out-of-scope-finding suppression. The prompt is the existing
`prompts/mode-security-audit.txt` plus two additive runtime context blocks:
the project spec (tracking-issue body) and the money lens reference.

All four routes to `status=complete` are gated: the judge-`complete` arm
(both `ENABLE_VALIDATION` values), the externally-merged-PR recovery path,
and the merge-conflict completion path. Wherever a route would previously
write `complete` (or dispatch validation) it first requires
`security_pass_status == "passed"` for the current integration head SHA;
otherwise it transitions to `security-pass` and runs the pass on that tick.
Recording the audited head SHA in state makes the gate idempotent and
self-clearing: fix commits change the head, which invalidates the previous
pass result and forces a re-pass.

## Phases & Merge Strategy

Executed by the AI orchestrator, one PR per phase, each landing directly in
production.

1. **Phase 1 — Shift-left prompt hardening.**
   Scope: security sections/checklists in `mode-plan.txt`,
   `mode-implement.txt`, `mode-judge.txt`; ninth consolidator lens; new
   `prompts/references/security-money-lens.txt`; additive subsection in
   `unattended_system_instructions.md`; contract YAML updates; template
   mirrors; tests.
   Done when: prompts render with the new blocks, contract tests pass, and
   no existing lens/section text is reordered or reworded.
   Rollback: revert the PR — prompts return to prior text; nothing else
   references the new reference file at this point (interpolation of
   `{{REFERENCE_SECURITY_MONEY_LENS}}` ships inside this same phase, so the
   file and its consumers land together).

2. **Phase 2 — Audit-engine project-pass mode (inert).**
   Scope: `scripts/security_audit.sh` gains `SECURITY_AUDIT_OUTPUT_MODE`
   (default `issues`, preserving current behaviour byte-for-byte),
   `SECURITY_AUDIT_DIFF_BASE`/`SECURITY_AUDIT_DIFF_HEAD`,
   `SECURITY_AUDIT_FINDINGS_OUT`, and the two additive prompt-context
   blocks; contract tests.
   Done when: default-mode behaviour is provably unchanged (existing tests
   green) and the new mode emits schema-stable findings JSON in a sandbox
   test. Nothing calls the new mode yet.
   Rollback: revert the PR — the weekly audit keeps its current path.

3. **Phase 3 — Poller state machine, dark-launched.**
   Scope: `security-pass`/`security-pass-fixing` statuses, inline pass
   invocation, fix loop, all four bypass closures, no-skip routing, labels,
   state fields, status rendering, `/re-security-pass`, Telegram alerts,
   `orchestrate_poll.yml` env plumbing — all behind `ENABLE_SECURITY_PASS`
   default `false`; README/agents.md docs for the flagged feature; tests.
   Done when: with the flag off, the full poller test suite is
   behaviour-identical; with the flag on in sandbox tests, every completion
   route provably passes through the gate and the fix loop advances/exhausts
   correctly.
   Rollback: set `ENABLE_SECURITY_PASS=false` (repo var) instantly; or
   revert the PR.
   Note: if Phase 3 merges before Phase 2, the flag default `false` keeps it
   inert and safe; the runtime additionally fails **closed** (treats the
   pass as blocked, logs `SECURITY_PASS_FAILED reason=engine_unavailable`,
   alerts) if the flag is on but the engine mode is absent — a
   misconfiguration must never silently skip a mandatory gate.

4. **Phase 4 — Flip the default to `true` + closing docs.**
   Scope: `ENABLE_SECURITY_PASS` default flips to `true` in
   `orchestrate_poll.yml`; README "How it works" append-only bullet updated
   from "flagged" to default-on wording; changelog fragment.
   Done when: a fresh orchestrator project in this repo (or a designated
   consumer) runs the pass without any repo-var override.
   Rollback: single-line default flip back to `false`.
   Precondition: Phases 2 and 3 live on the branch consumers sync from —
   recorded as an ACCEPTED risk below, per the clarification round.

## Implementation Steps

### Phase 1 — prompt hardening

1. `prompts/references/security-money-lens.txt` **[new]** — the lens:
   idempotency of money mutations (unique-index-backed keys, §10.E
   patterns), atomic balance updates / lost-write races, decimal-vs-float
   for money amounts, TOCTOU on check-then-debit sequences, replay
   protection on payment/webhook endpoints, authorization on every
   money-touching surface, no secrets or credentials in code or logs.
2. `prompts/mode-plan.txt` (and its `prompts/_templates/` mirror if one
   exists) — add a required "Security considerations" section to the plan
   output contract: trust boundaries touched, authz for new surfaces, input
   validation, abuse cases alongside the existing edge-case requirement;
   interpolate `{{REFERENCE_SECURITY_MONEY_LENS}}`.
3. `prompts/mode-implement.txt` (and mirror) — add a short pre-commit
   security checklist block (validate external input at boundaries,
   parameterized queries, authz on new endpoints, no secret material in
   code, fail-closed error paths) plus the money-lens reference.
4. `prompts/mode-judge.txt` (and mirror) — add application-security check
   criterion (injection, authz gaps, secret leaks, unsafe deserialization
   in the merged code) after the existing criteria 6–7, without renumbering
   existing criteria text that other assets reference.
5. `prompts/review-consolidator.txt` — append a ninth lens
   `IMPLICIT-EXECUTION & TRUST-BOUNDARY RISKS`, text aligned with the
   reviewer checklist's lens, after the existing eighth (DIATAXIS) lens; the
   first seven lens names and order stay byte-for-byte stable per the
   documented contract in `agents.md`.
6. `unattended_system_instructions.md` — additive subsection under the Core
   Priorities area: security is priority 1 in concrete terms (the checklist
   themes above), money-handling correctness named explicitly. No
   renumbering of existing sections.
7. `prompts/contracts/*.yml` — register `REFERENCE_SECURITY_MONEY_LENS` as
   an allowed optional var for `mode-plan`, `mode-implement`, and
   `mode-security-audit`; `scripts/render_prompt.py` reference-injection
   table updated if references are enumerated there.
8. Tests — extend the existing prompt/contract test modules to pin: new
   sections present, first-seven consolidator lenses unchanged, reference
   file resolves, render succeeds for every touched prompt.
9. `changelog.d/<pr>-shift-left-security-prompts.md` **[new]**.

### Phase 2 — audit-engine project-pass mode

10. `scripts/security_audit.sh` — add `SECURITY_AUDIT_OUTPUT_MODE`
    (`issues` default | `findings-json`): in `findings-json` mode skip
    tracker-issue read/write, last-SHA markers, follow-up caps, and
    Telegram, and write the post-validation, post-FP-catalog,
    post-confidence-gate findings array (plus counts of suppressed
    findings) to `SECURITY_AUDIT_FINDINGS_OUT`.
11. `scripts/security_audit.sh` — add explicit diff-scope inputs
    `SECURITY_AUDIT_DIFF_BASE`/`SECURITY_AUDIT_DIFF_HEAD` that feed the
    existing incremental-scope file list and out-of-scope suppression;
    absent inputs preserve the current tracker-marker behaviour exactly.
12. `scripts/security_audit.sh` — additive prompt-context appenders: the
    project spec block (path to a file whose content is the tracking-issue
    body, supplied by the caller) and `{{REFERENCE_SECURITY_MONEY_LENS}}`
    injection for the pass invocation; the weekly path remains unchanged.
13. Tests — extend `tests/test_security_audit_workflow_contract.py`: default
    mode unchanged; `findings-json` mode emits schema-stable output;
    diff-scope inputs override marker-derived scope; confidence gate honors
    `SECURITY_PASS_CONFIDENCE_GATE` when the caller maps it in.
14. `changelog.d/<pr>-security-audit-findings-json-mode.md` **[new]**.

### Phase 3 — poller state machine (dark-launched)

15. `scripts/orchestrate_lib.py::build_tracking_state` — seed
    `security_pass_cycle: 0`, `security_pass_status: "pending"`,
    `security_pass_active_fix_issues: []`, `security_pass_head_sha: ""`.
16. `scripts/orchestrate_poll_process.sh` — new function
    `run_security_pass_inline`: resolve merge-base(default, integration
    head); write the tracking-issue body to a context file; invoke the
    Phase 2 engine mode under `scripts/codex_heartbeat.sh`, read-only
    sandbox, `WORKFLOW_EDITOR_MODEL`, `xhigh`; parse
    `SECURITY_AUDIT_FINDINGS_OUT`; record `security_pass_head_sha`; emit
    `SECURITY_PASS_STARTED` / `SECURITY_PASS_CLEAN` /
    `SECURITY_PASS_BLOCKED`. Engine absent or unparseable output with the
    flag on → fail closed (`SECURITY_PASS_FAILED
    reason=engine_unavailable`, Telegram, project stays gated).
17. `scripts/orchestrate_poll_process.sh` — new function
    `create_security_pass_fix_issue`: one consolidated issue per cycle,
    findings table in the body, standard orchestrator metadata block,
    labels `ai:clarification` + `ai:orchestrator-managed`, `Refs
    #<tracking>` linkage (§19), persisted into
    `security_pass_active_fix_issues`; emit
    `SECURITY_PASS_FIX_ISSUE_CREATED`.
18. Judge `complete)` arm (near lines 17686–17752) — when
    `ENABLE_SECURITY_PASS=true` and `security_pass_status != "passed"` for
    the current integration head: set `status="security-pass"`, run the
    pass this tick; on clean, fall through to the existing
    validation-dispatch / finalize logic unchanged; on findings, set
    `status="security-pass-fixing"` and create the fix issue. The
    `clean_project_completion_skip` synthesized verdict (near lines
    17243–17258) reaches this same arm and therefore the same gate — no
    skip analogue is added for the pass.
19. Bypass closures — apply the identical gate before the `complete` writes
    in: the external-finalize recovery path (near lines 13592–13598), the
    merge-conflict completion path (near lines 13668–13669), and the
    validation-disabled completion write (near line 17698). Also gate
    `mark_validation_complete()` defensively: if `ENABLE_SECURITY_PASS=true`
    and `security_pass_status != "passed"` for the current head, do not
    merge — transition back to `security-pass` (covers `/revalidate`-style
    entries that could otherwise skip the pass).
20. Status dispatch block — add `security-pass` and `security-pass-fixing`
    handling parallel to `validating`/`validation-fixing` (near lines
    13684–13986): `security-pass` re-runs the pass if the previous tick was
    interrupted; `security-pass-fixing` watches the fix issue via the
    existing batched status helpers, and when the batch is terminal-merged,
    increments `security_pass_cycle`, clears `security_pass_head_sha`, and
    re-runs the pass; on `security_pass_cycle >= MAX_SECURITY_PASS_CYCLES`
    with findings still present → `status="failed"` variant label
    `ai:security-pass-failed`, `SECURITY_PASS_FAILED` log, Telegram
    CRITICAL.
21. Poller guards — exclude the new statuses from the terminal
    short-circuit (near line 14244); add them to the integration-sync
    bypass list (near lines 13628–13634); add `/re-security-pass` comment
    handling mirroring `/revalidate` (near line 14098): resets
    `security_pass_cycle`, clears the failed state, re-enters
    `security-pass`.
22. Labels & rendering — `ai:security-pass`, `ai:security-pass-fixing`,
    `ai:security-pass-failed` in `scripts/ai_labels.py`,
    `scripts/label_helpers.sh`, `.github/ai/label_contract.v1.json`, and
    the `set_tracking_phase_label` mutually-exclusive phase set;
    `render_tracking_issue_body_from_state`,
    `format_wave_status_comment`, and `update_completion_status_comment`
    render the new phase states.
23. `.github/workflows/orchestrate_poll.yml` — env rows `ENABLE_SECURITY_PASS`
    (default `false` this phase), `MAX_SECURITY_PASS_CYCLES` (default `3`),
    `SECURITY_PASS_CONFIDENCE_GATE` (default `8`), each overridable via repo
    vars like the validation flags.
24. Docs — README: append-only bullet under
    `anchor:orchestrator-pipeline-steps` describing the flagged pass;
    variables table rows; `agents.md`: workflow-architecture list entry,
    new stable log prefixes registered in both the prose list and the
    `LOG_PREFIX.name=` block, env-contract rows.
25. Tests — `tests/test_orchestrate_poll_process.py` sandbox additions:
    flag-off behaviour identical (regression guard on every touched path);
    flag-on: gate blocks each of the four completion routes; clean-skip
    routes into the pass; fix-loop advance, head-SHA invalidation after fix
    merge, cycle exhaustion, `/re-security-pass` recovery; fail-closed on
    missing engine.
26. `changelog.d/<pr>-orchestrator-security-pass-phase.md` **[new]**.

### Phase 4 — default flip

27. `.github/workflows/orchestrate_poll.yml` — `ENABLE_SECURITY_PASS`
    default `true`.
28. README + `agents.md` — flagged wording updated to default-on;
    `changelog.d/<pr>-security-pass-default-on.md` **[new]**.

## Files & Modules

- `prompts/references/security-money-lens.txt` [new]
- `prompts/mode-plan.txt` (+ `prompts/_templates/` mirror where present)
- `prompts/mode-implement.txt` (+ mirror where present)
- `prompts/mode-judge.txt` (+ mirror where present)
- `prompts/review-consolidator.txt`
- `unattended_system_instructions.md`
- `prompts/contracts/` — contract YAMLs for the touched prompts
- `scripts/render_prompt.py` (only if references are enumerated there)
- `scripts/security_audit.sh`
- `scripts/orchestrate_poll_process.sh`
- `scripts/orchestrate_lib.py`
- `scripts/ai_labels.py`, `scripts/label_helpers.sh`,
  `.github/ai/label_contract.v1.json`
- `.github/workflows/orchestrate_poll.yml`
- `README.md`, `agents.md`
- `tests/test_security_audit_workflow_contract.py`,
  `tests/test_orchestrate_poll_process.py`, prompt/contract test modules
- `changelog.d/` — one fragment per phase [new]

## Data Model / Index Changes

None. No MongoDB collections, indexes, or `/db/contracts/*` files are
touched.

## Tests

- **Unit/contract (Phase 1):** prompt render tests for every touched
  prompt; byte-stability pins for the first seven consolidator lenses;
  reference-file resolution.
- **Unit/contract (Phase 2):** `security_audit.sh` default-mode
  no-behaviour-change tests (existing suite must pass untouched); new-mode
  JSON schema, diff-scope override, gate/catalog application.
- **Integration (Phase 3):** sandboxed poller tests (the
  `_make_poller_sandbox` pattern) for every state transition listed in step
  25 — these are the load-bearing proof that the gate is genuinely
  non-bypassable, including the clean-skip route and all four completion
  routes.
- **End-to-end (Phase 4):** one real orchestrator project run in this repo
  after the flip, observed to pass through `ai:security-pass` before
  `ai:validated`; the nightly validation selftest continues green.
- **Manual:** none required (§18) — verification rides existing CI and the
  poller.

## Risks & Mitigations

- **False positives block money projects hard (Q3: B — everything
  blocks).** Mitigation: confidence gate ≥ 8, shared FP-exclusion catalog,
  bounded cycles with a terminal human backstop (`/re-security-pass`), and
  the catalog is extendable when a recurring FP class appears.
- **Phase 4 precondition on Phases 2+3.** `ACCEPTED — pending Phases 2 and 3
  being live on the synced branch` (explicitly accepted in the
  clarification round, `Q6: A`). Defense-in-depth: Phase 3's fail-closed
  engine check turns a mis-ordered flip into a loud blocked state, never a
  silent skip.
- **Inline pass lengthens poll ticks.** The judge already runs inline
  `codex exec` at `xhigh` on the same cadence; the pass runs at most once
  per project completion attempt, wrapped in `codex_heartbeat.sh`.
  Mitigation: no per-tick re-run — state records the audited head SHA.
- **Fail-closed gate can wedge a project on engine outage.** Deliberate for
  a mandatory security gate; the wedge is visible (`SECURITY_PASS_FAILED` +
  Telegram + completion-status comment) and recoverable
  (`/re-security-pass` after the outage, or `ENABLE_SECURITY_PASS=false`
  repo var as the operator kill switch).
- **Prompt-hardening regressions in unrelated phases (plan/implement output
  drift).** Mitigation: additive-only edits, template-mirror discipline,
  contract tests pinning existing sections, and Phase 1 shipping alone so
  any drift is bisectable to it.
- **State-comment size growth.** New fields are a few scalars plus one
  small findings-derived issue list; the chunked `ORCHESTRATOR_STATE_V2`
  persistence already handles >65 KiB payloads.
- **Merge-conflict completion route interaction.** The gate on that route
  runs the pass against the integration head even though the project is
  conflict-recovering; if the branch is unbuildable the pass still audits
  text diffs (read-only), so no new failure mode is introduced there.

## Rollout

- Phase 3 is the dark launch: `ENABLE_SECURITY_PASS=false` default, opt-in
  per repo via a repository variable, validated on this repo first.
- Phase 4 is the ramp: default `true` everywhere the poller syncs. Operator
  kill switch remains the repo var; per-project recovery is
  `/re-security-pass`.
- Consumer propagation is the existing `@stable` sync (`update_workflows.yml`
  daily cron + release `repository_dispatch`); no consumer action required,
  no new wrapper, `standard` and `full` profiles both covered because the
  pass rides the poller.
- The weekly `security-audit.yml` keeps running unchanged as the
  default-branch drift net.

## References

- `docs/plans/orchestrator-validation-resilience-plan.md` — the
  validating/validation-fixing loop this plan clones.
- `docs/plans/orchestrator-prerequisite-gate-automation-plan.md` — prior
  gate-insertion precedent in the poller.
- `README.md` "Project Orchestrator" section and
  `docs/how-it-works.md` — lifecycle documentation to be extended.
- `agents.md` — stable log-prefix registry and orchestrator marker
  contracts.
- Session research (2026-08-29): line-level maps of
  `scripts/orchestrate_poll_process.sh` completion routes,
  `scripts/validate_process.sh` fix-loop mechanics, and
  `scripts/security_audit.sh` filtering internals underlying the line
  anchors cited above (anchors are indicative; implementers must re-locate
  by function name at implementation time).
