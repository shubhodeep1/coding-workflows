# Confine human steps to activation so orchestrator projects run unattended

## Summary

Make every plan build its feature "dark": each phase merges production-safe and
fully verifiable in CI with no human action, and every human step (secrets,
repo-vars, external accounts, DigitalOcean / Cloudflare mutations, DNS, manual
QA, `@stable` releases) is recorded once in a new `## Activation Gates (human)`
plan section and performed only at `/deploy-activate` / graded at
`/verify-activation`. The unattended pipeline stops parking issues in
`ai:blocked` for credentials that are only needed at runtime, and PR/release
sequencing waits get their own auto-resuming status instead of a human page
that nobody needs to answer.

## Automation wiring (§18.E)

| Question | Answer |
|---|---|
| New script / extended script / code-only? | Two new scripts: `scripts/lint_plan_activation_gates.py` (Phase 1) and `scripts/dependency_wait_resume.py` (Phase 4b). Everything else extends existing prompts, workflows, commands and `scripts/orchestrate_lib.py`. |
| Scheduler / PR-push entry points | Lint: `pull_request` on `docs/plans/**` via the new reusable `.github/workflows/lint_plan_activation_gates.yml`, called by `.github/workflows/internal-lint-plan-activation-gates.yml` here and by `workflow-templates/ai-lint-plan-activation-gates.yml` in consumers. Resume scan: the existing `*/5` cron poller, a new step in `.github/workflows/orchestrate_poll.yml` placed **before** the "Find active tracking issues" early exit (`orchestrate_poll.yml:190-215`). |
| New long-running supervisor (§18.C)? | No. The resume scan rides the existing poller cron. |
| DB work (§18.D)? | None. No MongoDB collections, indexes or contracts are touched (§10 N/A). |
| `docs/scripts-pending-removal.md` (§18.F) | No entry: neither new script is single-use, long-running, or a supervisor. The lint runs per PR and the resume scan per cron tick, both permanently. |

## Context

A survey of issues in the repos the orchestrator serves (`binance-personal` has
no issues) shows three distinct causes behind "human input required" stalls:

1. **Credentials treated as planning blockers.** `prompts/mode-clarify.txt`,
   `prompts/mode-plan.txt` and the inline clarify prompt at
   `.github/workflows/clarify.yml:909-915` emit `BLOCKED:` whenever "a private
   credential" is required, without asking whether the value is needed to
   *write* the code (almost never) or only to *run* it (almost always).
   `clarify.yml:1155-1190` and `plan.yml:1395-1440` then apply `ai:blocked`,
   post "Planning blocked: human input required.", and send a CRITICAL
   Telegram page. Example: tele-funtoken-msg-scoring#3230 (Tatum 401 needing
   `TATUM_API_KEY`).
2. **Sequencing waits reported with the same human-input message.**
   coding-workflows#4090 / #4091 ("PR #3968 is still open") and
   tele-funtoken-msg-scoring#4566 ("Patch and release coding-workflows first;
   the resulting immutable release SHA is required") need no human decision,
   only time. #4090/#4091's specific path is already fixed by
   `SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED` and
   `security_pass_unblock_filed_advisory_followups`
   (`scripts/orchestrate_poll_process.sh:1622`, `:6132`); the general case is not.
3. **Human steps written into the phases.** `/write-plan` has no slot for human
   work (`.claude/commands/write-plan.md:58-114`), so it leaks into done
   conditions: `tele-funtoken-msg-scoring/docs/plans/candle-rush-onboarding-plan.md:1154`
   makes manual browser QA (`Q11: A`) "an explicit done-condition of every
   phase". The good pattern already exists in
   `fantasy-usd-top-up-season-pool-hub-plan.md:1008`: "the USD default is `0`
   until an operator sets it", so Phase 1 ships live but dormant.
   `mode-orchestrate.txt` requires each sub-issue body to be self-contained,
   so these steps are copied into sub-issues that an unattended run cannot
   complete.

`/deploy-activate` (step 2c) and `/verify-activation` (step 5) already know how
to find and drive activation gates, but they re-derive them from the code after
the fact instead of starting from a list the plan committed to.

Genuine incident response such as tele-funtoken-msg-scoring#4395 (signer-key
rotation, fund movement, log purge) stays human by design: the deliverable
*is* the production mutation, and §22.B / §23.C / §24.D require asking.

## Goals

- G1. Every plan written by `/write-plan` carries a `## Activation Gates (human)`
  section: either a table of gates or an explicit `None` row.
