Given a GitHub issue (number / URL) — or any reference that identifies a project (PR, plan doc, feature name) — in `$ARGUMENTS`, determine two things about **this** repo (`shubhodeep1/coding-workflows`) at `main`: (1) is the project **fully implemented**, and (2) is it **activated** — i.e. will it **start working automatically** on its trigger, or does something still need to be done to make it run? Read-only: this command reports in chat and never edits files. `$ARGUMENTS` is free-form and should contain at least one concrete reference (`#1234`, an issue/PR URL, a plan path, or a clearly-named feature).

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Extract the issue number / URL, PR refs, plan-doc path, or feature name. If there is no concrete reference — just vague prose — stop and ask for one. Restate the parsed reference in the `Summary`.

2. **Understand the intended project.** Fetch the issue and everything linked to it — linked PRs, tracking comments, sub-issues, the orchestrator state comment if present — via `mcp__github__issue_read` / `pull_request_read` (or `gh`, see [Tool Access](#tool-access)). Pin down: what it builds, its acceptance criteria, and **how it is meant to run** — a cron schedule, a `pull_request` / `push` trigger, `repository_dispatch`, `workflow_dispatch` (manual), a long-running supervisor (§18.C), or on-demand. Also read `README.md` / `agents.md` for the feature's documented behavior and any env-var / repo-var contract.

3. **Implementation check.** Verify on `main` that the code / config / workflows / contracts the project requires actually exist and are correct: `Grep` / `Read` the implicated files, and confirm the linked PRs are **merged** (an open PR means not-yet-implemented). Classify **COMPLETE / PARTIAL / NOT**, with `file:line` and merged-PR citations.

4. **Activation check — the load-bearing question.** Implemented code does not mean *running* code. Determine whether it will actually execute:
   - **Workflow wiring** — is there a `.github/workflows/*.yml` with the right trigger, and is it reachable (not disabled, not gated behind an `if:` that is always false)?
   - **Trigger semantics** — a **cron** schedule only fires from the **default branch**, so a scheduled workflow that has not reached the default branch will never run; **`repository_dispatch`** needs a dispatcher *and* a token with scope (§14); **`workflow_dispatch`** is manual-only (never "automatic"); `pull_request` / `push` fire on the matching event.
   - **Feature flags / env vars / repo-vars** — is the feature gated behind a flag that **defaults OFF** (e.g. `*_ENABLED=0`, an empty roster var)? If so it is *implemented but dormant*. Name the exact variable, where it is read, and its default.
   - **Secrets / credentials** — does the run need a secret (`GH_PAT`, `TG_BOT_SECRET`, `TG_ADMIN_CHAT_ID`, a model key) that may be unset? An unset required secret means dormant.
   - **Supervisor / long-running** (§18.C) — does it need a supervisor that is actually started and wired into startup automation?
   - **DB gates** (§18.D) — does a backfill/migration run from code behind a gate, or is it waiting on a manual step (which would violate §18.A)?
   - **Consumer propagation** (§14) — if it is a workflow-template / `.claude` change, are consumers wired (`.github/ai/consumer_repos.json`, the `@stable` dispatch), and does activation require tagging a new `@stable` release?

5. **Verdict.** Combine the two checks into one of:
   - **LIVE** — implemented *and* activated; it runs automatically. State the exact trigger.
   - **DORMANT** — implemented but it will not run until a manual step. Enumerate the **exact** steps to activate (set `VAR=1`, add secret `Y`, merge to the default branch, flip a flag, start a supervisor, tag `@stable` for consumers).
   - **INCOMPLETE** — not fully implemented; list what is missing before activation is even possible.

6. **Report.** Emit the [Output Format](#output-format). Read-only — no edits, no PR.

## Output Format

```
Summary: <parsed reference; project in one phrase; verdict in one word>

Project: <issue #N — title>  (linked PRs: #…, merged? yes/no)
Implemented: COMPLETE / PARTIAL / NOT — <evidence: file:line, merged PR#>
Activated: YES / NO — <the gate: trigger / flag default / secret>
Will it run automatically?: YES (trigger: <cron «expr» from default branch | push | pull_request | repository_dispatch>) / NO

To activate (only if not already automatic):
1. <exact step — set REPO_VAR X=1 (read at <file:line>, default 0); add secret Y; merge <branch> to default; tag @stable; start supervisor Z>

Gaps / risks:
- <missing implementation, unset required secret, flag default-off, cron not on default branch, consumers not dispatched>
```

Omit empty sections; keep every claim cited.

## Tool Access

Read-only surface:

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `list_commits`, `get_file_contents`, `search_issues`, `search_pull_requests` for the project and its merge state. Read at `main`.
- **`gh` CLI** — the `GH_TOKEN` transport; shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene) — see **CLAUDE.md §23**. The SessionStart hook prints the resolved slug (`shubhodeep1/coding-workflows`). Verification is §23.A read-only work.
- **`Read` / `Grep` / `Glob`** — verify the implementation, the workflow triggers, and the flag defaults on the local `main` checkout. **`Bash`** for read-only inspection only.

## Rules

- **Read-only.** A verdict, not a change. No edits, no commits, no PR. If the project is DORMANT and the user wants it activated, that is a follow-up task (e.g. `/implement-plan-claude`, `/implement-plan-ai`, or a direct change) — this command only diagnoses.
- **"Activated" ≠ "implemented."** A merged feature sitting behind `FOO_ENABLED=0` (default off), or a scheduled workflow that has not reached the default branch, is implemented-but-dormant. Say so plainly and give the exact flip.
- **Evidence-based.** Cite the workflow trigger, the flag's default and read-site (`file:line`), and the merged PR#. Do not guess whether something runs — read the YAML and the gate.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never "automatic." `cron` requires the workflow on the default branch. `repository_dispatch` requires a dispatcher and token scope (§14). Name which one applies.
- **Account for secrets/vars the run needs.** An unset required secret or an empty required roster var means dormant even if the code is perfect.
- **Apply the §18 lens.** The bar for "will it start working automatically" is "it runs from code on its trigger with no operator action." If activation requires an operator to run a script or a mongo command, that is DORMANT (and, for new work, a §18.A violation worth flagging).
