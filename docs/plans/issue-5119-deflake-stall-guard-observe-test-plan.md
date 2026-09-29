# Deflake the stall-guard observe-only tests on loaded CI runners

Source issue: shubhodeep1/coding-workflows#5119 (https://github.com/shubhodeep1/coding-workflows/issues/5119)
Base branch: main
Security pass: run

## Summary

`tests/test_codex_stall_guard_scripts.py::test_stall_guard_caller_contracts_cover_observe_only_mode` races a 0.3 s window and fails intermittently in the required `CI / lint` job, which holds otherwise mergeable `claude/*` PRs for a human. This plan makes the observe-only tests wait for the guard's own verdict instead of sleeping a fixed time, so the stall observation no longer depends on runner load, and widens the one remaining sleep-based window.

## Context

- CI run 36529220329 on PR #4810 (head `df3b904`, which does not touch the test or the guard) failed at `tests/test_codex_stall_guard_scripts.py:203` with `AssertionError: ('review_rb_fix', 'CODEX_HEARTBEAT: phase=review_rb_fix elapsed_secs=2\n')`: a heartbeat but no `codex_stall_observed` line.
- `scripts/codex_stall_guard.sh` (lines 367–436) wakes at most once a second and emits `codex_stall_observed` only when a wake finds the child still alive and idle for at least `CODEX_STALL_TIMEOUT_SECONDS` (1 s in the test). The test child sleeps `time.sleep(1.3)` after its last output (test line 191), so the guard has a wake window of about 0.3 s.
- The idle clock starts when the guard **reads** the child's last output (`last_child_event_monotonic`, guard line 396), not when the child wrote it. A runner stall that delays that read shrinks the window by the same amount, so widening the fixed sleep only moves the threshold. Local simulation (4 CPU burners plus a helper that `SIGSTOP`s the guard for a fixed time at random moments, the child left running) on 2026-09-29:

