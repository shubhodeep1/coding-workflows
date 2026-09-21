# Release-blocker detection feeding the workflow-failure-heal intake

## Summary

Turn the conditions that silently block a stable release or a main-to-stable promotion (persistent skips, poller promotion failures, staleness) into `ai:workflow-heal` fix issues through the existing intake from #4165, with a Telegram alert on every blocker, so a human never has to notice a quiet skip loop by reading Actions.

## Context

- PR #4134 added the daily promote cycle (`promote-main-to-stable.yml` `cycle` job, `scripts/promote_main_cycle.sh`, the poller's `comprehensive_*` callbacks in `scripts/orchestrate_poll_process.sh`) and the 6-hourly `auto-release-stable.yml` (`scripts/auto_release_stable.sh`).
- PR #4165 added the workflow-failure heal: `workflow-failure-heal-intake.yml` accepts `repository_dispatch` (`workflow-failure-heal`) payloads of schema `workflow_failure_heal.v1`, `workflow_run` completions of the five release workflows with conclusion `failure` / `timed_out`, and a manual `workflow_dispatch`; `scripts/workflow_failure_heal_intake.sh` validates, fingerprints, dedups (open issue with the same `fp` marker), applies lineage and budget caps, runs the diagnosis model, and opens the issue. `scripts/workflow_failure_heal.py` holds the payload schema (`SOURCE_KINDS = issue, pull_request, workflow_run`), the fingerprint (`workflow_name | failing_step | normalised signature`), and `budget_decision`.
- PR #4185 made the two dispatchers avoid each other's gate and treat a cancelled gate as retryable.
- Observed on 2026-09-20/21: after the first cycle cancelled the stable release gate, `auto_release_stable.sh` skipped for 24 hours with `AUTO_RELEASE_SKIPPED reason=last_gate_failed conclusion=cancelled` and nothing filed an issue, because a skip is a successful run. The same blind spot covers `gate_busy`, `smoke_gate_cancelled`, `cycle_in_flight` held by an untrusted marker, the poller's `COMPREHENSIVE_PROMOTION_FAILED` / `COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED … retries_exhausted` / `COMPREHENSIVE_MARKER_UNTRUSTED` outcomes (Telegram only), and plain staleness (stable ahead of the tag, main ahead of stable with no cycle).
- Constraints that bind the design: §6 (the `workflow_failure_heal.v1` field names, `SOURCE_KINDS` values, log prefixes and env vars are identifiers: add, never rename), §15 (every new `gh api` call justified; reuse the runs and compare responses the producers already fetch), §18.A/B (no new scripts or workflows: detection folds into the three producers on their existing schedules), §18.F (registry entries updated in the same PR), §20 (one changelog fragment per phase PR), §19 (issue bodies use `Refs`, never auto-close keywords).

Decisions taken in the clarification round (all recorded here so the orchestrator has no open choice): detection in the three producers (Q1 A); a new `source_kind` `release_blocker` on the existing dispatch (Q2 A); the conditions and thresholds in the table below (Q3 A); deferrals excluded (Q4 A); intake-side occurrence throttle, stateless reporters (Q5 A); target branch per blocker (Q6 A); three phases, reporters on by default (Q7 A); reporters dispatch with `GH_PAT` and never fail their tick (Q8 A); the intake owns the alerts at the levels below (Q9 A).

## Goals

- Every condition in the table below produces exactly one open `ai:workflow-heal` issue per fingerprint, with the evidence (SHAs, skip lines, run URLs, age) in the body, targeting `stable` for release-side blockers and `main` for cycle and poller blockers.
- Every blocker report results in a Telegram alert at WARNING or above: a new issue, a repeat past the 24h throttle, a lineage cap (CRITICAL), an exhausted budget (WARNING), and blockers the model classifies `consumer-config` or `transient` (WARNING instead of ERROR / DEBUG).
- Repeated reports of the same blocker cost no model call and at most one occurrence comment and one alert per fingerprint per 24 hours.
- A reporter never fails or delays its own tick because reporting failed; every skip is a stable log line.
- Every threshold is a defaulted repo var; the whole feature has a kill switch that leaves the producers' current behaviour intact.

## Non-goals

