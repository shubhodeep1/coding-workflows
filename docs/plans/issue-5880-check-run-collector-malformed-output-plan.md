# Check-run collector: never report empty or unparseable API output as `ready`

Source issue: shubhodeep1/coding-workflows#5880 (https://github.com/shubhodeep1/coding-workflows/issues/5880)
Base branch: main
Security pass: run

## Summary

`scripts/collect_pr_check_runs_context.py` turns empty or unparseable check-run API output into `collection_status: ready` with 0 runs, and a retried `gh api` call can produce exactly that output. On a Claude-fixer PR this turns a clean review round into a 0-finding hand-off. This plan stops a retried call from leaving the failed attempt's output in front of the retry's, and makes malformed output a non-ready status.

## Context

- Issue #5880: PR #5741 review round 2 (head `5ffbaff`, run 36802256154) had 6 clean reviewers but was handed off instead of auto-merged. The post-review snapshot read `ready` with a single-digit run count. The head had 4 runs, and the collector step took about 600 s, which matches the `gh_retry` rate-limit wait. The same symptom is in `docs/implement-plan/issue-4886-numbered-session-titles.md` (round 2, head 6d881cb, run 36527410627: `ready`, `total_check_runs: 0`, and a local re-run counted 3 runs).
- Mechanism, **reproduced locally** with the existing collector test harness (mock `gh`): `_run_check_runs_api` (`scripts/collect_pr_check_runs_context.py:127-147`) runs `gh_retry gh api --paginate --slurp …`. `gh_retry` (`scripts/gh_helpers.sh:439-494`) streams every attempt's stdout into the same pipe. `gh api` prints the error response body to stdout on a failed request, so a failed attempt followed by a successful retry gives `{"message":…}[{…}]`. `_parse_pages` (`:150-159`) returns `[]` for text that does not parse. The wait view is then empty, `in_flight == 0`, and the loop ends with `final_status = "ready"` (`:421-423`) and 0 runs. Empty stdout, non-JSON stdout, and pages with no `check_runs` list behave the same way (all reproduced: each reads `ready`, `total_check_runs: 0`).
- The gate in `scripts/review_autofix_step_claude_fixer_handoff.sh:140-153` already requires `collection_status: ready` **and** `total_check_runs ≥ 1`, so a 0-run snapshot fails closed. That is safe but not convergent: with no dedicated verdict bot (`CLAUDE_FIXER_VERDICT_BOT_LOGIN` empty), the 0-finding hand-off can only be merged by a human.
- `gh_retry_to_file` (`scripts/gh_helpers.sh:505-…`) truncates its output file on every attempt, so only the last attempt's output remains. `scripts/claude_issue_intake.sh:75` already uses it with a one-line fallback shim for environments without `gh_helpers.sh`.
- Other callers of the collector: `scripts/check_failure_triage.sh:260-271` (diagnosis context only, no gate) and the review workflow's `Collect PR check-run failures (CI/lint autofix context)` step (`.github/workflows/review_autofix.yml:3088`, reviewer / editor context, which already treats every non-`ready` status as "unknown").

## Goals

- A retried check-run API call never leaves a failed attempt's stdout in front of the retry's output. Regression test: a failed attempt that prints a body, followed by a successful retry with 4 runs, yields `collection_status: ready` and `total_check_runs: 4`.
- Empty stdout, stdout that is not one JSON document, a JSON value with no page objects, and any page without a `check_runs` list never yield `collection_status: ready`. They are re-polled inside the existing wait budget (`CHECK_RUNS_WAIT_TIMEOUT_SECS`), and when the budget is spent the snapshot is `collection_status: api_error`, which the gate and the reviewer / editor prompts already treat as not ready.
- Every malformed read emits one structured, searchable warning (`CHECK_RUNS_AUTOFIX_MALFORMED_OUTPUT head_sha=<sha> reason=<empty|unparseable|no_pages|no_check_runs> bytes=<n>`), per §8.
- Valid responses, including a real head with 0 check runs (`[{"total_count":0,"check_runs":[]}]`), behave exactly as before.

## Non-goals

