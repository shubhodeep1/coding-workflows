<!-- changelog: changed -->
- **When the release gate's implement phase stalls, it now logs which implement run was waiting and what the runs around it were doing.** The 30-minute inactivity limit and the `timeout` result are unchanged.

In release gate run 40876853022 the e2e-smoke-test step "Phase 3b: Wait for PR creation (implement phase)" failed with "Implement phase stalled — no activity for 30 minutes". The only implement run it tracked for the smoke issue stayed `pending` the whole time, while the issue already carried `ai:implementing`. The log could not show why. One possibility is that another implement run for the same issue was doing the work in the issue's concurrency group, which the default branch's newer tracker already follows and `stable` does not yet. The other is that GitHub never started the run. The cause is not verified and may be environmental.

On that stall exit only, the `wait-implement` step in `.github/workflows/test-and-mark-stable.yml` now prints `stall-diag` lines before it exits:

- the tracked run's id, status, conclusion, `created_at`, `run_started_at`, `updated_at` and attempt;
- each of that run's jobs, with its status, start time, runner and running step;
- other unfinished implement runs created in the phase's window, with whether their title matches the smoke issue;
- implement runs in progress anywhere in the test repository, which shows a run started before the window that holds the issue's concurrency group.

Each run list shows at most 20 runs and ends with a `truncated, <n> more` line when there are more. The window's runs come from one page of the newest 100 runs, and a `note:` line says so when that page is full.

A warning names the run when the issue carries `ai:implementing` but the tracked run is still `pending`, `queued`, `waiting` or `requested`.

What this means for operators: the next stall shows whether a different run was doing the work or nothing started at all. The job still fails the same way.

### For contributors

The four reads (`actions/runs/<id>`, its jobs, the window's runs and the repository's in-progress runs) run once, only on the stall exit, with a 30-second timeout each and no retries. A failed read prints `unavailable`, and the diagnostics run in a subshell after `status=timeout` is written, so they cannot change the step's result. Text from the API is printed as JSON strings on lines that start with `stall-diag`, so a run title cannot inject a workflow command. Tests: `tests/test_test_and_mark_stable_plan_polling_guard.py` runs the step against a fake `gh` (pending run with a concurrency holder, unreadable and malformed payloads, no stall diagnostics on the PR and `implement_failed` paths, hostile run titles, a fresh run status that differs from the cached one, truncated run lists, a full window page, and the step's `env:` mapping).
