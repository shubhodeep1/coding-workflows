<!-- changelog: added -->
- **Plan autopilot: the poller can audit `docs/plans/` and start the next plan on its own (opt-in).**

With the repo variable `PLAN_AUTOPILOT_ENABLED=true`, a new `plan_autopilot` job in `orchestrate_poll.yml` runs whenever no orchestrator project is open, at most once per default-branch commit and UTC day. It runs the `/audit-plans` procedure without a person, including the stale-plan refresh. A new `PLAN_AUDIT` role does the audit in the isolated container, with no GitHub token. Plan refreshes, archived plans and registry removals land in one `ai/plan-autopilot-*` maintenance PR, which goes through the normal review pipeline. Each plan that needs a decision gets an `ai:plan-decision` issue. When the recommended plan was not edited by this run, the job starts it, or a bundle of up to 3 related plans, as one Claude-engine orchestrator project. The `ai:plan-autopilot` issue shows the latest report.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls in the poll job | 0 (reuses `find_tracking`'s listing and a cache lookup) |
| Extra jobs per poll tick | 0 while off; at most 1 per (default-branch SHA, UTC day) while on and idle |
| New repo variables | `PLAN_AUTOPILOT_ENABLED` (default `false`), `PLAN_AUTOPILOT_MAINTENANCE_PR_STALE_HOURS` (default `24`) |
| New labels | `ai:plan-autopilot`, `ai:plan-decision` (clarify skips both) |

What this means for operators: nothing changes until you set the variable. To stop it, set `PLAN_AUTOPILOT_ENABLED=false`. Answer a decision issue with `Q1: A` lines, never `/answer`.