- G2. `/write-plan` rules forbid any phase done condition, implementation step,
  or test that requires a human action. Each phase must pass CI with every
  gate unset. UI phases name an automated CI check instead of manual QA.
- G3. A CI lint fails a PR that adds or modifies a `docs/plans/*.md` without a
  well-formed Activation Gates section, and warns (without failing) on
  human-action wording inside `## Phases & Merge Strategy` /
  `## Implementation Steps`. It runs here and in all 13 consumer repos in
  `.github/ai/consumer_repos.json`.
- G4. Clarify and plan no longer emit `BLOCKED:` for runtime-only credentials,
  accounts or infra. They plan against the env/repo-var name with a default
  that leaves the feature off, and list the item under `Activation gates:`.
- G5. The orchestrator keeps human actions out of sub-issue bodies and lists
  them on the tracking issue as a plain (non-checkbox) "Activation gates" list.
- G6. A planner/clarifier output of `WAITING: <target>` parks the issue in a
  new `ai:waiting-on-dependency` label. The poller auto-resumes it when the
  target lands, and escalates it to `ai:blocked` after 7 days.
- G7. `/implement-plan-ai` and `/implement-plan-claude` refuse a plan that
  fails the lint rules and offer a retrofit PR. `/implement-plan-claude`'s
  per-phase verification rejects a done condition that needs a human.
- G8. `/deploy-activate` and `/verify-activation` seed their gate lists from the
  plan's section, then cross-check against code.

## Non-goals

