Audit every plan under `docs/` — read each one, classify it **implemented completely / partially / not implemented** against the *current* repo state, and recommend the single **next highest-value** plan to implement **that will not create merge conflicts with in-flight orchestrator work** (open `ai:orchestrator-tracking` issues and their unmerged PRs). If no candidate is conflict-safe, the correct output is *no recommendation*. The recommendation itself is report-only — this command never implements a plan — but every run also performs two bounded maintenance actions: it **archives verified-complete plans** (moves `docs/plans/` files classified COMPLETE into `docs/completed/`) and **sweeps `docs/scripts-pending-removal.md`** (removes scripts whose §18.F removal trigger is met and whose preflight checks all pass, deleting the registry entry alongside), committing and opening a maintenance PR only when either action changed something. `$ARGUMENTS` is optional — pass a filter (a subdirectory, a topic, or a glob) to narrow the audit; empty means audit all plans under `docs/plans/`.

$ARGUMENTS

## Procedure

1. **Read project context.** Read `README.md`, `agents.md`, and `CLAUDE.md` so "value" and "implemented" are judged against how this repo actually works.

2. **Enumerate the plans.** List `docs/plans/*.md` (the open plans) and `docs/completed/*.md` (already-shipped plans, used for dedup and to catch regressions). Include other plan-like docs under `docs/` only if they read as plans (e.g. `*-plan.md`, `*-improvements.md`). Honor any `$ARGUMENTS` filter. Record the full list up front.

3. **Read each plan in full.** For every plan, extract its goals / acceptance criteria, the files & modules it says it will touch, its phases, and any tests or rollout wiring it specifies. Do not rely on a plan's own "Status" line — verify against the repo.

4. **Verify each plan against the current repo — evidence-based.** For each plan, check whether the described code / config / workflows / contracts / tests actually exist and are wired:
   - `Grep` / `Glob` / `Read` for the functions, files, env vars, workflow triggers, and contract entries the plan names.
   - Run cheap, targeted checks where they settle a question (e.g. does a referenced test exist and pass).
   - Classify the plan:
     - **COMPLETE** — every goal is present and verified in the repo (cite `file:line` / test / merged wiring).
     - **PARTIAL** — some goals present, others missing (list what's done and what's missing).
     - **NOT IMPLEMENTED** — none present, or scaffold-only.
   - For `docs/completed/*` plans, default-trust but flag any you find regressed or never actually finished.

5. **Rank the remaining work by value.** Across the PARTIAL + NOT-IMPLEMENTED plans, weigh each by impact × leverage against cost and risk, and produce a **ranked candidate list** (best first). Justify the ranking with the same criteria the final pick must satisfy:
   - **§1 priority order** — security and correctness rank above performance and speed; a security/correctness plan outranks a perf plan of similar size.
   - **Dependencies** — a plan that unblocks several others is worth more; a plan blocked by missing prerequisites ranks lower.
   - **Blast radius / risk** and rough **effort** — prefer high-value, well-scoped, low-risk work.

   Ranking alone does not decide the recommendation — the pick is made in step 6, after the conflict screen.

6. **Screen candidates against in-flight orchestrator work (merge-conflict gate).** The recommendation must be startable *now* without colliding with work the AI pipeline already has in flight:
   - **Enumerate in-flight work.** List **open** orchestrator tracking issues (label `ai:orchestrator-tracking`) and the **unmerged** PRs belonging to them: open PRs whose head or base branch matches the integration-branch pattern (`^orchestrator/project-`, cf. `ORCH_INTEGRATION_BRANCH_PATTERN`) plus open PRs that reference a tracking issue (`Refs #N` / `Related to #N`). Use REST per §23.D (`gh api "repos/<owner>/<repo>/issues?labels=ai:orchestrator-tracking&state=open"`, one `state=open` pulls listing) and batch per §15 — one issue listing and one PR listing serve every project; do not query per candidate plan.
   - **Build the in-flight footprint.** Take the union of: (a) the files changed by each unmerged in-flight PR (`gh api "repos/<owner>/<repo>/pulls/<n>/files"`, paginated); and (b) for each open tracking issue whose remaining waves have not yet produced PRs, the files / modules the project's source plan declares it will touch — read that plan doc (the tracking issue names it). An open tracking issue counts even with zero open PRs: its undispatched waves are still incoming changes.
   - **Screen the candidates, best-ranked first.** A candidate **conflicts** when its declared touched files overlap the footprint, or when the two would change different files inside the same tightly-coupled unit (a script and the workflow step that calls it, a `/db/contracts/*` file and its collection code, a template and its consumer-repo wrapper). This is a judgement call on real overlap, not bare filename equality — shared but independent files (e.g. both append distinct rows to a doc table) may pass with a note. Record, for every screened-out candidate, the overlapping path(s) and the blocking issue / PR.
   - **Pick.** The recommendation is the **highest-ranked conflict-free candidate**. If *every* candidate carries conflict risk, recommend **nothing**: say so explicitly, report per candidate what blocks it and on which paths, and name the merge/close events that would unblock the audit — do not fall back to a "least risky" pick.
   - **Degraded mode.** If GitHub state cannot be read (missing/invalid token, API failure — degrade per §23.E, say so once), still report the value ranking but mark the pick **UNSCREENED**, instructing the user to check open `ai:orchestrator-tracking` issues and their PRs before starting. Never present an unscreened pick as conflict-safe.

