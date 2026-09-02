Given a GitHub issue (number / URL) — or any reference that identifies a project (PR, plan doc, feature name) — in `$ARGUMENTS`, determine three things: (1) is the project **fully implemented**, (2) is it **correct** — does the code actually do what the plan / issue specified, audited rather than assumed — and (3) is it **activated** — will it **start working automatically** on its trigger, or does something still need to be done to make it run? A project in this repo can live on one of two sides — **this consumer repo's own code/config**, or the **upstream workflow library** (`shubhodeep1/coding-workflows`) that this repo's wrappers call — and you decide which from the evidence. Diagnose, then fix: the verdict is graded against the side's ref and reported in chat, and then every **EVIDENCE-BASED** `[CONSUMER]`-side defect the audit found, plus every activation gap that is pure code in this repo, is fixed on a branch, verified, pushed, and opened as one ready-for-review PR whose body lists each issue, why it was an issue, and the fix applied (see [Fix Policy](#fix-policy)). `[UPSTREAM]`-side defects are never edited from this repo — they ship as proposed fixes routed to `/validate-consumer-issue`. Operator activation steps (repo-vars, secrets, upstream pin bumps, merges) are still enumerated, never performed.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Extract the issue number / URL, PR refs, plan-doc path, or feature name. If there is no concrete reference, stop and ask for one. Restate the parsed reference in the `Summary`.

2. **Understand the intended project and resolve `THIS_REPO`.** Fetch the issue and everything linked — linked PRs, tracking comments, sub-issues — via `mcp__github__issue_read` / `pull_request_read` (or `gh`). Pin down what it builds, its acceptance criteria, and **how it is meant to run** (cron, `pull_request` / `push`, `repository_dispatch`, `workflow_dispatch` manual, a long-running supervisor, or on-demand). Determine **`THIS_REPO`** — the `owner/repo` the command runs in (the SessionStart hook prints the resolved slug; otherwise derive from the git remote).

3. **Classify the side that owns activation.** Decide whether the project's "will it run" question is answered by:
   - **`[CONSUMER]`** — this repo's own workflows / config / code (e.g. a wrapper workflow, a repo-var the consumer sets). Read at `THIS_REPO@main`.
   - **`[UPSTREAM]`** — behavior that lives in the upstream library and only runs through this repo's wrapper at the **ref this repo is pinned to**. Read the upstream side pinned to `UPSTREAM_SHA` (see the pinning procedure below).
   - **`[BOTH]`** — a wrapper in this repo plus the upstream reusable workflow it calls; check each side by its matching rule.

   ### Resolving the upstream pin (for the `[UPSTREAM]` / `[BOTH]` side)
   Find every `uses:` / `repository:` / `ref:` in this repo's `.github/workflows/*.yml` that references `shubhodeep1/coding-workflows`. The exact `ref` is the consumer's pin:
   - tag `@vX.Y.Z` → `UPSTREAM_TAG = vX.Y.Z`, resolve `UPSTREAM_SHA` via `mcp__github__get_tag` / `list_tags`.
   - direct SHA → `UPSTREAM_TAG = <short-sha>`, `UPSTREAM_SHA = <full-sha>`.
   - moving `@stable` → resolve `UPSTREAM_SHA` via `list_tags`.
   - branch `@main` → resolve `UPSTREAM_SHA` to that branch's current tip (note it may move).
   Record `UPSTREAM_TAG` + `UPSTREAM_SHA`; pass `ref=<UPSTREAM_SHA>` on **every** upstream read. Never analyze upstream activation at `main` when this repo is pinned to a release — it is not running `main`.

4. **Implementation check — presence and plan conformance.** Verify the code / config / workflows / contracts the project requires exist, **at the ref for its side** (`THIS_REPO@main` for `[CONSUMER]`; `shubhodeep1/coding-workflows@UPSTREAM_SHA` for `[UPSTREAM]`). Confirm linked PRs are **merged**. Map **every acceptance criterion from Step 2 to the code that satisfies it** — a file existing is not the same as a criterion being met, and an unmapped criterion is PARTIAL. Classify **COMPLETE / PARTIAL / NOT** with citations. This classification is **provisional until Step 5 clears it**: a FAIL there downgrades COMPLETE to PARTIAL.