- Changing when a release or cycle is attempted (the guards from #4185 stay as they are).
- Fixing the E2E gate's own fragility (poller cancel cascade, job caps).
- Reporting `COMPREHENSIVE_PROMOTION_DEFERRED` (a designed outcome; stays a WARNING alert).
- Consumer repositories: the producers and the intake live only in coding-workflows, so nothing propagates through `.github/ai/consumer_repos.json` (§14 does not apply).
- Any change to the diagnosis prompt's classification tokens.

## Constraints

- §6: `SOURCE_KINDS` gains `release_blocker`; no existing kind, payload field, env var, log prefix, or CLI flag is renamed or removed. New env vars: `RELEASE_BLOCKER_HEAL_ENABLED` (default `true`), `RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS` (86400), `RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS` (21600), `RELEASE_BLOCKER_STABLE_STALE_SECS` (86400), `RELEASE_BLOCKER_CYCLE_STALE_SECS` (259200), `RELEASE_BLOCKER_CYCLE_SKIP_STREAK` (3), `RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS` (86400), `WORKFLOW_HEAL_OCCURRENCE_THROTTLE_SECS` (86400). New log prefixes: `RELEASE_BLOCKER_REPORTED`, `RELEASE_BLOCKER_REPORT_SKIPPED`. All names checked against the three scripts, the intake, and `agents.md` for collisions before use.
- §15: no per-item loops. The auto-release reporter reuses `runs_json`, `promote_runs_json`, `legacy_runs_json`, the tag lookups and the compare it already fetched; the cycle reporter reuses `self_runs_json`, the compare, the gate idle check and the in-flight issue list; the poller reporter reuses the state file and the metadata it already has. Each report is one `POST repos/<self>/dispatches`. The cycle's skip-streak check reads its own last N runs from the `self_runs_json` it already fetched (conclusions and `display_title` only; no log reads). The intake's throttle reads the timestamp from the existing occurrence-comment marker on the duplicate issue: one extra `GET issues/<n>/comments?per_page=100` on the duplicate path only.
- §18: no new workflow and no manual script. Detection runs inside `auto_release_stable.sh` (every 6h at :30), `promote_main_cycle.sh` (daily), and `orchestrate_poll_process.sh` (every poll tick, only at the moment an outcome is recorded).
- §19: issue bodies and alert texts use `Refs #N`.
- §20: one fragment per phase PR (`changelog.d/<pr>-release-blocker-heal-<phase>.md`).
- Security: the payload's evidence text is untrusted for the model (same `UNTRUSTED` framing the intake already uses for issue excerpts); the intake re-validates every field; reporters never include tokens; the `head_branch` / `target_branch` value is validated against `_BRANCH_RE` and existence.

## Approach

One additive payload kind, three stateless producers, one throttle in the consumer of the reports.

**Payload.** `source_kind: "release_blocker"` with these fields on top of the common ones: `workflow_name` (the producer workflow's display name, e.g. `Auto release stable`), `blocker_reason` (one of the reason tokens in the table), `blocker_evidence` (multi-line text: the stable log lines and SHAs, bounded to `ISSUE_EXCERPT_LIMIT`), `blocker_age_secs`, `head_branch` (the target branch: `stable` or `main`), `head_sha` (the blocked tip), `run_refs` (optional: the failed gate run for `last_gate_failed`, the stuck run for `release_in_flight`, the poller run otherwise), `reporter_run_url`. Validation: `blocker_reason` must match `^[a-z_:]{1,80}$` (the poller's `promotion_failed:<reason>` tokens carry a colon), `blocker_age_secs` a non-negative integer, `head_branch` required for this kind, `conclusion` not required.

**Fingerprint.** For `release_blocker` payloads with no failed jobs the intake computes `fingerprint(workflow_name, "blocker:" + blocker_reason, error_signature(blocker_evidence))`; SHAs and numbers are already normalised by `normalize_error_line`, so the same blocker on the same tip across ticks yields one fingerprint, and a new tip yields the same fingerprint too (the evidence names the reason, not the SHA, in its first lines). When `run_refs` carry a failed run (the `last_gate_failed` case), the existing failed-job path runs first and the fingerprint is the gate's own, so the report lands as an occurrence on the gate failure's heal issue instead of a second issue.

**Producers.** Each producer computes its conditions from data it already holds, builds the payload with `workflow_failure_heal.py build-blocker-payload`, and posts one `repository_dispatch`. Reporting is the last thing a tick does before its normal exit path and is wrapped so that any failure logs `RELEASE_BLOCKER_REPORT_SKIPPED reason=<dispatch_denied|payload_invalid|disabled>` and returns 0.

**Intake.** Accepts the new kind, derives the fingerprint as above, passes `blocker_evidence` to the model as untrusted context, sets the target branch from `head_branch`, throttles occurrence comments per fingerprint, and sends the alerts at the agreed levels. Everything else (dedup, lineage, budget, classification, routing, source checkout at `head_sha`) is unchanged.

| Producer | Condition | Reason token | Threshold (repo var, default) | Evidence | Target |
| --- | --- | --- | --- | --- | --- |
| `auto_release_stable.sh` | last gate on the stable tip concluded `failure` / `timed_out` / `startup_failure` and its `updated_at` is older than the threshold | `last_gate_failed` | `RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS` 86400 | tip, tag commit, gate run URL (as `run_refs`), conclusion, age | `stable` |
| `auto_release_stable.sh` | a gate / promote / mark-stable run has been queued or in progress longer than the threshold | `release_in_flight` | `RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS` 21600 | run URL, status, `run_started_at`, age | `stable` |
| `auto_release_stable.sh` | branch strictly ahead of the tag and the tag's release (`releases/latest` `published_at`, already fetched) older than the threshold with no gate run started on the tip since | `stable_release_stale` | `RELEASE_BLOCKER_STABLE_STALE_SECS` 86400 | tip, tag commit, last release tag and time, last skip reason | `stable` |
| `promote_main_cycle.sh` | this tick and the previous N-1 scheduled ticks all ended `gate_busy` or `smoke_gate_cancelled` (from `self_runs_json` conclusions plus the tick's own outcome) | `cycle_gate_unavailable` | `RELEASE_BLOCKER_CYCLE_SKIP_STREAK` 3 | the N run URLs and their skip reasons | `main` |
| `promote_main_cycle.sh` | `cycle_in_flight` where the open labelled tracking issue carries `COMPREHENSIVE_MARKER_UNTRUSTED` state (its latest state comment has `comprehensive_release_callback.untrusted_marker_alerted`) for longer than the threshold | `cycle_held_untrusted_marker` | `RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS` 86400 | tracking issue URL, alerted_at, age | `main` |
| `promote_main_cycle.sh` | main has code changes since the stable tag (the compare the tick already ran) and no scheduled cycle run with `outcome=dispatched` or a `PROMOTE_CYCLE_DISPATCHED` conclusion within the threshold (from `self_runs_json`: last run whose `conclusion == success` and whose tick dispatched, tracked by the new `PROMOTE_CYCLE_DISPATCHED` marker in `display_title` is not available, so the tick records its outcome in its own step summary and reads the previous ticks' outcomes from the runs' `name`-independent `conclusion` plus the cycle tracking-issue baseline markers already searched by `last_cycle_baseline_sha`) | `cycle_stale` | `RELEASE_BLOCKER_CYCLE_STALE_SECS` 259200 | main tip, tag commit, last baseline marker time, the tick's skip reason | `main` |
| poller | `COMPREHENSIVE_PROMOTION_FAILED` (any reason) | `promotion_failed:<reason>` | immediate | tracking issue URL, promote_sha, smoke_sha, reason, the release run URL when known | `main` |
| poller | `COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED … retries_exhausted=N` | `verification_not_dispatched` | immediate | tracking issue URL, dispatcher outcome | `main` |
| poller | `COMPREHENSIVE_MARKER_UNTRUSTED` (first alert only) | `marker_untrusted` | immediate | tracking issue URL, the untrusted comment's author | `main` |

For `cycle_stale` the source of truth is the marker search the tick already performs (`last_cycle_baseline_sha` reads the newest trusted `apply-analysis-cycle-baseline-sha` comment); the tick compares that comment's `created_at` (already in the comments payload it fetched) with now. No new data source is introduced.

**Alerts (intake, Q9 A).** `open` → WARNING "Release blocker filed: <reason> on <branch> (<issue url>)". `duplicate` past the throttle → WARNING "Release blocker still present after <age>: <reason> (<issue url>)"; within the throttle → log only. `escalate` → CRITICAL (unchanged). `budget_exhausted` → WARNING (unchanged). `consumer-config` / `transient` classification of a `release_blocker` payload → WARNING (was ERROR / DEBUG). Producer dispatch failure → the producer sends WARNING "Release blocker report could not be dispatched (<reason>)" through the helper it already sources (`tg_helpers.sh` in the two scripts, `tg_notify` in the poller). `tg_send_msg` no-ops when `TG_BOT_SECRET` or `TG_CHAT_ID` is unset, and neither `auto-release-stable.yml` nor the `cycle` job of `promote-main-to-stable.yml` binds them today, so phase 2 adds `TG_BOT_SECRET: ${{ secrets.TG_BOT_SECRET }}`, `TG_ADMIN_CHAT_ID: ${{ vars.TG_ADMIN_CHAT_ID }}` and `TG_CHAT_ID: ${{ vars.TG_ADMIN_CHAT_ID }}` to both jobs' `env:` (the same bindings `workflow-failure-heal-intake.yml` uses); the poller job already has them. The poller's existing CRITICAL alerts for the three outcomes stay.

Alternatives considered: a separate detector workflow (rejected, §18 and Q1); synthesising `workflow_run` payloads with a fake failure (rejected, it would lie to the dedup and the model); reporter-side dedup by searching heal issues (rejected, duplicates the intake's logic and costs a call per tick).

## Phases & Merge Strategy

The orchestrator ships each phase as its own PR straight to production. Each phase is independently mergeable, complete on its own, and production-safe.

1. **Intake accepts `release_blocker` reports, throttles occurrences, alerts.** Files: `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_intake.sh`, `.github/workflows/workflow-failure-heal-intake.yml` (new env row `WORKFLOW_HEAL_OCCURRENCE_THROTTLE_SECS`), `prompts/mode-workflow-failure-heal.txt` and `prompts/_templates/mode-workflow-failure-heal.txt` (a paragraph describing blocker reports as input, no new classification), `tests/test_workflow_failure_heal.py`, README, `agents.md`, changelog fragment. Done when a `release_blocker` payload filed through the manual `workflow_dispatch` input opens an issue targeting the payload's branch, a second identical payload within 24h adds no comment and no alert, and every existing intake test still passes. No producer exists yet, so merging this alone changes nothing at runtime. Rollback: revert the PR; no state is persisted beyond issue comments.
2. **Release-side and cycle-side reporters.** Files: `scripts/auto_release_stable.sh`, `scripts/promote_main_cycle.sh`, `.github/workflows/auto-release-stable.yml`, `.github/workflows/promote-main-to-stable.yml` (new defaulted env rows), `tests/test_auto_release_stable.py`, `tests/test_promote_main_cycle.py`, README, `agents.md`, `docs/scripts-pending-removal.md` (extend the two existing entries), changelog fragment. Done when each condition in the table produces one dispatch with the documented payload under the mocked `gh`, a denied dispatch logs `RELEASE_BLOCKER_REPORT_SKIPPED reason=dispatch_denied` and the tick still exits with its normal outcome, and `RELEASE_BLOCKER_HEAL_ENABLED=false` produces no dispatch. If this merges before phase 1, the intake answers each report with `skip reason=invalid_payload` and one Telegram WARNING per tick; that is the accepted cost (Q7 A) and stops as soon as phase 1 lands. Rollback: set `RELEASE_BLOCKER_HEAL_ENABLED=false` (instant, no deploy) or revert the PR.
3. **Poller reporters.** Files: `scripts/orchestrate_poll_process.sh`, `.github/workflows/orchestrate_poll.yml` (env rows), `tests/test_orchestrate_poll_promote_cycle.py`, `tests/test_orchestrate_poll_process.py` (harness: record `POST …/dispatches`), README 12f, `agents.md`, `docs/scripts-pending-removal.md`, changelog fragment. Done when each of the three outcomes dispatches exactly once per tracking issue (guarded by a `release_blocker_reported` key in `comprehensive_promotion` / `comprehensive_release_callback` state so a re-run tick does not re-report), and the existing promote-cycle harness tests still pass. Same out-of-order cost and rollback as phase 2.

No phase assumes another has merged; phases 2 and 3 degrade to a logged, alerted skip until phase 1 exists.

## Implementation Steps

Phase 1, intake:

1. `scripts/workflow_failure_heal.py`: add `"release_blocker"` to `SOURCE_KINDS`; add `BLOCKER_REASON_RE = ^[a-z_:]{1,80}$`, `BLOCKER_EVIDENCE_LIMIT = ISSUE_EXCERPT_LIMIT`; extend `validate_payload` so the new kind requires `workflow_name`, `blocker_reason`, `head_branch`, accepts optional `run_refs`, `head_sha`, `blocker_evidence`, `blocker_age_secs`, and does not require `conclusion`; carry the three new fields through the returned dict. Add `build_blocker_payload(*, repo, workflow_name, reason, evidence, age_secs, head_branch, head_sha, run_refs, reporter_run_url)` and a `build-blocker-payload` CLI subcommand with matching flags (this is what the producers call).
2. `scripts/workflow_failure_heal.py`: `compose_issue_title` returns `Release blocker: <reason> on <head_branch> (<workflow_name>)` for the new kind; `_context_lines` adds `Blocker reason`, `Blocked since` (age rendered as hours), `Blocked tip`, and an `Evidence (UNTRUSTED)` fenced block from `blocker_evidence`; `compose_issue_body`'s intro paragraph gets a third variant for blockers ("no run failed; the condition below has persisted…"). `compose-occurrence` output carries `<!-- workflow-failure-heal:occurrence_at=<iso> -->` in addition to the existing markers.
3. `scripts/workflow_failure_heal.py`: add `occurrence_throttled(comments, *, fp, now, throttle_secs)` returning true when the newest comment carrying `occurrence_at` for this `fp` is younger than the throttle, plus an `occurrence-throttle` subcommand reading a comments JSON file.
4. `scripts/workflow_failure_heal_intake.sh`: in the fingerprint block, when `SOURCE_KIND = release_blocker` and `LOG_FILES` is empty, write `blocker_evidence` to a file and compute `SIGNATURE` from it with `error-signature`, and set `FIRST_FAILING_STEP="blocker:${BLOCKER_REASON}"`; the `downstream_gate_failure` skip is unchanged (it keys on `workflow_run`).
5. `scripts/workflow_failure_heal_intake.sh`: in the `duplicate` branch, fetch the duplicate issue's comments (`GET repos/<repo>/issues/<n>/comments?per_page=100`, one call), run `occurrence-throttle`; when throttled log `duplicate_throttled existing_issue=… fp=… age=…` and exit 0 without commenting or alerting; otherwise post the occurrence and, for `release_blocker`, alert WARNING with the "still present after <age>" text instead of DEBUG.
6. `scripts/workflow_failure_heal_intake.sh`: target branch: for `release_blocker` use `HEAD_BRANCH` exactly as the `workflow_run` branch is used today (existence check and default fallback unchanged); `DIAG_REF` likewise; the diagnosis input's UNTRUSTED section prints `blocker_evidence` after the excerpts. Alerts: WARNING on `open` for the new kind; `consumer-config` and `transient` for the new kind alert WARNING.
7. `.github/workflows/workflow-failure-heal-intake.yml`: add `WORKFLOW_HEAL_OCCURRENCE_THROTTLE_SECS: ${{ vars.WORKFLOW_HEAL_OCCURRENCE_THROTTLE_SECS || '86400' }}`; the `workflow_dispatch` description mentions `release_blocker` payloads for manual testing.
8. Prompts: one paragraph in `prompts/_templates/mode-workflow-failure-heal.txt` (and the rendered `prompts/mode-workflow-failure-heal.txt`, regenerated with the existing `scripts/render_prompt.sh` flow) explaining that a `release_blocker` report has no failed job, that the evidence is a set of skip lines and SHAs, and that `transient` is the right answer only when the condition has already cleared.
9. Tests, docs, fragment: see Tests; README "Workflow Failure Heal" gets a "Release blockers" bullet, the env table gets the throttle row, `agents.md` gets the new marker; fragment `changelog.d/<pr>-release-blocker-heal-intake.md`.

Phase 2, release-side and cycle-side reporters:

10. `scripts/auto_release_stable.sh`: add config defaults `RELEASE_BLOCKER_HEAL_ENABLED`, `RELEASE_BLOCKER_GATE_FAILED_MIN_AGE_SECS`, `RELEASE_BLOCKER_IN_FLIGHT_MIN_AGE_SECS`, `RELEASE_BLOCKER_STABLE_STALE_SECS`, `RELEASE_BLOCKER_HEAL_PY` (default `scripts/workflow_failure_heal.py`); a `report_release_blocker <reason> <age> <evidence-file> [run-url]` function that builds the payload via `build-blocker-payload`, posts `POST repos/${GITHUB_REPOSITORY}/dispatches` with `event_type=workflow-failure-heal`, logs `RELEASE_BLOCKER_REPORTED reason=… age=… fp_hint=…` on success, `RELEASE_BLOCKER_REPORT_SKIPPED reason=disabled|dispatch_denied|payload_invalid` otherwise, alerts WARNING via `tg_send_msg` on `dispatch_denied`, and always returns 0.
11. `scripts/auto_release_stable.sh`: `skip_release` gains the hook: before exiting for `last_gate_failed`, compute the failed gate run's age from the `updated_at` already in `runs_json` and report when past the threshold with that run in `run_refs`; for `release_in_flight`, report when the oldest active run's `run_started_at` (already in the fetched runs) is older than the threshold; after the compare status `ahead` check and before dispatch, evaluate `stable_release_stale` from `releases?per_page=1` (already fetched for the tag deref path; if not, one call justified in a comment) and report, then continue with the normal dispatch. Reports never change the exit reason.
12. `scripts/promote_main_cycle.sh`: same config block plus `RELEASE_BLOCKER_CYCLE_SKIP_STREAK`, `RELEASE_BLOCKER_UNTRUSTED_HOLD_SECS`, `RELEASE_BLOCKER_CYCLE_STALE_SECS`; the same `report_release_blocker` function (duplicated deliberately: the two scripts share no library today, and §18 forbids a new standalone script; a shared `scripts/release_blocker_helpers.sh` sourced by both is acceptable if the orchestrator prefers it, as a non-executable helper). Hooks: in `skip_cycle`, for `gate_busy` and `smoke_gate_cancelled`, count the previous consecutive scheduled ticks in `self_runs_json` whose outcome was one of those two (the tick writes `outcome=<reason>` into its job summary and the run `name` is fixed, so the streak is read from the runs' `conclusion == success` plus the `PROMOTE_CYCLE_SKIPPED` line of each run's job summary via the existing `actions/runs/<id>/jobs` call only when the conclusion pattern matches; bounded to the streak size); report when the streak reaches the threshold. For `cycle_in_flight`, when the open labelled issue's newest state comment (already fetched by the in-flight guard's issue list plus one comments read that the `last_cycle_baseline_sha` path performs anyway) shows `untrusted_marker_alerted_at` older than the threshold, report. For `cycle_stale`: when the tick skips for any reason other than `disabled` / `insufficient_docs` and the newest trusted baseline marker comment is older than the threshold while main is `ahead` of the tag with code changes, report.
13. Workflows: add the new env rows (all `${{ vars.X || '<default>' }}`) to `auto-release-stable.yml` and the `cycle` job of `promote-main-to-stable.yml`, plus the Telegram bindings `TG_BOT_SECRET`, `TG_ADMIN_CHAT_ID` and `TG_CHAT_ID` (secret and repo var, same as the intake workflow) so the dispatch-failure WARNING can actually be sent; the untrusted-input contract test must keep passing (values bound via `env:`, never interpolated into `run:`).
14. Tests, docs, registry, fragment: see Tests; README env table rows and the Contributing bullets for both workflows; `agents.md` log prefixes `RELEASE_BLOCKER_REPORTED`, `RELEASE_BLOCKER_REPORT_SKIPPED`; `docs/scripts-pending-removal.md` entries for both scripts gain the reporter in their removal preflight ("no open `ai:workflow-heal` issue with `blocker_reason` from this script"); fragment `changelog.d/<pr>-release-blocker-heal-reporters.md`.

Phase 3, poller reporters:

15. `scripts/orchestrate_poll_process.sh`: config `RELEASE_BLOCKER_HEAL_ENABLED` (default `true`), `RELEASE_BLOCKER_HEAL_PY`; function `report_release_blocker_from_poller <reason> <evidence> [run-url]` mirroring step 10 but using `tg_notify` and `gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/dispatches"`; the payload's `head_branch` is `${DEFAULT_BRANCH_TRACKING:-main}`.
16. Hooks: in `comprehensive_promotion_hold_or_fail` (the failed branch), in the `COMPREHENSIVE_PROMOTION_FAILED` sites (`release_workflow_not_pinnable`, `dispatch_failed`, `release_failed`, `hold_timeout`), in the `retries_exhausted` branch of `handle_comprehensive_release_callback_if_needed`, and in the untrusted-marker branch (first alert only), call the reporter once and set `release_blocker_reported: true` under the state object each site already writes, so a later tick rebuilding state from the comment does not report again.
17. `.github/workflows/orchestrate_poll.yml`: env rows for the two vars.
18. Tests, docs, registry, fragment: see Tests; README 12f gains one sentence per reported outcome; `agents.md` prefixes if any new; registry entry for the poller cycle callbacks updated; fragment `changelog.d/<pr>-release-blocker-heal-poller.md`.

## Files & Modules

- `scripts/workflow_failure_heal.py` (edit): new kind, payload builder, title/body variants, occurrence throttle.
- `scripts/workflow_failure_heal_intake.sh` (edit): blocker fingerprint, throttle, target branch, alert levels.
- `.github/workflows/workflow-failure-heal-intake.yml` (edit): throttle env row.
- `prompts/_templates/mode-workflow-failure-heal.txt`, `prompts/mode-workflow-failure-heal.txt` (edit): blocker paragraph.
- `scripts/auto_release_stable.sh`, `.github/workflows/auto-release-stable.yml` (edit): reporter and env rows.
- `scripts/promote_main_cycle.sh`, `.github/workflows/promote-main-to-stable.yml` (edit): reporter and env rows.
- `scripts/orchestrate_poll_process.sh`, `.github/workflows/orchestrate_poll.yml` (edit): reporter and env rows.
- `tests/test_workflow_failure_heal.py`, `tests/test_auto_release_stable.py`, `tests/test_promote_main_cycle.py`, `tests/test_orchestrate_poll_promote_cycle.py`, `tests/test_orchestrate_poll_process.py` (edit).
- `README.md`, `agents.md`, `docs/scripts-pending-removal.md` (edit).
- `changelog.d/<pr>-release-blocker-heal-intake.md`, `changelog.d/<pr>-release-blocker-heal-reporters.md`, `changelog.d/<pr>-release-blocker-heal-poller.md` [new], one per phase PR.

## Data Model / Index Changes

None. No MongoDB collection is touched (§10 does not apply). Persistent state lives in GitHub issue comments (heal markers, the new `occurrence_at` marker) and in the poller's existing state comment (`release_blocker_reported`).

## Tests

Unit (`tests/test_workflow_failure_heal.py`):
- `validate_payload` accepts a minimal `release_blocker` payload, rejects one without `head_branch` or with a malformed `blocker_reason`, and drops `conclusion`.
- `build_blocker_payload` round-trips through `validate_payload`; evidence is bounded.
- fingerprint stability: two blocker payloads for different SHAs of the same reason share a fingerprint; different reasons differ.
- `occurrence_throttled` true within the window, false past it and when no marker exists.
- `compose_issue_body` / `compose_issue_title` for the new kind: target branch line, evidence block, no auto-close keywords.
- Intake driver (existing mocked-`gh` harness in the same file): a blocker payload opens an issue on `main`; a duplicate within 24h posts nothing and sends no alert; a duplicate past 24h posts one occurrence and alerts WARNING; `consumer-config` classification of a blocker alerts WARNING; a `last_gate_failed` blocker whose run ref has a failed job dedups onto the gate's issue.
- `test_intake_workflow_triggers_and_release_names` and `test_stable_log_prefixes_are_registered` extended for the new marker and prefixes.

Script tests (mocked `gh`, existing harnesses):
- `tests/test_auto_release_stable.py`: each of the three conditions dispatches once with the documented payload (assert on the recorded `dispatches` POST body); below threshold nothing dispatches; `RELEASE_BLOCKER_HEAL_ENABLED=false` nothing dispatches; a 403 on the dispatch logs `RELEASE_BLOCKER_REPORT_SKIPPED reason=dispatch_denied`, alerts, and the tick's own `AUTO_RELEASE_*` line and exit code are unchanged.
- `tests/test_promote_main_cycle.py`: skip streak of 3 reports, of 2 does not; untrusted hold past threshold reports; `cycle_stale` reports only when main is ahead with code changes and the newest baseline marker is older than the threshold; dispatch failure never changes the tick outcome.
- `tests/test_orchestrate_poll_promote_cycle.py` (harness records `POST …/dispatches` under `mock_store_extra`): each of the three outcomes dispatches exactly once, a second tick with the persisted state does not; `RELEASE_BLOCKER_HEAL_ENABLED=false` dispatches nothing; the existing CRITICAL alerts still fire.

Contract suites already in CI that must stay green: untrusted-input contract (new env rows bound via `env:`), gh_retry fallback contract, checkout ref audit, changelog fragment contract, inventory parity, `make generate-check`, `ruff`, `yamllint`, `actionlint`, `bash -n`.

End-to-end (manual, after phase 1 merges, no code): dispatch `workflow-failure-heal-intake.yml` by hand with a `release_blocker` payload for a throwaway reason and confirm the issue, the alert, and the throttle on a second dispatch; close the issue afterwards.

## Risks & Mitigations

- Reports arriving before phase 1 merges produce `invalid_payload` skips and one WARNING per tick. ACCEPTED — Q7 A; bounded to 4 alerts a day for auto-release and 1 for the cycle, and self-clearing.
- The heal budget (`WORKFLOW_HEAL_MAX_OPEN_ISSUES` 10) could be consumed by blockers and starve consumer reports. Mitigation: blockers share the budget by design (they are the more urgent class), and the WARNING on `budget_exhausted` names the reason; the operator can raise the cap.
- A blocker that the model classifies `transient` files nothing. Mitigation: it alerts WARNING (Q9 A) and, being stateless, is re-reported on the next tick past the throttle until it clears.
- `cycle_gate_unavailable` streak detection depends on reading the previous ticks' skip reason. Mitigation: read from the runs the tick already lists and at most `RELEASE_BLOCKER_CYCLE_SKIP_STREAK` job-summary fetches, only when the conclusion pattern already matches; fail open (no report) on any read error and log `RELEASE_BLOCKER_REPORT_SKIPPED reason=guard_unavailable`.
- Fingerprint drift: if evidence lines contain wording that changes between ticks the dedup would open a second issue. Mitigation: the first evidence line is the fixed `reason=` token line; tests assert stability across SHAs, ages and run IDs.
- `GH_PAT` missing in a producer job makes every dispatch fail. Mitigation: `dispatch_denied` is logged and alerted once per tick; the tick's own work is unaffected.
- `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID` missing in a producer job silences its dispatch-failure alert. Mitigation: phase 2 binds them (step 13) and its tests assert the producer's `tg_send_msg` call is made; the intake-side alerts do not depend on the producer bindings.
- Phase 1 must land the intake's job-log fix from PR #4194 (`gh api --allow-escape-sequences` on the logs call) or inherit it: without it `run_refs` evidence is empty and the `last_gate_failed` dedup onto the gate's issue cannot work. ACCEPTED — pending #4194 merging first; phase 1 re-checks this before relying on it.
- The exact age of the stable release comes from `releases?per_page=1`, which reflects the newest release, not necessarily the tag commit's release. ACCEPTED — pending observation of the first week of reports; the evidence carries both the tag commit and the release tag so a mismatch is visible in the issue.

## Rollout

- Kill switches: `RELEASE_BLOCKER_HEAL_ENABLED=false` stops every producer without a deploy; `WORKFLOW_HEAL_ENABLED=false` (existing) stops the intake.
- No migration, no dark launch: each phase is on by default at merge and observable through the stable log prefixes and the alerts.
- Order-independent: any phase can merge first (see Phases).
- Rollback per phase: revert the phase PR or flip its kill switch; issue comments and markers already written stay harmless.
- No consumer propagation (§14): every touched file lives only in coding-workflows and is not in `workflow-templates/` or `.claude/`.

## References

- PR #4134 (daily promote cycle), PR #4165 (workflow failure heal), PR #4185 (dispatcher guards, cancelled gates retry), PR #4171 and PR #4168 (E2E job cap).
- Runs that motivated this: `test-and-mark-stable` 35478030497 (release cancelled by the cycle gate), `promote-main-to-stable` 35479329523, `auto-release-stable` 35493297226 / 35509582202 / 35527851669 / 35546750782 (24 hours of `last_gate_failed` skips).
- README "Workflow Failure Heal" and 12f; `docs/scripts-pending-removal.md` entries for the heal, the cycle and auto-release.