7. **Archive verified-complete plans.** For every `docs/plans/*.md` plan step 4 classified **COMPLETE**, move it into `docs/completed/` with `git mv` (create the directory if missing) so history follows the file. If `docs/completed/` already holds a file with the same name, diff the two: byte-identical → `git rm` the `docs/plans/` copy; different → `git mv -f` so the `docs/plans/` version (the maintained copy) replaces the stale archive copy. PARTIAL and NOT-IMPLEMENTED plans never move — archival is the "this project is done" signal and must be earned by step 4's evidence, not by a plan's self-reported status.

8. **Sweep the removal registry.** Read `docs/scripts-pending-removal.md` (§18.F) and evaluate every entry:
   - **`permanent — review annually` entries are never auto-removed.** Report them as kept; they have no sunset.
   - For each entry with a concrete removal trigger, judge whether the trigger is met, then run **every** preflight check exactly as written and compare against the entry's expected result.
   - **Trigger met and all checks pass** → remove what the entry names (`git rm` the script / workflow) and delete the entry from the registry **in the same commit** — the registry is a live list, not an audit log. Remove only what the entry covers; adjacent cleanup is out of scope.
   - **Any check fails, cannot be run (missing token, API failure — degrade per §23.E), or is ambiguous** → keep the script and the entry, and report which check blocked removal. A partial pass is a fail.
   The full §18.F pass is what authorizes the §6 removal; there is no other path by which this command deletes a script.

9. **Land the maintenance changes — only when steps 7–8 changed anything.** If nothing moved and nothing was removed, skip this step entirely: the run stays report-only, with no commit and no PR. Otherwise:
   - Commit on the session's designated working branch when the harness assigns one; otherwise create `claude/audit-plans-maintenance-<YYYYMMDD>` (append `-2`, `-3`, … on collision). One commit for the plan archival, a separate commit for the registry sweep (§12.E).
   - When a removal changes observable behaviour (§20.A — a workflow, scheduled job, or operator-visible script), add a `changelog.d/<issue-or-pr>-<slug>.md` fragment in the same PR; plan moves alone are docs-only and need no fragment.
   - **PR-body contract:** the plan-archival lint (`.github/workflows/lint-plan-archival.yml`) fails any PR that adds `docs/completed/` files without referencing an issue. For each moved plan, include `Refs #N` for its tracking issue (or, when none exists, the issue / PR that shipped it) — never `Fixes` / `Closes` / `Resolves` (§19). When a referenced `ai:orchestrator-tracking` issue still has unchecked checkboxes, add a non-empty `## De-scoped phases` section naming every unshipped box with a rationale. Enumerate every removed script with its preflight evidence.
   - Push with `git push -u origin <branch>` (retry transient network errors with exponential backoff: 2s, 4s, 8s, 16s — up to 4 retries), then open a non-draft PR via `mcp__github__create_pull_request` against the default branch (resolve it dynamically — do **not** hardcode `main`). Pushing the branch and opening the PR are §23.B routine writes.

