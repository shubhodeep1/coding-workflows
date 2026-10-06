Given a reference to a **completed** project in `$ARGUMENTS` — a GitHub issue (number / URL), a PR, a plan doc, or a clearly-named feature — walk me through **deploying it and making it active** in this repo (`shubhodeep1/coding-workflows`), **one step at a time**, starting from a **fresh MacBook with no GitHub repos synced**. This is the action companion to `/verify-activation`: that command diagnoses (LIVE / DORMANT / INCOMPLETE) and ships code fixes for the defects it finds in a PR, but never performs operator activation steps; this one drives a DORMANT-but-complete project all the way to LIVE. Interactive by contract: emit **exactly one step**, then **stop and wait** for me to paste the command output (or say `done` / `next`) before emitting the next step. The deploy commands run on **my** machine — you guide, I execute and paste back; never run the mutating deploy steps yourself. **Two exceptions: DigitalOcean and Cloudflare steps.** When the session has `DIGITALOCEAN_ACCESS_TOKEN`, you run DigitalOcean API calls yourself per [DigitalOcean Steps](#digitalocean-steps); when it has the Cloudflare credential for the target site (`FUNTOKEN_IO_CF` or `FT_GAMES_CF`), you run Cloudflare calls yourself per [Cloudflare Steps](#cloudflare-steps) — in both cases reads freely, mutations only after I approve the emitted step. `$ARGUMENTS` should contain at least one concrete reference (`#1234`, an issue/PR URL, a plan path, or a clearly-named feature). **Resumable across sessions:** progress is persisted to a per-project activation log in the repo (`docs/deploy-activation/<ref-slug>.md`). Before emitting any step you **read that log first** and resume from the first not-yet-done step; after each completed step you update and push the log, so a fresh session — new machine, re-cloned container, or a later day — picks up exactly where the last one stopped instead of restarting the runbook. See [Activation Log](#activation-log).

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`, then load the activation log.** Extract the issue number / URL, PR refs, plan-doc path, or feature name. If there is no concrete reference — just vague prose — stop and ask for one. Restate the parsed reference in the opening summary. Then **read the activation log first** (see [Activation Log](#activation-log)): compute the log path from the reference and read `docs/deploy-activation/<ref-slug>.md`. If a log exists, it is the source of truth for progress — resume from the first step not marked `[x]`, skip the steps already done (and say so), and if it shows `Status: LIVE` report LIVE and stop. If I instead paste a plain statement of which steps are completed / remaining, reconcile that into the log before resuming. If no log exists, you will create one when you emit Step 1.

2. **Build the full deploy plan internally, before emitting Step 1.** Do all the read-only analysis up front so the runbook is stable; only the *delivery* is one-at-a-time.

   a. **Understand the project.** Fetch the issue and everything linked to it — linked PRs, tracking comments, sub-issues, the orchestrator state comment if present — via `mcp__github__issue_read` / `pull_request_read` (or `gh`, see [Tool Access](#tool-access)). Pin down what it builds, its acceptance criteria, and **how it is meant to run**: a cron schedule, a `pull_request` / `push` trigger, `repository_dispatch`, `workflow_dispatch` (manual), a long-running supervisor (§18.C), or on-demand. Read `README.md` / `agents.md` for the env-var / repo-var / secret contract.

   b. **Completeness gate — STOP if not complete.** This command is scoped to *completed* projects. Verify on the implicated branch (and on `main` where relevant) that the code / config / workflows / contracts actually exist and that the project's PR(s) are **merged or genuinely merge-ready**. Cheap checks: `Grep` / `Read` the files the project names; confirm linked-PR merge state. If it is clearly **INCOMPLETE** (code missing, PR neither merged nor open, scaffold-only), **do not emit a deploy runbook** — report exactly what is missing, point me to `/implement-plan-claude` or `/implement-plan-ai` (both hand off to the AI orchestrator), and stop.

   c. **Enumerate the activation gates** that stand between "code merged" and "runs automatically," using the `/verify-activation` lens — for *this* repo specifically:
      - **Default-branch reachability** — a **cron** schedule only fires from the **default branch**. If the project lives on a branch / unmerged PR, merging to `main` is an activation step. A Cloudflare Worker deploy from this command also requires the project's code on the protected default branch before any deploy step.
      - **Repo-vars / feature flags that default OFF** — e.g. `*_ENABLED=0`, an empty roster var. Name each variable, its **read-site (`file:line`)**, and its default.
      - **Required secrets** — does the run need `GH_PAT`, `TG_BOT_SECRET`, `TG_ADMIN_CHAT_ID`, a model API key, etc.? An unset required secret means dormant.
      - **Consumer propagation (§14)** — if it is a `workflow-templates/**` or `.claude/**` change consumers must pick up, activation includes **tagging a new `@stable` release** and confirming the `repository_dispatch` to every repo in `.github/ai/consumer_repos.json` (the `GH_PAT` needs `repo` scope on each). Read the actual release / dispatch workflow under `.github/workflows/` to get the **exact** tag/dispatch mechanism — do not hardcode it.
      - **DigitalOcean-side gates (§22)** — does activation touch a DigitalOcean-hosted resource (an App's env vars or spec, a forced redeploy, a managed-database setting)? If `DIGITALOCEAN_ACCESS_TOKEN` is present, read the current DO state yourself while planning (§22.A — self-serve, no asking). Resolve App / database IDs from the `## DigitalOcean resources` table in `agents.md` (§22.C); if a needed ID is not recorded, ask for it in Q/A format and record it per [DigitalOcean Steps](#digitalocean-steps).
      - **Cloudflare-side gates (§24)** — does activation touch a Cloudflare Worker (script, version, bindings, non-secret vars, routes, cron triggers), a KV / R2 / D1 binding, or a DNS record or zone setting on a covered site? If the matching credential is present, read the current Cloudflare state yourself while planning (§24.B — self-serve, no asking). Resolve Worker / zone identifiers from the `## Cloudflare resources` table in `agents.md` (§24.F), or discover them with a read; if one can be neither, ask for it in Q/A format and record it per [Cloudflare Steps](#cloudflare-steps).
      - **Supervisor / long-running (§18.C)** — does it need a supervisor that must be started and wired into startup automation?
      - **DB gates (§18.D)** — does a backfill / migration run from code behind a gate, or is it (wrongly) waiting on a manual step?

   d. **Compose the ordered runbook** and keep it as a stable numbered list you track across turns:
      - **Prerequisite bootstrap (fresh Mac, nothing synced):** install Homebrew → `brew install git gh` (plus any tool the deploy needs) → `gh auth login` (or export `GH_TOKEN`) with the scopes the deploy needs (`repo`; for §14 dispatch, a PAT with `repo` scope on the consumer repos) → clone `shubhodeep1/coding-workflows` and check out the branch / PR that holds the project.
      - **Activation steps:** one gate per step — `gh variable set NAME --body 1 -R shubhodeep1/coding-workflows` for a repo-var flip; `gh secret set NAME -R shubhodeep1/coding-workflows` for a secret; merge the PR to `main`; tag / move `@stable` per the release workflow; verify the consumer dispatch fired.
      - **Verification:** confirm the trigger will actually fire (a scheduled run appears, the dispatch was delivered, the flag now reads ON).

3. **Deliver the runbook one step at a time.** Follow the [Interaction Protocol](#interaction-protocol) exactly. The opening message previews the plan (summary + gate list + total step count) and then gives **Step 1 only**.

4. **Finish on a verified LIVE state.** The final step verifies the project now runs automatically; close with the `✅ LIVE` line from the [Output Format](#output-format).

## Interaction Protocol

This is the load-bearing behavior — honor it strictly:

- **One step per turn.** Emit a single step, then **end the turn**. Never include Step k+1 in the same message as Step k. Each step states: a short title; the **exact** command(s) to paste into a Mac terminal; what **success** looks like; and the literal ask — *"paste the output (or say `done`) and I'll give you the next step."*
- **Wait, then react to the pasted output.** On my reply, read what I pasted. If it shows success → advance to the next step. If it shows an **error or unexpected state** → do **not** advance; diagnose and emit a **corrective step** (still one at a time). If I say `done` with no output, trust me, but where a cheap read-only check settles it, verify before moving on.
- **Track progress visibly, and persist it.** Each turn, show a compact checklist of the runbook (done ✓ / current ▶ / remaining) so I always see where we are, e.g. `Step 4 of ~9`. The count may grow if a step fails and needs a fix-up — say so. The same checklist is mirrored into the activation log: after each step is confirmed, mark it `[x]` in `docs/deploy-activation/<ref-slug>.md`, then **commit and push the log** so the next session can resume (see [Activation Log](#activation-log)).
- **Adapt to reality.** If pasted output proves a gate is already satisfied (var already `1`, PR already merged, secret already present), **skip** that step and say why.
- **Never bundle, never auto-execute.** Do not merge steps to "save time," and do not run the mutating deploy commands yourself — they run on my machine. Read-only verification commands against your own checkout (or `mcp__github__*` / `gh ... --json` reads) are fine. **DigitalOcean and Cloudflare steps are the execution exceptions:** per [DigitalOcean Steps](#digitalocean-steps) and [Cloudflare Steps](#cloudflare-steps), when the matching credential is present you run the API call yourself — reads at any time, a mutation only after I approve that emitted step, never in the same turn as its proposal.
- **Stop conditions.** STOP and ask in Q/A format if a step needs a decision with material tradeoffs, would rename/remove a §6 identifier, or is otherwise ambiguous. STOP and report if the project turns out to be incomplete mid-run.

## Activation Log

This command is **resumable**. Progress for each project is persisted to a per-project log committed in the repo, so a fresh session — new machine, re-cloned container, or just a later day — picks up at the exact next step instead of restarting the runbook.

- **Path.** `docs/deploy-activation/<ref-slug>.md`, one file per project. Derive `<ref-slug>` deterministically from the parsed reference so the same project always maps to the same file:
  - issue → `issue-<N>`  ·  PR → `pr-<N>`  ·  plan doc → `plan-<basename-without-extension>`  ·  bare feature name → `feature-<kebab-case>`.
  - If `$ARGUMENTS` carries more than one reference, key the slug off the primary one in this priority: issue → PR → plan doc → feature name, and record the secondary references inside the file.
- **Read first (mandatory).** Before emitting any step, read this file. If it exists it is the source of truth for what is done: resume from the first step not marked `[x]`, skip the `[x]` steps (say that you are skipping them), and if `Status: LIVE` just report LIVE and stop. If it does not exist, create it when you emit Step 1.
- **Accept a pasted progress statement.** If I paste a list of steps already completed / still remaining (rather than raw command output), reconcile it into the log — mark the named steps `[x]`, leave the rest open — and resume from the first open step.
- **Update after every step.** When a step's pasted output (or a `done`) confirms success, mark it `[x]` with a one-line evidence note and the date, mark the next step current, refresh `Last updated` and `Last note`, then **commit and push the log** (below). On full completion set `Status: LIVE`; if a step is blocked, set `Status: BLOCKED` and record why in `Last note`.
- **Persistence (commit & push).** The log only helps a future session if it survives this container, so writing the file is not enough — commit it and `git push` to the working branch. This is the command's own bookkeeping, **not** a mutating deploy step, so it is fine to run yourself: it never touches `shubhodeep1/coding-workflows` settings, secrets, vars, merges, or release tags. Use the date from the session context for timestamps.

**Log file format:**

```
# Deploy-Activation Log — <project in one phrase>

- Reference: <#1234 / URL / plan path / feature>   (+ any secondary refs)
- Deploy target: shubhodeep1/coding-workflows
- How it runs: <cron «expr» from default branch | push | pull_request | repository_dispatch | supervisor>
- Status: IN_PROGRESS | BLOCKED | LIVE
- Last updated: <YYYY-MM-DD>
- Last note: <one line — where the last session stopped / why blocked>

## Runbook
1. [x] Prereqs: Homebrew, git, gh, auth, clone + checkout   — done <YYYY-MM-DD>: <evidence>
2. [x] Set repo-var X=1 (read at file:line, default 0)      — done <YYYY-MM-DD>: var now 1
3. [ ] Merge PR #N to main (cron needs default branch)
4. [ ] Verify LIVE

## Notes
- <free-form: errors hit, steps skipped because already-satisfied and why, decisions made>
```

## DigitalOcean Steps

Deploy steps that touch DigitalOcean — reading or changing an App's env vars or spec, forcing a redeploy, checking deployment status, managed-database settings, build/runtime logs — follow CLAUDE.md §22, which overrides this command's default "you guide, I execute" split:

- **Execute yourself when the token is present.** If `DIGITALOCEAN_ACCESS_TOKEN` is set (check nounset-safe: `[ -n "${DIGITALOCEAN_ACCESS_TOKEN:-}" ]`), you run the DigitalOcean API calls — via `doctl` when installed, otherwise the REST API (§22.A transport) — instead of handing me commands to paste. **Reads** (app spec, deployed env vars, deployment status, logs) are self-serve at any point while building or adapting the runbook (§22.A). **Mutations** (update an app's spec or env vars, force a redeploy, change a database setting) stay inside the one-step-at-a-time loop: emit the step naming the exact resource, the exact change, and the billing impact where known (§22.B); when I confirm (`done` / `go`), run it yourself, show the API output, and mark the step done. Never run a DO mutation in the same turn that proposes it.
- **Resource IDs come from the agents file (§22.C).** Before any DO call, read the `## DigitalOcean resources` table in `agents.md` and use the recorded App / database / Droplet IDs without re-asking. If a needed ID is **not** recorded: ask for it in §2 Q/A format (free-text answer allowed for the ID itself), verify it resolves with one read call, then **add it to that table** — creating the section if the file lacks it — and commit/push it together with the activation-log bookkeeping, so no future session ever asks for it again.
- **No token → fall back to guide-and-paste.** If the token is unset, or the API returns 401/403, say so and deliver the DO steps in this command's default mode: exact `doctl` / `curl` commands for me to run on my Mac (where I hold my own credentials) and paste back. Do not retry-loop, and do not ask me to fetch data the token could have fetched if it were present.
- **Token hygiene (§22.A) is unchanged.** Never print the token; reference it only as `$DIGITALOCEAN_ACCESS_TOKEN`; redact it from anything you echo into the conversation or the activation log.

## Cloudflare Steps

Deploy steps that touch Cloudflare — reading or changing a Worker's script, versions, bindings, non-secret vars, routes, or cron triggers; reading KV / R2 / D1 state; reading or changing DNS or zone settings on a covered site; tailing Worker logs — follow CLAUDE.md §24, which overrides this command's default "you guide, I execute" split the same way §22 does for DigitalOcean:

- **Pick the credential by site (§24.A).** `FUNTOKEN_IO_CF` covers `funtoken.io`; `FT_GAMES_CF` covers `ft.games` and `5m.fun`. They are different accounts and not interchangeable. If the target site is neither, stop and ask (§2 Q/A) instead of guessing. Check presence nounset-safe (e.g. `[ -n "${FUNTOKEN_IO_CF:-}" ]`), split the value on the first colon into account ID and API token, and use `wrangler` (with `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` exported from the two halves) when installed, otherwise the REST API (§24.A transport).
- **Execute yourself when the credential is present.** **Reads** (Worker scripts, settings, bindings, routes, deployments / versions, DNS records, KV / R2 / D1 listings, `wrangler tail`, analytics) are self-serve at any point while building or adapting the runbook (§24.B). **Worker deploys and edits** (§24.C — uploading a new Worker or version, editing bindings, non-secret vars, routes on covered zones, or cron triggers) stay inside the one-step-at-a-time loop. Apply the verification and credential isolation below before emitting the step. When I confirm (`done` / `go`), recheck the pin and run the approved change yourself. Prefer versioned uploads and never delete the previous version as part of a deploy. Never run a Cloudflare mutation in the same turn that proposes it.
- **Deploy only a verified, immutable default-branch commit.** If the project's PR is unmerged (even if merge-ready), make merging PR #N to the default branch an operator-run activation step *before* any Cloudflare deploy step. Resolve the actual default branch via `gh repo view --json defaultBranchRef`, then read `DEPLOY_SHA` via `gh api repos/<owner>/<repo>/commits/<default> --jq .sha` and require `gh api repos/<owner>/<repo>/branches/<default> --jq .protected` to return `true`. Fetch that branch (`git fetch origin <default>`), check out a detached worktree at precisely `DEPLOY_SHA` (`git worktree add --detach <tmp> "$DEPLOY_SHA"`), and verify `git -C <tmp> rev-parse HEAD` equals `DEPLOY_SHA` and `git -C <tmp> status --porcelain` is empty. Do not substitute the PR checkout or a moving ref. Recheck the API tip, protection, worktree HEAD and cleanliness just before executing the approved step; name `DEPLOY_SHA` in the proposed step and activation-log evidence. If protection, pinning, worktree verification or pre-deploy checks fail or cannot be confirmed, mark the Cloudflare step BLOCKED in the activation log and give only a corrective verification step. Never emit a Worker deploy command, including one for the operator to run, until the protected default-branch SHA, clean pinned worktree and pre-deploy checks are verified; recheck immediately before deployment.
- **Never execute unmerged code with credentials.** Never run repository code from an unmerged PR or branch in a shell holding `FUNTOKEN_IO_CF`, `FT_GAMES_CF`, `CLOUDFLARE_*`, `DIGITALOCEAN_ACCESS_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, or any other session credential. Test scripts, `npm ci` / `npm install` lifecycle scripts, build scripts and `wrangler deploy --dry-run` (which can execute `[build].command`) all count as execution; `Read` / `Grep` inspection does not.
- **Pre-deploy checks.** Prefer the GitHub check-runs for `DEPLOY_SHA` (`gh api repos/<owner>/<repo>/commits/$DEPLOY_SHA/check-runs`, read only); do not emit a deploy step while checks are failing or pending. When Wrangler and a safe sandbox are available, require a successful `wrangler deploy --dry-run` for `DEPLOY_SHA` before proposing the Worker deploy step; a failed dry run blocks deployment, not just the local check. Run this and any optional local checks only on the verified worktree inside a credential-free, no-egress sandbox: clear the environment with `env -i PATH="$PATH" HOME="$(mktemp -d)"`, and isolate network with `docker run --network none` (mount only the verified source read-only, never the host home, git credentials, or Docker socket) or `unshare -rn` with an equally isolated filesystem and home. If no such isolation is available, skip local checks and rely on the check-runs; never run the checks in the credential-bearing session shell.
- **Limit deploy credential exposure.** Deploy only from the verified worktree; confirm the Worker `name` in `wrangler.toml` matches the Worker recorded in `agents.md` under `## Cloudflare resources`. Run Wrangler with an allowlisted environment: `env -i PATH="$PATH" HOME="$(mktemp -d)" CLOUDFLARE_ACCOUNT_ID="$CF_ACCOUNT_ID" CLOUDFLARE_API_TOKEN="$CF_API_TOKEN" wrangler deploy` from that worktree (using the matching site's credential halves parsed per §24.A). Do not inherit any other session credentials: Wrangler build hooks may execute code during deployment. Use a clean HOME without stored tool credentials. The account-scoped Workers Scripts token cannot be narrowed to a single Worker here; provisioning a more restricted token is an operator task. For REST uploads, source the script from the same verified commit.
- **§24.D operations are ask-first on top of the step loop.** Deleting a Worker, route, custom domain, or cron trigger; creating, changing, or deleting DNS records or zone settings; deleting KV / R2 / D1 data; and account-level configuration each need a §2 Q/A question naming the account (env var), zone, Worker, object, and change before the step is emitted. After approval, you run it.
- **Secret values never transit the chat.** For a Worker secret (`wrangler secret put`), emit the exact command for me to run on my Mac and paste back the result; never ask me to paste the value, and never set it yourself from a value seen in the conversation.
- **Identifiers come from the agents file (§24.F).** Before any Cloudflare call, read the `## Cloudflare resources` table in `agents.md` and use recorded Worker names, zone IDs, routes, and namespace IDs without re-asking; prefer discovering a zone ID with a read over asking. If a needed identifier is neither recorded nor discoverable, ask for it in §2 Q/A format, verify it resolves with one read call, then **add it to that table** — creating the section if the file lacks it — and commit/push it together with the activation-log bookkeeping.
- **No credential → pause Cloudflare steps.** If the matching credential is unset, or an account-scoped read (the `workers/scripts` list) returns 401/403, say so once, name which credential failed, mark the Cloudflare step BLOCKED in the activation log, and continue only with independent non-Cloudflare work. Do not give manual Cloudflare API or deploy commands or ask me to run them; resume only when the matching session credential works and all deploy preconditions above have been verified. Do not diagnose the credential from `GET /client/v4/user/tokens/verify` — it returns 401 for these account-owned tokens even when they work. Do not retry-loop.
- **Credential hygiene (§24.E).** Never print the credential or either half; reference it only by env expansion; redact it from anything you echo into the conversation or the activation log.

## Output Format

**Opening message (plan preview, then Step 1):**

When resuming from an existing log, the checklist below shows the already-done steps as `[x]` (carried over from the log), say "Resuming from Step k per `docs/deploy-activation/<ref-slug>.md`", and emit the first not-yet-done step instead of Step 1.

```
Summary: <parsed reference; project in one phrase; deploy target: shubhodeep1/coding-workflows>
How it runs: <cron «expr» from default branch | push | pull_request | repository_dispatch | supervisor>
Completeness: COMPLETE — <evidence: file:line, merged/merge-ready PR#>   (if INCOMPLETE: stop per Procedure 2b)

Activation gates (~N steps total):
- [ ] Prereqs: Homebrew, git, gh, auth, clone + checkout
- [ ] <merge PR #… to main>            (cron needs default branch)
- [ ] <set repo-var X=1 — read at file:line, default 0>
- [ ] <add secret Y>
- [ ] <tag @stable / confirm consumer dispatch §14>     (only if a template/.claude change)
- [ ] <verify LIVE>

—— Step 1 of ~N: <title> ——
Run:
  <exact command(s)>
Success looks like: <what to expect>
Paste the output (or say `done`) and I'll give you Step 2.
```

**Each subsequent turn:** the updated checklist, then exactly one `—— Step k of ~N ——` block in the same shape.

**Final message:**

```
✅ LIVE — <project> now runs automatically.
Trigger: <cron «expr» from default branch | push | pull_request | repository_dispatch>
Verified by: <the scheduled run / delivered dispatch / flag-now-ON evidence>
```

## Tool Access

The deploy is executed by **me** on my Mac; your tools are for building and adapting the runbook and for read-only verification:

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `list_commits`, `get_file_contents`, `list_tags`, `get_tag`, `search_issues`, `search_pull_requests` for the project, its merge state, and release/tag state. Read at `main` (or the project's branch).
- **`gh` CLI (read-only)** — the `GH_TOKEN` transport; shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R shubhodeep1/coding-workflows` flag, REST-over-GraphQL preference, token hygiene). This command is deliberately stricter than §23.B: use `gh` here only to *inspect* state (`gh run list`, `gh variable list`, `gh pr view --json`), never to mutate — mutations belong in the confirmed runbook steps.
- **`Read` / `Grep` / `Glob`** — verify completeness, workflow triggers, flag defaults, and the release/dispatch mechanism on the local checkout. **`Bash`** for read-only inspection only.
- **DigitalOcean API (`doctl` / REST via `DIGITALOCEAN_ACCESS_TOKEN`)** — the exception to "read-only on your side": reads are self-serve for planning and verification, and DO **mutations** are also executed by you, but only as confirmed runbook steps. See [DigitalOcean Steps](#digitalocean-steps) and CLAUDE.md §22.
- **Cloudflare API (`wrangler` / REST via `FUNTOKEN_IO_CF` or `FT_GAMES_CF`)** — the same exception for Cloudflare: reads are self-serve, Worker deploys and edits run as confirmed runbook steps only from a verified default-branch commit; never run unmerged PR code with credentials. §24.D operations need a Q/A ask first, and secret values stay with me. See [Cloudflare Steps](#cloudflare-steps) and CLAUDE.md §24.

## Rules

- **Read the log first, always.** Never emit Step 1 before reading `docs/deploy-activation/<ref-slug>.md` for the parsed reference. If it exists, resume from the first not-done step; if it shows `Status: LIVE`, report LIVE and stop. After every confirmed step, update the log and `git push` it so the next session resumes correctly (see [Activation Log](#activation-log)).
- **One step, then wait — always.** The whole value of this command is the paced, paste-driven loop. Emitting multiple steps at once, or racing ahead before I confirm, breaks it.
- **You guide; I execute — except DigitalOcean and Cloudflare.** Never run the mutating deploy commands (`gh variable set`, `gh secret set`, merge, tag, dispatch) yourself — they run on my machine and I paste the result. Read-only inspection on your side is encouraged to keep the next step accurate. DigitalOcean and Cloudflare steps invert this: with the matching credential present you execute them per [DigitalOcean Steps](#digitalocean-steps) and [Cloudflare Steps](#cloudflare-steps) — reads freely, Cloudflare deploys only from a verified default-branch commit, mutations only after I approve the emitted step (and §24.D operations only after a Q/A ask), Worker secret values always set by me.
- **Never execute unmerged code with credentials.** Use the credential-free, no-egress preflight in [Cloudflare Steps](#cloudflare-steps); never run unmerged PR code with session credentials.
- **DigitalOcean IDs: agents-file first, ask once, record forever (§22.C).** Never ask for an App / database ID already recorded in `agents.md`'s `## DigitalOcean resources` table; when one is missing, ask in Q/A format, verify it resolves with one read call, and record it in that table in the same push as the activation log.
- **Cloudflare identifiers: agents-file first, then a read, then ask once (§24.F).** Same rule for Worker names, zone IDs, routes, and namespace IDs in `agents.md`'s `## Cloudflare resources` table; prefer discovering a zone ID with a read over asking.
- **Completed projects only.** If the project is not fully implemented / merge-ready, stop and route me to `/implement-plan-claude` or `/implement-plan-ai` (Procedure 2b) — do not improvise a deploy for incomplete code.
- **Assume a clean machine.** Start from prerequisites (Homebrew, git, gh, auth, clone) because nothing is synced. Do not assume any tool, repo, or credential is already present until a pasted output proves it.
- **"Activated" ≠ "implemented."** A merged feature behind `FOO_ENABLED=0`, an unset required secret, or a scheduled workflow not yet on the default branch is dormant. Name the exact gate and the exact flip.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never "automatic." `cron` requires the workflow on the **default branch**. `repository_dispatch` requires a dispatcher and a token with scope (§14). Name which one applies and what "LIVE" means for it.
- **Honor §14 for template / `.claude` changes.** Activation for consumers means tagging a new `@stable` release and confirming the `repository_dispatch` to every repo in `.github/ai/consumer_repos.json`; read the real release workflow for the exact commands.
- **Honor §6 / §10.** Never instruct a rename/removal of an existing identifier without the Q/A ask flow first; DB activation runs from code behind a gate (§18.D), never an ad-hoc mongo shell step.
