# Model-provider outages: wait instead of blocking PRs, then resume on recovery

Source issue: shubhodeep1/coding-workflows#5773 (https://github.com/shubhodeep1/coding-workflows/issues/5773)
Base branch: main
Security pass: run

## Summary

A model-provider outage (OpenRouter 402 credits, 401 key, 429 / 5xx with the whole reviewer panel down) is classified as `provider_unavailable` instead of a per-PR defect: it no longer counts toward the identical-failure cap, labels nothing `ai:review-blocked`, starts no Claude fixer, and files no heal issue. The first such failure opens one outage marker issue with one operator alert, and the review sweep probes the provider every 30 minutes and resumes the paused work on its own once it recovers.

## Context

On 2026-09-30 the OpenRouter account ran out of credits from about 17:13Z to 23:00Z. Every reviewer call failed with `AI_APICallError: Insufficient credits` / `HTTP Error 402: Payment Required` (run 36748847333, PR #5324). Today the failure path cannot tell that apart from a PR defect:

- In Claude-fixer mode the reviewer step fails and `"Assemble failure evidence"` (`.github/workflows/review_autofix.yml`) names it `workflow_failure` (the reviewer-evidence block of `"Post editor summary comment"` is skipped for `CLAUDE_FIXER_MODE`), so every PR got the same fingerprint `29efdb471a203aae…` and the cap (`count_identical_failures` in `scripts/workflow_failure_heal.py`) tripped on 35 heads.
- `"Mark linked issues review-blocked (workflow failure)"` labelled 13 linked issues and `fingerprint-cap-block` labelled 23 PRs.
- `.claude/scripts/check_in_status.py --hand-back` saw the failed `review / codex-agent` check as `ci-failed`, so the §26 checkers and the §26.H catch-all sweep (`scripts/claude_pr_sweep.py`) started fixers, which hit the hand-back cap and posted `hold` claims (PR #5324, #5178, #5215, …).
- The heal intake filed #5758 for the stable release run that failed in the same window.

Recovery needed a human for every step. The issue asks for classification, one alert, and automatic resume (operator Q18: A).

Related: #5660 (usage-limit resume sweep; its branch is not merged, so its pickup-side wake path is not on `main`), #4938 (environment blockers re-dispatch), #5758.

Binding rules: CLAUDE.md §6 (no renames; new identifiers unique), §15 (API budget), §18.A/B (no standalone manual script; wire into the existing sweep cron in the same PR), §18.F (registry entry), §19, §20 (changelog fragment), §23.C (a release re-run is a dispatch, so it is opt-in), §25/§26 (no PR watching; §26.H text updated), §27 (`review_autofix.yml` is 455,461 bytes; keep growth small), §28.C (`.claude/**` changes go twin-first).

## Goals

1. `scripts/workflow_failure_heal.py` detects provider outages in the failing stage's logs and names them `provider_unavailable` (the marker's `reason=`), with provider, HTTP status, kind, and key name.
2. `provider_unavailable` markers neither count toward `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL` nor toward the heal reporter's failure streak.
3. A `provider_unavailable` run applies no `ai:review-blocked` label, forces no orchestrator tick, sends no per-PR "PR autofix failed" alert, and posts a "paused" PR comment that says so.
4. `check_in_status.py` (twin) reports a not-done `provider-unavailable` state (`action: wait`) when every failed check on the head belongs to a run with a trusted `provider_unavailable` marker, in `--hand-back` and plain PR mode; no fixer starts and nothing counts toward `CLAUDE_FIX_HAND_BACK_CAP`.
5. The heal reporter reports a `provider_unavailable` failure at once, and the heal intake turns it into at most one open `ai:provider-outage` marker issue in coding-workflows plus one Telegram alert naming provider, status, and key, filing no heal issue. While the marker is open, failed release runs are recorded on it instead of being diagnosed.
6. The review sweep (`review_autofix_sweep.yml`, `*/30`) skips its review dispatches while the marker is open, apart from one 1-token probe call, and on a successful probe: re-dispatches review for every open PR whose last run failed `provider_unavailable` (this repo and registered consumers), removes only the outage-applied `ai:review-blocked` labels, releases only outage-caused holds, re-runs (opt-in) or reports the newest recorded release run, records what it handled, closes the marker, and sends one "recovered" alert.
7. The marker issue never enters clarify or the Claude issue route.
8. Tests cover the acceptance list, including an end-to-end replay of the 2026-09-30 comment and label fixtures of PR #5324, #5178 and #5215.

## Non-goals

- Fast-failing the summariser's 10 retries on a 402 inside a run (the run still ends in about 45 minutes; it no longer has side effects).
- Gating pull_request-triggered or orchestrator-dispatched review runs on the marker (the issue scopes the skip to the review sweep).
- Waking claude.ai sessions or posting `/reclarify` from Actions (AD-9).
- Cleaning up the 2026-09-30 incident's historic labels and holds (their markers say `workflow_failure`; humans already cleaned them).
- Rate-limit handling for the Claude account itself (#5660).

## Constraints

- §6: every new name was grepped and is unused: `provider_unavailable`, `ai:provider-outage`, `AUTOFIX_PROVIDER_UNAVAILABLE`, `AUTOFIX_PROVIDER_OUTAGE`, `PROVIDER_OUTAGE_PROBE_ENABLED`, `PROVIDER_OUTAGE_PROBE_MODEL`, `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED`, `scripts/provider_outage.py`, the markers `ai:provider-outage:v1`, `ai:provider-outage-release-run:v1`, `ai:provider-outage-resume:v1`. No existing identifier changes meaning; the `review-autofix-failure:v1` marker format is unchanged (only a new `reason=` value).
- §15: no per-iteration calls in hot loops. Steady state costs the sweep one REST read per tick (open marker list). Recovery is a one-off: one `gh api user`, one PR list per repo, one comment list per candidate PR updated since the outage opened, one events read per candidate label, one check-runs read per held PR, one dispatch per resumed PR. The intake adds one marker list (and at most one create + one re-list) per provider report.
- §18.A/B: no standalone script. `scripts/provider_outage.py` runs only from the heal intake script and a new job of the existing `review_autofix_sweep.yml` cron, in this PR.
- §18.F: registry entry for `scripts/provider_outage.py` and the probe job.
- §23.C: the release re-run is behind `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED` (default `false`); the review re-dispatches are the sweep's existing routine dispatch.
- §27: `review_autofix.yml` grows by about 3 KB, staying under 460,000 bytes.
- §28.C: `.claude/**` behaviour changes ship as `workflow-templates/.claude/**` twins with a hold claim and the twin-sync blocker.
- Secrets: the probe reads `OPENROUTER_API_KEY` from the sweep job env; alerts and comments name the key, never its value. Provider error lines quoted anywhere go through `redact_secrets`.

## Approach

Extend the two mechanisms that already see every review failure, instead of adding a new pipeline:

1. **Classify at the source.** `autofix-failure-fingerprint` gains `--provider-log-dir` / `--provider-log-file`. It scans the bounded tails of the reviewer, summariser and editor logs (`PREVIOUS_REVIEWS_DIR/*.log|*.err`, `RUNTIME_DIR/*.log|*stderr*.txt`) and the reviewer `status_*.txt` files. A detected outage overrides the reason with `provider_unavailable` and prints `provider_outage=provider=… status=… kind=… key=…`.
2. **Suppress in the failure path.** `"Assemble failure evidence"` exports `AUTOFIX_PROVIDER_UNAVAILABLE` / `AUTOFIX_PROVIDER_OUTAGE`; three step `if:`s gain a guard, and the failure comment takes a "paused" body. The counters skip `provider_unavailable` markers.
3. **One marker through the heal path.** The reporter reports `provider_unavailable` without waiting for a streak and puts the `provider_outage` line at the top of its evidence. The intake recognises it (or a provider signature in a release run's own logs, or any failed release run while a marker is open), calls `provider_outage.py record` and stops before the diagnosis model. `record` is race-safe: create, re-list, and the lowest-numbered open marker wins; a duplicate closes itself without an alert.
4. **Wait in the checkers.** `check_in_status.py` (twin) drops failed checks whose run id carries a trusted `provider_unavailable` marker; when nothing else failed it reports `provider-unavailable`, which routes to `wait`.
5. **Probe and resume in the sweep.** A new `provider-outage-probe` job runs first on the `*/30` tick and outputs `skip_review_dispatch`. `provider_outage.py tick` lists the open marker; with none it returns at once. Otherwise it probes with a 1-token completion and, on HTTP 200, runs the resume (Goal 6), writes the `ai:provider-outage-resume:v1` comment, closes the marker, and emits the recovered alert text. The `sweep` job `needs` the probe and fails open (`if: always()`).

Alternatives considered are recorded as auto-decisions below.

## Phases & Merge Strategy

This is a **single phase**. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase, and the parts are not independently useful (the probe has nothing to resume without the classification, and the classification without the probe leaves PRs waiting forever).

1. **Phase 1 — classify provider outages, wait, mark once, probe and resume.**
   - Scope: all of Implementation Steps 1–12.
   - Files: see Files & Modules.
   - Protected paths: `.claude/scripts/check_in_status.py`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/implement-plan-claude.md` (edited through their `workflow-templates/.claude/**` twins; all three have twins).
   - Done when: the new and updated tests pass, ruff is clean on the new and changed Python, `review_autofix.yml` stays under 480,000 bytes, and every Goal maps to a passing test or a wiring assertion.
   - Rollback: revert the PR. Every runtime piece also has a switch: `PROVIDER_OUTAGE_PROBE_ENABLED=false` returns the sweep to today's behaviour, and `WORKFLOW_HEAL_ENABLED=false` stops marker creation (it rides the heal path).

## Implementation Steps

1. `scripts/workflow_failure_heal.py`: add `PROVIDER_UNAVAILABLE_REASON`, the outage signature table, `detect_provider_outage(texts, zero_success)`, `read_provider_logs(dirs, files)` (bounded tails, status files, `Pass N complete: 0 reviewers successful`), the `provider-outage-detect` CLI, and `--provider-log-dir` / `--provider-log-file` on `autofix-failure-fingerprint` (override the reason, print `provider_outage=`). Skip `provider_unavailable` markers in `count_identical_failures` and `count_autofix_failure_streak`.
2. `.github/workflows/review_autofix.yml`:
   - `"Post editor summary comment"` (empty-noop marker call) and `"Assemble failure evidence"`: pass the provider log dirs. Assemble also exports `AUTOFIX_PROVIDER_UNAVAILABLE=true` and `AUTOFIX_PROVIDER_OUTAGE` when the reason is `provider_unavailable`.
   - `"Mark linked issues review-blocked (workflow failure)"`, `"Force orchestrate poll after workflow failure review-blocked label"`, `"Telegram failure"`: add `env.AUTOFIX_PROVIDER_UNAVAILABLE != 'true'`.
   - `"Post review-blocked comment on PR (workflow failure)"`: post the "paused: model provider unavailable" body when the flag is set.
3. `scripts/workflow_failure_heal_autofix_report.sh`: for `provider_unavailable`, skip the streak threshold and write the `provider_outage …` line second in the evidence.
4. `scripts/provider_outage.py` [new]: pure helpers (marker render/parse, release-run records, resume comment, PR/label/hold/release selection) plus thin `gh` / HTTP I/O. CLI `record`, `status`, `tick`. Docstring states the §15 budget and fail-open rules.
5. `scripts/workflow_failure_heal_intake.sh`: provider block. For an `autofix_failure` with reason `provider_unavailable` and a `provider_outage` evidence line, record and stop before log collection. For a `workflow_run`, detect on each raw job log before it is filtered, then record when a signature is found or a marker is already open. Send the open alert (`ERROR`) only when `record` reports `alert: true`. Log `skip reason=provider_unavailable`.
6. `.github/workflows/review_autofix_sweep.yml`: new `provider-outage-probe` job (`*/30` and dispatch; env `GH_PAT`, `OPENROUTER_API_KEY`, TG, `PROVIDER_OUTAGE_PROBE_ENABLED`, `PROVIDER_OUTAGE_PROBE_MODEL`, `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED`, `ALLOW_WORKFLOW_EDITS`) that runs `provider_outage.py tick`, sends the recovered alert (`WARNING`), and outputs `skip_review_dispatch`. `sweep` gains `needs: provider-outage-probe`, `if: always() && …`, and an early `AUTOFIX_SWEEP_SKIP_ALL reason=provider_outage` exit.
7. Label `ai:provider-outage`: `.github/ai/label_contract.v1.json` and `scripts/label_helpers.sh` (colour and description maps).
8. Exclusion: add `ai:provider-outage` to the issue-opened job `if:` of `.github/workflows/clarify.yml`, `.github/workflows/internal-clarify.yml`, `workflow-templates/ai-clarify.yml`, and to `CODEX_ONLY_LABELS` in `scripts/claude_issue_route.py`.
9. Twins (`workflow-templates/.claude/`): `scripts/check_in_status.py` (Goal 4, docstring, API budget), `commands/fix-claude-pr.md` and `commands/implement-plan-claude.md` (a review stopped for `provider_unavailable` is waited on, never asked about).
10. Docs: `CLAUDE.md` §26.H (the new not-done state), `agents.md` (review autofix section: reason, suppressions, marker, probe, vars, failure modes), `README.md` (Workflow Failure Heal: the provider outage path), `docs/scripts-pending-removal.md` entry, `changelog.d/5773-provider-outage-resume.md`.
11. Tests (see Tests) and their `ci.yml` registration.
12. Verify: run the new and touched suites, ruff, the workflow size guard, yamllint on the changed workflows.

## Files & Modules

- `scripts/workflow_failure_heal.py`
- `scripts/workflow_failure_heal_autofix_report.sh`
- `scripts/workflow_failure_heal_intake.sh`
- `scripts/provider_outage.py` [new]
- `scripts/label_helpers.sh`
- `scripts/claude_issue_route.py`
- `.github/workflows/review_autofix.yml`
- `.github/workflows/review_autofix_sweep.yml`
- `.github/workflows/clarify.yml`
- `.github/workflows/internal-clarify.yml`
- `.github/workflows/ci.yml`
- `.github/ai/label_contract.v1.json`
- `workflow-templates/ai-clarify.yml`
- `workflow-templates/.claude/scripts/check_in_status.py`
- `workflow-templates/.claude/commands/fix-claude-pr.md`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `CLAUDE.md` (also `workflow-templates/CLAUDE.md`, a symlink)
- `agents.md`, `README.md`
- `docs/scripts-pending-removal.md`
- `changelog.d/5773-provider-outage-resume.md` [new]
- `tests/test_provider_outage.py` [new], `tests/test_provider_outage_replay.py` [new], `tests/fixtures/provider_outage/` [new], `tests/test_check_in_status_provider_outage.py` [new], `tests/test_review_autofix_provider_outage_contract.py` [new], `tests/test_workflow_failure_heal.py`

## Tests

- Unit (`tests/test_workflow_failure_heal.py`, `tests/test_provider_outage.py`): each signature (402 text and JSON, 401, 429 / 5xx with and without the zero-success proof, a non-provider error); the reason override and `provider_outage=` output; a 402 marker does not raise `count_identical_failures` and does not end an earlier identical run; the streak skips outage comments; the reporter bypasses the streak for `provider_unavailable`; marker render/parse; `record` create / exists / race-duplicate (exactly one alert); `tick` with no marker, with the probe still failing, and with recovery; probe status mapping; resume selection includes only outage-caused PRs, labels (window and actor) and holds (window, failed checks, conflict); release re-run on and off; the resume comment makes a second tick a no-op.
- Twin (`tests/test_check_in_status_provider_outage.py`): `--hand-back` returns `provider-unavailable` / `wait` for an outage-only failed review check; a mixed failure still hands back the other check; an untrusted marker is ignored; plain PR mode is not `stuck`.
- Contract (`tests/test_review_autofix_provider_outage_contract.py`): the workflow guards and arguments, the comment branch, the sweep job wiring and fail-open `needs`, the intake block, the label in both label maps, the clarify `if:`s and `CODEX_ONLY_LABELS`, the workflow size guard.
- End-to-end replay (`tests/test_provider_outage_replay.py`): the 2026-09-30 comments and labels of PR #5324, #5178 and #5215 (trimmed fixtures) and a reviewer-log excerpt of run 36748847333, replayed through three failing runs each: no cap, no label, no hand-off, one alert; then a mocked recovery: three re-dispatches, the in-window label removed, the outage hold released, the marker closed, one recovered alert.

## Risks & Mitigations

- A real defect hidden behind an incidental 429 / 5xx → those kinds need proof that no reviewer succeeded (AD-2); 402 / 401 are account-wide.
- Probe model misconfigured (400 / 404) keeps the marker open → only HTTP 200 recovers; the tick logs `PROVIDER_OUTAGE_PROBE status=<n>` and the marker issue names `PROVIDER_OUTAGE_PROBE_MODEL`. ACCEPTED — an operator sees the open issue.
- Missing `OPENROUTER_API_KEY` in the sweep → probe logs `missing_key` and keeps the marker; the sweep keeps skipping. ACCEPTED — same visibility.
- Forged reporter dispatch opens a marker → the next tick's probe closes it (at most 30 minutes of skipped sweeps). ACCEPTED (AD-16).
- Twin-first: the live `.claude/scripts/check_in_status.py` (used by the §26 checkers and `claude_pr_sweep.py`) keeps today's behaviour until the twin sync; the template-parity tests stay red until then → hold claim and twin-sync blocker after the PR opens (§28.C interim rule).
- Consumer runs pinned to an older `@stable` keep today's behaviour until the next sync. ACCEPTED — normal propagation.
- The serialized heal intake can drop reports under load → resume scans PRs instead of relying on records (AD-6); the marker needs only one report.

## Rollout

Ships with the PR; consumers get the reusable-workflow and script changes on the next `@stable` sync (§14 registry: `.github/ai/consumer_repos.json`). The `.claude/` parts land with the twin sync. Switches: `PROVIDER_OUTAGE_PROBE_ENABLED` (default `true`), `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED` (default `false`), `PROVIDER_OUTAGE_PROBE_MODEL` (default `vars.XPOLL_SUMMARISER_MODEL` or `openai/gpt-6-luna`). Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-10-01] Where does the repo-wide outage marker live? — Picked: A — one `ai:provider-outage` issue in coding-workflows, opened by the heal intake from the reporter dispatch every repo's review failure already sends. Alternatives: B — a marker issue per repo, opened in the failure path; C — a git-ref lock. Why: one marker and one alert across all repos, it extends the workflow-heal path as the issue prefers (§18.A), and an issue is visible to the operator. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Which errors classify as `provider_unavailable`? — Picked: A — 402 / "Insufficient credits" and 401 on the provider anywhere in the failing stage's logs; 429 and 5xx only when no reviewer succeeded (no `status_*.txt` reads `success`, or a `0 reviewers successful` line). Alternatives: B — any provider error line; C — 402 only. Why: an incidental 429 next to a real defect must not bypass the cap and loop through probe and resume. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How do the counters treat outage markers? — Picked: A — skipped: neither counted nor ending a run of identical failures. Alternatives: B — they reset the count. Why: the issue says they do not count; B would also hide a real repeated failure around an outage. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] What does `check_in_status.py` report for an outage? — Picked: A — a not-done state `provider-unavailable` (`action: wait`) when every failed check on the head belongs to a run with a trusted `provider_unavailable` marker; other failed checks still hand back. Alternatives: B — any outage marker on the head suppresses every failed check. Why: precise; a real CI failure next to an outage still gets a fixer. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-01] How does the probe test the provider? — Picked: A — one 1-token chat completion to `PROVIDER_OUTAGE_PROBE_MODEL` (default `vars.XPOLL_SUMMARISER_MODEL` or `openai/gpt-6-luna`); only HTTP 200 counts as recovered. Alternatives: B — `GET /api/v1/key`; C — `GET /api/v1/credits`. Why: it exercises the exact failing path; key limits do not reflect account credits. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] How does the resume find the paused PRs? — Picked: A — scan open PRs updated since the outage opened in this repo and the registered consumers; resume those whose latest trusted failure marker on the current head is `provider_unavailable` with no later trusted hand-off or editor summary. Alternatives: B — per-PR records on the marker issue. Why: the heal intake is serialized and drops pending runs under load, so records would be lossy. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-10-01] Which `ai:review-blocked` labels does the resume remove? — Picked: A — on a resumed PR, or an issue a resumed PR references, when the label's latest `labeled` event is inside the outage window and was made by the workflow account. Alternatives: B — every `ai:review-blocked` label added during the window. Why: marker plus window, never a guess (B would remove labels real failures applied). Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-10-01] How is an outage-caused hold released? — Picked: A — when the latest trusted claim on a resumed PR's head is a hold posted inside the window, every failed check on the head is an outage run, and the PR is not conflicted, post a newer trusted claim `kind=review by=provider-outage-probe-<run id>`. Alternatives: B — delete the hold comment; C — leave holds for a human. Why: a newer claim is the documented way a hold lifts, and `review` claims do not count toward the hand-back cap. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-10-01] Does the probe wake Claude sessions or `/reclarify` issues? — Picked: A — no: sessions wait through their hourly checkers, which see the re-dispatched review; no session posts an outage question anymore, so there is nothing to `/reclarify`. Alternatives: B — `/reclarify` every `ai:claude-blocked` issue blocked during the window; C — port #5660's wake path. Why: Actions cannot reach claude.ai (§23.E, §24.G), #5660 is not merged, and B would guess. Applied in: no code change. Status: pending review
- AD-10 [plan, 2026-10-01] How are release runs handled? — Picked: A — the intake opens or extends the marker for a failed release run whose own job logs show a provider signature, and defers (records, no heal issue) any failed release run while a marker is open; on recovery the newest recorded run is re-run only when `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED=true` (default `false`), otherwise the recovered alert reports it. Alternatives: B — only runs whose own logs show a signature. Why: #5758's release run failed in wait steps whose own logs lack the 402 text, and the heal diagnosis model cannot run during an outage either. Applied in: phase 1 PR. Status: pending review
- AD-11 [plan, 2026-10-01] Are consumer repos covered? — Picked: A — yes: classification and suppression ride the reusable workflow, their reporters open the same marker, and the resume re-dispatches their `ai-review.yml` and cleans their labels and holds. Alternatives: B — this repo only. Why: the outage is account-wide and the registry already lists the repos. Applied in: phase 1 PR. Status: pending review
- AD-12 [plan, 2026-10-01] How is the marker issue kept out of the pipelines? — Picked: A — add `ai:provider-outage` to the clarify job `if:`s and to `CODEX_ONLY_LABELS`, the way `ai:security-audit` is excluded. Alternatives: B — a title prefix check. Why: same mechanism as the security tracker. Applied in: phase 1 PR. Status: pending review
- AD-13 [plan, 2026-10-01] Alert channel and levels? — Picked: A — Telegram `tg_send_msg`, `ERROR` when the marker opens and `WARNING` on recovery; the per-PR "PR autofix failed" alert is suppressed for outage runs. Alternatives: B — keep the per-PR alerts too. Why: the issue asks for one alert. Applied in: phase 1 PR. Status: pending review
- AD-14 [plan, 2026-10-01] Which switches? — Picked: A — `PROVIDER_OUTAGE_PROBE_ENABLED` (default `true`) for the probe and skip; marker creation rides `WORKFLOW_HEAL_ENABLED`; `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED` (default `false`). Alternatives: B — a separate switch for marker creation. Why: fewest new variables; §4 defaults given. Applied in: phase 1 PR. Status: pending review
- AD-15 [plan, 2026-10-01] How does this phase change `.claude/**`? — Picked: A — twin-first per Q40 (`check_in_status.py`, `fix-claude-pr.md`, `implement-plan-claude.md` twins), then a hold claim and the twin-sync blocker. Alternatives: B — edit `.claude/**` unattended; C — drop the `.claude/` part. Why: §28.C forbids B; the issue requires the check-in change. Applied in: phase 1 PR. Status: pending review
- AD-16 [plan, 2026-10-01] What does the intake trust for an autofix report? — Picked: A — `failure_reason=provider_unavailable` plus the reporter's `provider_outage` evidence line; the review job's own log is not readable while that job is still running. Alternatives: B — require a signature in the job log. Why: B never confirms (the job has not finished when the intake reads it); a forged report only pauses the sweep until the next probe. Applied in: phase 1 PR. Status: pending review

## References

- Issue #5773; related #5660, #4938, #5758.
- Run 36748847333 (PR #5324): `AI_APICallError: Insufficient credits`, `HTTP Error 402: Payment Required`.
- `scripts/workflow_failure_heal.py` (`count_identical_failures`, `autofix_failure_fingerprint`), `.claude/scripts/check_in_status.py` (`check_pr_hand_back`), `.github/workflows/review_autofix_sweep.yml`.
