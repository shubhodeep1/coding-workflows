Investigate a problem reported against this repo, identify the root cause, and either **ship a fix** or produce an **evidence-backed proposed fix** — the choice depends on *where* the root cause lives. `$ARGUMENTS` is **free-form prose** that may contain any combination of GitHub PR URLs, issue URLs, Actions run/job URLs, raw log URLs, `#1234` / `owner/repo#1234` references, workflow run IDs / job IDs, commit SHAs, branch / tag names, file paths, stack traces, and quoted error messages. Trace back through every relevant reference (PRs, issues, Actions logs, comments, commits) and bottom out on the root cause.

**Where does the root cause live? You decide, from the evidence.** A problem surfaced in this repo can originate in one of two places, and the classification drives which ref you read and whether you edit:

- **`[CONSUMER-INTERNAL]`** — the bug is in **this** repo's own code / config / workflows, unrelated to the upstream workflow library. → Investigate **this** repo at **`main`** (the local working checkout). **Read-write: ship the fix** here (apply, verify, commit, push, open a PR).
- **`[UPSTREAM]`** — the bug is in the upstream workflow library `shubhodeep1/coding-workflows`.
  - If **this repo *is* `shubhodeep1/coding-workflows`** (it consumes its own templates), investigate it at **`main`** and **ship the fix** here (read-write) — there is no separate downstream to hand off to.
  - Otherwise (this is a *different* consumer repo), investigate `shubhodeep1/coding-workflows` pinned to the **upstream ref this consumer is actually running** (NOT `main`). What happens next depends on whether the upstream library is **attached to this session** (see [Attached upstream checkout](#attached-upstream-checkout)):
    - **Upstream attached** (a pushable local checkout of `shubhodeep1/coding-workflows` exists in this session) → finish the investigation exactly as below, then **auto-chain into `/validate-consumer-issue`** and ship the upstream fix in that checkout (apply, verify, commit, push, open a PR). The session *can* push upstream, so the hand-off to a human is unnecessary.
    - **Upstream not attached** → produce a **read-only** proposed fix the user takes to a `shubhodeep1/coding-workflows` session — that session cannot commit / push to the upstream library. This is the unchanged legacy behaviour.
- **`[BOTH]`** — coordinated change spanning both; handle each side by the matching rule above.

Decide the classification from the parsed leads + the user's description + the evidence you gather. If it is genuinely unclear which side owns the root cause, investigate **both** sides and surface the ambiguity under **Open Questions** rather than guessing.

$ARGUMENTS

## Steps

1. **Parse `$ARGUMENTS`** — Scan the prose for every actionable lead. Be greedy; missing a lead silently is worse than restating an irrelevant one.
   - GitHub PR URLs / `#1234` references / `owner/repo#1234` references
   - GitHub issue URLs / `#1234` references
   - Actions log URLs (e.g. `https://github.com/<owner>/<repo>/actions/runs/<id>`, raw log URLs, job log URLs)
   - Workflow run IDs, job IDs, artifact URLs
   - Commit SHAs (full or short)
   - Branch names, tag names
   - File paths and stack traces
   - Error messages quoted in prose
   - Any free-form description of expected behaviour, suspected cause, or what the user has already tried — capture this verbatim; it shapes prioritisation in Steps 3–4 but never overrides evidence.

   Restate the parsed leads (and the verbatim user description, or `none`) back to the user in the **Summary** so any miss is visible. If `$ARGUMENTS` contains zero actionable leads (no URLs, no refs, no SHAs, no file paths — just vague prose), stop and ask the user for at least one concrete reference.

2. **Classify the issue and resolve the target repo + ref + mode.** This is the decision the rest of the command hangs on. Do it explicitly and record it in the **Evidence Ledger**.

   First, determine **`THIS_REPO`** — the `owner/repo` slug of the repo the command is running in. The SessionStart hook prints the resolved slug; otherwise derive it from the git remote. (In Claude Code Web the remote is a local proxy, so prefer the SessionStart slug.)

   Then classify the root cause as `[CONSUMER-INTERNAL]`, `[UPSTREAM]`, or `[BOTH]` using the parsed leads + description + any early evidence (a stack trace pointing at this repo's own files is `[CONSUMER-INTERNAL]`; a failure inside a reusable workflow / script / prompt pulled from `shubhodeep1/coding-workflows` is `[UPSTREAM]`). When unsure, lean toward investigating both and decide once the evidence is in.

   From the classification, set the target repo, the ref to read, and the mode:

   - **`[CONSUMER-INTERNAL]`** → `TARGET_REPO = THIS_REPO`, `TARGET_REF = main` (the local working checkout). **Mode = read-write** (apply the fix per the Decision Rule in Step 9).
   - **`[UPSTREAM]` and `THIS_REPO == shubhodeep1/coding-workflows`** → `TARGET_REPO = shubhodeep1/coding-workflows`, `TARGET_REF = main`. **Mode = read-write**. Resolve `TARGET_SHA` to the current `main` tip via `mcp__github__list_commits` with `sha=main` (or read files directly from the local `main` checkout). Do **not** run the stable-pinning procedure below — this repo is the upstream, and the fix lands on `main`.
   - **`[UPSTREAM]` and `THIS_REPO != shubhodeep1/coding-workflows`** → `TARGET_REPO = shubhodeep1/coding-workflows`, pinned to the **upstream ref this consumer is actually running** (the stable-pinning procedure below). The mode depends on `UPSTREAM_ATTACHED`, resolved by the [Attached upstream checkout](#attached-upstream-checkout) procedure below:
     - `UPSTREAM_ATTACHED = yes` → **Mode = read-write-attached-upstream**. Investigate read-only against `UPSTREAM_SHA` exactly as in the read-only mode, then hand the finished diagnosis to the auto-chain in Step 9, which lands the fix in `UPSTREAM_CHECKOUT`.
     - `UPSTREAM_ATTACHED = no` → **Mode = read-only** (propose the fix; the user takes it to a `shubhodeep1/coding-workflows` session). Unchanged legacy behaviour.
   - **`[BOTH]`** → run both the consumer-internal path and the upstream path; apply only the edits the current session is allowed to push (the `[CONSUMER-INTERNAL]` side, and the upstream side when `THIS_REPO == shubhodeep1/coding-workflows` or `UPSTREAM_ATTACHED = yes`), and propose the rest read-only.

   ### Attached upstream checkout

   Resolve this **only** for the `[UPSTREAM]` / `[BOTH]` path when `THIS_REPO != shubhodeep1/coding-workflows` — when this repo *is* the library there is no second checkout to find. A session can have more than one repo attached (e.g. the user added `shubhodeep1/coding-workflows` alongside this consumer repo); when the upstream library is present as a **pushable working tree**, the read-only hand-off in Step 9 is replaced by the auto-chain.

   Set two fields and record both in the **Evidence Ledger**:

   - `UPSTREAM_CHECKOUT` — absolute path of the local `shubhodeep1/coding-workflows` working tree, or `none`.
   - `UPSTREAM_ATTACHED` — `yes` only when every check below passes; otherwise `no`.

   Detection (local only — no network, no repo mutation):

   ```bash
   # Find candidate working trees whose remotes point at the upstream library.
   # Claude Code Web rewrites remotes to a local proxy
   # (http://...@127.0.0.1:PORT/git/<owner>/<repo>), so match on the slug, not the host.
   for candidate in "$PWD" "$PWD"/* "$PWD"/.. "$PWD"/../* "$HOME" "$HOME"/*; do
     [ -d "$candidate/.git" ] || continue
     git -C "$candidate" remote -v 2>/dev/null | grep -q 'shubhodeep1/coding-workflows' || continue
     git -C "$candidate" rev-parse --show-toplevel 2>/dev/null
   done | sort -u
   ```

   Then, for the candidate you selected (skip any tree that is `THIS_REPO`'s own checkout):

   - **It is really the library** — the tree contains `workflow-templates/` and `.github/ai/consumer_repos.json`. A consumer repo that merely *mentions* the slug in a wrapper YAML is not the library.
   - **It is clean** — `git -C "$UPSTREAM_CHECKOUT" status --porcelain` is empty. A dirty tree holds someone else's in-flight work; do **not** branch over it. Record `UPSTREAM_ATTACHED = no`, note the dirty tree under **Open Questions**, and take the read-only path.
   - **It is reachable and pushable** — `git -C "$UPSTREAM_CHECKOUT" ls-remote --heads origin >/dev/null` succeeds, and `git -C "$UPSTREAM_CHECKOUT" push --dry-run origin HEAD:refs/heads/<probe-branch>` reports no permission error (`--dry-run` contacts the server without creating anything).

   If more than one candidate qualifies, prefer the one whose `origin` slug matches exactly and record the others under **Open Questions**. If zero qualify, `UPSTREAM_CHECKOUT = none`, `UPSTREAM_ATTACHED = no` — and the command behaves exactly as it did before this section existed. **Never clone, fetch-into, or otherwise attach the upstream repo yourself**: an unattached upstream is a supported, unchanged outcome, not a problem to fix.

   ### Stable-pinning procedure (only for `[UPSTREAM]` when `THIS_REPO != shubhodeep1/coding-workflows`)

   All upstream reads in this case MUST be pinned to the ref the consumer's failing run was executing. Pinning to the *current* `stable` tag without checking the consumer's pin is wrong: if the consumer is pinned to `@v1.2.3` and `stable` has since moved to `@v1.3.0`, reading at `stable` analyses code the consumer is not running. Resolve once, up front, into **two distinct fields**:

   - `UPSTREAM_TAG` — the human-readable label the consumer is pinned to (e.g. `v1.2.3`, `stable`, `main`, or the literal SHA if pinned by SHA). Used for citations and the report.
   - `UPSTREAM_SHA` — the resolved commit SHA the tag/branch/ref points at. **This is the single canonical value passed as `ref=` to every GitHub MCP call** (alias of `TARGET_SHA` for this path). Pinning to the SHA (not the tag name) makes the analysis stable even if a moving pointer like `stable` is updated mid-investigation.

   Resolution procedure:

   - **First, inspect the consumer's wrapper YAML** (the workflows under `.github/workflows/<name>.yml` in this repo, plus any composite actions they delegate to) to find every `uses:` / `repository:` / `ref:` entry that references `shubhodeep1/coding-workflows`. The exact `ref` (tag, branch, or SHA) is the consumer's pin and is the authoritative input.
     - If the wrapper pins a specific tag (e.g. `@v1.2.3`) → `UPSTREAM_TAG = v1.2.3`; resolve to `UPSTREAM_SHA` via `mcp__github__get_tag` (or `list_tags`).
     - If the wrapper pins a SHA directly → `UPSTREAM_TAG = <short-sha>`; `UPSTREAM_SHA = <full-sha>`.
     - If the wrapper pins the moving `stable` pointer (e.g. `@stable`) → `UPSTREAM_TAG = stable`; resolve `UPSTREAM_SHA` via `mcp__github__list_tags` (this repo uses a moving `stable` pointer set by `scripts/mark-stable.sh`).
     - If the wrapper pins an upstream branch (e.g. `@main`) → `UPSTREAM_TAG = main`. Resolve `UPSTREAM_SHA` to the upstream branch's commit at the time the failing run executed — **NOT** the consumer run's `head_sha` (that's a SHA in the consumer repo and will 404 against `shubhodeep1/coding-workflows`):
       1. Read the failing run's start timestamp from the run metadata.
       2. Use `mcp__github__list_commits` with `sha=<upstream-branch>` (and an `until=<run-start-time>` filter if the tool exposes one; otherwise paginate and pick the first commit whose committer date is `≤` the run-start time). That SHA is `UPSTREAM_SHA`.
       3. If the historical lookup is not feasible (timestamp missing, API constraints), fall back to the upstream branch's current tip SHA. Note explicitly that the branch may have moved since the run.
       In all branch-pinned cases, record the precise vs. fallback path in the Evidence Ledger and surface the inherent imprecision under **Open Questions** — the consumer is not on a stable release.
   - **Fallback when the consumer pin cannot be determined** (e.g. the wrapper file is inaccessible after retries): try `mcp__github__list_tags` for `stable`, else the highest semver tag (`vX.Y.Z`), else `mcp__github__get_latest_release`. Set `UPSTREAM_TAG` and `UPSTREAM_SHA` from whichever succeeded. Record the fallback path and the assumption under **Open Questions**.
   - **Also resolve `PREVIOUS_UPSTREAM_TAG` and `PREVIOUS_UPSTREAM_SHA`** — the tag immediately preceding `UPSTREAM_TAG` in semver order, plus its resolved SHA. List tags with `mcp__github__list_tags`, sort by semver, pick the entry below `UPSTREAM_TAG`. This is needed in Step 4 to scope the regression search ("changes merged between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA`"). If `UPSTREAM_TAG` is the lowest tag (or is a branch / direct SHA), set both `PREVIOUS_*` fields to null and skip the regression-search step.
   - Record `UPSTREAM_TAG`, `UPSTREAM_SHA`, `PREVIOUS_UPSTREAM_TAG`, `PREVIOUS_UPSTREAM_SHA` in the **Evidence Ledger**. Every later upstream tool call MUST pass `ref=<UPSTREAM_SHA>`. Every citation should be written as `<owner>/<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>` so the reader sees both the human label and the canonical SHA.
   - If `UPSTREAM_SHA` cannot be resolved at all after retries (see **Retry Rule**), record this under **Inaccessible Resources** and stop — this path cannot produce a pinned analysis without a SHA. Do not silently fall back to `main`.

   Record the chosen classification, `THIS_REPO`, `TARGET_REPO`, `TARGET_REF`/`TARGET_SHA`, the mode (read-write / read-write-attached-upstream / read-only), and — for the pinned upstream path — `UPSTREAM_ATTACHED` and `UPSTREAM_CHECKOUT` in the **Evidence Ledger** before proceeding.

3. **Download any logs** referenced in the input — Choose the fetch tool by URL shape; `curl` against a rendered GitHub page returns HTML, not log content.
   - **GitHub Actions run / job URLs** (e.g. `https://github.com/<owner>/<repo>/actions/runs/<id>`, `.../runs/<id>/job/<id>`, `.../runs/<id>/attempts/<n>`): use the appropriate GitHub MCP tool (e.g. `mcp__github__get_workflow_run_logs`, `mcp__github__get_job_logs`, or whichever workflow-log tool is exposed in the current session — search `mcp__github__*` for `log`). When `GH_TOKEN` is set in the session environment (the SessionStart hook installs `gh` and authenticates with it), `gh run view --log <run-id> -R <owner>/<repo>`, `gh run view --log --job <job-id> -R <owner>/<repo>`, `gh run view --log-failed <run-id> -R <owner>/<repo>` (failed-step lines only), and `gh api repos/<owner>/<repo>/actions/runs/<id>/logs` are also available — see [Tool Access](#tool-access) for why `-R` is mandatory in this environment. These return the raw log payload. Do NOT `curl` these rendered URLs.
   - **Raw log URLs / artifact URLs / external (non-GitHub) URLs**: use `curl --fail-with-body -sSL -o /tmp/<unique-name>.log -w '%{http_code}\n' <url>` so HTTP errors are detected reliably; plain `curl -sL` exits 0 on 4xx/5xx and silently downloads the server's error page.

   Use a distinct `<unique-name>` per URL (e.g. derived from run/job ID, or numbered `log-1.log`, `log-2.log`) so concurrent downloads don't overwrite each other. Track the local path for each log alongside its `log-<n>` index in the Evidence Ledger so later citations (`log-2 L42: "..."`) are unambiguous when multiple logs are in play.

   Verify the status / payload before treating the result as a log:
   - `2xx` → proceed.
   - `5xx`, `429`, network/timeout/connection-reset/DNS errors → transient; retry per the **Retry Rule**.
   - `401`, `403`, `404`, `410` → hard failure; record under **Inaccessible Resources** and follow the **Inaccessible resources** rule.

   Read each log **in full**. Do not skim. Note: error messages, stack traces, exit codes, OOM/signal kills, timeouts, deprecation warnings, dependency mismatches, environment/config issues, and every embedded reference to PRs / issues / commits / run IDs / artifact URLs — these are leads to follow in Step 4.

4. **Expand the investigation — keep pulling artifacts until the root cause is nailed down.** Before proposing or applying any fix, follow every lead. When a log/PR/comment names another run / job / artifact / commit / PR, fetch it; don't infer from the name alone. Either `mcp__github__*` or `gh` CLI works (see [Tool Access](#tool-access)) — when `GH_TOKEN` is set, both can reach run logs, job logs, PRs, issues, commits, and file contents. If the diagnosis isn't yet evidence-based, the next step is to read more, not to guess.

   **In `THIS_REPO` (current working directory):**
   - PRs / issues referenced → fetch via `mcp__github__pull_request_read` / `mcp__github__issue_read`. Read description, comments, linked issues, review threads, status checks.
   - Workflow YAML used by the failing run → read the wrapper at `.github/workflows/<name>.yml` to identify which upstream reusable workflow / action / script it calls and at what ref (this also feeds the Step 2 classification: if the failure is inside this repo's own logic it is `[CONSUMER-INTERNAL]`; if it is inside the delegated upstream workflow it is `[UPSTREAM]`).
   - `.github/ai/consumer_repos.json` and any consumer-side config that affects which upstream behaviour is active.
   - `git blame` / `git log -p -- <file>` on `THIS_REPO` files in stack traces. For a `[CONSUMER-INTERNAL]` issue, this is the primary evidence trail.
   - Other recent runs of the same workflow (regression vs. flake).

   **For an `[UPSTREAM]` issue, in `shubhodeep1/coding-workflows`:**
   - When `THIS_REPO == shubhodeep1/coding-workflows`, read at `main` (`ref=main` or the local checkout). When `THIS_REPO` is a *different* consumer, use `mcp__github__get_file_contents` with `ref=<UPSTREAM_SHA>` for **every** read — never read upstream files at `main` in that case, because a fix proposed against `main` may not apply against the consumer's pinned wrapper. **This holds even when `UPSTREAM_ATTACHED = yes`**: the attached checkout sits at whatever ref it was cloned to (usually the default branch), which is *not* what the consumer ran. Diagnose from `UPSTREAM_SHA`; use `UPSTREAM_CHECKOUT` only for writing in Step 9.
   - Reusable workflow that the wrapper calls (under `workflow-templates/` or `.github/workflows/`).
   - Scripts invoked by that workflow (under `scripts/`).
   - Prompt files invoked by those scripts (under `prompts/`).
   - Any contracts (`db/contracts/*.yml`), agents config (`agents.md`), or system rules referenced by the failing path (unattended workflow paths use `unattended_system_instructions.md`; interactive Claude Code sessions use `CLAUDE.md`).
   - Regression window — the commits most likely to have introduced the bug:
     - Pinned case (`THIS_REPO != shubhodeep1/coding-workflows`): PRs / commits that touched the relevant files between `PREVIOUS_UPSTREAM_SHA` and `UPSTREAM_SHA` (resolved in Step 2). Use `mcp__github__list_commits` with `path=<file>` and a `since` / `sha` window bounded by the two refs, plus `mcp__github__search_issues` / `mcp__github__search_pull_requests` scoped to the repo. If `PREVIOUS_UPSTREAM_SHA` is null, skip this and note it under **Open Questions**.
     - `main` case (`THIS_REPO == shubhodeep1/coding-workflows`): recent commits to the implicated files on `main` (`mcp__github__list_commits` with `path=<file>`, or local `git log -p -- <file>`).
   - Cross-reference: intersect files changed in the regression window with the files implicated by the stack trace / log.

   **Retry transient errors** (5xx, 429, timeouts, connection resets, DNS failures) on every fetch — log download, GitHub MCP, raw HTTP — per the **Retry Rule**. A transient blip is not an excuse to give up on a lead.

5. **Identify every distinct issue** — List each unique issue. For each:
   - Exact error message or log line (with `log-<n> L<line>` reference into the saved log file when multiple logs are present, otherwise just `L<line>`)
   - Root cause, not symptom
   - Whether it is the primary failure or a cascading/secondary failure
   - Whether the root cause is **`[CONSUMER-INTERNAL]`** (in `THIS_REPO`), **`[UPSTREAM]`** (in `shubhodeep1/coding-workflows` — cite as `@main` or `@<UPSTREAM_TAG> (<short-sha>)` per the Step 2 path), or **environmental** (runner / network / external service)

6. **Correlate to source code** — Locate the exact files and lines responsible. **Read the actual files at `TARGET_REF` / `TARGET_SHA`.** Never guess at code structure, function signatures, env vars, or workflow inputs. Cite `THIS_REPO` files as `<path>:<line>`; cite pinned upstream files as `shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>):<path>:<line>`; cite upstream-at-main files as `shubhodeep1/coding-workflows@main:<path>:<line>`.

7. **Build the Evidence Ledger** — Every claim and every fix (applied or proposed) must cite:
   - The Step 2 decision once at the top: classification, `THIS_REPO`, `TARGET_REPO`, `TARGET_REF`/`TARGET_SHA`, mode. For the pinned upstream path, also `UPSTREAM_TAG`, `UPSTREAM_SHA`, `PREVIOUS_UPSTREAM_TAG`, `PREVIOUS_UPSTREAM_SHA`.
   - The list of parsed leads from Step 1 (URLs, refs, SHAs, paths) and the user's verbatim description (or `none`)
   - Log line number(s) and exact text — `log-<n> L<line>: "..."` when multiple logs are present, otherwise just `L<line>: "..."` (with the `/tmp/` path of each saved log)
   - Source location: `<path>:<line>` for `THIS_REPO`; `<repo>@<ref> (<short-sha>):<file>:<line>` for upstream
   - Commit SHA / PR # that introduced the relevant code (when applicable)
   - Test output or reproduction result (when applicable)

   No citation → no claim. No claim → no fix.

8. **Attempt reproduction (where feasible)** — If the failure can be reproduced locally without privileged credentials, run the failing command/test and record the result. A failure to reproduce is itself evidence (environment-specific, flake, cache-poisoned, runner-specific, auth-walled) — surface it; do not paper over it.

9. **Decide and act — driven by the mode set in Step 2.** Classify each finding as `EVIDENCE-BASED` (fully supported by log + code reading at the target ref + reproduction where feasible) or `HYPOTHESIS` (plausible but unverified).

   - **Read-write modes** — `[CONSUMER-INTERNAL]`, and `[UPSTREAM]` when `THIS_REPO == shubhodeep1/coding-workflows`:
     - All findings `EVIDENCE-BASED` and no missing/inaccessible resource blocks root cause or fix verification → design the fix, apply it, verify it (re-run the repro / failing test when feasible), commit, push, and open a PR. Do not ask. Report using the **Final output structure** afterward, with the applied fix and the branch / PR link.
     - Any `HYPOTHESIS` finding, or any missing/inaccessible resource that blocks root cause or fix verification → stop before editing. Report and ask the user how to proceed.
     - Add or extend a test when fixing a defect that lacked coverage; do not ship a behaviour fix without verification.
   - **Read-write-attached-upstream mode** — `[UPSTREAM]` when `THIS_REPO != shubhodeep1/coding-workflows` **and** `UPSTREAM_ATTACHED = yes`:
     - Produce exactly what the read-only mode produces — the evidence-based proposed diff with `file:line` anchors at `UPSTREAM_SHA` — then run the [Auto-chain](#auto-chain-to-validate-consumer-issue-read-write-attached-upstream-mode-only) below instead of stopping at the hand-off.
     - The same gate applies before chaining: any `HYPOTHESIS` finding, or any missing/inaccessible resource that blocks root cause or fix verification → do **not** chain. Report and ask, exactly as the other read-write modes do.
   - **Read-only mode** — `[UPSTREAM]` when `THIS_REPO != shubhodeep1/coding-workflows` **and** `UPSTREAM_ATTACHED = no`:
     - Do **NOT** apply fixes. With no upstream checkout attached, this session cannot push to the library. Surface the proposed diff (as a fenced code block with `file:line` anchors at `UPSTREAM_SHA`) labeled `[UPSTREAM]` and let the user open a `shubhodeep1/coding-workflows` session to implement it.
   - **`[BOTH]`** — apply the side this session is allowed to push (the `[CONSUMER-INTERNAL]` side, and the upstream side when `THIS_REPO == shubhodeep1/coding-workflows` or `UPSTREAM_ATTACHED = yes`); propose the rest read-only with the matching target-repo label. Land and report the `[CONSUMER-INTERNAL]` side **first**, then chain the upstream side — one PR per repo; never mix files from two repos into one commit.
   - **Environmental** (service down, rate limit, runner outage) → no fix; mark as **environmental / non-actionable** and recommend re-running once the environment recovers.

   Label every fix with its **target repo**: `[CONSUMER]` (lands in `THIS_REPO`), `[UPSTREAM]` (lands in `shubhodeep1/coding-workflows`), or `[BOTH]`.

   ### Auto-chain to `/validate-consumer-issue` (read-write-attached-upstream mode only)

   The investigation above is unchanged and completes in full first — parsed leads, Evidence Ledger, root causes, the proposed upstream diff, and (for `[BOTH]`) the applied-and-pushed `[CONSUMER-INTERNAL]` fix. Only then:

   1. **Resolve `UPSTREAM_BASE`** — the branch the upstream fix lands on, derived from the consumer's pin (`UPSTREAM_TAG`, resolved in Step 2):
      - `UPSTREAM_TAG = stable`, or the pin could not be resolved → `UPSTREAM_BASE = stable`. This is the default and the common case.
      - `UPSTREAM_TAG = main` → `UPSTREAM_BASE = main`.
      - `UPSTREAM_TAG` is a version tag (`vX.Y.Z`) or a raw SHA → `UPSTREAM_BASE = stable`. A tag is not a mergeable PR base, so the fix lands on the `stable` **branch** even though it was validated at the pinned SHA. Say so in the report and the PR body, and if `stable` has moved past the pin, re-verify the fix against `stable` before pushing — the code there may differ from what the consumer ran.
      - Whenever `UPSTREAM_BASE = stable`, carry the standing caveat: the fix must also be ported to `main`, or the next `main → stable` promotion silently reverts it.
   2. **Invoke `/validate-consumer-issue`** with a self-contained argument — it re-validates independently and must not have to re-derive context:
      - the reported issue: the symptom, `THIS_REPO` as the reporting consumer, and the upstream ref it was running (`UPSTREAM_TAG` + `UPSTREAM_SHA`);
      - the proposed fix: the diff from this investigation with its `shubhodeep1/coding-workflows@<UPSTREAM_TAG> (<short-sha>):<file>:<line>` anchors;
      - the landing target: `UPSTREAM_BASE` and `UPSTREAM_CHECKOUT` (the attached working tree it writes in);
      - the consumer-side PR link, when `[BOTH]` already landed one.
   3. **Respect its verdict.** `/validate-consumer-issue` owns the decision to land: it may return `MISCONFIG`, `NOT-REPRODUCIBLE`, or judge the proposed fix `INCORRECT` and derive a different one. Never override it, and never re-apply a fix it declined. Its two verdicts and its outcome ship alongside this command's output (Step 10).
   4. **Work inside `UPSTREAM_CHECKOUT`** — every `git` invocation passes `-C "$UPSTREAM_CHECKOUT"`, never the consumer's tree: `git -C "$UPSTREAM_CHECKOUT" fetch origin "$UPSTREAM_BASE"`, then `git -C "$UPSTREAM_CHECKOUT" checkout -B <work-branch> "origin/$UPSTREAM_BASE"`. Name the branch for the fix and suffix it with the base (e.g. `claude/<slug>-stable`) so the target is obvious. Open the PR with `base = $UPSTREAM_BASE`, ready for review.
   5. **Link, don't auto-close.** The upstream PR body references the consumer issue as `Refs <owner>/<repo>#<N>` — never `Fixes` / `Closes` / `Resolves`. Cross-repo auto-close keywords fire on merge, and an orchestrator-tracking issue closed that way kills the state machine that owns it.
   6. **Push failure → fall back cleanly.** If the push is rejected (no write access to `shubhodeep1/coding-workflows`, protected branch, expired auth) after the **Retry Rule** retries for transient errors, restore the attached checkout to the state you found it in — `git -C "$UPSTREAM_CHECKOUT" checkout <original-branch>` then `git -C "$UPSTREAM_CHECKOUT" branch -D <work-branch>` — and revert to the read-only mode output: the proposed diff plus the instruction to open a `shubhodeep1/coding-workflows` session. Leave nothing half-applied in a tree the user did not ask you to modify, and report the push failure and its reason explicitly.

10. **Final output structure** — Always produce, in this order:
    - **Summary** (1–3 lines, including the parsed leads from Step 1, the Step 2 classification + mode, and the target ref — `@main` or `UPSTREAM_TAG (UPSTREAM_SHA)`; in read-write-attached-upstream mode also name `UPSTREAM_CHECKOUT` and `UPSTREAM_BASE`)
    - **Evidence Ledger** (numbered)
    - **Root Cause(s)** — confidence label, plus a target-repo label only when the root cause is a code defect (`[CONSUMER]` / `[UPSTREAM]` / `[BOTH]`). Environmental root causes are marked non-actionable instead and carry no target-repo label.
    - **Fix(es)** — for read-write modes, what was **applied** (with `<file>:<line>` anchors and the branch / PR link); for read-only mode, the **proposed** diff with `<repo>@<UPSTREAM_TAG> (<short-sha>):<file>:<line>`, target-repo label, confidence label, and rationale. Environmental issues do not get a Fix entry.
    - **Upstream hand-off** — only in read-write-attached-upstream mode: the `/validate-consumer-issue` run's two verdicts (issue + fix), its outcome (landed / declined / asked), `UPSTREAM_BASE`, the upstream branch / PR link, and the port-to-`main` caveat when it landed on `stable`. If the push failed and the run fell back to read-only, say so here with the reason.
    - **Inaccessible Resources** — exact URLs the user must open manually, with what's needed from each and what conclusion is blocked without it
    - **Reproduction Result**
    - **Open Questions** — surface remaining ambiguity rather than guessing
    - **Next Step for the User** — for read-write modes, the branch / PR to review (in read-write-attached-upstream mode, both the upstream PR and, for `[BOTH]`, the consumer PR); for read-only mode, a one-sentence instruction to open a `shubhodeep1/coding-workflows` session plus a copy-paste-ready prompt summarising the proposed change. If the only finding is environmental, instruct the user to re-run after the environment recovers and explain why no code change is recommended.

11. **Cleanup** — Remove every temp log file written to `/tmp/` during Step 3 when done.

## Tool Access

GitHub reads can go through either of two equivalent paths — pick whichever is exposed in the current session:

- **`mcp__github__*` MCP tools** — assumed always available. Preferred when the schema fits cleanly (e.g. `get_workflow_run_logs`, `get_job_logs`, `pull_request_read`, `issue_read`, `list_commits`, `get_file_contents`, `list_tags`, `search_issues`, `search_pull_requests`). For the pinned upstream path, all upstream reads MUST pass `ref=<UPSTREAM_SHA>`; for the `main` path, pass `ref=main`.
- **`gh` CLI** — available when `gh` is installed in the session and `GH_TOKEN` or `GITHUB_TOKEN` is set in the environment (consumer repos that ship a SessionStart hook to install `gh` will have both). **Verify auth state directly with `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status` (nounset-safe) before deciding `gh` is unavailable — don't infer it from the SessionStart log.** A consumer hook's secondary probe may only check `actions:read` and emit a `NOTE` / `WARNING` even when `gh` works fine for PRs, issues, commits, and file contents; treat those messages as scope hints, not as "gh is dead." Commands like `gh run view --log <run-id> -R <owner>/<repo>`, `gh run view --log --job <job-id> -R <owner>/<repo>`, `gh run view --log-failed <run-id> -R <owner>/<repo>`, and `gh api repos/<owner>/<repo>/actions/runs/<id>/logs` work for any repo the token has read access to. Useful as a fallback when an MCP call is awkward or returns truncated output. If a *specific* `gh` call returns 401/403/404 for one resource (GitHub returns 404 for many auth-walled / private resources, so treat it as a permission-or-visibility error rather than "resource missing"), or `gh` is missing entirely, fall back to the MCP tool for that call — don't conclude `gh` is broken globally and don't stop the investigation.

**Always pass `-R <owner>/<repo>` on `gh` calls that need repo context.** In Claude Code Web sessions the only git remote points at a local proxy (e.g. `http://...@127.0.0.1:PORT/git/<owner>/<repo>`), so `gh` cannot auto-detect the GitHub repo from `git remote -v`; bare `gh run view ...` fails with `failed to determine base repo`. The SessionStart hook prints the resolved slug — use that for `THIS_REPO`, and use `shubhodeep1/coding-workflows` for upstream calls.

**Keep going until the diagnosis is evidence-based.** A single PR, issue, or run rarely contains the whole story. If the root cause isn't yet supported by log + source citations at `TARGET_REF`/`TARGET_SHA`, pull the next layer: re-fetch the run with `--log-failed`, fetch sibling/previous runs of the same workflow, fetch the linked PR/issue, fetch the workflow YAML at the target ref, fetch artifacts, follow `git blame` on the failing line. Only stop reading when (a) you have an evidence-based fix (applied for read-write modes, proposed for read-only mode), (b) the missing piece is blocked by an **Inaccessible Resource** that is recorded transparently, or (c) every reasonable lead is exhausted.

## Rules

- **Mode is set by the Step 2 classification, not assumed.** Read-write (apply, verify, commit, push, PR) for `[CONSUMER-INTERNAL]` issues and for `[UPSTREAM]` issues when `THIS_REPO == shubhodeep1/coding-workflows`. Read-write-attached-upstream for `[UPSTREAM]` issues when `THIS_REPO` is a *different* consumer **and** a pushable upstream checkout is attached (`UPSTREAM_ATTACHED = yes`) — investigate read-only against `UPSTREAM_SHA`, then auto-chain into `/validate-consumer-issue` to land the fix in `UPSTREAM_CHECKOUT`. Read-only (propose a fix the user takes elsewhere) for `[UPSTREAM]` issues when `UPSTREAM_ATTACHED = no` — that session cannot push to the upstream library. Never edit files in a repo this session cannot push to.
- **Attachment is detected, never created.** `UPSTREAM_ATTACHED` comes from the [Attached upstream checkout](#attached-upstream-checkout) checks — a real, clean, pushable working tree of `shubhodeep1/coding-workflows` present in the session. Do not clone, attach, or otherwise acquire the upstream repo to unlock the write path: an unattached upstream keeps the legacy read-only behaviour, which is a correct outcome and not a failure. A dirty attached tree counts as *not attached* — never branch over someone else's in-flight work.
- **One repo per commit, one PR per repo.** The consumer fix and the upstream fix are separate commits in separate trees with separate PRs. Every write to the attached upstream tree goes through `git -C "$UPSTREAM_CHECKOUT"`; never stage upstream files from the consumer's working directory or vice versa.
- **Ref selection by case.** `[CONSUMER-INTERNAL]` → `THIS_REPO@main`. `[UPSTREAM]` + `THIS_REPO == shubhodeep1/coding-workflows` → `shubhodeep1/coding-workflows@main`. `[UPSTREAM]` + different consumer → pin every upstream read to `UPSTREAM_SHA` (the resolved SHA from Step 2, never the bare tag name — moving tags like `stable` can shift mid-investigation). Reading upstream files at `main` in the *pinned* case is a bug — that consumer is not running `main`. **An attached upstream checkout does not change the read ref**: diagnosis stays pinned to `UPSTREAM_SHA`; `UPSTREAM_CHECKOUT` is a write target, and the branch the fix lands on is `UPSTREAM_BASE` (Step 9's auto-chain), which is not necessarily the same commit.
- **Always download the complete log.** Never truncate or skip sections. When multiple log URLs are present, this rule applies to each — download every accessible log in full before drawing conclusions.
- **Retry Rule**: For transient HTTP/GitHub errors (5xx, 429, timeouts, connection resets, DNS failures), retry with exponential backoff (2s, 4s, 8s, 16s — up to 4 retries) before declaring failure. Applies to every fetch: the log download, GitHub MCP calls, raw HTTP follow-ups.
- **Inaccessible resources** — If a resource is still unreachable after retries, or returns a hard failure (401, 403, 404, 410), or is auth-walled / expired / private, record it under **Inaccessible Resources**:
  - The exact URL
  - What is needed from it
  - What conclusion is blocked without it

  Stop that specific line of inquiry, but continue the broader analysis if the primary log + target ref are accessible and the root cause / fix is still supported by available evidence. Do not make claims that depend on the inaccessible content — surface those gaps under **Open Questions** instead. Abort only if (a) the primary log itself is inaccessible, (b) the target ref cannot be resolved, or (c) the missing resource blocks the root-cause conclusion.
- **Prefer `mcp__github__*` for GitHub reads; fall back to `gh` CLI when `GH_TOKEN` is set and the MCP surface is awkward** (see [Tool Access](#tool-access)). The command operates against whatever scope the host session permits; it does not attempt to widen scope.
- **`gh` calls that need repo context MUST pass `-R <owner>/<repo>` explicitly.** Claude Code Web's git remote is a local proxy that `gh` cannot auto-resolve — bare `gh run view --log <id>` fails with `failed to determine base repo`. The SessionStart hook prints the resolved `THIS_REPO` slug; use that for `THIS_REPO` reads, and `shubhodeep1/coding-workflows` for upstream reads.
- **Prioritise the root cause** — the first meaningful error in the log — over cascading failures.
- **Multiple independent failures** → address each separately, each with its own evidence and fix, classified independently (one may be `[CONSUMER-INTERNAL]` and another `[UPSTREAM]`).
- **Environmental failures** (service down, rate limit, runner outage) — say so explicitly rather than proposing a code fix. Mark them as **environmental / non-actionable** (no target-repo label, no Fix entry) and recommend re-running once the environment recovers. Only assign a target-repo label if the evidence shows an actual code defect underlying the environmental symptom (e.g. a missing retry around a transiently-flaky service).
- **Forbidden silent moves** (whether applying or proposing the fix): modifying tests to make them pass (unless the test is genuinely wrong, with evidence), broadening `except`/`catch` blocks, suppressing warnings, version-bumping without verified compatibility, adding retries to mask deterministic failures, and — in the *pinned* upstream case — switching the consumer's pinned upstream ref to `main` to dodge a stable-tag bug. (Reading `shubhodeep1/coding-workflows` at `main` is correct and expected when `THIS_REPO == shubhodeep1/coding-workflows`; it is forbidden only as a workaround for a different consumer's stable-tag bug.)
- **No guessing.** Every diagnosis cites evidence; every fix is tied to a specific log line and source location at a specific ref. If you cannot find evidence, ask or surface the gap — do not invent it.
- **No scope creep.** Stay within the failure(s) implied by the input. Do not propose unrelated cleanups, refactors, or "while we're in here" changes.
