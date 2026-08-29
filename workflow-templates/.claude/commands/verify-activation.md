Given a GitHub issue (number / URL) — or any reference that identifies a project (PR, plan doc, feature name) — in `$ARGUMENTS`, determine three things: (1) is the project **fully implemented**, (2) is it **correct** — does the code actually do what the plan / issue specified, audited rather than assumed — and (3) is it **activated** — will it **start working automatically** on its trigger, or does something still need to be done to make it run? A project in this repo can live on one of two sides — **this consumer repo's own code/config**, or the **upstream workflow library** (`shubhodeep1/coding-workflows`) that this repo's wrappers call — and you decide which from the evidence. Read-only: this command reports in chat and never edits files.

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

   Report findings; do not fix them (see [Rules](#rules)). A defect on the `[UPSTREAM]` side is not this repo's to patch — route it via `/validate-consumer-issue`.

6. **Activation check — the load-bearing question.** Implemented ≠ running. Determine whether it will actually execute:
   - **Wrapper wiring (consumer side)** — does this repo have the `.github/workflows/*.yml` wrapper with the right trigger, calling the upstream reusable workflow at the expected ref? A feature that exists upstream but is **not wired into a consumer wrapper** will never run here.
   - **Trigger semantics** — **cron** only fires from the **default branch**; **`repository_dispatch`** needs a dispatcher + token scope; **`workflow_dispatch`** is manual-only; `pull_request` / `push` fire on the matching event.
   - **Feature flags / repo-vars / env** — is it gated behind a flag/var that **defaults OFF** (e.g. `*_ENABLED=0`, an empty roster var)? Consumers commonly must *opt in* by setting a repo-var. Name the exact variable, where it is read, and its default.
   - **Secrets** — does the run need a secret (a model key, `GH_PAT`, a Telegram token) the consumer must add in repo settings? Unset → dormant.
   - **Upstream version gap** — is this repo pinned to an `UPSTREAM_TAG` that **predates** the feature? If the feature landed upstream after the pinned release, it is not available here until the pin is bumped — that is the activation step.
   - **Supervisor / DB gates** — does it need a started supervisor or a code-gated migration rather than a manual step?

7. **Verdict.**
   - **LIVE** — implemented, correctness-audited, *and* activated; runs automatically. State the trigger and the side. A project with `Correctness: CONCERNS` can still be LIVE — say so with the concerns attached.
   - **DORMANT** — implemented but needs a manual step. Enumerate the **exact** steps (set repo-var `X=1`, add secret `Y`, add/adjust the wrapper workflow, bump the upstream pin to `@vA.B.C`, merge to the default branch, start a supervisor).
   - **INCOMPLETE** — not fully implemented on its side, **including a Step 5 FAIL that downgraded `Implemented:` to PARTIAL**; list what is missing or defective.

8. **Report.** Emit the [Output Format](#output-format). Read-only — no edits.

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

To activate (only if not already automatic):
1. <exact step — set repo-var X=1; add secret Y; add/adjust wrapper .github/workflows/Z.yml; bump upstream pin @vA.B.C → @vA.B.D; merge to default branch>

Gaps / risks:
- <not wired into a wrapper, defect that downgraded the verdict, repo-var default-off, unset secret, upstream pin predates the feature, cron not on default branch>
```

Omit empty sections; keep every claim cited to a ref.

## Tool Access

Read-only surface:

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read` (including its files method, for the Step 5 audit surface), `list_commits`, `list_tags`, `get_tag`, `get_file_contents`, `search_issues`, `search_pull_requests`. For the upstream side, pass `ref=<UPSTREAM_SHA>` on every read; for the consumer side, read `THIS_REPO@main`.
- **`gh` CLI** — the `GH_TOKEN` transport, when `gh` is installed; shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene) — see **CLAUDE.md §23**. Use the SessionStart slug for `THIS_REPO` and `shubhodeep1/coding-workflows` for upstream reads. Verification is §23.A read-only work.
- **`Read` / `Grep` / `Glob`** — verify the consumer wrapper, audit the code, and read the triggers and flag defaults on the local checkout.
- **`Bash`** — read-only inspection, plus the Step 5 checks: running this repo's existing tests, linters, and validators against the local checkout. Never `Edit` / `Write`, never `git commit` / `push`, never `gh workflow run` or any other mutating call.
- **Subagents** — optional read-only fan-out (`Explore` / Sonnet) when the Step 5 audit surface is large; the parent judges the findings.

## Rules

- **Read-only.** A verdict, not a change. No edits, no commits, no PR. Step 5 may *execute* this repo's tests and linters, but only in a form that changes nothing (see [Tool Access](#tool-access)). Defects go to a follow-up — `/investigate-issue` for a consumer-side defect, `/validate-consumer-issue` for an upstream one.
- **"Implemented" ≠ "correct."** Code that exists, is wired, and is reachable can still do the wrong thing. COMPLETE is earned by tracing every acceptance criterion to code that does what it claims — never by the presence of a file, a merged PR, or a plan's self-reported status.
- **A `FAIL` downgrades `Implemented:` to PARTIAL**, which makes the verdict INCOMPLETE and stops `/deploy-activate` at its completeness gate. That is the intended coupling — do not report a defective project as LIVE.
- **Never mark `PASS` on something unverified.** An unverifiable concern is `CONCERNS` with a HYPOTHESIS finding, and a check that could not run is reported as `could-not-run`. Silence is not evidence of correctness, and an upstream side that could only be read is not thereby proven correct.
- **Pick the side from evidence.** A feature implemented upstream but not wired into a consumer wrapper, or gated behind a repo-var the consumer never set, is **DORMANT for this repo** even though the upstream code is perfect. Say which side owns the gate.
- **Pin upstream reads to the consumer's ref.** Analyzing upstream activation at `main` when this repo is pinned to a release is wrong — the imprecision can flip the verdict. Read at `UPSTREAM_SHA`.
- **"Activated" ≠ "implemented."** Name the exact gate: wrapper wiring, a `*_ENABLED` default, an unset secret, or an upstream pin that predates the feature.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never automatic. `cron` requires the workflow on the default branch. `repository_dispatch` needs a dispatcher + token scope.
- **The most common consumer activation steps are opt-in:** setting a repo-var, adding a secret, adding/adjusting a wrapper workflow, or bumping the upstream pin. Enumerate them precisely so the user can act without guessing.
