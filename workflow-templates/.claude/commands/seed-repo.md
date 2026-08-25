Seed a **new consumer repository** so it runs the coding-workflows automation: given a target `owner/repo` in `$ARGUMENTS` (plus an optional profile — `core`, `standard`, or `full`; default **`standard`**), copy the profile's wrapper workflows, the `.claude/` command/hook assets, and the root `CLAUDE.md` from the upstream library (`shubhodeep1/coding-workflows` at the **`stable`** ref — the ref consumers pin and the daily sync updates from, never `main`) into the target repo via a seed branch + PR, set the target's `WORKFLOW_PROFILE` repo variable (after asking, §23.C), and register the repo in the library's `.github/ai/consumer_repos.json` (§14 — mandatory). After the seed PR merges and the user adds the required secrets, the existing `ai-update-workflows.yml` sync (daily 04:00 UTC cron + `@stable` `repository_dispatch`) owns all future updates — this command is **initial onboarding only** and is a no-op on an already-seeded repo.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Extract the target `owner/repo` (required — if absent or ambiguous, stop and ask, §2). Extract an optional profile token: `core`, `standard`, or `full`; default **`standard`**. Any other profile word → stop and ask (§2) after listing the manifests that actually exist under `workflow-templates/profiles/` at the source ref. Remaining free text may carry modifiers: `no-register` (skip step 7 — flag the unmet §14 obligation prominently in the report), or `branch=<name>` (override the seed branch name). Naming the target repo in the invocation is the user's grant of write scope to that repo for this seeding (§23.C's "outside the scope the session was given" does not apply to it).

2. **Resolve the source.** Everything is read from `shubhodeep1/coding-workflows` at the **`stable`** ref — the ref consumer wrappers pin (`@stable`) and the ref `ai-update-workflows.yml` syncs from. Seeding from `main` would hand the consumer files the next sync immediately rewrites; never do it unless the free text explicitly says `@main`, and then say so in the report. If the current session *is* the library repo, `git fetch origin stable` and read from `origin/stable` (never from a dirty working tree); otherwise fetch each file via `mcp__github__get_file_contents` with `ref=stable` (or `gh api .../contents/...?ref=stable`, §23.D).

3. **Preflight the target.** Confirm the repo exists and is writable (attach it via the session's repo-attachment mechanism when one exists, else `GH_TOKEN`, §23.D). Record its default branch. Then detect prior seeding — **any** of: `.github/workflows/ai-update-workflows.yml` exists in the target; the target is already listed in the library's `.github/ai/consumer_repos.json`; three or more `ai-*.yml` wrappers already present. If already seeded, **stop without writing**: report what was found, and point out that updates belong to the sync — to change the wrapper set, set/adjust the `WORKFLOW_PROFILE` repo variable (offer to do that, §23.C ask-first) rather than re-seeding.

4. **Compose the seed file set** (byte-for-byte copies — never hand-edit a template, so the first sync run diffs clean as a no-op):
   - Every wrapper listed in `workflow-templates/profiles/<profile>.txt` → target `.github/workflows/<same-name>`.
   - **`workflow-templates/ai-update-workflows.yml` — always, whatever the profile.** The sync treats it as its self-updater sentinel and will never create or overwrite it in a consumer (`SELF_TEMPLATE` guard in `update_workflows.yml`), and `core`/`standard` manifests omit it — a seed without it never receives another update.
   - The whole `workflow-templates/.claude/` tree → target `.claude/` (commands, `hooks/session-start.sh`, `hooks/pr_merge_status_guard.py`, `settings.json`) — the same set the sync's ".claude/ assets" step mirrors afterwards.
   - Root `CLAUDE.md` → target root `CLAUDE.md`. In the library checkout `workflow-templates/CLAUDE.md` is a symlink to `../CLAUDE.md` — copy the **dereferenced content** (the sync uses `cp -L` for the same reason).
   If the target already has any of these paths with **different** content (e.g. its own root `CLAUDE.md`), do not overwrite silently — stop and ask (§2) with one batched question listing every collision and a recommended per-file action.

5. **Land the seed in the target repo.** Branch `seed/coding-workflows-<profile>` (or the `branch=` override) off the target's default branch; commit everything as one commit, `chore: seed coding-workflows automation (profile: <profile>)`; push with retry/backoff on network errors; open a **ready** PR against the default branch. The PR body lists the wrapper set, the `.claude/` assets, the profile, and the follow-up checklist from step 8. §19 applies: no auto-close keywords against any `ai:orchestrator-tracking` issue.

6. **Configure the target (one batched §2 Q/A — repo administration is ask-first, §23.C).** Ask a single batch covering: **(a)** set repo variable `WORKFLOW_PROFILE=<profile>` now (RECOMMENDED — the sync defaults to `full` when the variable is unset, so an unset variable silently widens a `core`/`standard` seed on the first cron run); **(b)** optionally dispatch the target's `ai-update-workflows.yml` once after the seed PR merges, as an immediate sync sanity check (a dispatch is §23.C territory, hence asked here). Perform the approved items yourself (§18 — never hand the user commands to run). **Secrets are the one exception**: secret values must never transit this chat, so instead name exactly what the user must add in the target's Settings → Secrets and variables → Actions — `GH_PAT` (required, `repo` scope), `OPENROUTER_API_KEY` (required), `TG_BOT_SECRET` (optional, Telegram notifications) — plus the repo setting "Allow auto-merge" if they keep `ENABLE_AUTO_MERGE` at its `true` default.

7. **Register the consumer in the library (§14 — mandatory unless `no-register`).** Add `"owner/repo"` to the JSON array in `.github/ai/consumer_repos.json` in `shubhodeep1/coding-workflows`, preserving formatting and appending at the end. In a library session: commit on the session's designated working branch and open a PR to the default branch (or fold into that branch's existing open PR); from elsewhere: land it via a small API-driven PR. Remind the user in the report that the library's release-workflow `GH_PAT` must have `repo` scope on the new consumer for the `@stable` `repository_dispatch` to reach it. Registration is its own commit/PR — never mixed into the target repo's seed PR.

8. **Report.** Emit the [Output Format](#output-format): what was seeded, both PR links, config performed, and the remaining checklist ordered by what blocks what (merge seed PR → add secrets → labels appear via `ai-sync-labels.yml` on its own triggers → daily sync takes over).

## Output Format

```
Target: <owner/repo> — profile: <core|standard|full> (default standard)
Source: shubhodeep1/coding-workflows@stable (<resolved SHA>)

Seeded (<N> files):
- .github/workflows/<wrapper>.yml  × <n>  (profile manifest + ai-update-workflows.yml)
- .claude/  (commands × <n>, hooks × <n>, settings.json)
- CLAUDE.md

PRs:
- Seed:         <target PR URL>  (branch <name>, base <default branch>)
- Registration: <library PR URL>  (consumer_repos.json)  |  SKIPPED (no-register) — §14 unmet

Config:
- WORKFLOW_PROFILE=<profile>  set | declined | pending answer
- First sync dispatch         requested | declined | n/a (runs on daily cron)

Remaining checklist (user):
1. Merge the seed PR (and the registration PR).
2. Add secrets in <owner/repo>: GH_PAT (repo scope), OPENROUTER_API_KEY, [TG_BOT_SECRET].
3. Grant the library's release GH_PAT repo scope on <owner/repo> (for @stable dispatch).
4. [Enable "Allow auto-merge" if keeping ENABLE_AUTO_MERGE=true.]
```

## Tool Access

- **`mcp__github__*`** — primary transport (§23.D): `get_file_contents` (`ref=stable`) to read templates and probe the target, `create_branch` / `push_files` / `create_pull_request` to land the seed, `list_branches` for the default branch.
- **`gh api` via `GH_TOKEN`** (through `Bash`) — fallback when MCP is unavailable or scope-gated; REST over GraphQL (§23.D). Never echo the token (§23.E).
- **`Read` / `Grep` / `Glob` / `Bash`** — in a library session: read `workflow-templates/` at `origin/stable`, edit `consumer_repos.json`, and run the local git push for the registration change.
- **Repo attachment** — when the host session supports attaching repos, attach the target instead of reaching around the session's declared scope (§23.A).

## Rules

- **Standard is the default profile**; `core` and `full` only when named in `$ARGUMENTS`.
- **Always include `ai-update-workflows.yml`**, whatever the profile — the sync never creates its own updater, and a consumer without it is orphaned at its seed-day snapshot.
- **Always set `WORKFLOW_PROFILE`** (after the step 6 ask) when the profile is not `full` — unset, the first sync widens the wrapper set to `full`.
- **Byte-identical copies from `stable`.** No hand edits, no `main`-sourced files (unless explicitly requested), no partial-profile cherry-picking — a manifest subset request other than the three named profiles is a §2 ask.
- **§14 is mandatory**: skipping registration requires an explicit `no-register` in `$ARGUMENTS` and a prominent flag in the report.
- **Idempotent by refusal**: an already-seeded target gets a report, not a second seed. Profile changes go through `WORKFLOW_PROFILE`, not re-seeding.
- **Two PRs, never mixed**: the seed PR in the target repo, the registration PR in the library. §19 (no auto-close keywords against `ai:orchestrator-tracking` issues) and §21 (merged-PR commit guard) apply to both.
- **Secrets never transit the chat.** Name them, point at the settings page; never ask the user to paste a value and never write one anywhere.
- **Stop and ask (§0/§2)** on: missing/ambiguous target, unknown profile, file collisions in the target, an unwritable target repo, or any state that makes the "already seeded" call unclear.
