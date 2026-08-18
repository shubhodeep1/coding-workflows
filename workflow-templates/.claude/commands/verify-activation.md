Given a GitHub issue (number / URL) — or any reference that identifies a project (PR, plan doc, feature name) — in `$ARGUMENTS`, determine two things: (1) is the project **fully implemented**, and (2) is it **activated** — will it **start working automatically** on its trigger, or does something still need to be done to make it run? A project in this repo can live on one of two sides — **this consumer repo's own code/config**, or the **upstream workflow library** (`shubhodeep1/coding-workflows`) that this repo's wrappers call — and you decide which from the evidence. Read-only: this command reports in chat and never edits files.

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

4. **Implementation check.** Verify the code / config / workflows / contracts the project requires exist and are correct, **at the ref for its side** (`THIS_REPO@main` for `[CONSUMER]`; `shubhodeep1/coding-workflows@UPSTREAM_SHA` for `[UPSTREAM]`). Confirm linked PRs are **merged**. Classify **COMPLETE / PARTIAL / NOT** with citations.

5. **Activation check — the load-bearing question.** Implemented ≠ running. Determine whether it will actually execute:
   - **Wrapper wiring (consumer side)** — does this repo have the `.github/workflows/*.yml` wrapper with the right trigger, calling the upstream reusable workflow at the expected ref? A feature that exists upstream but is **not wired into a consumer wrapper** will never run here.
   - **Trigger semantics** — **cron** only fires from the **default branch**; **`repository_dispatch`** needs a dispatcher + token scope; **`workflow_dispatch`** is manual-only; `pull_request` / `push` fire on the matching event.
   - **Feature flags / repo-vars / env** — is it gated behind a flag/var that **defaults OFF** (e.g. `*_ENABLED=0`, an empty roster var)? Consumers commonly must *opt in* by setting a repo-var. Name the exact variable, where it is read, and its default.
   - **Secrets** — does the run need a secret (a model key, `GH_PAT`, a Telegram token) the consumer must add in repo settings? Unset → dormant.
   - **Upstream version gap** — is this repo pinned to an `UPSTREAM_TAG` that **predates** the feature? If the feature landed upstream after the pinned release, it is not available here until the pin is bumped — that is the activation step.
   - **Supervisor / DB gates** — does it need a started supervisor or a code-gated migration rather than a manual step?

6. **Verdict.**
   - **LIVE** — implemented *and* activated; runs automatically. State the trigger and the side.
   - **DORMANT** — implemented but needs a manual step. Enumerate the **exact** steps (set repo-var `X=1`, add secret `Y`, add/adjust the wrapper workflow, bump the upstream pin to `@vA.B.C`, merge to the default branch, start a supervisor).
   - **INCOMPLETE** — not fully implemented on its side; list what is missing.

7. **Report.** Emit the [Output Format](#output-format). Read-only — no edits.

## Output Format

```
Summary: <parsed reference; project in one phrase; side ([CONSUMER]/[UPSTREAM]/[BOTH]); verdict>

Project: <issue #N — title>  (linked PRs: #…, merged? yes/no)
Side: [CONSUMER] THIS_REPO@main  |  [UPSTREAM] shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>)  |  [BOTH]
Implemented: COMPLETE / PARTIAL / NOT — <evidence: file:line, merged PR#>
Activated: YES / NO — <the gate: wrapper wiring / repo-var default / secret / upstream pin>
Will it run automatically?: YES (trigger: <cron from default branch | push | pull_request | repository_dispatch>) / NO

To activate (only if not already automatic):
1. <exact step — set repo-var X=1; add secret Y; add/adjust wrapper .github/workflows/Z.yml; bump upstream pin @vA.B.C → @vA.B.D; merge to default branch>

Gaps / risks:
- <not wired into a wrapper, repo-var default-off, unset secret, upstream pin predates the feature, cron not on default branch>
```

Omit empty sections; keep every claim cited to a ref.

## Tool Access

Read-only surface:

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `list_commits`, `list_tags`, `get_tag`, `get_file_contents`, `search_issues`, `search_pull_requests`. For the upstream side, pass `ref=<UPSTREAM_SHA>` on every read; for the consumer side, read `THIS_REPO@main`.
- **`gh` CLI** — the `GH_TOKEN` transport, when `gh` is installed; shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene) — see **CLAUDE.md §23**. Use the SessionStart slug for `THIS_REPO` and `shubhodeep1/coding-workflows` for upstream reads. Verification is §23.A read-only work.
- **`Read` / `Grep` / `Glob`** — verify the consumer wrapper, triggers, and flag defaults on the local checkout. **`Bash`** for read-only inspection only.

## Rules

- **Read-only.** A verdict, not a change. No edits, no commits, no PR.
- **Pick the side from evidence.** A feature implemented upstream but not wired into a consumer wrapper, or gated behind a repo-var the consumer never set, is **DORMANT for this repo** even though the upstream code is perfect. Say which side owns the gate.
- **Pin upstream reads to the consumer's ref.** Analyzing upstream activation at `main` when this repo is pinned to a release is wrong — the imprecision can flip the verdict. Read at `UPSTREAM_SHA`.
- **"Activated" ≠ "implemented."** Name the exact gate: wrapper wiring, a `*_ENABLED` default, an unset secret, or an upstream pin that predates the feature.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never automatic. `cron` requires the workflow on the default branch. `repository_dispatch` needs a dispatcher + token scope.
- **The most common consumer activation steps are opt-in:** setting a repo-var, adding a secret, adding/adjusting a wrapper workflow, or bumping the upstream pin. Enumerate them precisely so the user can act without guessing.
