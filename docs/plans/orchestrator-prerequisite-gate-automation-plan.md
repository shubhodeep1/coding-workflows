# Orchestrator Prerequisite-Gate Automation

## Summary

Let the orchestrator satisfy the machine-checkable prerequisites its own wave
issues declare — today, "a live-key smoke workflow must have passed since
dependency X merged" — by dispatching the workflow, verifying the result, and
carrying the evidence into the `/answer` comment it already posts. Planning
then proceeds unattended. When the prerequisite genuinely fails, the current
behaviour is preserved byte-for-byte: no auto-answer, `ai:blocked`, CRITICAL
Telegram alert — with the failing run URL added to the message.

The gate stays opt-in per issue: an issue with no prerequisite declaration
behaves exactly as it does today.

## Context

Project #3845 (opencode cutover for `review_autofix`) encodes a human gate in
each wave issue body: *"Re-dispatch `opencode-live-smoke.yml` … and record a
successful run URL before starting this issue. If either editor slug fails or
evidence is absent, emit `BLOCKED: …` rather than editing."*
(`docs/plans/opencode-review-autofix-cutover-plan.md:381-384`, restated in the
body of issues #3849 and #3865.)

That instruction is correct and the planner honours it correctly. The problem
is that nothing in the unattended pipeline can *satisfy* it. The orchestrator's
auto-answer is a fixed string with no evidence slot
(`.github/workflows/clarify.yml:520-521`):

```
/answer [auto-answered-by-orchestrator]

Auto answer was posted because this issue is labeled ai:orchestrator-managed
and this run was not a forced human /reclarify.
```

So every wave issue carrying a live-smoke prerequisite takes the same path: a
full planning run burns ~5 minutes and an LLM call, `plan.yml:1117-1144` parses
the `BLOCKED:` line, `plan.yml:1245-1270` applies `ai:blocked` and fires a
CRITICAL Telegram alert, and the project stalls until a human dispatches a
workflow and pastes a URL.

Two of the project's three waves hit this:

| Wave | Issue | Blocked at | Unblocked at | Stalled |
|---|---|---|---|---|
| P2 | #3849 | 2026-08-27 12:28:21Z (comment 5439135441) | 2026-08-28 05:11:59Z (comment 5448711775) | ~16h 45m |
| P3 | #3865 | 2026-08-28 06:11:18Z (comment 5449105311) | 2026-08-28 07:2xZ (this session) | ~1h 15m |

The unblock is mechanical both times: dispatch `opencode-live-smoke.yml`, wait
~2.5 minutes, post `/answer` quoting the run URL. In #3849 the human `/answer`
carrying the run URL was enough on its own — the very next planning run
returned `STATUS: CLEAR` (comment 5448753771, which cites *"run `33143208405`
completed successfully on `main` with all eight configured slots"* as its
prerequisite evidence). That is the proof the mechanism needs no prompt change:
evidence in the `/answer` body is already read and accepted by the planner.

Two secondary defects make the gate harder to automate than it should be:

1. **The smoke's results are not machine-readable.** The per-slug pass/fail
   table is written only to `$GITHUB_STEP_SUMMARY`
   (`.github/workflows/opencode-live-smoke.yml:300-313`). Nothing per-slug
   reaches stdout, so `gh run view --log` on a smoke run shows no slug results
   at all. A checker can read the job conclusion but cannot assert *"both
   editor slugs passed"* without scraping HTML.
2. **The job conclusion is the wrong gate signal.** The smoke fails the whole
   job if *any* configured slot fails (`:315-318`), but a wave's prerequisite
   names specific slots. Re-dispatching this project's smoke at the post-P2
   head (run `33150962476`) returned `conclusion: failure` on a single
   reviewer slot — `opencode_agent_failure phase=live_smoke role=reviewer
   model=minimax/minimax-m3 rc=1
   failure_class=call_1_FAIL_empty_output__PASS_PASS` — while **both editor
   slugs passed**. A gate that reads the conclusion would have blocked P3 on a
   read-side flake that P3's prerequisite says nothing about. The gate must
   assert per-slot results, which requires (1).
3. **Freshness is invisible.** The gate is "green *since* P2 merged", but
   nothing correlates a run's timestamp with a dependency's merge commit. In
   this incident the newest green smoke (run `33143208405`, finished
   04:57:35Z) predated P2's merge (`f59655b`, 06:02:19Z) by 65 minutes — a
   distinction no human eyeballing "the smoke is green" would reliably catch.

## Automation wiring (§18.E)

- **New script:** one — `scripts/satisfy_issue_prerequisites.sh`. It is never
  invoked by hand; its only caller is an existing workflow step (§18.A).
- **Extends, does not add, a workflow entry point:** the `Orchestrator-managed
  fast path` step in `.github/workflows/clarify.yml:508-521`, which already
  runs on every `ai:orchestrator-managed` issue and already posts the
  `/answer`. No new trigger, no new schedule.
- **Reuses the existing dispatcher:** `scripts/dispatch_and_watch_workflow_run.sh`
  already does dispatch-with-retry, registration polling, completion polling,
  allowed-conclusion checking, and fail-open flags. It is used today only by
  `.github/workflows/test-and-mark-stable.yml`. No second dispatcher is
  written (§15 reuse, §5 minimal change).
- **No new supervisor** (§18.C): the work is bounded (one dispatch, one watch,
  ~3 min) and completes inside the clarify job.
- **No database work** (§10, §18.D): the gate's state is the GitHub Actions run
  list plus the evidence comment. Nothing is persisted.
- **§18.F registry:** one entry for `scripts/satisfy_issue_prerequisites.sh`,
  type `long-running`, removal trigger **"permanent — review annually"**,
  preflight check "no issue body in the last 90 days contains an
  `ai:prerequisite` block".

## Goals

- An orchestrator wave issue whose prerequisite is *satisfiable* never reaches
  a human: the gate is dispatched, verified, and evidenced automatically.
- An orchestrator wave issue whose prerequisite genuinely *fails* still stops,
  still labels `ai:blocked`, still alerts CRITICAL — with strictly more
  information than today (the failing run URL and the failing slugs).
- Evidence is never fabricated: the satisfier posts a run URL only after
  reading that run's own machine-readable result.
- Freshness is decided by data, not by eyeball: a run counts only if it started
  after the declared dependency's merge commit.

## Non-goals

- No change to the planner prompt or to `BLOCKED:` parsing. The existing
  evidence-in-`/answer` path already works (proved by #3849).
- No change to human-authored issues, to `/reclarify`, or to any issue without
  an `ai:prerequisite` block.
- No general-purpose "run any workflow the issue asks for" capability. Only the
  declared, allow-listed gate kinds run (see D3).
- No auto-merge, no auto-approval, no change to `@stable` gating.

## Approach

Three phases, each independently revertible.

### Phase A — make the smoke's result machine-readable

`.github/workflows/opencode-live-smoke.yml`:

1. Echo the same per-slug table to stdout in addition to `$GITHUB_STEP_SUMMARY`
   (the loop at `:300-313` gains a second sink). Log-based verification then
   works with `gh run view --log`.
2. Write `${runtime_dir}/smoke-results.json` — one object per slot with
   `source_slot`, `role`, `model_slug`, `first`, `second`, `model_evidence`,
   `reasoning_evidence`, `status` — and upload it as an artifact named
   `opencode-live-smoke-results`. The `results_file` TSV already carries every
   field (`:284-287`); this is a serialisation, not new measurement.
3. Prefix each row on stdout with a stable, greppable token
   (`OPENCODE_SMOKE_ROW_V1`), so a checker never has to parse a Markdown table.

Phase A is useful on its own and ships first: it is what makes any gate check
trustworthy, and it is the deferred half of this session's Q2.

### Phase B — declare the gate, and satisfy it before the auto-answer

**Declaration.** The orchestrator writes an HTML-comment block into the wave
issue body when it creates the issue from the plan (alongside the existing
`Orchestrator metadata` block). Human-authored issues may carry one too:

```
<!-- ai:prerequisite
kind: workflow_run
workflow: opencode-live-smoke.yml
ref: orchestrator/project-3845
require_slots: editor-primary,editor-fallback
fresher_than: merge_of:p2-opencode-read-side
-->
```

Unrecognised keys, an unknown `kind`, or a malformed block are a hard
no-satisfy: the satisfier does nothing and today's blocked path runs. Absence
of the block is the default and means "behave exactly as today".

**Satisfaction.** `scripts/satisfy_issue_prerequisites.sh`, called from the
`Orchestrator-managed fast path` step *before* the `/answer` is posted:

1. Parse the block. No block → exit 0, caller posts today's fixed body.
2. Resolve `fresher_than` to a timestamp: for `merge_of:<local_id>`, the
   committer date of that wave's merge commit on the integration branch.
3. Look for an existing satisfying run: one
   `gh api repos/<slug>/actions/workflows/<file>/runs?branch=<ref>&per_page=10`
   call, newest first, first run whose `run_started_at` is later than the
   freshness timestamp. Read its `opencode-live-smoke-results` artifact and
   require every slot in `require_slots` to be `PASS` (`PASS(text)` counts).
   Cache the response for the cycle (§15).

   Note the deliberate absence of `status=success`: satisfaction is decided by
   the required slots' rows, not by the job conclusion. Run `33150962476` is
   the worked example — `conclusion: failure` from a `minimax/minimax-m3`
   reviewer slot, `editor-primary` and `editor-fallback` both green. Under a
   conclusion-based gate P3 stays blocked on a slot its prerequisite never
   mentions; under the slot-based gate it proceeds. A run whose required slots
   are all `PASS` satisfies the gate even when the job is red, and a green job
   missing a required slot (e.g. dispatched with a `model_filter`) does not
   satisfy a gate that names slots the run never exercised.
4. If none exists, dispatch one via `dispatch_and_watch_workflow_run.sh`
   (`--workflow`, `--ref`, `--allowed-conclusions success`, completion timeout
   600s), then re-run step 3 against the run it reports.
5. On success, emit the evidence block on stdout for the caller to append to
   the `/answer` body:

   ```
   /answer [auto-answered-by-orchestrator]

   Prerequisite satisfied automatically: `opencode-live-smoke.yml` run
   <url> completed successfully on `<ref>` after <dep> merged (<sha>).
   Required slots: editor-primary PASS, editor-fallback PASS.
   ```
6. On failure, timeout, or any ambiguity, print the reason, exit non-zero, and
   **post no auto-answer at all**. The issue keeps `ai:planning` off, the
   existing blocked path is reached exactly as it is today, and the reason
   string (with the failing run URL) rides along in the alert.

**Budget.** At most one dispatch per issue per orchestrator cycle, recorded via
the existing `phase_cap_note_dispatch` mechanism
(`scripts/orchestrate_poll_process.sh:9683`). A satisfying run found in step 3
costs one API call and no model spend — so the second and later waves of a
project reuse the first wave's run whenever freshness allows.

### Phase C — enrich the blocked path

`.github/workflows/plan.yml:1245-1270` and the Telegram body gain the
satisfier's reason string when one exists: which workflow ran, which run URL,
which slots failed. A human who is paged still has to act, but arrives with the
diagnosis instead of starting one. No control-flow change.

## Decisions

### D1 — Evidence rides in the `/answer` body, not in a new state store

**Chosen:** append the evidence to the comment the orchestrator already posts.
**Alternatives:** a new label (`ai:prereq-satisfied`); a memory-ledger entry; a
check run on the issue.
**Why:** it is the mechanism that already demonstrably works — #3849's human
`/answer` carrying a run URL produced `STATUS: CLEAR` on the next planning run
with no code change anywhere. It also keeps the audit trail in the thread a
human reads, and needs no schema, no new label in `_AI_PHASE_LABELS`
(`scripts/label_helpers.sh:120`), and no §10 contract.

### D2 — Reuse `dispatch_and_watch_workflow_run.sh`

**Chosen:** call the existing helper.
**Alternatives:** inline `gh workflow run` + a poll loop (the shape used at
`scripts/orchestrate_poll_process.sh:7597` and `:13114`); a new dispatcher.
**Why:** the helper already solves dispatch retry/backoff, the
run-registration race (a dispatched run is not immediately listable),
completion polling, and conclusion filtering. Re-deriving that inline is how
the poller ended up with four near-duplicate dispatch sites.

### D3 — Allow-list the gate kinds; fail closed on anything else

**Chosen:** `kind: workflow_run` only, with `workflow` constrained to an
explicit allow-list (initially `opencode-live-smoke.yml`).
**Alternatives:** run whatever workflow the issue names.
**Why:** issue bodies are attacker-adjacent input in any repo that accepts
outside issues, and the satisfier runs with `GH_PAT`. An unconstrained
"dispatch what the issue says" primitive is a privilege-escalation gadget. The
allow-list keeps the blast radius at "re-runs a smoke that was already going to
be run".

### D4 — Freshness is computed, never asserted

**Chosen:** compare `run_started_at` against the dependency merge commit's
committer date.
**Alternatives:** a fixed TTL ("green within 24h"); trust the newest green run.
**Why:** this incident is precisely a 65-minute inversion that a TTL would have
waved through. The dependency merge time is the semantically correct boundary
and is one `git log -1` / one API call away.

## Files likely to change

| File | Phase | Change |
|---|---|---|
| `.github/workflows/opencode-live-smoke.yml` | A | stdout rows + results JSON + artifact upload |
| `tests/test_opencode_live_smoke_results_contract.py` `[new]` | A | row token, JSON shape, artifact name |
| `scripts/satisfy_issue_prerequisites.sh` `[new]` | B | parse, freshness, find-or-dispatch, evidence |
| `.github/workflows/clarify.yml` | B | call the satisfier in the fast-path step before posting `/answer` |
| `scripts/orchestrate_poll_process.sh` | B | write the `ai:prerequisite` block when creating wave issues |
| `tests/test_satisfy_issue_prerequisites.py` `[new]` | B | block parsing, allow-list, fail-closed paths, evidence text |
| `.github/workflows/plan.yml` | C | carry the reason string into the blocked comment + alert |
| `docs/scripts-pending-removal.md` | B | §18.F entry |
| `README.md` / `agents.md` | B | document the block and the allow-list |
| `changelog.d/<issue>-orchestrator-prereq-gate.md` `[new]` | A–C | one fragment (§20) |

## Risks and edge cases

- **Model spend.** Each dispatch is 16 live OpenRouter calls (8 slots × 2). The
  find-first-then-dispatch order plus the per-cycle cap keeps this at roughly
  one smoke per project wave, which is what the plan already intended a human
  to do by hand.
- **Clarify job latency.** The fast-path step grows by up to the completion
  timeout (600s cap; observed smoke runtime ~2.5 min). Acceptable against a
  ~17h human round trip, but the timeout must fail closed, not hang.
- **Dispatch loop.** A permanently red smoke must not be re-dispatched every
  cycle. The per-cycle cap plus `ai:blocked` (which stops the auto-answer path
  entirely) bounds it; a third guard — skip dispatch if the newest run for this
  `(workflow, ref)` failed within the last hour — is cheap and worth adding.
- **Ref that does not carry the workflow.** Dispatching a `workflow_dispatch`
  workflow against a ref where the file is absent 404s. This already bit the
  project once (run 33087059507's context: the smoke was unregistered because
  it had never existed on the default branch, PR #3858). Treat 404 as
  fail-closed with an explicit reason string.
- **§6.** Every identifier introduced is new and collision-checked:
  `satisfy_issue_prerequisites.sh`, `AI_PREREQ_GATE_V1`, `PREREQ_GATE_*`,
  `OPENCODE_SMOKE_ROW_V1`, `ai:prerequisite`. None appears anywhere in
  `scripts/`, `.github/`, `prompts/`, or `tests/` today. Nothing is renamed.
- **Unattended pipelines** read `unattended_system_instructions.md` and never
  see `CLAUDE.md`; the gate is workflow-level and needs no instruction-file
  change in either file.

## Open questions for the user

> **Q1: Should a satisfying run be reusable across waves of the same project,
> or must each wave dispatch its own?**
>
> Choices:
> - **A** — Reusable when freshness allows: wave N+1 accepts wave N's run if it
>   started after N+1's declared dependency merged. Cheapest, and correct by
>   the plan's own wording. (RECOMMENDED)
> - **B** — One fresh dispatch per wave issue, always. Simpler to reason about,
>   costs one extra smoke per wave.
>
> Reply: `Q1: A`

> **Q2: What should the satisfier do when the prerequisite's `ref` is the
> integration branch but the plan text says "after P2 merges to `main`"?**
>
> Choices:
> - **A** — Use the ref the block declares, and require the dependency's merge
>   commit to be an ancestor of it. Matches how the orchestrator actually
>   merges waves (into `orchestrator/project-*`, not `main`). (RECOMMENDED)
> - **B** — Require `main` and block until the integration PR merges. Stricter,
>   but stalls every wave behind integration.
>
> Reply: `Q2: A`

> **Q3: Phase ordering — ship all three phases as one project, or land Phase A
> alone first?**
>
> Choices:
> - **A** — Land Phase A immediately as a small standalone PR (it is a strict
>   improvement with no new mechanism), then run B+C as an orchestrator
>   project. (RECOMMENDED)
> - **B** — One project, three waves, nothing ships until the whole gate is
>   designed and implemented.
>
> Reply: `Q3: A`

## Rollout

1. Phase A merges; the next smoke dispatch produces machine-readable rows and
   the results artifact. No behaviour depends on them yet.
2. Phase B merges with the allow-list containing exactly
   `opencode-live-smoke.yml`. The next orchestrator wave issue that declares a
   block exercises the satisfier end to end.
3. Verify on one real wave: the `/answer` comment carries an evidence line, the
   planning run returns `STATUS: CLEAR`, and no `ai:blocked` label is applied.
4. Phase C merges; confirm a deliberately failing smoke still produces
   `ai:blocked` plus a CRITICAL alert, now naming the run.
5. Rollback: revert any phase independently. Reverting B restores the fixed
   auto-answer string and the human gate; reverting A only removes extra
   output.

## Verification

- `bash -n` on both changed shell scripts; `shellcheck` per repo config.
- New unit tests for block parsing (valid, absent, malformed, unknown kind,
  workflow not on the allow-list) and for the evidence-text shape.
- A dry-run mode (`--check-only`) replayed against the three runs this plan was
  written from, which between them cover every branch of step 3:
  `33143208405` (green, all slots, but older than the P2 merge → rejected on
  freshness), `33150962476` (red job, both required slots `PASS` → accepted),
  and the two editor-filtered runs dispatched for #3865 (green, required slots
  present → accepted).
- Release gate green.
