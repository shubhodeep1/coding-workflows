Given a GitHub issue (number / URL) — or any reference that identifies a project (PR, plan doc, feature name) — in `$ARGUMENTS`, determine three things about **this** repo (`shubhodeep1/coding-workflows`) at `main`: (1) is the project **fully implemented**, (2) is it **correct** — does the code actually do what the plan / issue specified, audited rather than assumed — and (3) is it **activated** — i.e. will it **start working automatically** on its trigger, or does something still need to be done to make it run? Read-only: this command reports in chat and never edits files. `$ARGUMENTS` is free-form and should contain at least one concrete reference (`#1234`, an issue/PR URL, a plan path, or a clearly-named feature).

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

   Report findings; do not fix them (see [Rules](#rules)).

5. **Activation check — the load-bearing question.** Implemented code does not mean *running* code. Determine whether it will actually execute:
   - **Workflow wiring** — is there a `.github/workflows/*.yml` with the right trigger, and is it reachable (not disabled, not gated behind an `if:` that is always false)?
   - **Trigger semantics** — a **cron** schedule only fires from the **default branch**, so a scheduled workflow that has not reached the default branch will never run; **`repository_dispatch`** needs a dispatcher *and* a token with scope (§14); **`workflow_dispatch`** is manual-only (never "automatic"); `pull_request` / `push` fire on the matching event.
   - **Feature flags / env vars / repo-vars** — is the feature gated behind a flag that **defaults OFF** (e.g. `*_ENABLED=0`, an empty roster var)? If so it is *implemented but dormant*. Name the exact variable, where it is read, and its default.
   - **Secrets / credentials** — does the run need a secret (`GH_PAT`, `TG_BOT_SECRET`, `TG_ADMIN_CHAT_ID`, a model key) that may be unset? An unset required secret means dormant.
   - **Supervisor / long-running** (§18.C) — does it need a supervisor that is actually started and wired into startup automation?
   - **DB gates** (§18.D) — does a backfill/migration run from code behind a gate, or is it waiting on a manual step (which would violate §18.A)?
   - **Consumer propagation** (§14) — if it is a workflow-template / `.claude` change, are consumers wired (`.github/ai/consumer_repos.json`, the `@stable` dispatch), and does activation require tagging a new `@stable` release?

6. **Verdict.** Combine the three checks into one of:
   - **LIVE** — implemented, correctness-audited, *and* activated; it runs automatically. State the exact trigger. A project with `Correctness: CONCERNS` can still be LIVE — say so with the concerns attached.
   - **DORMANT** — implemented but it will not run until a manual step. Enumerate the **exact** steps to activate (set `VAR=1`, add secret `Y`, merge to the default branch, flip a flag, start a supervisor, tag `@stable` for consumers).
   - **INCOMPLETE** — not fully implemented, **including a Step 4 FAIL that downgraded `Implemented:` to PARTIAL**; list what is missing or defective before activation is even possible.

7. **Report.** Emit the [Output Format](#output-format). Read-only — no edits, no PR.

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

To activate (only if not already automatic):
1. <exact step — set REPO_VAR X=1 (read at <file:line>, default 0); add secret Y; merge <branch> to default; tag @stable; start supervisor Z>

Gaps / risks:
- <missing implementation, defect that downgraded the verdict, unset required secret, flag default-off, cron not on default branch, consumers not dispatched>
```

Omit empty sections; keep every claim cited.

## Tool Access

Read-only surface:

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read` (including its files method, for the Step 4 audit surface), `list_commits`, `get_file_contents`, `search_issues`, `search_pull_requests` for the project and its merge state. Read at `main`.
- **`gh` CLI** — the `GH_TOKEN` transport; shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene) — see **CLAUDE.md §23**. The SessionStart hook prints the resolved slug (`shubhodeep1/coding-workflows`). Verification is §23.A read-only work.
- **`Read` / `Grep` / `Glob`** — verify the implementation, audit the code, and read the workflow triggers and flag defaults on the local `main` checkout.
- **`Bash`** — read-only inspection, plus the Step 4 checks: running the repo's existing tests, linters, and validators against the local checkout. Never `Edit` / `Write`, never `git commit` / `push`, never `gh workflow run` or any other mutating call.
- **Subagents** (§16) — optional read-only fan-out (`Explore` / Sonnet) when the Step 4 audit surface is large; the parent judges the findings.

## Rules

- **Read-only.** A verdict, not a change. No edits, no commits, no PR. Step 4 may *execute* the repo's tests and linters, but only in a form that changes nothing (see [Tool Access](#tool-access)). If the project is DORMANT, or the audit found defects, and the user wants them addressed, that is a follow-up task (e.g. `/investigate-issue`, `/code-review --fix`, `/implement-plan-claude`, `/implement-plan-ai`, or a direct change) — this command only diagnoses.
- **"Implemented" ≠ "correct."** Code that exists, is wired, and is reachable can still do the wrong thing. COMPLETE is earned by tracing every acceptance criterion to code that does what it claims — never by the presence of a file, a merged PR, or a plan's self-reported status.
- **A `FAIL` downgrades `Implemented:` to PARTIAL**, which makes the verdict INCOMPLETE and stops `/deploy-activate` at its completeness gate. That is the intended coupling — do not report a defective project as LIVE.
- **Never mark `PASS` on something unverified.** An unverifiable concern is `CONCERNS` with a HYPOTHESIS finding, and a check that could not run is reported as `could-not-run`. Silence is not evidence of correctness.
- **"Activated" ≠ "implemented."** A merged feature sitting behind `FOO_ENABLED=0` (default off), or a scheduled workflow that has not reached the default branch, is implemented-but-dormant. Say so plainly and give the exact flip.
- **Evidence-based.** Cite the workflow trigger, the flag's default and read-site (`file:line`), and the merged PR#. Do not guess whether something runs — read the YAML and the gate.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never "automatic." `cron` requires the workflow on the default branch. `repository_dispatch` requires a dispatcher and token scope (§14). Name which one applies.
- **Account for secrets/vars the run needs.** An unset required secret or an empty required roster var means dormant even if the code is perfect.
- **Apply the §18 lens.** The bar for "will it start working automatically" is "it runs from code on its trigger with no operator action." If activation requires an operator to run a script or a mongo command, that is DORMANT (and, for new work, a §18.A violation worth flagging).
