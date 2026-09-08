Given a GitHub issue (number / URL) — or any reference that identifies a project (PR, plan doc, feature name) — in `$ARGUMENTS`, determine three things about **this** repo (`shubhodeep1/coding-workflows`) at `main`: (1) is the project **fully implemented**, (2) is it **correct** — does the code actually do what the plan / issue specified, audited rather than assumed — and (3) is it **activated** — i.e. will it **start working automatically** on its trigger, or does something still need to be done to make it run? Diagnose, then fix: the verdict is graded against `main` and reported in chat, and then every **EVIDENCE-BASED** defect the audit found, plus every activation gap that is pure code in this repo, is fixed on a branch, verified, pushed, and opened as one ready-for-review PR whose body lists each issue, why it was an issue, and the fix applied (see [Fix Policy](#fix-policy)). Operator activation steps (repo-vars, secrets, merges, `@stable` tags) are still enumerated, never performed. `$ARGUMENTS` is free-form and should contain at least one concrete reference (`#1234`, an issue/PR URL, a plan path, or a clearly-named feature).

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Extract the issue number / URL, PR refs, plan-doc path, or feature name. If there is no concrete reference — just vague prose — stop and ask for one. Restate the parsed reference in the `Summary`.

2. **Understand the intended project.** Fetch the issue and everything linked to it — linked PRs, tracking comments, sub-issues, the orchestrator state comment if present — via `mcp__github__issue_read` / `pull_request_read` (or `gh`, see [Tool Access](#tool-access)). Pin down: what it builds, its acceptance criteria, and **how it is meant to run** — a cron schedule, a `pull_request` / `push` trigger, `repository_dispatch`, `workflow_dispatch` (manual), a long-running supervisor (§18.C), or on-demand. Also read `README.md` / `agents.md` for the feature's documented behavior and any env-var / repo-var contract.

3. **Implementation check — presence and plan conformance.** Verify on `main` that the code / config / workflows / contracts the project requires actually exist: `Grep` / `Read` the implicated files, and confirm the linked PRs are **merged** (an open PR means not-yet-implemented). Map **every acceptance criterion from Step 2 to the code that satisfies it** — a file existing is not the same as a criterion being met, and an unmapped criterion is PARTIAL. Classify **COMPLETE / PARTIAL / NOT**, with `file:line` and merged-PR citations. This classification is **provisional until Step 4 clears it**: a FAIL there downgrades COMPLETE to PARTIAL.

4. **Correctness audit — COMPLETE must be earned, never assumed.** Existing, wired code can still be wrong, so audit the implementation before letting Step 3 stand at COMPLETE. This step runs on **every** invocation.

   **Audit surface.** The project's own footprint: the files the linked merged PRs touched (`mcp__github__pull_request_read` with the files method, or `gh api repos/<owner>/<repo>/pulls/<N>/files` — one call per PR, §15), plus the files the plan / issue names, plus the call sites immediately reachable from them. Not the whole repo. For a large footprint, fan out read-only `Explore` / Sonnet subagents per §16 and judge their findings yourself.

   **What to audit** — read the real code at `main`, never infer correctness from a diff summary or a PR description:
   - **Plan conformance** — does each criterion's implementation do what the plan *said*, or does it diverge (a weaker check, a different default, a dropped case, a `TODO` left behind, a stub that returns early)? Divergence is a finding even when the code is otherwise sound.
   - **Security first (§1)** — injection (shell / SQL / template), secrets leaking into logs, PR bodies, or commit messages, missing authz, unsafe deserialization, an unquoted expansion in a workflow.
   - **Correctness defects** — inverted condition, wrong operator, swapped arguments, off-by-one, wrong env var / repo-var / field / index / collection name, a return path that silently skips the work.
   - **Error paths and boundaries (§3)** — unvalidated input, unchecked external / API responses, a path that fails closed where it must fail open (or the reverse), `set -euo pipefail` interactions, unhandled non-zero exits.
   - **Concurrency and idempotency (§3, §10.E)** — races, lost writes, a re-run that double-acts instead of converging, a lock without lease expiry.
   - **§6 naming** — did the project rename, remove, or repurpose an existing identifier without an alias? That is a shipped breaking change and a BLOCKER.
   - **§10 MongoDB** — any collection / query / index change ships its `/db/contracts/*.yml` update; query–index alignment (§10.G); unique-index null / missing / empty rules (§10.D).
   - **§15 API budget** — a new per-item `gh api` call added inside a loop where a batched or cached call already existed.
   - **§18 automation bias** — a standalone manual-invocation script, an ungated DB operation, or a new single-use / long-running script or supervisor missing its `docs/scripts-pending-removal.md` entry (§18.F).
   - **Tests** — does the project ship coverage for the behavior it added, and does that test actually exercise the criterion rather than assert a tautology?
   - **Docs (§7)** — did a behavior change land without its `README.md` / `agents.md` update?

   **Run the checks.** Execute the repo's existing tests, linters, and validators that bear on the footprint against the local checkout — read-only in effect: **no edits, no commits, no push, no workflow dispatch, no network mutations**, and formatters in `--check` / dry-run mode only. Record every command and its result. A check that cannot run (missing dependency, needs a secret) is recorded as `could-not-run` with the reason — never silently dropped.

   **Classify every finding** as `EVIDENCE-BASED` (the code was read, or a check was run, and it demonstrates the defect) or `HYPOTHESIS` (plausible but unverified), and rank it `BLOCKER` (breaks the project's stated behavior, or trips a §6 / §10 hard rule) or `CONCERN` (real but non-blocking).

   **Then classify the step:**
   - **PASS** — every acceptance criterion traces to code that does what it claims, no BLOCKER finding stands, and every check bearing on an open question ran and passed.
   - **CONCERNS** — no BLOCKER, but CONCERN findings stand, or an open question depends on a check that could not run. Step 3's classification is unchanged; the findings still ship in the report.
   - **FAIL** — at least one EVIDENCE-BASED BLOCKER. **Downgrade Step 3's `Implemented:` to PARTIAL** — a project that does not do what it says is not completely implemented. A HYPOTHESIS finding never produces FAIL on its own; it stays a CONCERN until verified.

   Record every finding with its evidence and `file:line`; fixes are applied in Step 7, after the verdict has been graded against `main` (see [Fix Policy](#fix-policy)).

5. **Activation check — the load-bearing question.** Implemented code does not mean *running* code. Determine whether it will actually execute:
   - **Workflow wiring** — is there a `.github/workflows/*.yml` with the right trigger, and is it reachable (not disabled, not gated behind an `if:` that is always false)?
   - **Trigger semantics** — a **cron** schedule only fires from the **default branch**, so a scheduled workflow that has not reached the default branch will never run; **`repository_dispatch`** needs a dispatcher *and* a token with scope (§14); **`workflow_dispatch`** is manual-only (never "automatic"); `pull_request` / `push` fire on the matching event.
   - **Feature flags / env vars / repo-vars** — is the feature gated behind a flag that **defaults OFF** (e.g. `*_ENABLED=0`, an empty roster var)? If so it is *implemented but dormant*. Name the exact variable, where it is read, and its default.
   - **Secrets / credentials** — does the run need a secret (`GH_PAT`, `TG_BOT_SECRET`, `TG_ADMIN_CHAT_ID`, a model key) that may be unset? An unset required secret means dormant.
   - **Supervisor / long-running** (§18.C) — does it need a supervisor that is actually started and wired into startup automation?
   - **DB gates** (§18.D) — does a backfill/migration run from code behind a gate, or is it waiting on a manual step (which would violate §18.A)?
   - **Consumer propagation** (§14) — if it is a workflow-template / `.claude` change, are consumers wired (`.github/ai/consumer_repos.json`, the `@stable` dispatch), and does activation require tagging a new `@stable` release?

   **Tag every gap** as `CODE-FIXABLE` — the gate is a file in this repo that Step 7 can correct (a missing or mis-wired workflow file, a wrong input / secret / permission name, an `if:` that can never be true because of a typo, a missing `docs/scripts-pending-removal.md`, `/db/contracts/*.yml`, `README.md` / `agents.md`, or `.github/ai/consumer_repos.json` entry) — or `OPERATOR` — the gate is a repo setting or a release action (set a repo-var, flip a `*_ENABLED` default, add a secret, merge to the default branch, tag `@stable`, dispatch a workflow, start a supervisor). `OPERATOR` gaps go to `To activate`; `CODE-FIXABLE` gaps go to Step 7.

6. **Verdict.** Combine the three checks into one of:
   - **LIVE** — implemented, correctness-audited, *and* activated; it runs automatically. State the exact trigger. A project with `Correctness: CONCERNS` can still be LIVE — say so with the concerns attached.
   - **DORMANT** — implemented but it will not run until a manual step. Enumerate the **exact** steps to activate (set `VAR=1`, add secret `Y`, merge to the default branch, flip a flag, start a supervisor, tag `@stable` for consumers).
   - **INCOMPLETE** — not fully implemented, **including a Step 4 FAIL that downgraded `Implemented:` to PARTIAL**; list what is missing or defective before activation is even possible.

   The verdict is graded against `main` **before** any fix is applied; the fix PR from Step 7 does not upgrade it (see [Rules](#rules)).

7. **Fix — apply, verify, ship.** Select the findings that qualify under the [Fix Policy](#fix-policy): every **EVIDENCE-BASED** Step 4 finding (BLOCKER and CONCERN alike) and every Step 5 gap tagged `CODE-FIXABLE`. If nothing qualifies, go to Step 8 with `Fix PR: none` and the reason. Otherwise:
   a. **Branch.** Resolve the default branch dynamically (do not hardcode `main`) and create `claude/verify-activation-<ref-slug>` from it (append `-2`, `-3`, … on collision). If an **open** PR already exists for that branch from an earlier run, check it out and push onto it — one PR per project (§12.A). If that PR has **merged**, restart the branch from the default branch per §21.A; never stack on merged history.
   b. **Apply** each fix as the smallest change that removes the defect (§12.C: a guard, a bounds check, a corrected name, the missing wiring line), extending existing mechanisms rather than adding new ones (§5). A fix that would rename or remove a §6 identifier, change a §10 contract without an obvious update path, flip a documented default, or that has more than one plausible shape with material tradeoffs is **not applied** — it goes to `Not fixed` with a §2 Q/A question (§12.D).
   c. **Verify** every fix: re-run the Step 4 check that demonstrated the defect and confirm it now passes; add or extend a test when the defect had no coverage (§12.C). Re-run the full set of Step 4 checks on the final tree. A fix whose verification cannot run, or fails, is reverted and reported under `Not fixed` as `could not verify` — never ship an unverified change.
   d. **Docs and changelog.** Update `README.md` / `agents.md` when a fix changes documented behaviour (§7), the matching `/db/contracts/*.yml` when it touches a query or index (§10.A), and add one `changelog.d/<issue>-<slug>.md` fragment when the fixes change observable behaviour (§20.A) — never edit `CHANGELOG.md` directly.
   e. **Commit** per scope (§12.E): one commit per finding, or per theme of related findings — never one per file. Each message names the finding it resolves and why it was a defect, e.g. `fix(<area>): <what changed> — verify-activation finding <file:line>: <why>`.
   f. **Push and open the PR.** `git push -u origin <branch>` (retry transient network errors with exponential backoff: 2s, 4s, 8s, 16s — up to 4 retries). Write the body per [PR Body](#pr-body), lint it with `PYTHONDONTWRITEBYTECODE=1 python3 scripts/lint_pr_body_auto_close.py --pr-body-file <file> --repo shubhodeep1/coding-workflows` (§19: reference the project issue as `Refs #N`, never `Fixes` / `Closes` / `Resolves #N`), then open a ready-for-review PR (`draft: false`) via `mcp__github__create_pull_request` against the default branch. Never push to the default branch.

8. **Report.** Emit the [Output Format](#output-format). The verdict lines describe `main` as audited; `Fix PR`, `Fixes applied`, and `Not fixed` describe what this run changed and what it deliberately left alone.

## Output Format

```
Summary: <parsed reference; project in one phrase; verdict in one word>

Project: <issue #N — title>  (linked PRs: #…, merged? yes/no)
Implemented: COMPLETE / PARTIAL / NOT — <evidence: file:line, merged PR#>
Correctness: PASS / CONCERNS / FAIL — <surface: N files audited, M checks run; headline finding or "no defects found">
Activated: YES / NO — <the gate: trigger / flag default / secret>
Will it run automatically?: YES (trigger: <cron «expr» from default branch | push | pull_request | repository_dispatch>) / NO

Audit findings (omit only when PASS with nothing to note):
- [BLOCKER|CONCERN] <file:line> — <the defect and why it is wrong> (EVIDENCE-BASED | HYPOTHESIS)

Checks run:
- <command> — pass / fail / could-not-run (<reason>)

Fix PR: <url> (branch claude/verify-activation-<slug>, N commits) / none — <no fixable findings | every finding needs a decision | could not verify>

Fixes applied (omit when none):
- [BLOCKER|CONCERN|GAP] <file:line> — Issue: <what was wrong>. Why it is an issue: <the acceptance criterion, CLAUDE.md rule, or runtime behaviour it broke>. Fix: <what changed> (<short-sha>). Verified by: <check that now passes>.

Not fixed (omit when none):
- [BLOCKER|CONCERN|GAP] <file:line> — <reason: HYPOTHESIS | §6 rename | §10 contract | §12.D tradeoff | OPERATOR gap | could not verify> — <Q-ID when a decision would unblock it>

To activate (only if not already automatic — OPERATOR steps, never performed by this command):
1. <exact step — set REPO_VAR X=1 (read at <file:line>, default 0); add secret Y; merge <branch> to default; tag @stable; start supervisor Z>

Gaps / risks:
- <missing implementation, defect that downgraded the verdict, unset required secret, flag default-off, cron not on default branch, consumers not dispatched>
```

Omit empty sections; keep every claim cited.

## Fix Policy

Verdict first, fixes second: Steps 3–6 grade the project as it stands on `main`, and only then does Step 7 change anything. Step 7 runs under CLAUDE.md §12 (PR Review Mode): proactive scope, evidence required, §6 and §10 untouched.

**Fix automatically (no ask):**
- Every Step 4 finding classified **EVIDENCE-BASED**, BLOCKER or CONCERN — the code was read or a check was run and the defect is demonstrated. The §12.B categories apply: security, crash / data-loss, correctness defects, missing error handling at boundaries, type / contract violations, stale docs and misleading comments, latent bugs in adjacent code the audit exercised, a missing or tautological test for a criterion.
- Every Step 5 gap tagged **`CODE-FIXABLE`** — the gate is a file in this repo (see the Step 5 list).

**Never fix automatically — report it, and ask in §2 Q/A format when a decision would unblock it:**
- **HYPOTHESIS** findings — a fix for an unverified defect is a guess. Verify first (read more, run the check); if it cannot be verified in this run it stays a CONCERN under `Not fixed`.
- **§6** renames, removals, or repurposing of any existing identifier; **§10** changes without an obvious contract-update path.
- **§12.D** items: architectural refactors or new abstractions, multiple plausible fixes with material tradeoffs, behaviour changes to documented contracts (`README.md`, `agents.md`, `/db/contracts/*`), scope explosion (10+ files).
- **`OPERATOR`** gaps — repo settings and release actions rather than code: setting a repo-var or flipping a `*_ENABLED` default, adding a secret, merging to the default branch, tagging `@stable`, dispatching a workflow, starting a supervisor, provisioning infrastructure. §22.B, §23.C, and §24.D are ask-first and stay so; these are enumerated under `To activate` exactly as before.
- Anything whose verification cannot run in this session.

**Every fix is traceable.** Each applied fix states, in the chat report and the PR body: the finding it resolves (`file:line`), why it was an issue (the acceptance criterion, rule, or runtime behaviour it broke), what changed (commit), and the check that proves it. A fix that cannot state all four is not applied.

**One PR per run, one PR per project.** All fixes from a run land in the same PR (§12.A); a re-run against the same project pushes onto the existing open PR instead of opening another; a merged PR is never reused (§21).

### PR Body

```
## /verify-activation fix — <project ref>

Refs #<N>

**Verdict on the default branch before this PR:** <LIVE | DORMANT | INCOMPLETE> — Implemented: <…>, Correctness: <…>, Activated: <…>

## Findings fixed
| # | Finding | Why it is an issue | Fix | Verified by |
| --- | --- | --- | --- | --- |
| 1 | [BLOCKER] `file:line` — <defect> | <criterion / rule / behaviour it broke> | <what changed> (<sha>) | <check> |

## Not fixed (needs a decision or out of reach)
- <finding> — <reason>

## Checks run
- <command> — pass / fail

## To activate (operator steps, unchanged by this PR)
1. <step>
```

## Tool Access

Steps 1–6 are read-only; Step 7 writes to this repo's fix branch and nothing else.

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read` (including its files method, for the Step 4 audit surface), `list_commits`, `get_file_contents`, `search_issues`, `search_pull_requests`, `list_branches` (branch-collision check) for the project and its merge state, read at `main`; `create_pull_request` to open the Step 7 fix PR.
- **`gh` CLI** — the `GH_TOKEN` transport; shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene) — see **CLAUDE.md §23**. The SessionStart hook prints the resolved slug (`shubhodeep1/coding-workflows`). Steps 1–6 are §23.A read-only work; pushing the fix branch and opening the PR are §23.B routine writes — self-serve; §23.C operations (repo settings, secrets, variables, workflow dispatch, merges) are never part of Step 7.
- **`Read` / `Grep` / `Glob`** — verify the implementation, audit the code, and read the workflow triggers and flag defaults on the local `main` checkout.
- **`Edit` / `Write`** — Step 7 only, on the fix branch, for the files a qualifying fix touches (plus the docs, contract, and changelog fragment that fix requires).
- **`Bash`** — read-only inspection and the Step 4 checks (the repo's existing tests, linters, and validators against the local checkout; formatters in `--check` mode) during Steps 1–6; in Step 7 additionally the fix verification, `git checkout -b` / `git commit` / `git push -u origin <branch>`, and the §19 PR-body lint. Never push to the default branch, never `gh workflow run`, never a repo-settings / secret / variable mutation.
- **Subagents** (§16) — optional read-only fan-out (`Explore` / Sonnet) when the Step 4 audit surface is large; the parent judges the findings.

## Rules

- **Verdict first, then fix — and the verdict is graded against `main`.** Steps 3–6 report the project as it stands on the default branch; Step 7 then ships fixes on a branch. A fix PR does **not** upgrade the verdict: a project audited FAIL stays INCOMPLETE in this run's report, with `Fix PR:` pointing at the remedy. Re-run `/verify-activation` after the PR merges to earn the upgrade; `/deploy-activate` keeps gating on the reported verdict.
- **Fix only what the evidence demonstrates.** EVIDENCE-BASED findings and `CODE-FIXABLE` gaps are fixed; HYPOTHESIS findings, §6 / §10 / §12.D items, and `OPERATOR` gaps are reported (see [Fix Policy](#fix-policy)). Never fix speculatively, never silently widen scope, never edit a test to pass without evidence the test is wrong, never broaden a `catch` / `except` or add a retry to mask a deterministic failure.
- **Every fix is verified before it is pushed, and every fix is explained.** The report and the PR body carry, per fix: the issue, why it was an issue, what changed, and the check that proves it. A bare "fixed" is not acceptable.
- **Operator activation steps are never performed by this command.** Repo-vars, secrets, default flips, merges, `@stable` tags, workflow dispatches, supervisors, and infrastructure stay under `To activate` for the operator or `/deploy-activate`.
- **"Implemented" ≠ "correct."** Code that exists, is wired, and is reachable can still do the wrong thing. COMPLETE is earned by tracing every acceptance criterion to code that does what it claims — never by the presence of a file, a merged PR, or a plan's self-reported status.
- **A `FAIL` downgrades `Implemented:` to PARTIAL**, which makes the verdict INCOMPLETE and stops `/deploy-activate` at its completeness gate. That is the intended coupling — do not report a defective project as LIVE.
- **Never mark `PASS` on something unverified.** An unverifiable concern is `CONCERNS` with a HYPOTHESIS finding, and a check that could not run is reported as `could-not-run`. Silence is not evidence of correctness.
- **"Activated" ≠ "implemented."** A merged feature sitting behind `FOO_ENABLED=0` (default off), or a scheduled workflow that has not reached the default branch, is implemented-but-dormant. Say so plainly and give the exact flip.
- **Evidence-based.** Cite the workflow trigger, the flag's default and read-site (`file:line`), and the merged PR#. Do not guess whether something runs — read the YAML and the gate.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never "automatic." `cron` requires the workflow on the default branch. `repository_dispatch` requires a dispatcher and token scope (§14). Name which one applies.
- **Account for secrets/vars the run needs.** An unset required secret or an empty required roster var means dormant even if the code is perfect.
- **Apply the §18 lens.** The bar for "will it start working automatically" is "it runs from code on its trigger with no operator action." If activation requires an operator to run a script or a mongo command, that is DORMANT (and, for new work, a §18.A violation worth flagging).
