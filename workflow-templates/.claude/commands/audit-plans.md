Audit every plan under `docs/` — read each one, classify it **implemented completely / partially / not implemented** against the *current* repo state, and recommend the single **next highest-value** plan to implement **that will not create merge conflicts with in-flight project work** — or, when related plans are better shipped together, a **bundle** of up to 3 related plans to run as **one** orchestrator project (step 6) — AI-orchestrator projects (open `ai:orchestrator-tracking` issues and their unmerged PRs; both `/implement-plan-ai` and `/implement-plan-claude` start these). If no candidate is conflict-safe, the correct output is *no recommendation*. The recommendation itself is report-only — this command never implements a plan — but every run also performs three bounded maintenance actions: it **refreshes stale plans** (step 4a: updates open plans that drifted from the default branch or overlap other plans, so the next plan handed off matches the repo as it is now), it **archives verified-complete plans** (moves `docs/plans/` files classified COMPLETE into `docs/completed/`) and **sweeps `docs/scripts-pending-removal.md`** (removes scripts whose §18.F removal trigger is met and whose preflight checks all pass, deleting the registry entry alongside), committing and opening a maintenance PR only when any of these changed something. `$ARGUMENTS` is optional — pass a filter (a subdirectory, a topic, or a glob) to narrow the audit; empty means audit all plans under `docs/plans/`.

$ARGUMENTS

## Procedure

1. **Read project context.** `CLAUDE.md` is already loaded; search `README.md` and `agents.md` for sections relevant to the plans being audited, and read those instead of both files end to end, so "value" and "implemented" are judged against how this repo actually works.

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

4a. **Check every open plan for staleness, then refresh it.** A plan written weeks ago can describe a repo that no longer exists. For each `docs/plans/*` plan step 4 classified PARTIAL or NOT IMPLEMENTED, compare it against the default branch as it is now and against the other plans, and mark it **CURRENT** or **STALE**:
   - **Baseline.** Run `git fetch origin <default>` (resolve the default branch, do not hardcode `main`) and check against `origin/<default>`. The plan's baseline is its last commit, `git log -1 --format=%cI -- <plan>`; `git log --since=<baseline> origin/<default> -- <paths>` lists what changed under the plan's declared files since it was written.
   - **Drift against the default branch.** A plan is STALE when any of these holds, each cited with the plan line and the repo evidence: a file, function, env var, repo variable, workflow, job, step, label, contract or test it names was renamed, moved or removed; a premise about current behaviour no longer holds (the code it describes now works differently); part of its scope was already shipped by other work; or it relies on a CLAUDE.md section, command or pipeline that was retired.
   - **Overlap with other plans.** A plan is also STALE when it duplicates scope another open plan in `docs/plans/` owns, contradicts another plan's approach to the same code, or conflicts with a `docs/completed/` plan that shipped after its baseline. Name the other plan and the overlapping goal or file.
   - **Refresh every STALE plan in place** with `Edit`: update renamed paths and identifiers to their current names; mark goals already shipped as done with the commit or PR that shipped them; rewrite steps whose premise changed so they start from the current code; and resolve an overlap by keeping the shared piece in one plan and replacing it in the other with a cross-reference. Keep each plan's goals, scope and structure: a refresh adapts *how* the plan gets there, never *what* it delivers.
   - **Do not guess at intent.** When a fix would change a plan's goals or scope, or two plans contradict and either approach could win, leave that part unchanged and mark the plan **NEEDS DECISION**. Ask about it in the §2 Q/A format at the end of the report. A NEEDS DECISION plan is not a candidate in steps 5–6 until it is answered.
   - **Plans in flight are checked, not edited.** An orchestrator project is already being planned from a plan that an open `ai:orchestrator-tracking` issue names. Report such a plan's drift and leave the file alone. Fetch the open tracking-issue listing here, once, and reuse it in step 6 (§15).
   - Steps 5–6 rank and screen the refreshed text, so the declared files the conflict gate compares are current.