5. **Correctness audit — COMPLETE must be earned, never assumed.** Existing, wired code can still be wrong, so audit the implementation before letting Step 4 stand at COMPLETE. This step runs on **every** invocation, **at the ref for the side being audited** — never audit upstream code at `main` when this repo is pinned to a release.

   **Audit surface.** The project's own footprint: the files the linked merged PRs touched (`mcp__github__pull_request_read` with the files method, or `gh api repos/<owner>/<repo>/pulls/<N>/files` — one call per PR, §15), plus the files the plan / issue names, plus the call sites immediately reachable from them. Not the whole repo. For a large footprint, fan out read-only `Explore` / Sonnet subagents per §16 and judge their findings yourself.

   **What to audit** — read the real code at the side's ref, never infer correctness from a diff summary or a PR description:
   - **Plan conformance** — does each criterion's implementation do what the plan *said*, or does it diverge (a weaker check, a different default, a dropped case, a `TODO` left behind, a stub that returns early)? Divergence is a finding even when the code is otherwise sound.
   - **Security first** — injection (shell / SQL / template), secrets leaking into logs, PR bodies, or commit messages, missing authz, unsafe deserialization, an unquoted expansion in a workflow.
   - **Correctness defects** — inverted condition, wrong operator, swapped arguments, off-by-one, wrong env var / repo-var / field / index / collection name, a return path that silently skips the work.
   - **Error paths and boundaries** — unvalidated input, unchecked external / API responses, a path that fails closed where it must fail open (or the reverse), `set -euo pipefail` interactions, unhandled non-zero exits.
   - **Concurrency and idempotency** — races, lost writes, a re-run that double-acts instead of converging, a lock without lease expiry.
   - **Naming immutability** — did the project rename, remove, or repurpose an existing identifier without an alias? On the upstream side that breaks every pinned consumer, so it is always a BLOCKER.
   - **Wrapper–upstream contract (`[BOTH]`)** — do the wrapper's `with:` inputs, secrets, and permissions match what the upstream reusable workflow expects **at `UPSTREAM_SHA`**? A renamed or newly-required input that the wrapper does not pass is a BLOCKER even when both sides are individually correct.
   - **Database changes** — any collection / query / index change ships its contract update; query–index alignment; unique-index null / missing / empty rules.
   - **API budget** — a new per-item `gh api` call added inside a loop where a batched or cached call already existed.
   - **Automation bias** — a standalone manual-invocation script, an ungated DB operation, or a new single-use / long-running script or supervisor missing its removal-registry entry.
   - **Tests** — does the project ship coverage for the behavior it added, and does that test actually exercise the criterion rather than assert a tautology?
   - **Docs** — did a behavior change land without its `README.md` / `AGENTS.md` update?

   **Run the checks.** For code checked out locally (the consumer side), execute the repo's existing tests, linters, and validators that bear on the footprint — read-only in effect: **no edits, no commits, no push, no workflow dispatch, no network mutations**, and formatters in `--check` / dry-run mode only. Upstream code pinned to `UPSTREAM_SHA` is usually not checked out here: audit it by reading at that ref and record the checks you could not run. Record every command and its result; a check that cannot run (not checked out, missing dependency, needs a secret) is recorded as `could-not-run` with the reason — never silently dropped.

   **Classify every finding** as `EVIDENCE-BASED` (the code was read at the right ref, or a check was run, and it demonstrates the defect) or `HYPOTHESIS` (plausible but unverified), and rank it `BLOCKER` (breaks the project's stated behavior, or trips a naming / DB-contract hard rule) or `CONCERN` (real but non-blocking).

   **Then classify the step:**
   - **PASS** — every acceptance criterion traces to code that does what it claims, no BLOCKER finding stands, and every check bearing on an open question ran and passed.
   - **CONCERNS** — no BLOCKER, but CONCERN findings stand, or an open question depends on a check that could not run (including an upstream side that could only be read, not executed). Step 4's classification is unchanged; the findings still ship in the report.
   - **FAIL** — at least one EVIDENCE-BASED BLOCKER. **Downgrade Step 4's `Implemented:` to PARTIAL** — a project that does not do what it says is not completely implemented. A HYPOTHESIS finding never produces FAIL on its own; it stays a CONCERN until verified.

   Record every finding with its side, evidence, and `file:line`; `[CONSUMER]`-side fixes are applied in Step 8, after the verdict has been graded (see [Fix Policy](#fix-policy)). A defect on the `[UPSTREAM]` side is not this repo's to patch — report it with a proposed fix (`file:line` at `UPSTREAM_SHA`) and route it via `/validate-consumer-issue`.

6. **Activation check — the load-bearing question.** Implemented ≠ running. Determine whether it will actually execute:
   - **Wrapper wiring (consumer side)** — does this repo have the `.github/workflows/*.yml` wrapper with the right trigger, calling the upstream reusable workflow at the expected ref? A feature that exists upstream but is **not wired into a consumer wrapper** will never run here.
   - **Trigger semantics** — **cron** only fires from the **default branch**; **`repository_dispatch`** needs a dispatcher + token scope; **`workflow_dispatch`** is manual-only; `pull_request` / `push` fire on the matching event.
   - **Feature flags / repo-vars / env** — is it gated behind a flag/var that **defaults OFF** (e.g. `*_ENABLED=0`, an empty roster var)? Consumers commonly must *opt in* by setting a repo-var. Name the exact variable, where it is read, and its default.
   - **Secrets** — does the run need a secret (a model key, `GH_PAT`, a Telegram token) the consumer must add in repo settings? Unset → dormant.
   - **Upstream version gap** — is this repo pinned to an `UPSTREAM_TAG` that **predates** the feature? If the feature landed upstream after the pinned release, it is not available here until the pin is bumped — that is the activation step.
   - **Supervisor / DB gates** — does it need a started supervisor or a code-gated migration rather than a manual step?

   **Tag every gap** as `CODE-FIXABLE` — the gate is a file in **this** repo that Step 8 can correct (a missing or mis-wired wrapper `.github/workflows/*.yml`, a wrapper `with:` input / secret / permission name that does not match what upstream expects at `UPSTREAM_SHA`, an `if:` that can never be true because of a typo, a missing `/db/contracts/*.yml`, `README.md` / `AGENTS.md`, or removal-registry entry) — or `OPERATOR` — the gate is a repo setting, a release action, or upstream: set a repo-var, flip a `*_ENABLED` default, add a secret, bump the upstream pin, merge to the default branch, start a supervisor, or a change that must land in `shubhodeep1/coding-workflows`. `OPERATOR` gaps go to `To activate`; `CODE-FIXABLE` gaps go to Step 8.

7. **Verdict.**
   - **LIVE** — implemented, correctness-audited, *and* activated; runs automatically. State the trigger and the side. A project with `Correctness: CONCERNS` can still be LIVE — say so with the concerns attached.
   - **DORMANT** — implemented but needs a manual step. Enumerate the **exact** steps (set repo-var `X=1`, add secret `Y`, add/adjust the wrapper workflow, bump the upstream pin to `@vA.B.C`, merge to the default branch, start a supervisor).
   - **INCOMPLETE** — not fully implemented on its side, **including a Step 5 FAIL that downgraded `Implemented:` to PARTIAL**; list what is missing or defective.

   The verdict is graded **before** any fix is applied; the fix PR from Step 8 does not upgrade it (see [Rules](#rules)).

8. **Fix — apply, verify, ship (consumer side only).** Select the findings that qualify under the [Fix Policy](#fix-policy): every **EVIDENCE-BASED** `[CONSUMER]`-side Step 5 finding (BLOCKER and CONCERN alike) and every Step 6 gap tagged `CODE-FIXABLE`. `[UPSTREAM]` findings never qualify; for a `[BOTH]` finding, only the half that lives in this repo's wrapper or code qualifies. If nothing qualifies, go to Step 9 with `Fix PR: none` and the reason. Otherwise:
   a. **Branch.** Resolve `THIS_REPO`'s default branch dynamically (do not hardcode `main`) and create `claude/verify-activation-<ref-slug>` from it (append `-2`, `-3`, … on collision). If an **open** PR already exists for that branch from an earlier run, check it out and push onto it — one PR per project. If that PR has **merged**, restart the branch from the default branch (`git fetch origin <default> && git checkout -B <branch> origin/<default>`); never stack on merged history.
   b. **Apply** each fix as the smallest change that removes the defect (a guard, a bounds check, a corrected input name, the missing wiring line), extending existing mechanisms rather than adding new ones. A fix that would rename or remove an existing identifier, change a DB contract without an obvious update path, flip a documented default, bump the upstream pin, or that has more than one plausible shape with material tradeoffs is **not applied** — it goes to `Not fixed` with a Q/A question.
   c. **Verify** every fix: re-run the Step 5 check that demonstrated the defect and confirm it now passes; add or extend a test when the defect had no coverage. Re-run the full set of Step 5 checks on the final tree. For a wrapper fix, re-read the upstream reusable workflow at `UPSTREAM_SHA` and confirm the wrapper's `with:` inputs, secrets, and permissions now match it. A fix whose verification cannot run, or fails, is reverted and reported under `Not fixed` as `could not verify` — never ship an unverified change.
   d. **Docs and changelog.** Update `README.md` / `AGENTS.md` when a fix changes documented behaviour, the matching `/db/contracts/*.yml` when it touches a query or index, and add one `changelog.d/<issue>-<slug>.md` fragment when the fixes change observable behaviour — never edit `CHANGELOG.md` directly.
   e. **Commit** per scope: one commit per finding, or per theme of related findings — never one per file. Each message names the finding it resolves and why it was a defect, e.g. `fix(<area>): <what changed> — verify-activation finding <file:line>: <why>`.
   f. **Push and open the PR.** `git push -u origin <branch>` (retry transient network errors with exponential backoff: 2s, 4s, 8s, 16s — up to 4 retries). Write the body per [PR Body](#pr-body); reference the project issue as `Refs #N`, never `Fixes` / `Closes` / `Resolves #N` (an auto-close keyword against an `ai:orchestrator-tracking` issue kills the orchestrator's state machine on merge). Open a ready-for-review PR (`draft: false`) via `mcp__github__create_pull_request` against `THIS_REPO`'s default branch. Never push to the default branch, never push to `shubhodeep1/coding-workflows`.

9. **Report.** Emit the [Output Format](#output-format). The verdict lines describe the audited refs; `Fix PR`, `Fixes applied`, and `Not fixed` describe what this run changed and what it deliberately left alone.

## Output Format

```
Summary: <parsed reference; project in one phrase; side ([CONSUMER]/[UPSTREAM]/[BOTH]); verdict>

Project: <issue #N — title>  (linked PRs: #…, merged? yes/no)
Side: [CONSUMER] THIS_REPO@main  |  [UPSTREAM] shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>)  |  [BOTH]
Implemented: COMPLETE / PARTIAL / NOT — <evidence: file:line, merged PR#>
Correctness: PASS / CONCERNS / FAIL — <surface: N files audited at <ref>, M checks run; headline finding or "no defects found">
Activated: YES / NO — <the gate: wrapper wiring / repo-var default / secret / upstream pin>
Will it run automatically?: YES (trigger: <cron from default branch | push | pull_request | repository_dispatch>) / NO

Audit findings (omit only when PASS with nothing to note):
- [BLOCKER|CONCERN] [CONSUMER|UPSTREAM] <file:line> — <the defect and why it is wrong> (EVIDENCE-BASED | HYPOTHESIS)

Checks run:
- <command> — pass / fail / could-not-run (<reason>)

Fix PR: <url> (branch claude/verify-activation-<slug>, N commits) / none — <no fixable findings | all findings are [UPSTREAM] | every finding needs a decision | could not verify>

Fixes applied (omit when none; always [CONSUMER]):
- [BLOCKER|CONCERN|GAP] [CONSUMER] <file:line> — Issue: <what was wrong>. Why it is an issue: <the acceptance criterion, rule, or runtime behaviour it broke>. Fix: <what changed> (<short-sha>). Verified by: <check that now passes>.

Not fixed (omit when none):
- [BLOCKER|CONCERN|GAP] [CONSUMER|UPSTREAM] <file:line> — <reason: UPSTREAM side — proposed fix: <one line>, route via /validate-consumer-issue | HYPOTHESIS | naming rename | DB contract | tradeoff needs a decision | OPERATOR gap | could not verify> — <Q-ID when a decision would unblock it>

To activate (only if not already automatic — OPERATOR steps, never performed by this command):
1. <exact step — set repo-var X=1; add secret Y; add/adjust wrapper .github/workflows/Z.yml; bump upstream pin @vA.B.C → @vA.B.D; merge to default branch>

Gaps / risks:
- <not wired into a wrapper, defect that downgraded the verdict, repo-var default-off, unset secret, upstream pin predates the feature, cron not on default branch>
```

Omit empty sections; keep every claim cited to a ref.

## Fix Policy

Verdict first, fixes second: Steps 4–7 grade the project as it stands on its side's ref, and only then does Step 8 change anything. Step 8 runs under CLAUDE.md §12 (PR Review Mode): proactive scope, evidence required, naming immutability and DB contracts untouched. **Only this repo is ever edited.** The upstream library is read at `UPSTREAM_SHA` and never written from a consumer session, whatever else is attached.

**Fix automatically (no ask):**
- Every Step 5 finding tagged `[CONSUMER]` and classified **EVIDENCE-BASED**, BLOCKER or CONCERN — the code was read or a check was run and the defect is demonstrated. The §12.B categories apply: security, crash / data-loss, correctness defects, missing error handling at boundaries, type / contract violations, stale docs and misleading comments, latent bugs in adjacent code the audit exercised, a missing or tautological test for a criterion.
- The consumer half of a `[BOTH]` finding — typically the wrapper's `with:` inputs, secrets, or permissions brought back in line with what upstream expects at `UPSTREAM_SHA`.
- Every Step 6 gap tagged **`CODE-FIXABLE`** — the gate is a file in this repo (see the Step 6 list).

**Never fix automatically — report it, and ask in §2 Q/A format when a decision would unblock it:**
- **`[UPSTREAM]`** findings and the upstream half of `[BOTH]` — report the proposed fix with a `file:line` anchor at `UPSTREAM_SHA` and route it via `/validate-consumer-issue`.
- **HYPOTHESIS** findings — a fix for an unverified defect is a guess. Verify first (read more, run the check); if it cannot be verified in this run it stays a CONCERN under `Not fixed`.
- Renames, removals, or repurposing of any existing identifier (§6); DB changes without an obvious contract-update path (§10).
- **§12.D** items: architectural refactors or new abstractions, multiple plausible fixes with material tradeoffs, behaviour changes to documented contracts (`README.md`, `AGENTS.md`, `/db/contracts/*`), scope explosion (10+ files).
- **`OPERATOR`** gaps — repo settings, release actions, and the upstream pin rather than code: setting a repo-var or flipping a `*_ENABLED` default, adding a secret, bumping the upstream pin, merging to the default branch, dispatching a workflow, starting a supervisor, provisioning infrastructure. §22.B, §23.C, and §24.D are ask-first and stay so; these are enumerated under `To activate` exactly as before.
- Anything whose verification cannot run in this session.

**Every fix is traceable.** Each applied fix states, in the chat report and the PR body: the finding it resolves (`file:line`), why it was an issue (the acceptance criterion, rule, or runtime behaviour it broke), what changed (commit), and the check that proves it. A fix that cannot state all four is not applied.

**One PR per run, one PR per project.** All fixes from a run land in the same PR; a re-run against the same project pushes onto the existing open PR instead of opening another; a merged PR is never reused (§21).

### PR Body

```
## /verify-activation fix — <project ref>

Refs #<N>

**Verdict before this PR:** <LIVE | DORMANT | INCOMPLETE> — Side: <[CONSUMER]|[UPSTREAM]|[BOTH]>, Implemented: <…>, Correctness: <…>, Activated: <…>

## Findings fixed
| # | Finding | Why it is an issue | Fix | Verified by |
| --- | --- | --- | --- | --- |
| 1 | [BLOCKER] [CONSUMER] `file:line` — <defect> | <criterion / rule / behaviour it broke> | <what changed> (<sha>) | <check> |

## Not fixed (needs a decision, upstream, or out of reach)
- <finding> — <reason>

## Checks run
- <command> — pass / fail

## To activate (operator steps, unchanged by this PR)
1. <step>
```

## Tool Access

Steps 1–7 are read-only; Step 8 writes to this repo's fix branch and nothing else.

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read` (including its files method, for the Step 5 audit surface), `list_commits`, `list_tags`, `get_tag`, `get_file_contents`, `search_issues`, `search_pull_requests`, `list_branches` (branch-collision check). For the upstream side, pass `ref=<UPSTREAM_SHA>` on every read; for the consumer side, read `THIS_REPO@main`. `create_pull_request` opens the Step 8 fix PR against `THIS_REPO` only.
- **`gh` CLI** — the `GH_TOKEN` transport, when `gh` is installed; shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene) — see **CLAUDE.md §23**. Use the SessionStart slug for `THIS_REPO` and `shubhodeep1/coding-workflows` for upstream reads. Steps 1–7 are §23.A read-only work; pushing the fix branch and opening the PR are §23.B routine writes — self-serve; §23.C operations (repo settings, secrets, variables, workflow dispatch, merges) are never part of Step 8.
- **`Read` / `Grep` / `Glob`** — verify the consumer wrapper, audit the code, and read the triggers and flag defaults on the local checkout.
- **`Edit` / `Write`** — Step 8 only, on the fix branch, for the files a qualifying `[CONSUMER]`-side fix touches (plus the docs, contract, and changelog fragment that fix requires). Never a file in an attached upstream checkout.
- **`Bash`** — read-only inspection and the Step 5 checks (this repo's existing tests, linters, and validators against the local checkout; formatters in `--check` mode) during Steps 1–7; in Step 8 additionally the fix verification and `git checkout -b` / `git commit` / `git push -u origin <branch>`. Never push to the default branch, never push to `shubhodeep1/coding-workflows`, never `gh workflow run`, never a repo-settings / secret / variable mutation.
- **Subagents** — optional read-only fan-out (`Explore` / Sonnet) when the Step 5 audit surface is large; the parent judges the findings.

## Rules

- **Verdict first, then fix — and the verdict is graded against the audited refs.** Steps 4–7 report the project as it stands; Step 8 then ships `[CONSUMER]`-side fixes on a branch. A fix PR does **not** upgrade the verdict: a project audited FAIL stays INCOMPLETE in this run's report, with `Fix PR:` pointing at the remedy. Re-run `/verify-activation` after the PR merges to earn the upgrade; `/deploy-activate` keeps gating on the reported verdict.
- **Fix only this repo, and only what the evidence demonstrates.** EVIDENCE-BASED `[CONSUMER]` findings and `CODE-FIXABLE` gaps are fixed; `[UPSTREAM]` findings, HYPOTHESIS findings, naming / DB-contract / §12.D items, and `OPERATOR` gaps are reported (see [Fix Policy](#fix-policy)). An upstream defect goes to `/validate-consumer-issue` with a proposed fix. Never fix speculatively, never silently widen scope, never edit a test to pass without evidence the test is wrong, never broaden a `catch` / `except` or add a retry to mask a deterministic failure.
- **Every fix is verified before it is pushed, and every fix is explained.** The report and the PR body carry, per fix: the issue, why it was an issue, what changed, and the check that proves it. A bare "fixed" is not acceptable.
- **Operator activation steps are never performed by this command.** Repo-vars, secrets, default flips, upstream pin bumps, merges, workflow dispatches, supervisors, and infrastructure stay under `To activate` for the operator or `/deploy-activate`.
- **"Implemented" ≠ "correct."** Code that exists, is wired, and is reachable can still do the wrong thing. COMPLETE is earned by tracing every acceptance criterion to code that does what it claims — never by the presence of a file, a merged PR, or a plan's self-reported status.
- **A `FAIL` downgrades `Implemented:` to PARTIAL**, which makes the verdict INCOMPLETE and stops `/deploy-activate` at its completeness gate. That is the intended coupling — do not report a defective project as LIVE.
- **Never mark `PASS` on something unverified.** An unverifiable concern is `CONCERNS` with a HYPOTHESIS finding, and a check that could not run is reported as `could-not-run`. Silence is not evidence of correctness, and an upstream side that could only be read is not thereby proven correct.
- **Pick the side from evidence.** A feature implemented upstream but not wired into a consumer wrapper, or gated behind a repo-var the consumer never set, is **DORMANT for this repo** even though the upstream code is perfect. Say which side owns the gate.
- **Pin upstream reads to the consumer's ref.** Analyzing upstream activation at `main` when this repo is pinned to a release is wrong — the imprecision can flip the verdict. Read at `UPSTREAM_SHA`.
- **"Activated" ≠ "implemented."** Name the exact gate: wrapper wiring, a `*_ENABLED` default, an unset secret, or an upstream pin that predates the feature.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never automatic. `cron` requires the workflow on the default branch. `repository_dispatch` needs a dispatcher + token scope.
- **The most common consumer activation steps are opt-in:** setting a repo-var, adding a secret, adding/adjusting a wrapper workflow, or bumping the upstream pin. Enumerate them precisely so the user can act without guessing.
