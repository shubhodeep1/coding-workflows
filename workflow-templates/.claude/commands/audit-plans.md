Audit every plan under `docs/` — read each one, classify it **implemented completely / partially / not implemented** against the *current* repo state, and recommend the single **next highest-value** plan to implement. Read-only: this command reports in chat and never edits files or opens a PR. `$ARGUMENTS` is optional — pass a filter (a subdirectory, a topic, or a glob) to narrow the audit; empty means audit all plans under `docs/plans/`.

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

5. **Rank the remaining work by value.** Across the PARTIAL + NOT-IMPLEMENTED plans, weigh each by impact × leverage against cost and risk, and pick the **single next highest-value plan** to implement. Justify the pick in a short paragraph that accounts for:
   - **§1 priority order** — security and correctness rank above performance and speed; a security/correctness plan outranks a perf plan of similar size.
   - **Dependencies** — a plan that unblocks several others is worth more; a plan blocked by missing prerequisites ranks lower.
   - **Blast radius / risk** and rough **effort** — prefer high-value, well-scoped, low-risk work.

6. **Report.** Emit the [Output Format](#output-format). No edits, no commits, no PR.

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

➡ Next highest-value to implement: <plan>
Why: <2–4 lines — value, cost, §1 priority, dependencies. Reference /implement-plan-claude (in-session) or /implement-plan-ai (AI orchestrator) as the follow-up.>
```

Omit empty sections. Keep evidence terse but specific — a classification without a citation is a guess.

## Tool Access

Read-only surface:

- **`Read` / `Grep` / `Glob`** — primary tools for reading plans and verifying repo state.
- **`Bash`** — for cheap verification only (running a targeted test, listing files). No mutating git operations.
- **`mcp__github__*` / `gh` CLI** — only if a plan references an issue / PR whose merge state settles whether it shipped. Read-only work (§23.A); shared rules live in **CLAUDE.md §23** (auth check, the mandatory `-R <owner>/<repo>` flag, REST-over-GraphQL preference, token hygiene) — see **CLAUDE.md §23**.

## Rules

- **Read-only.** This command produces a verdict, not changes. No file edits, no commits, no PR. To act on the recommendation, the user runs `/implement-plan-claude` (Claude implements it in-session) or `/implement-plan-ai` (hand it to the AI orchestrator).
- **Verify, don't trust the plan's self-reported status.** Classification must cite repo evidence (`file:line`, a passing/failing test, an absent symbol). "The plan says it's done" is not evidence.
- **Treat `docs/completed/` as presumed-done but auditable** — surface any plan there that the repo shows is regressed or was never fully implemented.
- **Value ranking obeys §1.** Security and correctness outrank performance and speed. Factor dependencies (unblocking value), blast radius, and effort into the single recommendation.
- **One recommendation, fully justified.** The deliverable is the classification table plus exactly one "next highest-value" pick with a falsifiable rationale — not a vague "several of these would be good."