- Changing `gh_retry` itself. Its stream-through stdout affects every `gh_retry … | jq` caller, which is a wider change (AD-1); it is recorded under Notes and belongs with #5873.
- The 600 s rate-limit wait (#5873) and the incomplete-checks hand-off (#4900).
- The Claude-fixer gate logic, the hand-off format, and the verdict-bot path.
- Any new `collection_status` value (AD-2).

## Constraints

- §5 minimal change set: one script, its tests, and the docs that describe the changed command and failure mode.
- §6 naming immutability: `collection_status` value set, `PR_CHECK_RUNS_CONTEXT` header layout, env vars (`CHECK_RUNS_*`, `PR_CHECK_RUNS_CONTEXT_FILE`), and existing function names stay unchanged. New identifiers (`_check_runs_payload_problem`, the shell variable `check_runs_out_file`, the log key `CHECK_RUNS_AUTOFIX_MALFORMED_OUTPUT`) were checked for clashes.
- §8: diagnostic logging with context keys for the new failure path.
- §9: tabs in Python; the embedded shell string keeps its existing style.
- §15: no new API call shape. A malformed read is re-polled on the existing poll / backoff schedule and inside the existing wait budget, the same bound that already applies while checks are in flight.
- §20: a `changelog.d/` fragment (`fixed`), because the snapshot's behaviour on malformed output changes.
- §27: no workflow file changes.

## Approach

1. In `_run_check_runs_api`, replace `gh_retry gh api …` with `gh_retry_to_file "${check_runs_out_file}" gh api …` into a `mktemp` file (removed by an `EXIT` trap), then `cat` the file. Keep the same command, pagination, and exit status. Add the same fallback shim `claude_issue_intake.sh` uses when `gh_helpers.sh` cannot be sourced.
2. Add `_check_runs_payload_problem(raw_text) -> str`, which returns `""` for a valid payload and otherwise one of `empty`, `unparseable`, `no_pages`, `no_check_runs`. A payload is valid when it parses as one JSON document, holds at least one page (a list of pages, or one page object), and every page is an object whose `check_runs` is a list. `_parse_pages` and `_extract_runs` keep their current behaviour.
3. In the poll loop, after a successful API call, check the payload first. On a problem, emit the warning; when the deadline has passed, set `final_status = "api_error"` and stop. Otherwise sleep `poll_interval` (capped by the remaining budget) and poll again, without touching the unchanged-snapshot counters.

Alternatives considered: fixing only the parse side (B in AD-1) leaves the real cause, so every rate-limited retry still loses its clean round; a new `parse_error` status (C in AD-2) would change the documented value set and both prompt scripts for no gain over `api_error`.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one.

1. **Phase 1 — collector fix, tests, docs.**
   - Files: `scripts/collect_pr_check_runs_context.py`, `tests/test_review_autofix_review_pipeline_contract.py`, `README.md`, `probably_unnecessary_but_read_if_stuck.md`, `changelog.d/5880-check-run-collector-malformed-output.md` [new].
   - Done when: the regression and malformed-output tests below pass and fail on the current code, the existing collector, Claude-fixer, and reasoning-schedule tests pass, and the docs name `gh_retry_to_file` and the malformed-output failure mode.
   - Rollback: revert the phase PR. The script returns to the old behaviour, and the gate keeps failing closed on a 0-run snapshot.

## Implementation Steps

Phase 1:
1. `scripts/collect_pr_check_runs_context.py:127-147`: change the embedded shell script to use `gh_retry_to_file` into a temp file, with the fallback shim and an `EXIT` trap.
2. `scripts/collect_pr_check_runs_context.py` (next to `_parse_pages`): add `_check_runs_payload_problem`.
3. `scripts/collect_pr_check_runs_context.py:404-442`: in the poll loop, check the payload after a successful call, warn, and either re-poll within the budget or end with `api_error`.
4. `tests/test_review_autofix_review_pipeline_contract.py`: add the tests below; update the static assertion that pins `gh_retry gh api --paginate --slurp` to the new call; change the two module-level tests whose fake API returns `"[]"` (zero pages, now malformed) to return one valid empty page.
5. `README.md:95` (`CHECK_RUNS_AUTOFIX_ENABLED` row) and `probably_unnecessary_but_read_if_stuck.md` (check-run context contract): name `gh_retry_to_file` and the malformed-output → re-poll → `api_error` behaviour.
6. `changelog.d/5880-check-run-collector-malformed-output.md`: the `fixed` fragment per §20.

## Files & Modules

- `scripts/collect_pr_check_runs_context.py`
- `tests/test_review_autofix_review_pipeline_contract.py`
- `README.md`
- `probably_unnecessary_but_read_if_stuck.md`
- `changelog.d/5880-check-run-collector-malformed-output.md` [new]

## Tests

Unit / harness tests (the existing `_run_collect_pr_check_runs_harness`, real `scripts/gh_helpers.sh`, mock `gh`):
- **Retry does not concatenate:** attempt 1 exits 1 with a JSON error body on stdout and a transient stderr; attempt 2 returns 4 completed runs (`GH_RETRY_MAX_ATTEMPTS=2`). Expect `ready`, `total_check_runs: 4`. Fails on the current code (`total_check_runs: 0`).
- **Malformed output is never ready:** empty stdout, non-JSON stdout, `[]`, `[{"message":"x"}]`, and a page whose `check_runs` is not a list, each with `CHECK_RUNS_WAIT_TIMEOUT_SECS=0`. Expect `collection_status: api_error`, the api_error notice, and the `CHECK_RUNS_AUTOFIX_MALFORMED_OUTPUT` warning with the right `reason`.
- **Malformed output is re-polled inside the budget:** a malformed read followed by a valid read with a short budget. Expect `ready` with the valid count and two API calls.
- **Valid empty head stays ready:** `[{"total_count":0,"check_runs":[]}]` still reads `ready`, `total_check_runs: 0` (the gate then fails closed as today).

Existing suites to run: `tests/test_review_autofix_review_pipeline_contract.py`, `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_review_autofix_reasoning_schedule.py`, `tests/test_workflow_gh_retry_fallback_contract.py`, plus the changelog fragment lint if the repo has one.

End to end: the next Claude-fixer round whose post-review collector call is retried reads the real run count and auto-merges a clean round; the runtime validation stage runs the suites above against the project branch.

## Risks & Mitigations

- A response shape `gh api --slurp` returns that the validator rejects would make every snapshot `api_error` and stop all Claude-fixer auto-merges. Mitigation: the validator accepts both a list of pages and a single page object, requires only the `check_runs` list the existing parser already reads, and the valid-empty-head test pins the zero-run shape.
- Re-polling a malformed read spends API calls. Mitigation: the existing wait budget and poll interval bound it (default 300 s / 20 s, at most about 15 calls), the same as waiting on in-flight checks.
- `gh_helpers.sh` missing in a consumer checkout. Mitigation: the fallback shim runs the command once into the file, the same behaviour as today's `gh_retry` shim.

## Rollout

No flag. The script ships through the existing support-script bootstrap (`REQUIRED_BOOTSTRAP_SCRIPTS` already lists it), so consumer repos get it on the next `@stable` sync. Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-10-01] Where should the concatenated retry output be stopped? — Picked: A — in the collector, by switching its call to `gh_retry_to_file` (per-attempt truncation, existing helper and shim pattern). Alternatives: B — change `gh_retry` to buffer each attempt's stdout for every caller; C — both. Why: §5 smallest change that fixes this issue; B changes a shared helper used by every `gh_retry … | jq` pipe and belongs with #5873. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What status should empty, unparseable, or `check_runs`-less output produce? — Picked: A — re-poll inside the existing wait budget, then `collection_status: api_error`. Alternatives: B — `api_error` at once with no re-poll; C — a new `parse_error` status. Why: A can still recover a clean round from a one-off bad read at no extra budget, and `api_error` is already in the documented value set (§6) and handled as not ready by the gate and both prompts. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How strict is the payload check? — Picked: A — every page must be an object with a `check_runs` list, and there must be at least one page. Alternatives: B — accept the payload if any page has a `check_runs` list. Why: a partial payload could hide runs and read as a clean, complete snapshot; strict is the fail-closed choice (§1). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Which existing tests change? — Picked: A — update the static assertion that pins `gh_retry gh api --paginate --slurp`, and change the two module-level tests' fake `"[]"` payload to one valid empty page. Alternatives: B — keep `"[]"` valid as "no pages". Why: `gh api --slurp` always returns at least one page, so a zero-page payload is malformed output; those tests exercise the writer-error paths, not the payload shape. Applied in: phase 1 PR. Status: pending review

## Notes

- `gh_retry`'s stream-through stdout can affect other `gh_retry gh api … | jq` callers in the same way (a failed attempt's error body reaches `jq` ahead of the retry's output). Out of scope here (AD-1); relevant to #5873.

## References

- Issue #5880; Refs #5873, #4900, #5582; PR #5741 (run 36802256154).
- `docs/implement-plan/issue-4886-numbered-session-titles.md` (the same symptom on run 36527410627).
