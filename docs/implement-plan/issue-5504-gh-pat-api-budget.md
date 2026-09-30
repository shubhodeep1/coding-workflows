# Implement-Plan Log — GH_PAT API budget: measure per job, cut the top poller and review consumers, re-queue failed /reclarify routing

- Plan: docs/plans/issue-5504-gh-pat-api-budget-plan.md
- Source issue: shubhodeep1/coding-workflows#5504
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-5504-gh-pat-api-budget   Final PR: (opened after this commit)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened by the invoking session (session_016HuYAARw9B56SzoGrzmB7g); phase 1 in progress.

## Phases
1. [ ] Phase 1 — budget logging, daily report, poller and collector cuts, /reclarify re-queue

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How much of "cut the biggest consumers" belongs in this single phase, given that measured data cannot exist before the project merges into `main`? — Picked: A — Cut the consumers the current evidence already identifies (the poller's per-PR conflict and noop reads, and the collector's pagination), and let the daily report rank the rest. Alternatives: B — Measure and protect only, with no cuts; C — Also refactor the reviewer watchdogs, check-run polling, and orchestrator timeline re-reads now. Why: acts on verified §15 defects without speculative refactors (§8). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the budget logic live? — Picked: A — A standalone `scripts/gh_pat_budget.sh` that each job runs from its existing verified checkout (the gate via one sparse checkout). Alternatives: B — Functions in `scripts/gh_helpers.sh`, which the gate does not stage; C — A composite action referenced `@stable`, which would not resolve until the next release. Why: the only option that works in every instrumented job today. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] How are calls attributed to a job, when `remaining` is account-wide and about 40 projects run concurrently? — Picked: A — Log `remaining` and `used_in_job` as the issue asks, plus an exact `gh_calls` count from a counting `gh` shim. Alternatives: B — Only `remaining` and `used_in_job`; C — `GH_DEBUG=api` tracing. Why: without per-job counts, a "top consumers" report would mostly measure concurrency; C is noisy and risks leaking headers. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Which jobs are instrumented? — Picked: A — Review `gate` and `codex-agent`, clarify, the poller, the review sweep, the Claude PR catch-all, the intake, and the new re-queue job. Alternatives: B — Also the five rare review jobs; C — Only review and clarify. Why: covers the high-volume paths the issue names while keeping `review_autofix.yml` well under the §27 guard. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Where does the daily report run, and where is it published? — Picked: A — A new internal daily workflow `gh-pat-budget-report.yml` on `GITHUB_TOKEN`, publishing to the job summary, a JSON artifact, and `GH_PAT_BUDGET_REPORT` log lines. Alternatives: B — A third cron on `review_autofix_sweep.yml`, which would need its `sweep` job's `if` rewritten; C — The weekly `workflow-log-analysis.yml`, which only runs weekly; D — Also update a tracking-issue comment. Why: a daily cadence with no change to existing schedules, and no PAT use. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-30] How does a failed `/reclarify` routing retry later instead of being lost? — Picked: A — The failure step labels `ai:reclarify-requeue` and comments with `GITHUB_TOKEN`; the hourly `reclarify-requeue` job re-posts `/reclarify` with the PAT once `core.remaining` is at least 500, then removes the label; one automatic retry per human `/reclarify`. Alternatives: B — A marker comment only, with the sweep scanning comments (more calls); C — A budget pre-check that defers the run without routing. Why: durable, visible, bounded, and it spends PAT calls only once the budget has recovered. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-30] Which PRs does the conflict sweep still read over REST? — Picked: A — Only those whose listed `mergeStateStatus` is `DIRTY` or missing, never draft `claude/*` PRs. Alternatives: B — Also `UNKNOWN`; C — Replace the REST read entirely. Why: identical outcomes to the current code, which drops non-dirty and unknown PRs after the read, and the REST read still gives the fresh head SHA for update-branch. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-30] How is the noop sweep's per-PR comment scan cut? — Picked: A — One search-API prefilter per tick, with fail-open to the full scan and the kill switch `NOOP_SEARCH_PREFILTER_ENABLED`. Alternatives: B — Skip `claude/*` heads when Claude-fixer mode is on; C — Aliased GraphQL comment batches. Why: the largest saving with unchanged decisions for every candidate PR; search has its own limit. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-09-30] What is done about separate credentials? — Picked: A — Document a proposal in `agents.md` and the final report, with no code or secret changes. Alternatives: B — Add optional secret inputs now. Why: secrets are a §23.C operator decision, as the issue says. Applied in: no code change. Status: pending review
- AD-10 [plan, 2026-09-30] Which clarify failures are re-queued? — Picked: A — Only failures of a human `/reclarify` run before routing completed (`clarify_route` did not succeed, or the Claude handoff failed), never cancellations, and never a run already triggered by an automatic re-post. Alternatives: B — Every failed `/reclarify` run, including later LLM failures; C — Failures and cancellations. Why: protects exactly the routing path, avoids repeating LLM clarify runs, and respects the intentional cancel-in-progress. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- security_pass_skip.py: {"skip": false, "reason": "no skip label"} → Security pass: run.
- Stale Routine sweep at start: nothing to delete (kept 58, not ours 42).