10. **Report.** Emit the [Output Format](#output-format).

## Output Format

```
Plans audited: N  (docs/plans: X, docs/completed: Y)

Completely implemented:
- <plan> — <evidence: file:line / test / merged wiring>

Partially implemented:
- <plan> — done: <…>; missing: <…>

Not implemented:
- <plan> — <one-line scope>

Regressions / mislabeled (only if any):
- <plan> — <what's off>

In-flight orchestrator work: <issue #N (open, waves pending), PR #M (open, files: …)>  (or: none / UNAVAILABLE — <why>)

Conflict-screened out (only if any):
- <plan> — overlaps <path(s)> with <issue #N / PR #M>

Archived to docs/completed/ (only if any):
- <plan> — moved; Refs #<issue> (<boxes all ticked / de-scoped in PR body>)

Registry sweep (docs/scripts-pending-removal.md): removed X, kept Y of N entries
- removed: <script path> — trigger met; all preflight checks passed
- kept: <script path> — <permanent — review annually / which check failed or was unrunnable>

Maintenance: <branch> → <PR url>   (or: no changes — report-only run)

➡ Next highest-value to implement: <plan>
Why: <2–4 lines — value, cost, §1 priority, dependencies, and confirmation of no overlap
with in-flight orchestrator work (or the UNSCREENED caveat). Reference
/implement-plan-claude (in-session) or /implement-plan-ai (AI orchestrator) as the follow-up.>
```

When the conflict gate excludes every candidate, replace the final block with:

```
➡ No recommendation — every viable plan risks merge conflicts with in-flight orchestrator work.
- <plan> — blocked by <issue #N / PR #M> on <path(s)>
Re-run /audit-plans once <the blocking PRs merge / the blocking projects complete>.
```

Omit empty sections. Keep evidence terse but specific — a classification without a citation is a guess.

## Tool Access

Audit surface (read) plus the bounded step 7–9 write path:

- **`Read` / `Grep` / `Glob`** — primary tools for reading plans and verifying repo state.
- **`Bash`** — cheap verification (running a targeted test, listing files), step-8 preflight-check commands, and the step 7–9 maintenance writes: `git mv` / `git rm`, commit, push. No mutating git operations outside steps 7–9.
- **`mcp__github__*` / `gh` CLI** — read paths (§23.A): (a) when a plan references an issue / PR whose merge state settles whether it shipped, (b) the step-6 conflict gate — listing open `ai:orchestrator-tracking` issues, their unmerged PRs, and those PRs' changed files — and (c) registry preflight checks that query GitHub state; plus one routine write (§23.B): opening the step-9 maintenance PR. Shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene); batch the gate's calls per §15/§23.F.

## Rules

- **Report-first; writes are bounded to maintenance.** The verdict, ranking, and recommendation are chat output — this command never implements a plan and never edits code outside steps 7–9: plan moves into `docs/completed/`, §18.F-gated script + registry-entry removals, the changelog fragment those removals may require, and the commit / push / PR that lands them. A run with nothing to archive or remove makes no commit and opens no PR. To act on the recommendation, the user runs `/implement-plan-claude` (Claude implements it in-session) or `/implement-plan-ai` (hand it to the AI orchestrator).
- **Archival must be earned.** Only plans step 4 classified COMPLETE — every goal cited — move to `docs/completed/`; a PARTIAL plan never moves, whatever its own Status line says. Use `git mv` so history follows, and satisfy the plan-archival lint via step 9's PR-body contract.
- **The preflight gate is a hard gate.** No script is removed while any §18.F check fails, cannot be run, or is ambiguous, and no `permanent — review annually` entry is ever auto-removed. The removal's blast radius stays inside what the registry entry names.
- **Verify, don't trust the plan's self-reported status.** Classification must cite repo evidence (`file:line`, a passing/failing test, an absent symbol). "The plan says it's done" is not evidence.
- **Treat `docs/completed/` as presumed-done but auditable** — surface any plan there that the repo shows is regressed or was never fully implemented.
- **Value ranking obeys §1.** Security and correctness outrank performance and speed. Factor dependencies (unblocking value), blast radius, and effort into the single recommendation.
- **The conflict gate is a hard gate.** A candidate that overlaps in-flight orchestrator work (step 6) is never the recommendation, whatever its value rank — value does not buy its way past conflict risk. When no candidate passes, "no recommendation" is the correct, complete output; a hedged or "least risky" pick is not.
- **Screening is evidence-based too.** Every screened-out candidate must cite the blocking issue / PR and the overlapping path(s); every conflict-free pick must state what was screened against. If GitHub state was unreadable, the pick is reported as UNSCREENED — never silently assumed safe.
- **One recommendation (or an explicit none), fully justified.** The deliverable is the classification table plus exactly one conflict-free "next highest-value" pick with a falsifiable rationale — or the explicit no-recommendation block — not a vague "several of these would be good."
