Given a reference to a **completed** project in `$ARGUMENTS` — a GitHub issue (number / URL), a PR, a plan doc, or a clearly-named feature — walk me through **deploying it and making it active** in this repo, **one step at a time**, starting from a **fresh MacBook with no GitHub repos synced**. A project here can live on one of two sides — **this consumer repo's own code/config**, or the **upstream workflow library** (`shubhodeep1/coding-workflows`) that this repo's wrappers call — and you decide which from the evidence. This is the action companion to `/verify-activation`: that command diagnoses (LIVE / DORMANT / INCOMPLETE) and ships code fixes for the defects it finds in a PR, but never performs operator activation steps; this one drives a DORMANT-but-complete project all the way to LIVE. Interactive by contract: emit **exactly one step**, then **stop and wait** for me to paste the command output (or say `done` / `next`) before emitting the next step. The deploy commands run on **my** machine — you guide, I execute and paste back; never run the mutating deploy steps yourself. **Two exceptions: DigitalOcean and Cloudflare steps.** When the session has `DIGITALOCEAN_ACCESS_TOKEN`, you run DigitalOcean API calls yourself per [DigitalOcean Steps](#digitalocean-steps); when it has the Cloudflare credential for the target site (`FUNTOKEN_IO_CF` or `FT_GAMES_CF`), you run Cloudflare calls yourself per [Cloudflare Steps](#cloudflare-steps) — in both cases reads freely, mutations only after I approve the emitted step. `$ARGUMENTS` should contain at least one concrete reference. **Resumable across sessions:** progress is persisted to a per-project activation log in this repo (`docs/deploy-activation/<ref-slug>.md`). Before emitting any step you **read that log first** and resume from the first not-yet-done step; after each completed step you update and push the log, so a fresh session — new machine, re-cloned container, or a later day — picks up exactly where the last one stopped instead of restarting the runbook. See [Activation Log](#activation-log).

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`, then load the activation log.** Extract the issue number / URL, PR refs, plan-doc path, or feature name. If there is no concrete reference, stop and ask for one. Restate the parsed reference in the opening summary. Then **read the activation log first** (see [Activation Log](#activation-log)): compute the log path from the reference and read `docs/deploy-activation/<ref-slug>.md` in `THIS_REPO`. If a log exists, it is the source of truth for progress — resume from the first step not marked `[x]`, skip the steps already done (and say so), and if it shows `Status: LIVE` report LIVE and stop. If I instead paste a plain statement of which steps are completed / remaining, reconcile that into the log before resuming. If no log exists, you will create one when you emit Step 1.

2. **Build the full deploy plan internally, before emitting Step 1.** Do all the read-only analysis up front so the runbook is stable; only the *delivery* is one-at-a-time.

   a. **Understand the project and resolve `THIS_REPO`.** Fetch the issue and everything linked — linked PRs, tracking comments, sub-issues — via `mcp__github__issue_read` / `pull_request_read` (or `gh`). Pin down what it builds, its acceptance criteria, and **how it is meant to run** (cron, `pull_request` / `push`, `repository_dispatch`, `workflow_dispatch` manual, a supervisor, or on-demand). Determine **`THIS_REPO`** — the `owner/repo` the command runs in (the SessionStart hook prints the resolved slug; otherwise derive it from the git remote).

   b. **Classify the side that owns activation.**
      - **`[CONSUMER]`** — this repo's own workflows / config / code (a wrapper workflow, a repo-var this repo sets). Read at `THIS_REPO@main`.
      - **`[UPSTREAM]`** — behavior that lives in the upstream library and only runs through this repo's wrapper at the **ref this repo is pinned to**. Read upstream pinned to `UPSTREAM_SHA` (see below).
      - **`[BOTH]`** — a wrapper here plus the upstream reusable workflow it calls.

      **Resolving the upstream pin (for the `[UPSTREAM]` / `[BOTH]` side):** find every `uses:` / `repository:` / `ref:` in this repo's `.github/workflows/*.yml` that references `shubhodeep1/coding-workflows`. The exact `ref` is the pin: tag `@vX.Y.Z` → resolve `UPSTREAM_SHA` via `mcp__github__get_tag` / `list_tags`; direct SHA → use it; moving `@stable` → resolve via `list_tags`; branch `@main` → resolve to that branch's tip. Record `UPSTREAM_TAG` + `UPSTREAM_SHA`; pass `ref=<UPSTREAM_SHA>` on **every** upstream read.

   c. **Completeness gate — STOP if not complete.** This command is scoped to *completed* projects. Verify the code / config / workflows / contracts exist and the project's PR(s) are **merged or genuinely merge-ready**, **at the ref for its side** (`THIS_REPO@main` for `[CONSUMER]`; `shubhodeep1/coding-workflows@UPSTREAM_SHA` for `[UPSTREAM]`). If clearly **INCOMPLETE** (code missing, PR neither merged nor open, scaffold-only), **do not emit a deploy runbook** — report what is missing, point me to `/implement-plan-claude` or `/implement-plan-ai`, and stop.

   d. **Enumerate the activation gates** between "code present" and "runs automatically here," using the `/verify-activation` lens:
      - **Wrapper wiring (consumer side)** — does this repo have the `.github/workflows/*.yml` wrapper with the right trigger, calling the upstream reusable workflow at the expected ref? A feature that exists upstream but is **not wired into a consumer wrapper** will never run here — adding/adjusting the wrapper is an activation step.
      - **Default-branch reachability** — a **cron** schedule only fires from the **default branch**; a wrapper change must be merged to the default branch to take effect. A Cloudflare Worker deploy also requires the project's code on the protected default branch of `THIS_REPO` before any deploy step.
      - **Repo-vars / feature flags that default OFF** — `*_ENABLED=0`, an empty roster var. Consumers commonly must **opt in** by setting a repo-var. Name each variable, its read-site, and its default.
      - **Required secrets** — does the run need a model API key, `GH_PAT`, a Telegram token, etc. that I must add in this repo's settings? Unset → dormant.
      - **Upstream version gap** — is this repo pinned to an `UPSTREAM_TAG` that **predates** the feature? If so the feature is not available here until the pin is bumped — **bumping the pin** is the activation step.
      - **DigitalOcean-side gates (CLAUDE.md §22)** — does activation touch a DigitalOcean-hosted resource (an App's env vars or spec, a forced redeploy, a managed-database setting)? If `DIGITALOCEAN_ACCESS_TOKEN` is present, read the current DO state yourself while planning (§22.A — self-serve, no asking). Resolve App / database IDs from the `## DigitalOcean resources` table in this repo's root agents file (`AGENTS.md` / `agents.md`, whichever casing the repo has) per §22.C; if a needed ID is not recorded, ask for it in Q/A format and record it per [DigitalOcean Steps](#digitalocean-steps).
      - **Cloudflare-side gates (CLAUDE.md §24)** — does activation touch a Cloudflare Worker (script, version, bindings, non-secret vars, routes, cron triggers), a KV / R2 / D1 binding, or a DNS record or zone setting on a covered site? If the matching credential is present, read the current Cloudflare state yourself while planning (§24.B — self-serve, no asking). Resolve Worker / zone identifiers from the `## Cloudflare resources` table in this repo's root agents file (`AGENTS.md` / `agents.md`, whichever casing the repo has) per §24.F, or discover them with a read; if one can be neither, ask for it in Q/A format and record it per [Cloudflare Steps](#cloudflare-steps).
      - **Supervisor / DB gates** — does it need a started supervisor or a code-gated migration rather than a manual step?

   e. **Compose the ordered runbook** and keep it as a stable numbered list you track across turns:
      - **Prerequisite bootstrap (fresh Mac, nothing synced):** install Homebrew → `brew install git gh` (plus any tool the deploy needs) → `gh auth login` (or export `GH_TOKEN`) with the scopes the deploy needs → clone `THIS_REPO` and check out the branch / PR that holds the project.
      - **Activation steps:** one gate per step — `gh variable set NAME --body 1 -R <THIS_REPO>` for a repo-var; `gh secret set NAME -R <THIS_REPO>` for a secret; edit the wrapper `.github/workflows/*.yml` (add it, fix the trigger, or bump the `ref:` pin) then commit / push / open a PR / merge to the default branch.
      - **Verification:** confirm the trigger will actually fire (a scheduled run appears, the workflow is enabled, the flag now reads ON).

3. **Deliver the runbook one step at a time.** Follow the [Interaction Protocol](#interaction-protocol) exactly. The opening message previews the plan (summary + side + gate list + total step count) and then gives **Step 1 only**.

4. **Finish on a verified LIVE state.** The final step verifies the project now runs automatically here; close with the `✅ LIVE` line from the [Output Format](#output-format).

## Interaction Protocol

This is the load-bearing behavior — honor it strictly:

- **One step per turn.** Emit a single step, then **end the turn**. Never include Step k+1 in the same message as Step k. Each step states: a short title; the **exact** command(s) to paste into a Mac terminal (or the precise YAML edit to make); what **success** looks like; and the literal ask — *"paste the output (or say `done`) and I'll give you the next step."*
- **Wait, then react to the pasted output.** On my reply, read what I pasted. If it shows success → advance. If it shows an **error or unexpected state** → do **not** advance; diagnose and emit a **corrective step** (still one at a time). If I say `done` with no output, trust me, but where a cheap read-only check settles it, verify before moving on.
- **Track progress visibly, and persist it.** Each turn, show a compact checklist (done ✓ / current ▶ / remaining), e.g. `Step 4 of ~9`. The count may grow if a step fails and needs a fix-up — say so. The same checklist is mirrored into the activation log: after each step is confirmed, mark it `[x]` in `docs/deploy-activation/<ref-slug>.md`, then **commit and push the log** so the next session can resume (see [Activation Log](#activation-log)).
- **Adapt to reality.** If pasted output proves a gate is already satisfied (var already set, wrapper already present, pin already current), **skip** that step and say why.
- **Never bundle, never auto-execute.** Do not merge steps, and do not run the mutating deploy commands or git pushes yourself — they run on my machine. Read-only verification (`mcp__github__*` / `gh ... --json` reads, `Read`/`Grep` on your checkout) is fine. **DigitalOcean and Cloudflare steps are the execution exceptions:** per [DigitalOcean Steps](#digitalocean-steps) and [Cloudflare Steps](#cloudflare-steps), when the matching credential is present you run the API call yourself — reads at any time, a mutation only after I approve that emitted step, never in the same turn as its proposal.
- **Stop conditions.** STOP and ask in Q/A format if a step needs a decision with material tradeoffs, would rename/remove a §6 identifier, or is ambiguous. STOP and report if the project turns out to be incomplete mid-run.

## Activation Log

This command is **resumable**. Progress for each project is persisted to a per-project log committed in `THIS_REPO`, so a fresh session — new machine, re-cloned container, or just a later day — picks up at the exact next step instead of restarting the runbook.

- **Path.** `docs/deploy-activation/<ref-slug>.md`, one file per project. Derive `<ref-slug>` deterministically from the parsed reference so the same project always maps to the same file:
  - issue → `issue-<N>`  ·  PR → `pr-<N>`  ·  plan doc → `plan-<basename-without-extension>`  ·  bare feature name → `feature-<kebab-case>`.
  - If `$ARGUMENTS` carries more than one reference, key the slug off the primary one in this priority: issue → PR → plan doc → feature name, and record the secondary references inside the file.
- **Read first (mandatory).** Before emitting any step, read this file in `THIS_REPO`. If it exists it is the source of truth for what is done: resume from the first step not marked `[x]`, skip the `[x]` steps (say that you are skipping them), and if `Status: LIVE` just report LIVE and stop. If it does not exist, create it when you emit Step 1.
- **Accept a pasted progress statement.** If I paste a list of steps already completed / still remaining (rather than raw command output), reconcile it into the log — mark the named steps `[x]`, leave the rest open — and resume from the first open step.
- **Update after every step.** When a step's pasted output (or a `done`) confirms success, mark it `[x]` with a one-line evidence note and the date, mark the next step current, refresh `Last updated` and `Last note`, then **commit and push the log** (below). On full completion set `Status: LIVE`; if a step is blocked, set `Status: BLOCKED` and record why in `Last note`.
- **Persistence (commit & push).** The log only helps a future session if it survives this container, so writing the file is not enough — commit it and `git push` to the working branch of `THIS_REPO`. This is the command's own bookkeeping, **not** a mutating deploy step, so it is fine to run yourself: it never touches repo settings, secrets, vars, merges, wrapper edits, or upstream pins. Use the date from the session context for timestamps.

**Log file format:**

```
# Deploy-Activation Log — <project in one phrase>

- Reference: <#1234 / URL / plan path / feature>   (+ any secondary refs)
- Side: [CONSUMER] | [UPSTREAM] | [BOTH]
- Deploy target: <THIS_REPO>   (+ upstream pin <UPSTREAM_TAG> (<short-sha>) if [UPSTREAM]/[BOTH])
- How it runs: <cron «expr» from default branch | push | pull_request | repository_dispatch | supervisor>
- Status: IN_PROGRESS | BLOCKED | LIVE
- Last updated: <YYYY-MM-DD>
- Last note: <one line — where the last session stopped / why blocked>

## Runbook
1. [x] Prereqs: Homebrew, git, gh, auth, clone THIS_REPO + checkout   — done <YYYY-MM-DD>: <evidence>
2. [x] Set repo-var X=1 (read at file:line, default 0)                — done <YYYY-MM-DD>: var now 1
3. [ ] Bump upstream pin @vA.B.C → @vA.B.D (if the pin predates the feature)
4. [ ] Merge wrapper change to the default branch (cron needs default branch)
5. [ ] Verify LIVE

## Notes
- <free-form: errors hit, steps skipped because already-satisfied and why, decisions made>
```

## DigitalOcean Steps

Deploy steps that touch DigitalOcean — reading or changing an App's env vars or spec, forcing a redeploy, checking deployment status, managed-database settings, build/runtime logs — follow CLAUDE.md §22, which overrides this command's default "you guide, I execute" split:

- **Execute yourself when the token is present.** If `DIGITALOCEAN_ACCESS_TOKEN` is set (check nounset-safe: `[ -n "${DIGITALOCEAN_ACCESS_TOKEN:-}" ]`), you run the DigitalOcean API calls — via `doctl` when installed, otherwise the REST API (§22.A transport) — instead of handing me commands to paste. **Reads** (app spec, deployed env vars, deployment status, logs) are self-serve at any point while building or adapting the runbook (§22.A). **Mutations** (update an app's spec or env vars, force a redeploy, change a database setting) stay inside the one-step-at-a-time loop: emit the step naming the exact resource, the exact change, and the billing impact where known (§22.B); when I confirm (`done` / `go`), run it yourself, show the API output, and mark the step done. Never run a DO mutation in the same turn that proposes it.
- **Resource IDs come from the agents file (§22.C).** Before any DO call, read the `## DigitalOcean resources` table in this repo's root agents file (`AGENTS.md` / `agents.md`, whichever casing the repo has) and use the recorded App / database / Droplet IDs without re-asking. If a needed ID is **not** recorded: ask for it in §2 Q/A format (free-text answer allowed for the ID itself), verify it resolves with one read call, then **add it to that table** — creating the section if the file lacks it — and commit/push it together with the activation-log bookkeeping, so no future session ever asks for it again.
- **No token → fall back to guide-and-paste.** If the token is unset, or the API returns 401/403, say so and deliver the DO steps in this command's default mode: exact `doctl` / `curl` commands for me to run on my Mac (where I hold my own credentials) and paste back. Do not retry-loop, and do not ask me to fetch data the token could have fetched if it were present.
- **Token hygiene (§22.A) is unchanged.** Never print the token; reference it only as `$DIGITALOCEAN_ACCESS_TOKEN`; redact it from anything you echo into the conversation or the activation log.

## Cloudflare Steps

Deploy steps that touch Cloudflare — reading or changing a Worker's script, versions, bindings, non-secret vars, routes, or cron triggers; reading KV / R2 / D1 state; reading or changing DNS or zone settings on a covered site; tailing Worker logs — follow CLAUDE.md §24, which overrides this command's default "you guide, I execute" split the same way §22 does for DigitalOcean:

- **Pick the credential by site (§24.A).** `FUNTOKEN_IO_CF` covers `funtoken.io`; `FT_GAMES_CF` covers `ft.games` and `5m.fun`. They are different accounts and not interchangeable. If the target site is neither, stop and ask (§2 Q/A) instead of guessing. Check presence nounset-safe (e.g. `[ -n "${FUNTOKEN_IO_CF:-}" ]`), split the value on the first colon into account ID and API token, and use `wrangler` (with `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` exported from the two halves) when installed, otherwise the REST API (§24.A transport).
- **Execute yourself when the credential is present.** **Reads** (Worker scripts, settings, bindings, routes, deployments / versions, DNS records, KV / R2 / D1 listings, `wrangler tail`, analytics) are self-serve at any point while building or adapting the runbook (§24.B). **Worker deploys and edits** (§24.C — uploading a new Worker or version, editing bindings, non-secret vars, routes on covered zones, or cron triggers) stay inside the one-step-at-a-time loop. Apply the verification and credential isolation below before emitting the step. When I confirm (`done` / `go`), recheck the pin and run the approved change yourself. Prefer versioned uploads and never delete the previous version as part of a deploy. Never run a Cloudflare mutation in the same turn that proposes it.
- **Deploy only a verified, immutable default-branch commit.** Workers live in `THIS_REPO`: for the `[CONSUMER]` side, use `THIS_REPO@<default>`, not an upstream checkout or an unmerged consumer PR. If the project's PR is unmerged (even if merge-ready), make merging PR #N to the default branch an operator-run activation step *before* any Cloudflare deploy step. Resolve the actual default branch via `gh repo view --json defaultBranchRef`, then read `DEPLOY_SHA` via `gh api repos/<owner>/<repo>/commits/<default> --jq .sha` and require `gh api repos/<owner>/<repo>/branches/<default> --jq .protected` to return `true`. Fetch that branch (`git fetch origin <default>`), check out a detached worktree at precisely `DEPLOY_SHA` (`git worktree add --detach <tmp> "$DEPLOY_SHA"`), and verify `git -C <tmp> rev-parse HEAD` equals `DEPLOY_SHA` and `git -C <tmp> status --porcelain` is empty. Do not substitute the PR checkout or a moving ref. Recheck the API tip, protection, worktree HEAD and cleanliness just before executing the approved step; name `DEPLOY_SHA` in the proposed step and activation-log evidence. If protection, pinning, worktree verification or pre-deploy checks fail or cannot be confirmed, mark the Cloudflare step BLOCKED in the activation log and give only a corrective verification step. Never emit a Worker deploy command, including one for the operator to run, until the protected default-branch SHA, clean pinned worktree and pre-deploy checks are verified; recheck immediately before deployment.
- **Never execute unmerged code with credentials.** Never run repository code from an unmerged PR or branch in a shell holding `FUNTOKEN_IO_CF`, `FT_GAMES_CF`, `CLOUDFLARE_*`, `DIGITALOCEAN_ACCESS_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, or any other session credential. Test scripts, `npm ci` / `npm install` lifecycle scripts, build scripts and `wrangler deploy --dry-run` (which can execute `[build].command`) all count as execution; `Read` / `Grep` inspection does not.
- **Pre-deploy checks.** Prefer the GitHub check-runs for `DEPLOY_SHA` (`gh api repos/<owner>/<repo>/commits/$DEPLOY_SHA/check-runs`, read only); do not emit a deploy step while checks are failing or pending. When Wrangler and a safe sandbox are available, require a successful `wrangler deploy --dry-run` for `DEPLOY_SHA` before proposing the Worker deploy step; a failed dry run blocks deployment, not just the local check. Run this and any optional local checks only on the verified worktree inside a credential-free, no-egress sandbox: clear the environment with `env -i PATH="$PATH" HOME="$(mktemp -d)"`, and isolate network with `docker run --network none` (mount only the verified source read-only, never the host home, git credentials, or Docker socket) or `unshare -rn` with an equally isolated filesystem and home. If no such isolation is available, skip local checks and rely on the check-runs; never run the checks in the credential-bearing session shell.
- **Limit deploy credential exposure.** Deploy only from the verified worktree; confirm the Worker `name` in `wrangler.toml` matches the Worker recorded in the root agents file (`AGENTS.md` / `agents.md`) under `## Cloudflare resources`. Run Wrangler with an allowlisted environment: `env -i PATH="$PATH" HOME="$(mktemp -d)" CLOUDFLARE_ACCOUNT_ID="$CF_ACCOUNT_ID" CLOUDFLARE_API_TOKEN="$CF_API_TOKEN" wrangler deploy` from that worktree (using the matching site's credential halves parsed per §24.A). Do not inherit any other session credentials: Wrangler build hooks may execute code during deployment. Use a clean HOME without stored tool credentials. The account-scoped Workers Scripts token cannot be narrowed to a single Worker here; provisioning a more restricted token is an operator task. For REST uploads, source the script from the same verified commit.
- **§24.D operations are ask-first on top of the step loop.** Deleting a Worker, route, custom domain, or cron trigger; creating, changing, or deleting DNS records or zone settings; deleting KV / R2 / D1 data; and account-level configuration each need a §2 Q/A question naming the account (env var), zone, Worker, object, and change before the step is emitted. After approval, you run it.
- **Secret values never transit the chat.** For a Worker secret (`wrangler secret put`), emit the exact command for me to run on my Mac and paste back the result; never ask me to paste the value, and never set it yourself from a value seen in the conversation.
- **Identifiers come from the agents file (§24.F).** Before any Cloudflare call, read the `## Cloudflare resources` table in this repo's root agents file (`AGENTS.md` / `agents.md`, whichever casing the repo has) and use recorded Worker names, zone IDs, routes, and namespace IDs without re-asking; prefer discovering a zone ID with a read over asking. If a needed identifier is neither recorded nor discoverable, ask for it in §2 Q/A format, verify it resolves with one read call, then **add it to that table** — creating the section if the file lacks it — and commit/push it together with the activation-log bookkeeping.
- **No credential → pause Cloudflare steps.** If the matching credential is unset, or an account-scoped read (the `workers/scripts` list) returns 401/403, say so once, name which credential failed, mark the Cloudflare step BLOCKED in the activation log, and continue only with independent non-Cloudflare work. Do not give manual Cloudflare API or deploy commands or ask me to run them; resume only when the matching session credential works and all deploy preconditions above have been verified. Do not diagnose the credential from `GET /client/v4/user/tokens/verify` — it returns 401 for these account-owned tokens even when they work. Do not retry-loop.
- **Credential hygiene (§24.E).** Never print the credential or either half; reference it only by env expansion; redact it from anything you echo into the conversation or the activation log.

## Output Format

**Opening message (plan preview, then Step 1):**

When resuming from an existing log, the checklist below shows the already-done steps as `[x]` (carried over from the log), say "Resuming from Step k per `docs/deploy-activation/<ref-slug>.md`", and emit the first not-yet-done step instead of Step 1.

```
Summary: <parsed reference; project in one phrase; side [CONSUMER]/[UPSTREAM]/[BOTH]; deploy target: THIS_REPO>
Side: [CONSUMER] THIS_REPO@main  |  [UPSTREAM] shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>)  |  [BOTH]
How it runs: <cron «expr» from default branch | push | pull_request | repository_dispatch | supervisor>
Completeness: COMPLETE — <evidence: file:line, merged/merge-ready PR#>   (if INCOMPLETE: stop per Procedure 2c)

Activation gates (~N steps total):
- [ ] Prereqs: Homebrew, git, gh, auth, clone THIS_REPO + checkout
- [ ] <add/adjust wrapper .github/workflows/Z.yml — trigger / upstream ref>
- [ ] <bump upstream pin @vA.B.C → @vA.B.D>     (only if the pin predates the feature)
- [ ] <set repo-var X=1 — read at file:line, default 0>
- [ ] <add secret Y>
- [ ] <merge wrapper change to the default branch>     (cron needs default branch)
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
✅ LIVE — <project> now runs automatically in <THIS_REPO>.
Trigger: <cron «expr» from default branch | push | pull_request | repository_dispatch>
Verified by: <the scheduled run / enabled workflow / flag-now-ON evidence>
```

## Tool Access

The deploy is executed by **me** on my Mac; your tools are for building and adapting the runbook and for read-only verification:

- **`mcp__github__*` MCP tools** — `issue_read`, `pull_request_read`, `list_commits`, `list_tags`, `get_tag`, `get_file_contents`, `search_issues`, `search_pull_requests`. For the upstream side, pass `ref=<UPSTREAM_SHA>` on every read; for the consumer side, read `THIS_REPO@main`.
- **`gh` CLI (read-only)** — the `GH_TOKEN` transport, when `gh` is installed; shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene) — see **CLAUDE.md §23**. This command is deliberately stricter than §23.B: use `gh` here only to *inspect* state (`gh run list`, `gh variable list`, `gh pr view --json`), never to mutate — mutations belong in the confirmed runbook steps.
- **`Read` / `Grep` / `Glob`** — verify completeness, the consumer wrapper, triggers, flag defaults, and the upstream pin on the local checkout. **`Bash`** for read-only inspection only.
- **DigitalOcean API (`doctl` / REST via `DIGITALOCEAN_ACCESS_TOKEN`)** — the exception to "read-only on your side": reads are self-serve for planning and verification, and DO **mutations** are also executed by you, but only as confirmed runbook steps. See [DigitalOcean Steps](#digitalocean-steps) and CLAUDE.md §22.
- **Cloudflare API (`wrangler` / REST via `FUNTOKEN_IO_CF` or `FT_GAMES_CF`)** — the same exception for Cloudflare: reads are self-serve, Worker deploys and edits run as confirmed runbook steps only from a verified default-branch commit; never run unmerged PR code with credentials. §24.D operations need a Q/A ask first, and secret values stay with me. See [Cloudflare Steps](#cloudflare-steps) and CLAUDE.md §24.

## Rules

- **Read the log first, always.** Never emit Step 1 before reading `docs/deploy-activation/<ref-slug>.md` in `THIS_REPO` for the parsed reference. If it exists, resume from the first not-done step; if it shows `Status: LIVE`, report LIVE and stop. After every confirmed step, update the log and `git push` it so the next session resumes correctly (see [Activation Log](#activation-log)).
- **One step, then wait — always.** The whole value of this command is the paced, paste-driven loop. Emitting multiple steps at once, or racing ahead before I confirm, breaks it.
- **You guide; I execute — except DigitalOcean and Cloudflare.** Never run the mutating deploy commands (`gh variable set`, `gh secret set`, the wrapper edit's commit/push, merge) yourself — they run on my machine and I paste the result. Read-only inspection on your side keeps the next step accurate. DigitalOcean and Cloudflare steps invert this: with the matching credential present you execute them per [DigitalOcean Steps](#digitalocean-steps) and [Cloudflare Steps](#cloudflare-steps) — reads freely, Cloudflare deploys only from a verified default-branch commit, mutations only after I approve the emitted step (and §24.D operations only after a Q/A ask), Worker secret values always set by me.
- **Never execute unmerged code with credentials.** Use the credential-free, no-egress preflight in [Cloudflare Steps](#cloudflare-steps); never run unmerged PR code with session credentials.
- **DigitalOcean IDs: agents-file first, ask once, record forever (§22.C).** Never ask for an App / database ID already recorded in the agents file's `## DigitalOcean resources` table; when one is missing, ask in Q/A format, verify it resolves with one read call, and record it in that table in the same push as the activation log.
- **Cloudflare identifiers: agents-file first, then a read, then ask once (§24.F).** Same rule for Worker names, zone IDs, routes, and namespace IDs in the agents file's `## Cloudflare resources` table; prefer discovering a zone ID with a read over asking.
- **Pick the side from evidence, pin upstream reads to the consumer's ref.** A feature implemented upstream but not wired into a consumer wrapper, or gated behind a repo-var this repo never set, is **DORMANT for this repo** even though the upstream code is perfect. Analyzing upstream activation at `main` when this repo is pinned to a release is wrong — read at `UPSTREAM_SHA`.
- **Completed projects only.** If the project is not fully implemented / merge-ready on its side, stop and route me to `/implement-plan-claude` or `/implement-plan-ai` (Procedure 2c) — do not improvise a deploy for incomplete code.
- **Assume a clean machine.** Start from prerequisites (Homebrew, git, gh, auth, clone) because nothing is synced. Do not assume any tool, repo, or credential is present until a pasted output proves it.
- **"Activated" ≠ "implemented."** Name the exact gate: wrapper wiring, a `*_ENABLED` default, an unset secret, or an upstream pin that predates the feature — and the exact flip.
- **Distinguish trigger types precisely.** `workflow_dispatch` is manual, never automatic. `cron` requires the workflow on the **default branch**. `repository_dispatch` needs a dispatcher + token scope. Name which applies and what "LIVE" means for it.
- **Honor §6.** Never instruct a rename/removal of an existing identifier without the Q/A ask flow first.