- Changing how incident-response work (e.g. #4395) is handled. A task whose
  deliverable is itself a production mutation still ends in `BLOCKED:` or
  `ai:needs-human`.
- Machine-checkable prerequisites such as "a live-key smoke run passed since X
  merged". Those belong to `docs/plans/orchestrator-prerequisite-gate-automation-plan.md`,
  which stays a separate plan (Q19: A).
- Building a browser-test harness. This plan changes the planning rules only.
  Each repo's plans build their own automated checks (Q17: A).
- Editing tele-funtoken-msg-scoring's repo-local onboarding assets
  (`/onboard-game`, `/onboard-free-game`,
  `docs/templates/new-gambling-game-onboarding-template.md`). The
  orchestrator's single-repo invariant forbids it (Q18: A). Recorded as
  follow-up work in that repo once this lands.
- Retrofitting existing plans in bulk. The lint checks only added or modified
  plans (Q8: A). The commands offer a per-plan retrofit on demand (Q22: A).
- Making the lint a required status check (a §23.C administrative write).
- Replacing `security_pass_unblock_filed_advisory_followups`. It keeps working
  unchanged (§5).
- Changing the CRITICAL Telegram page that fires when an issue is parked
  (Q14: C).

## Constraints

- **§6 naming immutability.** No identifier is renamed or removed. New
  identifiers, checked for collisions against the whole repo:
  `scripts/lint_plan_activation_gates.py`, `scripts/dependency_wait_resume.py`,
  `.github/workflows/lint_plan_activation_gates.yml`,
  `.github/workflows/internal-lint-plan-activation-gates.yml`,
  `workflow-templates/ai-lint-plan-activation-gates.yml`, label
  `ai:waiting-on-dependency`, output line `WAITING:`, comment marker
  `AI_DEPENDENCY_WAIT_V1`, repo var `DEPENDENCY_WAIT_ESCALATE_DAYS`, log key
  `DEPENDENCY_WAIT_RESUME`, decomposition field `activation_gates`, plan
  heading `## Activation Gates (human)`. The `BLOCKED:` contract is narrowed in
  prompt text only; its parser and label path are unchanged.
- **§14 consumer propagation.** `workflow-templates/.claude/commands/*.md` and
  the new wrapper reach all 13 repos in `.github/ai/consumer_repos.json` on the
  next `@stable` release through `update_workflows.yml`. Reusable-workflow and
  prompt changes are picked up via `@stable` pins.
- **§15 API budget.** See Phase 4b: at most 1 GraphQL call per tick when no
  issue is waiting.
- **§19.** No auto-close keywords against `ai:orchestrator-tracking` issues in
  any PR body or commit.
- **§20.** Every phase that changes behaviour ships one `changelog.d/<n>-<slug>.md`
  fragment.
- **§27.** `plan.yml` (112,849 B), `clarify.yml` (73,520 B) and
  `orchestrate_poll.yml` (53,036 B) stay far below 480,000 B. New logic goes
  into `scripts/`, not inline `run:` bodies.
- **Prompt assembly.** Edit both the legacy body `prompts/mode-*.txt` and the
  include source `prompts/_templates/mode-*.txt` (agents.md "Prompt assembly
  note"). Respect `scripts/prompt_size_budget.py`.
- **Untrusted input.** A `WAITING:` target comes from LLM output that read
  untrusted issue text. Its repo is restricted to the current repo or
  `shubhodeep1/coding-workflows`, and its number must be a positive integer.
  Its only effect is posting `/answer`.

## Approach

**Build dark, activate later.** Human dependencies surfaced while planning fall
into three buckets, and `/write-plan`'s clarification round must place each one:

| Bucket | Examples | Where it goes |
|---|---|---|
| Decide now | product/design choices, business constants only a human knows | Clarification answers, then plan content |
| Defer to activation | secrets, API keys, repo-vars, external accounts, DO/CF mutations, DNS, manual QA, `@stable` release | `## Activation Gates (human)` row. The code reads a named var with a default that leaves the feature off, and logs that it is off |
| Eliminate | sandbox keys for tests, live calls in CI | Mocks / recorded fixtures in CI. A real smoke test becomes a `manual-qa` or `verify` gate |

The section format is fixed so a script can check it:

```md
## Activation Gates (human)

| Action | Kind | Name | Read site | Default when unset | Behaviour when unset | Verified by |
|---|---|---|---|---|---|---|
| Set Tatum API key | secret | `TATUM_API_KEY` | `backend/tatum.py:<line or "new">` | unset | sweeper logs `tatum_disabled` and skips | `/verify-activation`: sweeper run logs a 200 |
```

or exactly one row whose `Action` cell is `None`. `Kind` is one of `secret`,
`repo-var`, `env-var`, `external-account`, `do-mutation`, `cf-mutation`, `dns`,
`manual-qa`, `stable-release`, `other`. `/deploy-activate` performs the rows
one step at a time. `/verify-activation` grades them (LIVE / DORMANT).

**Sequencing waits.** Clarify/plan output `WAITING: pr=<owner>/<repo>#<N>` or
`WAITING: release=shubhodeep1/coding-workflows@stable contains=<owner>/<repo>#<N>`
when the only blocker is an unmerged PR or an unreleased upstream change.
The workflow parks the issue in `ai:waiting-on-dependency` with a trusted
`AI_DEPENDENCY_WAIT_V1` comment marker and keeps today's CRITICAL page
(Q14: C). The poller resumes it by posting `/answer [auto-answered-by-poller]`,
reusing the path `plan.yml` already honours for `ai:blocked`, and sends no
Telegram on resume (Q23: A). After `DEPENDENCY_WAIT_ESCALATE_DAYS` (default
`7`) it swaps to `ai:blocked` with a CRITICAL page (Q15: A).

Alternatives considered: re-running clarify on a timer (wastes an LLM call per
tick), and extending only the security-pass unblocker (covers #4090/#4091 but
not #4566 or future cases).

## Decisions

### D1 — Fixed-schema section plus a CI lint, not prose-only rules
- **Chosen:** A required `## Activation Gates (human)` table with a closed `Kind` enum. The lint fails on a missing or malformed section and warns on human wording in phases (Q2: A, Q9: A, Q11: A).
- **Alternatives considered:** Prose rules in `/write-plan` only; failing on the wording heuristic too.
- **Why:** Prose rules drift. The wording heuristic produces false positives, so it only warns.

### D2 — Lint only added or modified plans
- **Chosen:** `git diff --diff-filter=AM <base>...<head> -- 'docs/plans/*.md'` (Q8: A).
- **Alternatives considered:** Lint all plans.
- **Why:** 17 plans here and 13 in tele-funtoken predate the section. A retrofit happens when a plan is next touched or dispatched (Q22: A).

### D3 — Narrow `BLOCKED:` by rule text, no flag
- **Chosen:** A prompt-only change shipped directly (Q3: A, Q20: A).
- **Alternatives considered:** A repo-var gate defaulting on.
- **Why:** A revert undoes it, and the parser contract is unchanged.

### D4 — New `ai:waiting-on-dependency` label with poller resume
- **Chosen:** A distinct label, resumed by a scan that runs every tick even with no active project (Q4: A, Q12: A, Q13: A, Q16: A).
- **Alternatives considered:** Rewording `ai:blocked`; resuming only while a project is active.
- **Why:** #4566 is a standalone issue, and the poller exits early when no tracking issue is open.

### D5 — Alerting
- **Chosen:** Keep today's CRITICAL page on park. No Telegram on resume. CRITICAL on 7-day escalation (Q14: C, Q23: A, Q15: A).
- **Alternatives considered:** INFO on park and resume; silent.
- **Why:** User decision.

### D6 — `activation_gates` as an optional decomposition field
- **Chosen:** Add an optional `activation_gates: [{action, kind, name}]` array to `orchestrate_decomposition.v1`. It is rendered as a plain list on the tracking issue.
- **Alternatives considered:** Checkbox rows; parsing it out of `project_summary`.
- **Why:** `lint-plan-archival.yml` requires every tracking checkbox ticked before archival, so gates that are done only after completion must not be checkboxes. Optional keeps old outputs valid.

### D7 — Commands check the plan without the lint script present
- **Chosen:** Commands run `scripts/lint_plan_activation_gates.py --files <plan>` when the file exists (this repo), otherwise apply the same two rules by reading the plan.
- **Alternatives considered:** Fetch the script from `@stable`.
- **Why:** Consumer checkouts have no `scripts/`, and the rules are short.

## Phases & Merge Strategy

Each phase is its own PR, production-safe at merge, and stays working if the
others never merge. P2 and P4a both edit `prompts/mode-clarify.txt` and
`prompts/mode-plan.txt` (plus `_templates/` copies), so they must not share a
wave. Declare a `dependency_edges` entry P2 → P4a for file contention only;
neither relies on the other's behaviour. P4 is split into 4a/4b to keep each
issue within the orchestrator's 60-minute budget. Merge order is irrelevant
for correctness.

1. **Phase 1: `/write-plan` section, rules and the lint.**
   - **Files:** `.claude/commands/write-plan.md`, `workflow-templates/.claude/commands/write-plan.md`, `scripts/lint_plan_activation_gates.py` [new], `tests/test_lint_plan_activation_gates.py` [new], `.github/workflows/lint_plan_activation_gates.yml` [new], `.github/workflows/internal-lint-plan-activation-gates.yml` [new], `workflow-templates/ai-lint-plan-activation-gates.yml` [new], `.github/workflows/ci.yml` (unit-test step), `agents.md`, `README.md`, `changelog.d/`.
   - **Done when:** lint unit tests pass in CI. A PR adding a plan without the section fails `internal-lint-plan-activation-gates.yml`. A PR adding a plan with a `None` row passes. Human wording in phases shows as a `::warning` annotation.
   - **Rollback:** revert the PR. No state is written.
2. **Phase 2: narrow `BLOCKED:` for runtime-only needs.**
   - **Files:** `prompts/mode-clarify.txt`, `prompts/_templates/mode-clarify.txt`, `prompts/mode-plan.txt`, `prompts/_templates/mode-plan.txt`, `.github/workflows/clarify.yml` (inline prompt `:909-915`), `tests/test_plan_clarify_blocked_output.py`, `changelog.d/`.
   - **Done when:** prompt-contract tests assert the new wording is present in all five places. `BLOCKED:` parsing tests still pass. The plan output contract lists `Activation gates:`.
   - **Rollback:** revert. The pipeline returns to blocking on credentials.
3. **Phase 3: orchestrator keeps human actions out of sub-issues.**
   - **Files:** `prompts/mode-orchestrate.txt`, `prompts/_templates/mode-orchestrate.txt`, `.github/ai/orchestrate_schema.v1.json`, `scripts/orchestrate_lib.py` (`validate_decomposition` `:84`, `build_tracking_issue_body` `:1146`), `tests/test_orchestrate_lib.py`, `changelog.d/`.
   - **Done when:** a decomposition with `activation_gates` validates and renders a plain "Activation gates" list (no `- [ ]`). A decomposition without the field validates and renders byte-identically to today.
   - **Rollback:** revert. The field is optional, so in-flight projects are unaffected.
4. **Phase 4a: park sequencing waits in `ai:waiting-on-dependency`.**
   - **Files:** the four `mode-clarify` / `mode-plan` prompt files, `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `scripts/label_helpers.sh` (`:48`, `:103`, `:133`), `.github/ai/label_contract.v1.json`, `scripts/orchestrate_lib.py` (`PHASE_LABELS_PRIORITY` `:1454`, `DEDICATED_HANDLER_PHASES` `:1523`), label/phase tests, `changelog.d/`.
   - **Done when:** a fixture output containing `WAITING: pr=...` produces `ai:waiting-on-dependency`, the marker comment and a CRITICAL page. Standalone stall recovery skips the label. `/answer` on a waiting issue moves it to `ai:planning`. Without 4b this behaves exactly like today's `ai:blocked` plus a clearer message.
   - **Rollback:** revert. Any issue already labelled is resumed by a human `/answer`, as today.
5. **Phase 4b: poller resume scan and 7-day escalation.**
   - **Files:** `scripts/dependency_wait_resume.py` [new], `tests/test_dependency_wait_resume.py` [new], `.github/workflows/orchestrate_poll.yml` (new step before `:190`), `agents.md`, `README.md`, `changelog.d/`.
   - **Done when:** with no waiting issues the step makes 1 API call and exits 0. A waiting issue whose PR merged gets exactly one `/answer [auto-answered-by-poller]`, even across repeated ticks (idempotent via the durable marker). A 7-day-old wait moves to `ai:blocked` with one CRITICAL page. Every failure is fail-open (exit 0, `::warning`).
   - **Rollback:** revert. Waiting issues fall back to a human `/answer`.
6. **Phase 5: commands check plans and seed activation from them.**
   - **Files:** `.claude/commands/implement-plan-ai.md`, `.claude/commands/implement-plan-claude.md`, `.claude/commands/deploy-activate.md`, `.claude/commands/verify-activation.md`, and the four `workflow-templates/.claude/commands/` copies (`deploy-activate.md` and `verify-activation.md` differ between the two locations; edit each copy on its own), `changelog.d/`.
   - **Done when:** the text implements the steps below. `tests/test_audit_consumer_drift.py` and any command-contract tests pass.
   - **Rollback:** revert.

## Implementation Steps

### Phase 1
1. `write-plan.md` step 3 (`:11`): add the three-bucket classification (decide now / defer / eliminate) for every human dependency, asked in the clarification round.
2. `write-plan.md` Plan Structure (`:58-114`): insert `## Activation Gates (human)` after `## Rollout` with the fixed table, the `Kind` enum and the `None` form.
3. `write-plan.md` `## Phases & Merge Strategy` text (`:92`) and Rules (`:156-171`): no done condition, step or test may need a human action. Each phase must pass CI with every gate unset. UI phases name an automated CI check (e.g. a Playwright test path), and manual QA becomes a `manual-qa` gate. Mirror all of this byte-for-byte into the `workflow-templates` copy (currently identical).
4. `scripts/lint_plan_activation_gates.py` [new], modelled on `scripts/lint_plan_decisions.py` (tabs, `argparse`, fenced-code skipping).
   - **Inputs:** `--files <paths...>` or `--base-ref <ref>`. With `--base-ref`, it collects added/modified `docs/plans/*.md`.
   - **Errors (exit 1):** heading `## Activation Gates (human)` absent; table missing; header not exactly the seven columns; `Kind` outside the enum; `Name` empty on a non-`None` row; a `None` row mixed with other rows.
   - **Warnings (exit 0, `::warning file=<path>,line=<n>::`):** regex hits inside `## Phases & Merge Strategy` or `## Implementation Steps` for `operator (must|sets|runs|provides|creates)`, `manual(ly)? (QA|browser|verification|step)`, `human (pass|review|sign-?off|QA)`, `gh (secret|variable) set`, `provide (the )?(key|token|secret|credential)`.
   - **Output:** errors as `::error file=...,line=...::` with log key `PLAN_ACTIVATION_GATES_LINT`.
5. `tests/test_lint_plan_activation_gates.py` [new]: cases for a valid table, a valid `None` row, a missing section, a bad header, a bad `Kind`, a mixed `None`, a warning-only hit, fenced-code false positives, and base-ref diff selection. Wire it into `ci.yml` beside the decisions lint tests (`:483-487`), plus `mark-stable.yml` / `test-and-mark-stable.yml` where the decisions tests run.
6. `.github/workflows/lint_plan_activation_gates.yml` [new]: `workflow_call`. Checks out the caller at full depth and the support source into `.codex-workflow-src` using the `sync_ai_labels.yml:48-72` pattern, then runs the script with `--base-ref origin/${{ github.base_ref }}`.
7. `.github/workflows/internal-lint-plan-activation-gates.yml` [new]: `pull_request` with `paths: ['docs/plans/**']`, calling the local reusable workflow.
8. `workflow-templates/ai-lint-plan-activation-gates.yml` [new]: the managed-file header comment, `pull_request` with `paths: ['docs/plans/**']`, calling `...lint_plan_activation_gates.yml@stable`.
9. `agents.md` / `README.md`: add a "Plan activation gates lint" section under an existing anchor, covering the heading, table, enum, error vs warning behaviour, and the consumer wrapper. Add a `changelog.d/` fragment.

### Phase 2
1. In all four `mode-clarify` / `mode-plan` prompt files and the `clarify.yml` inline prompt, amend the `BLOCKED:` rule. A credential, API key, token, account, DNS record, or infra resource needed only when the code runs is **not** a `BLOCKED:` trigger. Plan the code to read it from a named env/repo var whose default leaves the feature off (and logs that it is off), use mocks/fixtures in tests, and list it under `Activation gates:`. `BLOCKED:` remains correct only when the value must be written into the code or plan itself, or when the task's deliverable is itself a production mutation (e.g. #4395).
2. `mode-plan` output contract: add `Activation gates:` (a list of `<kind> <name> — <default when unset>`, or `none`) after "Testing considerations". Update the six-step readiness gate's `IMPLEMENT-READY` evidence to confirm that no step needs a human.
3. Extend `tests/test_plan_clarify_blocked_output.py` with contract assertions for the new wording in all five locations and unchanged `BLOCKED:` parsing. Add a fragment.

### Phase 3
1. `mode-orchestrate.txt` + `_templates`: issue bodies must not instruct any human action. Human actions from the project description go into top-level `activation_gates`. Sub-issue bodies instead state "reads `<NAME>`, defaults to off when unset".
2. `.github/ai/orchestrate_schema.v1.json`: add optional `activation_gates` (array of `{action, kind, name}` strings, `maxItems` 50). `additionalProperties: false` stays.
3. `orchestrate_lib.py` `validate_decomposition`: validate the optional array. `build_tracking_issue_body`: when non-empty, render `### Activation gates (human — performed at /deploy-activate)` as `- <kind>: <name> — <action>` lines (no checkboxes) before `### Dependencies`. Output with the field absent is unchanged.
4. Tests: validation accept/reject, rendering, absent-field byte equality, and `render_tracking_issue_body_from_state` leaving the list untouched. Add a fragment.

### Phase 4a
1. Prompts: add the `WAITING:` rule. When the only blocker is an unmerged PR (`WAITING: pr=<owner>/<repo>#<N>`) or an unreleased upstream change (`WAITING: release=shubhodeep1/coding-workflows@stable contains=<owner>/<repo>#<N>`), emit that line instead of `BLOCKED:`.
2. `clarify.yml` / `plan.yml` parse steps (next to `clarify.yml:1089-1116` and `plan.yml:1252-1270`): parse `WAITING:` before `BLOCKED:`. Validate the target (repo is the current repo or `shubhodeep1/coding-workflows`, number a positive integer). An invalid target falls back to the `BLOCKED:` path with the raw reason.
3. New handler steps modelled on `plan.yml:1395-1440`:
   - Swap to `ai:waiting-on-dependency`, removing the same phase labels.
   - Post "Waiting on dependency: <target>" with `<!-- AI_DEPENDENCY_WAIT_V1 {"kind":"pr|release","repo":"...","number":N,"since":"<ISO8601>"} -->`.
   - Send the CRITICAL `tg_send_tracked` page as today.
   - The step body lives in a `scripts/` helper if it pushes the workflow toward §27 limits (it will not at current sizes).
4. `plan.yml:575-592` `/answer` gate: treat `ai:waiting-on-dependency` like `ai:blocked` (→ `ai:planning`).
5. Label registration: color and description in `label_helpers.sh`, `_AI_PHASE_LABELS`, `label_contract.v1.json`, `PHASE_LABELS_PRIORITY` (after `ai:blocked`), `DEDICATED_HANDLER_PHASES` (so standalone stall recovery skips it). Update `test_ai_labels.py`, `test_label_helpers_phase_transition_contract.py`, `test_ai_label_precreation_contract.py`. Add a fragment.

### Phase 4b
1. `scripts/dependency_wait_resume.py` [new]. Docstring states the input, output, API-call count and fail-open behaviour (§15).
   - **Call 1:** one GraphQL query for open issues labelled `ai:waiting-on-dependency` with their last 30 comments. When no issue is waiting, it exits here.
   - **Markers:** take the newest trusted `AI_DEPENDENCY_WAIT_V1` marker per issue. Trusted means authored by the PAT identity, using the same trust check as `security_pass_unblock_filed_advisory_followups`.
   - **Call 2:** one aliased GraphQL query for the merge state of every distinct PR target.
   - **Release check:** for release targets, resolve the target PR's merge commit (from call 2), then one REST `compare/<sha>...stable` per distinct sha, cached for the tick. `status` of `behind` or `identical` means released.
   - **Resolved target:** unless a durable resume marker already exists, post `/answer [auto-answered-by-poller]` with the marker. `plan.yml` then moves the issue to `ai:planning`. No Telegram (Q23: A).
   - **Expired wait:** when `now - since > DEPENDENCY_WAIT_ESCALATE_DAYS` (repo var, default `7`), swap to `ai:blocked`, comment the reason, and send one CRITICAL page.
   - **Logging:** `DEPENDENCY_WAIT_RESUME issue=<N> target=<t> outcome=resumed|waiting|escalated|invalid_marker|read_failed`.
   - **Failure handling:** any failure leaves the issue for the next tick; exit 0 always.
2. `orchestrate_poll.yml`: add a step before "Find active tracking issues" (`:190`) that runs the script with `GH_TOKEN: secrets.GH_PAT` and `continue-on-error: true`. It runs on every tick regardless of `has_tracking`.
3. `tests/test_dependency_wait_resume.py` [new]: stubbed `gh` covering no-waiters (1 call), merged PR → one `/answer`, repeated tick → no second `/answer`, release contains / not-yet, 7-day escalation, untrusted marker ignored, API failure → exit 0. Wire it into `ci.yml`.
4. `agents.md` / `README.md`: document the label lifecycle, `DEPENDENCY_WAIT_ESCALATE_DAYS`, the call budget and failure modes. Add a fragment.

### Phase 5
1. `implement-plan-ai.md` step 1 and `implement-plan-claude.md` step 1: after resolving the plan, check it (run the lint script when present, otherwise apply the two rules by reading). On failure, stop and offer a small plan PR that adds the section and moves human steps out of the phases. Dispatch or implement only after it merges (Q22: A).
2. `implement-plan-claude.md` step 5: a phase done condition that needs a human action is a verification failure. Record it and stop with `Status: BLOCKED` plus a Q/A ask, instead of waiting on a person.
3. `deploy-activate.md` step 2c and `verify-activation.md` step 5: when the plan has `## Activation Gates (human)`, start from its rows, then cross-check against the code. Flag a row with no read site in the code, and add a gate that exists in the code but not in the table, noting the drift. `manual-qa` and `stable-release` rows become runbook steps (deploy) or `To activate` lines (verify).
4. Apply each edit to both copies. Add a fragment.

## Files & Modules

- `.claude/commands/write-plan.md`, `workflow-templates/.claude/commands/write-plan.md`
- `.claude/commands/implement-plan-ai.md`, `workflow-templates/.claude/commands/implement-plan-ai.md`
- `.claude/commands/implement-plan-claude.md`, `workflow-templates/.claude/commands/implement-plan-claude.md`
- `.claude/commands/deploy-activate.md`, `workflow-templates/.claude/commands/deploy-activate.md`
- `.claude/commands/verify-activation.md`, `workflow-templates/.claude/commands/verify-activation.md`
- `prompts/mode-clarify.txt`, `prompts/_templates/mode-clarify.txt`
- `prompts/mode-plan.txt`, `prompts/_templates/mode-plan.txt`
- `prompts/mode-orchestrate.txt`, `prompts/_templates/mode-orchestrate.txt`
- `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/ci.yml`, `.github/workflows/mark-stable.yml`, `.github/workflows/test-and-mark-stable.yml`
- `.github/workflows/lint_plan_activation_gates.yml` [new], `.github/workflows/internal-lint-plan-activation-gates.yml` [new], `workflow-templates/ai-lint-plan-activation-gates.yml` [new]
- `.github/ai/orchestrate_schema.v1.json`, `.github/ai/label_contract.v1.json`
- `scripts/orchestrate_lib.py`, `scripts/label_helpers.sh`
- `scripts/lint_plan_activation_gates.py` [new], `scripts/dependency_wait_resume.py` [new]
- `tests/test_lint_plan_activation_gates.py` [new], `tests/test_dependency_wait_resume.py` [new], `tests/test_plan_clarify_blocked_output.py`, `tests/test_orchestrate_lib.py`, `tests/test_ai_labels.py`, `tests/test_label_helpers_phase_transition_contract.py`, `tests/test_ai_label_precreation_contract.py`
- `README.md`, `agents.md`, `changelog.d/<n>-<slug>.md` (one per phase)

## Tests

- **Unit:** the two new test files; extended prompt-contract, orchestrate-lib and label tests (above). All run in `ci.yml`.
- **Integration (CI):** Phase 1 is self-verifying. Its own PR adds no plan, but a fixture test runs the script against sample plans. After merge, the first plan PR exercises the internal wrapper.
- **Behavioural:** Phase 4a/4b fixtures replay the #4566 output as `WAITING: release=shubhodeep1/coding-workflows@stable contains=...` and #4090 as `WAITING: pr=shubhodeep1/coding-workflows#3968`.
- **No manual steps** in any phase done condition. This plan dogfoods its own rule.

## Risks & Mitigations

- The model keeps emitting `BLOCKED:` for runtime credentials despite the new text. Mitigation: explicit counter-examples in the prompt. Phase 2 tests pin the wording. `ACCEPTED — pending discovery`: real behaviour is only observable on live issues. Measure via `ai:blocked` reasons over the 30 days after rollout.
- The model over-applies the narrowing and plans code that really does need a value (e.g. a hardcoded contract address). Mitigation: the rule keeps `BLOCKED:` for values written into code. Review catches the rest.
- The lint breaks consumer PRs that edit an older plan. Mitigation: it only fires on added/modified plans (D2), and the error message names the fix and the `None` form.
- Heuristic warnings are noisy. Mitigation: warnings never fail the check (D1).
- A spoofed `WAITING` target or marker. Mitigation: target validation, trusted-author marker check, and the only effect is posting `/answer`.
- Poller step cost on every tick. Mitigation: one GraphQL call when idle, and `continue-on-error`.
- The `activation_gates` schema field is rejected by an older `@stable` validator. Mitigation: the field is optional and prompt and validator ship in the same phase and release. `ACCEPTED`: a consumer on an old pin simply never emits it.
- P2/P4a file contention. Mitigation: a `dependency_edges` entry P2 → P4a.

## Rollout

- No feature flags (Q20: A). `DEPENDENCY_WAIT_ESCALATE_DAYS` defaults to `7` (§4).
- Phases merge to `main` in any order, with P2 before P4a for file contention only.
- Consumers pick everything up on the next `@stable` release: reusable workflows and prompts via the pin, wrapper and `.claude/commands` via `update_workflows.yml` (daily 04:00 UTC plus the `@stable` dispatch). `ai-sync-labels.yml` creates the new label on the same dispatch, and `ensure_label_exists` covers any race.
- Rollback is a per-phase revert. Issues parked in `ai:waiting-on-dependency` at revert time are resumed with a human `/answer`.

## Activation Gates (human)

| Action | Kind | Name | Read site | Default when unset | Behaviour when unset | Verified by |
|---|---|---|---|---|---|---|
| Dispatch `test-and-mark-stable.yml` to cut a new `@stable` (§23.C ask-first) | stable-release | `stable` tag | `.github/workflows/update_workflows.yml` fetch step | previous `@stable` | consumers keep running pre-change behaviour | `/verify-activation`: `ai-lint-plan-activation-gates.yml` present in a consumer repo, and the `ai:waiting-on-dependency` label exists there |

## References

- Issues: tele-funtoken-msg-scoring#3230, #4395, #4010, #4566; coding-workflows#4090, #4091
- `tele-funtoken-msg-scoring/docs/plans/candle-rush-onboarding-plan.md:1154`, `fantasy-usd-top-up-season-pool-hub-plan.md:1008`
- `docs/plans/orchestrator-prerequisite-gate-automation-plan.md` (complementary, separate)
- `scripts/orchestrate_poll_process.sh:1622`, `:6132` (precedent unblocker)
- `.claude/commands/write-plan.md`, `deploy-activate.md`, `verify-activation.md`, `implement-plan-ai.md`, `implement-plan-claude.md`
- Clarification answers: Q1–Q6 A; Q7–Q13 A; Q14 C; Q15–Q23 A
- Follow-up (outside this repo): update tele-funtoken-msg-scoring's `/onboard-game`, `/onboard-free-game` and `docs/templates/new-gambling-game-onboarding-template.md` to the new section once this ships (Q18: A)