5. **Rank the remaining work by value.** Across the PARTIAL + NOT-IMPLEMENTED plans, weigh each by impact × leverage against cost and risk, and produce a **ranked candidate list** (best first). Justify the ranking with the same criteria the final pick must satisfy:
   - **§1 priority order** — security and correctness rank above performance and speed; a security/correctness plan outranks a perf plan of similar size.
   - **Dependencies** — a plan that unblocks several others is worth more; a plan blocked by missing prerequisites ranks lower.
   - **Blast radius / risk** and rough **effort** — prefer high-value, well-scoped, low-risk work.

   Ranking alone does not decide the recommendation — the pick is made in step 6, after the conflict screen.

6. **Screen candidates against in-flight project work (merge-conflict gate).** The recommendation must be startable *now* without colliding with work already in flight from the **AI orchestrator** (`/implement-plan-ai` or `/implement-plan-claude`; each project is an open `ai:orchestrator-tracking` issue):
   - **Fetch GitHub state once.** Use REST per §23.D (`gh api "repos/<owner>/<repo>/issues?labels=ai:orchestrator-tracking&state=open"`, one `state=open` pulls listing) and batch per §15 — one issue listing and one PR listing serve every project; do not query per candidate plan or per project. Reuse the issue listing step 4a already fetched.
   - **Enumerate in-flight orchestrator work.** List **open** orchestrator tracking issues (label `ai:orchestrator-tracking`) and the **unmerged** PRs belonging to them: open PRs whose head or base branch matches the integration-branch pattern (`^orchestrator/project-`, cf. `ORCH_INTEGRATION_BRANCH_PATTERN`) plus open PRs that reference a tracking issue (`Refs #N` / `Related to #N`).
   - **Build the in-flight footprint.** Take the union of:
     - (a) the files changed by each unmerged in-flight PR (`gh api "repos/<owner>/<repo>/pulls/<n>/files"`, paginated);
     - (b) for each open tracking issue whose remaining waves have not yet produced PRs, the files / modules the project's source plan declares it will touch — read that plan doc (the tracking issue names it). A bundle project's tracking issue names 2–3 plans (step 6, `/implement-plan-ai` Bundles): read every plan it names and add each one's declared files, not just the first. An open tracking issue counts even with zero open PRs: its undispatched waves are still incoming changes;
   - **Set aside plans that are already in flight.** A candidate that is itself the source plan of an in-flight project — an open tracking issue names it — is removed from the candidate list and reported as **in flight** with its owner (`orchestrator issue #N`). For a bundle project this applies to every plan the tracking issue names, not only the first-named one. An identified in-flight plan is neither the recommendation nor a conflict-screened candidate: someone is already implementing it.
   - **Screen the remaining candidates, best-ranked first.** A candidate **conflicts** when its declared touched files overlap the footprint, or when the two would change different files inside the same tightly-coupled unit (a script and the workflow step that calls it, a `/db/contracts/*` file and its collection code, a template and its consumer-repo wrapper). This is a judgement call on real overlap, not bare filename equality — shared but independent files (e.g. both append distinct rows to a doc table) may pass with a note. Record, for every screened-out candidate, the overlapping path(s) and the blocking issue / PR.
   - **Pick.** The recommendation is the **highest-ranked conflict-free candidate**. If *every* candidate carries conflict risk, recommend **nothing**: say so explicitly, report per candidate what blocks it and on which paths, and name the merge/close events that would unblock the audit — do not fall back to a "least risky" pick.
   - **Bundle related plans into one project when appropriate.** Plans that touch the same code cannot run as separate projects at the same time (each would conflict with the other), so they would otherwise ship one audit cycle after another. One orchestrator project can sequence them inside a single issue DAG instead. Starting from the pick (the **anchor**), consider the remaining conflict-free candidates in ranking order — never an in-flight or screened-out plan — and add one as a bundle **member** only when all of these hold:
     - **Related** to the anchor or to a member already added: the two declare overlapping touched files or change the same tightly-coupled unit (the same test as the conflict screen above), or one depends on the other (a declared prerequisite, or one's phases build on code the other adds). Sharing only an independent file — one the conflict screen lets pass, such as distinct rows appended to one doc table — does not make two plans related. Unrelated plans are never bundled, however small or conflict-free — they stay separate recommendations for later audits.
     - **Prerequisites satisfied:** every prerequisite the candidate declares is already shipped on the default branch, or is the anchor or a member already added. A candidate with any other unshipped prerequisite is skipped: the bundle's issue DAG can only order the plans it is given.
     - **Within the caps:** at most **3 plans** and at most **8 phases** in the bundle, counting each plan's phases from its Phases & Merge Strategy section (a plan with no phase split counts as 1; for a PARTIAL plan count only the phases still missing). A candidate that would break either cap is skipped, not truncated.
     A candidate ranked lower only because a prerequisite was missing may join when that prerequisite is the anchor or a member. The anchor's own declared prerequisites follow the same rule before any bundle forms: each must already be shipped on the default branch or join as a member (passing every condition above, and listed ahead of the anchor). If any anchor prerequisite can do neither (it is unshipped and in flight, screened out, or would break a cap), the anchor is not startable now and is not recommended, alone or bundled: report it as **blocked on an unshipped prerequisite**, naming that prerequisite, then repeat the pick and this bundle step from the next-ranked conflict-free candidate; if none remains, the no-recommendation outcome applies. List the bundle in dependency order (prerequisites first) and record, for each member, the shared path / unit or the dependency that relates it. If no other candidate qualifies as a member, the recommendation stays the single anchor plan — only an anchor whose own prerequisites are all shipped; a blocked anchor is never recommended, per the rule above. Bundling never applies to an UNSCREENED pick (degraded mode) or to a no-recommendation outcome.
   - **Degraded mode.** If GitHub state cannot be read (missing/invalid token, API failure — degrade per §23.E, say so once), report the value ranking but mark the pick **UNSCREENED**, instructing the user to check open `ai:orchestrator-tracking` issues and their PRs before starting. Never present an unscreened pick as conflict-safe.

7. **Archive verified-complete plans.** For every `docs/plans/*.md` plan step 4 classified **COMPLETE**, move it into `docs/completed/` with `git mv` (create the directory if missing) so history follows the file. If `docs/completed/` already holds a file with the same name, diff the two: byte-identical → `git rm` the `docs/plans/` copy; different → `git mv -f` so the `docs/plans/` version (the maintained copy) replaces the stale archive copy. PARTIAL and NOT-IMPLEMENTED plans never move — archival is the "this project is done" signal and must be earned by step 4's evidence, not by a plan's self-reported status.

8. **Sweep the removal registry.** Read `docs/scripts-pending-removal.md` (§18.F) and evaluate every entry:
   - **`permanent — review annually` entries are never auto-removed.** Report them as kept; they have no sunset.
   - For each entry with a concrete removal trigger, judge whether the trigger is met, then run **every** preflight check exactly as written and compare against the entry's expected result.
   - **Trigger met and all checks pass** → remove what the entry names (`git rm` the script / workflow) and delete the entry from the registry **in the same commit** — the registry is a live list, not an audit log. Remove only what the entry covers; adjacent cleanup is out of scope.
   - **Any check fails, cannot be run (missing token, API failure — degrade per §23.E), or is ambiguous** → keep the script and the entry, and report which check blocked removal. A partial pass is a fail.
   The full §18.F pass is what authorizes the §6 removal; there is no other path by which this command deletes a script.

9. **Land the maintenance changes — only when steps 4a, 7 or 8 changed anything.** If nothing was refreshed, moved or removed, skip this step entirely: the run stays report-only, with no commit and no PR. Otherwise:
   - Commit on the session's designated working branch when the harness assigns one; otherwise create `claude/audit-plans-maintenance-<YYYYMMDD>` (append `-2`, `-3`, … on collision). One commit for the plan refreshes, one for the plan archival, and a separate commit for the registry sweep (§12.E).
   - When a removal changes observable behaviour (§20.A — a workflow, scheduled job, or operator-visible script), add a `changelog.d/<issue-or-pr>-<slug>.md` fragment in the same PR; plan refreshes and moves alone are docs-only and need no fragment.
   - Enumerate every refreshed plan in the PR body with what was stale and what changed, so a reviewer can check each refresh against the evidence.
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

Refreshed — stale against the default branch or other plans (only if any):
- <plan> — <what was stale, with evidence> → <what the refresh changed>

Stale, needs decision (only if any; asked as Q-IDs at the end):
- <plan> — <the goal / scope / contradiction a person must settle>

Stale but in flight — reported, not edited (only if any):
- <plan> — <orchestrator issue #N>; <what drifted>

In-flight orchestrator work: <issue #N (open, waves pending), PR #M (open, files: …)>  (or: none / UNAVAILABLE — <why>)

In flight — not candidates (only if any):
- <plan> — <orchestrator issue #N>

Conflict-screened out (only if any):
- <plan> — overlaps <path(s)> with <issue #N / PR #M>

Blocked on an unshipped prerequisite (only if any):
- <plan> — needs <prerequisite plan> (<in flight in orchestrator issue #N / screened out: overlaps <path(s)> with <issue #N / PR #M> / over the bundle caps>)

Archived to docs/completed/ (only if any):
- <plan> — moved; Refs #<issue> (<boxes all ticked / de-scoped in PR body>)

Registry sweep (docs/scripts-pending-removal.md): removed X, kept Y of N entries
- removed: <script path> — trigger met; all preflight checks passed
- kept: <script path> — <permanent — review annually / which check failed or was unrunnable>

Maintenance: <branch> → <PR url>   (or: no changes — report-only run)

➡ Next highest-value to implement: <plan>
Why: <2–4 lines — value, cost, §1 priority, dependencies, and confirmation of no overlap
with in-flight orchestrator work (or the UNSCREENED caveat). Reference
/implement-plan-claude (Claude-engine orchestrator hand-off) or /implement-plan-ai (default-engine hand-off) as the follow-up.>
```

When step 6 formed a bundle, replace the final block with the following. List the plans, and the `Hand off:` paths, in dependency order (prerequisites first): a member that is a prerequisite of the anchor goes ahead of the anchor. Write one `- <plan>` line per bundled plan; the anchor line follows any member it depends on.

```
➡ Next highest-value to implement: bundle of N related plans as ONE orchestrator project
- <plan> — <P> phases; <anchor | related via <shared path(s) / coupled unit / depends on <plan>>>
Total: N plans, P phases (caps: 3 plans, 8 phases)
Why: <2–4 lines — combined value, why one project beats separate projects (the members
would conflict with each other or depend on each other), §1 priority, and confirmation that
no member overlaps in-flight orchestrator work.>
Hand off: /implement-plan-claude <plan path> <plan path> …   (or /implement-plan-ai with the same paths)
```

When the conflict gate excludes every candidate, replace the final block with:

```
➡ No recommendation — every viable plan risks merge conflicts with in-flight orchestrator work.
- <plan> — blocked by <issue #N / PR #M> on <path(s)>   (or: blocked on unshipped prerequisite <plan>)
Re-run /audit-plans once <the blocking PRs merge / the blocking projects complete>.
```

Use the same block when step 6 ends with no recommendation because the remaining candidates are blocked on unshipped prerequisites rather than (or as well as) conflicts: the headline then says `every viable plan risks merge conflicts with in-flight orchestrator work or is blocked on an unshipped prerequisite` (or names only the prerequisite cause when no candidate conflicts), and the re-run line names the prerequisite plan shipping as the unblock event.

Omit empty sections. Keep evidence terse but specific — a classification without a citation is a guess.

## Tool Access

Audit surface (read) plus the bounded step 4a and 7–9 write path:

- **`Read` / `Grep` / `Glob`** — primary tools for reading plans and verifying repo state.
- **`Edit`** — the step-4a refresh of stale `docs/plans/*` files, and nothing else.
- **`Bash`** — cheap verification (running a targeted test, listing files), the step-4a `git fetch` / `git log` drift checks, step-8 preflight-check commands, and the step 7–9 maintenance writes: `git mv` / `git rm`, commit, push. No mutating git operations outside steps 7–9.
- **`mcp__github__*` / `gh` CLI** — read paths (§23.A): (a) when a plan references an issue / PR whose merge state settles whether it shipped, (b) the step-6 conflict gate — listing open `ai:orchestrator-tracking` issues, their unmerged PRs, and those PRs' changed files — and (c) registry preflight checks that query GitHub state; plus one routine write (§23.B): opening the step-9 maintenance PR. Shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene); batch the gate's calls per §15/§23.F.

## Rules

- **Report-first; writes are bounded to maintenance.** The verdict, ranking, and recommendation are chat output — this command never implements a plan and never edits code outside steps 4a and 7–9: refreshes of stale `docs/plans/*` files, plan moves into `docs/completed/`, §18.F-gated script + registry-entry removals, the changelog fragment those removals may require, and the commit / push / PR that lands them. A run with nothing to refresh, archive or remove makes no commit and opens no PR.
- **A refresh adapts the plan, never its intent.** Every refresh edit cites the drift or overlap that caused it. Goals, scope and structure stay as written; a change to any of them is NEEDS DECISION, asked in the §2 Q/A format, never applied. Plans named by an open `ai:orchestrator-tracking` issue and plans under `docs/completed/` are never refreshed. To act on the recommendation, the user runs `/implement-plan-claude` (Claude-engine orchestrator hand-off) or `/implement-plan-ai` (default-engine orchestrator hand-off).
- **Archival must be earned.** Only plans step 4 classified COMPLETE — every goal cited — move to `docs/completed/`; a PARTIAL plan never moves, whatever its own Status line says. Use `git mv` so history follows, and satisfy the plan-archival lint via step 9's PR-body contract.
- **The preflight gate is a hard gate.** No script is removed while any §18.F check fails, cannot be run, or is ambiguous, and no `permanent — review annually` entry is ever auto-removed. The removal's blast radius stays inside what the registry entry names.
- **Verify, don't trust the plan's self-reported status.** Classification must cite repo evidence (`file:line`, a passing/failing test, an absent symbol). "The plan says it's done" is not evidence.
- **Treat `docs/completed/` as presumed-done but auditable** — surface any plan there that the repo shows is regressed or was never fully implemented.
- **Value ranking obeys §1.** Security and correctness outrank performance and speed. Factor dependencies (unblocking value), blast radius, and effort into the single recommendation.
- **The conflict gate is a hard gate.** A candidate that overlaps in-flight orchestrator work (step 6) is never the recommendation, whatever its value rank — value does not buy its way past conflict risk. When no candidate passes, "no recommendation" is the correct, complete output; a hedged or "least risky" pick is not.
- **In-flight plans are never candidates.** A plan an open tracking issue is already implementing is reported as in flight, not recommended — even when it ranks first.
- **Screening is evidence-based too.** Every screened-out candidate must cite the blocking issue / PR and the overlapping path(s); every conflict-free pick must state what was screened against. If GitHub state was unreadable, the pick is reported as UNSCREENED — never silently assumed safe.
- **One recommendation (or an explicit none), fully justified.** The deliverable is the classification table plus exactly one conflict-free "next highest-value" pick with a falsifiable rationale — a single plan, or one bundle of related plans for one orchestrator project — or the explicit no-recommendation block — not a vague "several of these would be good."
- **Bundles are earned by relatedness, not size.** Every bundle member must be conflict-free against in-flight work and related to the anchor or another member (shared files, a coupled unit, or a dependency), with each relation cited. Never exceed 3 plans or 8 phases, never bundle in degraded mode, and never recommend several separate projects at once.
