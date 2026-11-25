<!-- changelog: added -->
- **`/audit-plans` now checks every open plan against the current default branch and the other plans, and refreshes stale plans before it recommends one.** The next plan handed to the orchestrator matches the repo as it is now, not as it was when the plan was written.

Before this change the audit checked only whether a plan had shipped, so a plan written weeks earlier could be recommended with renamed paths, outdated premises, or scope another plan already covered. New step 4a in `.claude/commands/audit-plans.md` compares each PARTIAL or NOT IMPLEMENTED plan in `docs/plans/` with `origin/<default>`, starting from the plan's last commit. It also compares the plan with the other open plans and with `docs/completed/` plans shipped since then. Stale plans are edited in place and land in the audit's existing maintenance PR. Ranking and the merge-conflict gate then read the refreshed text.

| The numbers that matter | Value |
| --- | --- |
| New audit step | 4a, between classification (step 4) and ranking (step 5) |
| Extra GitHub API calls | 0 (step 6 reuses the tracking-issue listing step 4a fetches) |
| New report sections | 3 (refreshed, needs decision, stale but in flight) |

What this means for operators: the maintenance PR from `/audit-plans` can now carry plan edits as well as archival moves and registry removals, and its body lists what was stale in each plan and what changed. A refresh never changes a plan's goals or scope. A plan that would need that is flagged NEEDS DECISION, asked as a Q-ID question, and left out of the recommendation until you answer. Plans already being implemented by an open `ai:orchestrator-tracking` project are reported but never edited.

### For contributors

`tests/test_audit_plans_command.py` pins step 4a's order, its comparisons, the intent-preserving refresh rule, the single tracking-issue fetch, and the step 9 maintenance-PR wiring. Consumer repos receive the updated command from `workflow-templates/.claude/commands/` on the next `@stable` sync.