| Child behaviour | Guard stalls | Result |
| --- | --- | --- |
| `time.sleep(1.3)` (today) | 0.7 s | 10 of 10 runs failed, same assertion as CI |
| `time.sleep(2.5)` (the issue's example) | 0.7 s | 10 of 10 passed |
| `time.sleep(2.5)` | 1.2 s | 2 of 8 runs failed |
| wait for the guard's `state=observed` status file | 1.2 s | see Tests (acceptance evidence) |

- Plain CPU load alone (8 burners on 4 CPUs, or 6 burners pinned to one CPU with the test) did not reproduce the failure in 20 runs: the kernel favours the sleeping guard, so the CI failure is a descheduled or steal-time stall, which the `SIGSTOP` helper models.
- The same file has two more tests with a sleep-versus-interval window: `test_codex_stall_guard_observe_only_records_event_idle_without_killing_child` (child `time.sleep(2.4)` against the 1 s stall timeout, failed 1 of 15 runs under 1.2 s stalls) and `test_codex_stall_guard_heartbeat_appends_budget_fields_when_run_budget_env_present` (child `time.sleep(2.4)` against the 1 s heartbeat interval). The two kill-mode tests use a child that sleeps 1000 s and are killed by the guard, so they have no such window.
- The file runs in `ci.yml` (line 601, `CI / lint`), `mark-stable.yml` (line 369), `test-and-mark-stable.yml` (line 4178), and `scripts/run_validation_repo_checks.sh` (line 83).

## Goals

- Every observe-only stall assertion holds regardless of how late the guard wakes, as long as the whole run stays inside the existing 20 s subprocess timeout.
- The acceptance run from the issue passes: 20 runs in a row of the changed tests under a parallel CPU burner, and additionally under simulated 1.2 s guard stalls (the setting where a 2.5 s sleep still fails).
- Every existing assertion is unchanged, and the observe-only contract is still checked for all 8 callers in `CALLER_CONTRACTS`.
- No retry, rerun, or skip is added. The suite does not get slower.

## Non-goals

- Any change to `scripts/codex_stall_guard.sh` or its callers. The guard behaves correctly; only the test timing is wrong.
- The kill-mode tests (no timing window).
- `docs/operations/master-session.md` (the master session's own tracking doc, which it updates after merge, AD-4).
- Other flaky tests. The issue asks for one issue per flake.

## Constraints

- §5: test-only change, limited to `tests/test_codex_stall_guard_scripts.py`.
- §6: no existing identifier is renamed or removed. The new module constants `OBSERVED_STATUS_WAIT_SECS` and `WAIT_FOR_OBSERVED_STATUS_SNIPPET` and the child-side names `guard_status_path` / `observed_deadline` do not collide with anything in the repo (checked with `grep`).
- §9: tabs for indentation (the file already uses tabs).
- §20: test-only changes take no `changelog.d/` fragment.
- §18: no new script. The stress helpers used for verification stay out of the repo.
- The issue's rule: no retry, rerun, or skip.

## Approach

The two observe-only tests stop guessing when the guard wakes. After its last output, the child polls the status file the guard writes (`--status-file`) every 50 ms until it contains the line `state=observed`, then prints its final line and exits. The guard emits `codex_stall_observed` on stderr before it writes that status file (`scripts/codex_stall_guard.sh` lines 426–429), so once the child sees the file, every asserted artifact is already written. The child still outlives the observation and exits 0, so the tests still prove that observe-only mode records the stall **without** killing the child (`codex_stall_killed` absent, return code 0, final stdout line present). The polling produces no output, so the child stays idle for the guard's purposes. The wait is bounded by `OBSERVED_STATUS_WAIT_SECS = 15`, under the 20 s subprocess timeout: a guard that never observes still fails the existing assertions quickly instead of raising `TimeoutExpired`. The status file path reaches the child as `argv[2]`, which `_run_guard_for_contract` now passes after the pid file. Kill-mode children ignore it.

The budget test asserts on heartbeat lines, which the guard writes only to its own stderr, so there is nothing for the child to wait on without changing the guard (§5). Its child sleep grows from 2.4 s to 3.5 s, which widens its heartbeat window from 1.4 s to 2.5 s for 1.1 s more runtime.

Alternatives considered (AD-1): the issue's example `time.sleep(1.3)` → `time.sleep(2.5)` still failed 2 of 8 runs under 1.2 s stalls and adds about 10 s per run; a larger fixed sleep (for example 4 s) only moves the threshold and adds about 22 s per run.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue is one test-timing fix in one file.

1. **Phase 1 — deterministic observe-only stall tests** — Files: `tests/test_codex_stall_guard_scripts.py`. Done: the file passes locally; 20 consecutive runs of the three changed tests pass under a parallel CPU burner and simulated 1.2 s guard stalls; `ruff` and `pyflakes` are clean on the file; every assertion line is unchanged in the diff. Rollback: revert the phase PR; the tests return to their fixed sleeps.

## Implementation Steps

1. `tests/test_codex_stall_guard_scripts.py` after `CALLER_CONTRACTS`: add `OBSERVED_STATUS_WAIT_SECS = 15` and `WAIT_FOR_OBSERVED_STATUS_SNIPPET` (the child-side bounded poll for `state=observed` in the file named by `argv[2]`, tolerant of a missing file), with a comment naming the read-time idle clock and #5119.
2. `_run_guard_for_contract`: pass `str(status_file)` after `str(child_pid_file)` in the child command.
3. `test_stall_guard_caller_contracts_cover_observe_only_mode`: replace `time.sleep(1.3)` with the snippet (child body becomes newline-separated statements; outputs unchanged).
4. `test_codex_stall_guard_observe_only_records_event_idle_without_killing_child`: replace `time.sleep(2.4)` with the snippet and pass `str(status_file)` as `argv[2]` (AD-2).
5. `test_codex_stall_guard_heartbeat_appends_budget_fields_when_run_budget_env_present`: `time.sleep(2.4)` → `time.sleep(3.5)` (AD-3).

## Files & Modules

- `tests/test_codex_stall_guard_scripts.py`

## Tests

- Unit: `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_codex_stall_guard_scripts.py` (the CI entry point) passes.
- Acceptance stress (local, not committed): 20 consecutive runs of the three changed tests with 4 `yes > /dev/null` burners and a helper that `SIGSTOP`s the guard for 1.2 s at random moments; and 20 consecutive runs with CPU burners only. Both are expected to pass 20 of 20. The pre-fix baseline under 0.7 s stalls fails 10 of 10 with the CI assertion, which shows the harness reproduces the flake.
- Lint: `ruff check` and `pyflakes` on the file.
- Diff check: no `assert` line changes.

## Risks & Mitigations

- A regression that stops the guard from writing `state=observed` makes each observe-only contract run take the 15 s deadline before failing — ACCEPTED: the failure is still reported by the unchanged assertions, well inside the 20 s timeout.
- The child now depends on the status-file format (`state=observed` line) — mitigated: the test already asserts `status["state"] == "observed"` from the same file, so a format change fails the test either way.
- The budget test keeps a fixed sleep — ACCEPTED: its window grows to 2.5 s, and making it deterministic would need a guard change (§5, AD-3).

## Rollout

Test-only. Lands on `main` through the project's final PR; the `CI / lint` job picks it up on the next run of every PR that merges `main`. No consumer impact: the tests live only in this repo.

## References

- #5119 (this issue), PR #4810 and CI run 36529220329 (where it surfaced), #4798
- `scripts/codex_stall_guard.sh`, `tests/test_codex_stall_guard_scripts.py`

## Auto-decisions

- AD-1 [plan, 2026-09-29] How should the observe-only contracts test stop racing the guard? — Picked: A — the child waits (bounded, 15 s) for the guard's `state=observed` status file, then exits. Alternatives: B — `time.sleep(1.3)` → `time.sleep(2.5)` as the issue's example; C — a larger fixed sleep such as 4 s. Why: the guard's idle clock starts at the read of the child's last output, so a stall delays both ends and any fixed sleep keeps a load threshold (B failed 2 of 8 runs under simulated 1.2 s stalls); A is deterministic, keeps every assertion, and shortens the suite. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Does `test_codex_stall_guard_observe_only_records_event_idle_without_killing_child` (child `time.sleep(2.4)` against a 1 s stall timeout) get the same fix? — Picked: A — yes, the same status-file wait. Alternatives: B — leave it. Why: it is the same sleep-versus-timeout pattern the issue asks to fix and failed 1 of 15 runs under simulated 1.2 s stalls. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is `test_codex_stall_guard_heartbeat_appends_budget_fields_when_run_budget_env_present` (child `time.sleep(2.4)` against the 1 s heartbeat interval) fixed? — Picked: A — widen the sleep to 3.5 s. Alternatives: B — leave it; C — make it deterministic by having the guard write heartbeats to a file. Why: heartbeats go only to the guard's stderr, so there is nothing for the child to wait on without a guard change (§5); 3.5 s widens the window from 1.4 s to 2.5 s for 1.1 s more runtime. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Update the #5119 rows in `docs/operations/master-session.md`? — Picked: A — no; the master session owns that doc and marks rows fixed after merge. Alternatives: B — mark the rows fixed in the phase PR. Why: B would claim a fix before it merges and conflict with the master's frequent edits (§5). Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` printed `{"skip": false, "label": null, "reason": "no skip label"}`, so `Security pass: run`.
- No `changelog.d/` fragment: test-only change (§20.A).
