# Automatically re-run a PR's cancelled CI once per head from the review sweep

Source issue: shubhodeep1/coding-workflows#4713 (https://github.com/shubhodeep1/coding-workflows/issues/4713)
Base branch: main
Security pass: run

## Summary

Extend the 30-minute `sweep` job of `.github/workflows/review_autofix_sweep.yml` so that, for every open non-draft PR, a `CI` run that ended `cancelled` or `startup_failure` on the PR's current head is re-run once (failed jobs only), capped by `run_attempt`, with a kill switch. Nothing acts on a cancelled `CI` run today, so a PR sits "unstable" until a human steps in (#4633, 2026-09-28).

## Context

- `.claude/scripts/check_in_status.py:91` (`FAILED_CHECK_CONCLUSIONS`) counts `failure`, `timed_out`, `action_required`, `startup_failure` only. A `cancelled` `CI` run is invisible to the §26 checker, the Claude fixer and the §26.H catch-all sweep, and a clean Claude-fixer review never sees a ready check snapshot.
- `.github/workflows/ci.yml` (workflow name `CI`) has one job, `lint`, with `timeout-minutes: 45`. `main`'s runs 36367681221 and 36368393442 were cancelled at that limit and stalled #4633. PR #4706 raises the limit and #4707 splits the job, but a lost runner or a manual cancel can still cancel a run.
- The `sweep` job already lists every open PR once per tick (`gh api --paginate repos/<repo>/pulls`), filters to non-draft, and has `actions: write` (workflow-level `permissions`).
- `review_autofix_sweep.yml` is an internal workflow: it is not shipped through `workflow-templates/`, and it works on `github.repository` only.

## Goals

- For each open, non-draft, same-repository PR, take the newest `ci.yml` run whose `head_sha` is the PR's current head. When it concluded `cancelled` or `startup_failure`, has `run_attempt == 1`, and no `ci.yml` run for that head is queued or in progress, call `POST /repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs` once.
- Never re-run `failure` (or any other conclusion).
- A run for a superseded head is never matched, because only the PR's current head SHA is looked up.
- At most one automatic re-run per head: attempt 2 or later is always skipped.
- §15: one `ci.yml` runs listing per tick, matched locally against the head SHAs the sweep already fetched; no per-PR read. Call count documented in the helper's docstring.
- One log line per decision: `CI_CANCELLED_RERUN pr=<n> head=<sha12> run=<id|none> action=<rerun|skip> reason=<reason>`, plus a `CI_CANCELLED_RERUN_END` summary. Both prefixes listed in `agents.md` "Stable log prefixes (contractual)".
- Kill switch repo variable `CI_CANCELLED_AUTO_RERUN_ENABLED`, default `true` (§4), documented in the README "Required Variables" table.
- Tests for every rule; a `changelog.d/` fragment.

## Non-goals

- Re-running real `failure` conclusions (they stay with the Claude fixer as `ci-failed`).
- Speeding up `CI` (#4707) or changing its timeout (#4706).
- Changing `check_in_status.py`'s failed-check set.
- Consumer repos (see AD-7).

## Constraints

- §1: a re-run is made only for same-repository heads (AD-4).
- §4 / §6: new repo variable with a default; every existing identifier, step `name:`, log prefix and env var of the sweep stays unchanged. The existing enumerate step only gains a `head_sha` field in its local projection and writes that projection to a runner-temp file.
- §9: Python with tabs; YAML 2-space.
- §15: one listing call; POSTs only for the runs actually re-run.
- §18.A/B: no manual script. The helper is a step body run by the existing scheduled job (cron `*/30 * * * *`, `workflow_dispatch`); it is not a single-use or long-running script, so no `docs/scripts-pending-removal.md` entry.
- §20: changelog fragment. §27: `review_autofix_sweep.yml` is about 19 KB; the logic lives in `scripts/`, so the file stays far below 480,000 bytes.

## Approach

1. The `Enumerate open PRs and dispatch internal-review.yml` step adds `head_sha: .head.sha` to its jq projection and writes the snapshot to `${RUNNER_TEMP}/review-autofix-sweep-prs.json` right after computing it (before the zero-candidate exit). A failed write is a `::warning::`, never a step failure.
2. Two new steps follow in the `sweep` job, both `if: ${{ !cancelled() }}` so a failed dispatch loop does not stop them:
   - `actions/checkout@v5` (depth 1, `persist-credentials: false`, sparse `scripts`), placed after the enumerate step so a checkout failure can never block review dispatches;
   - `Re-run cancelled CI once per PR head`, which runs `scripts/ci_cancelled_rerun.py` with `GH_TOKEN` (`secrets.GH_PAT || secrets.GITHUB_TOKEN`, as the enumerate step), `REPOSITORY`, `HEAD_REF_FILTER`, `DRY_RUN`, and `CI_CANCELLED_AUTO_RERUN_ENABLED: ${{ vars.CI_CANCELLED_AUTO_RERUN_ENABLED || 'true' }}`.
3. `scripts/ci_cancelled_rerun.py`:
   - switch off (anything other than `1`/`true`/`yes`/`on`, case-insensitive) → one `action=skip reason=disabled` line, no API call, exit 0;
   - missing or unreadable snapshot → `reason=no_pr_snapshot`, exit 0;
   - local per-PR filters, each logged: `head_ref_filter`, `skip_ai_marker`, `fork_head`, `no_head_sha`;
   - when at least one PR passes them, one `GET repos/<repo>/actions/workflows/ci.yml/runs?per_page=100` (unfiltered, so it holds completed and active runs; AD-2). A failed listing is one `::warning::` and exit 0;
   - per PR: runs whose `head_sha` equals the head; none → `no_ci_run`; any not `completed` → `active_run`; newest (by `created_at`, then `id`) conclusion not `cancelled`/`startup_failure` → `conclusion_<c>`; `run_attempt != 1` → `already_rerun`; dry run → `dry_run`; else POST `rerun-failed-jobs` → `action=rerun reason=<conclusion>`. For `startup_failure` only, a refused `rerun-failed-jobs` falls back once to `POST …/rerun` (AD-6). A failed POST → `::warning::` with `reason=rerun_failed`, counted as an error;
   - always exits 0 (fail open), except on argument errors.

Alternatives: the hourly `claude-pr-catch-all` job (AD-1), inline bash (AD-3), and the literal `status=completed` listing plus active-status listings (AD-2) were considered and rejected.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one.

1. **Phase 1 — cancelled-CI re-run in the review sweep.**
   - Files: `scripts/ci_cancelled_rerun.py` [new], `.github/workflows/review_autofix_sweep.yml`, `tests/test_ci_cancelled_rerun.py` [new], `.github/workflows/ci.yml` (one test step), `README.md`, `agents.md`, `changelog.d/4713-rerun-cancelled-ci.md` [new].
   - Done when: the new tests and the existing sweep contract tests (`tests/test_review_autofix_sweep_zero_candidate_fast_exit.py`, `tests/test_review_autofix_sweep_stale_queued.py`, `tests/test_claude_pr_sweep.py`, `tests/test_workflow_file_size_limit.py`) pass; `actionlint`/YAML parse of the edited workflows succeeds; README, agents.md and the changelog fragment are in the PR.
   - Rollback: revert the PR, or set `CI_CANCELLED_AUTO_RERUN_ENABLED=false` for an immediate stop without a deploy.

## Implementation Steps

1. Add `scripts/ci_cancelled_rerun.py` per Approach 3, with a §15 batching docstring (input shape, output, call count, fail-open behaviour).
2. Edit `.github/workflows/review_autofix_sweep.yml`: projection + snapshot write in the enumerate step; add the checkout and re-run steps; extend the header comment with a short note.
3. Add `tests/test_ci_cancelled_rerun.py` and a `ci.yml` step running it.
4. README: append a `CI_CANCELLED_AUTO_RERUN_ENABLED` row to the bottom of the "Required Variables" table (its anchor asks for bottom appends).
5. agents.md: add `CI_CANCELLED_RERUN` and `CI_CANCELLED_RERUN_END` to "Stable log prefixes (contractual)", and one bullet next to the existing sweep bullet describing the rule set.
6. `changelog.d/4713-rerun-cancelled-ci.md` (`<!-- changelog: added -->`).

## Tests

Unit (`tests/test_ci_cancelled_rerun.py`, pytest, gh calls stubbed):
- `cancelled` attempt 1 → re-run; attempt 2 → skip; `failure` → skip; `startup_failure` → re-run, including the fallback to `rerun` when `rerun-failed-jobs` is refused;
- superseded head (cancelled run on an older SHA, none on the current head) → skip; active run on the head → skip; newest run wins over an older cancelled one;
- switch off → skip with zero API calls; dry run, `head_ref_filter`, `[skip ai]`, fork head → skip with no POST;
- listing failure and POST failure fail open (exit 0, warning);
- missing snapshot → skip; no eligible PR → no listing call;
- wiring: the `sweep` job carries the re-run step with the kill-switch default `'true'`, `!cancelled()`, and a checkout placed after the enumerate step; the enumerate step writes the snapshot with `head_sha` before its zero-candidate exit; `ci.yml` runs the new test file.

Existing: the zero-candidate and stale-queued sweep contract tests, `test_claude_pr_sweep.py`, and the workflow size test must still pass.

## Risks & Mitigations

- `rerun-failed-jobs` refused for a run with no jobs (`startup_failure`) → one fallback to the full `rerun` (AD-6); both count toward the same `run_attempt` cap.
- The newest 100 `ci.yml` runs may not include an old head's run → `reason=no_ci_run`, no action. ACCEPTED — heads that old are rare, and the cancelled run is then far past the point where a re-run helps.
- A deliberately cancelled run is re-run once → capped at one by `run_attempt`; the kill switch stops it repo-wide.
- Checkout step failure → placed after the dispatch loop, so dispatches are unaffected.

## Rollout

Ships on merge to `main`; the next 30-minute tick uses it. Kill switch `CI_CANCELLED_AUTO_RERUN_ENABLED=false`. No consumer propagation (AD-7).

## Auto-decisions

- AD-1 [planning, 2026-09-28] Which sweep job hosts the re-run? — Picked: A — the 30-minute `sweep` job. Alternatives: B — the hourly `claude-pr-catch-all` job. Why: it already lists every open non-draft PR (the issue asks for all PRs, not only `claude/*`), has `actions: write`, and re-runs within 30 minutes; the catch-all sees only `claude/*` PRs and its job permissions lack `actions: write`. Applied in: phase 1. Status: pending review
- AD-2 [planning, 2026-09-28] Shape of the runs read? — Picked: A — one unfiltered `ci.yml/runs?per_page=100` listing. Alternatives: B — `status=completed` listing plus three active-status listings (4 calls); C — `status=completed` only, without the active-run rule. Why: one call answers both the newest-run and no-active-run rules, and it sees a run whose re-run is already queued, which a completed-only listing drops. Applied in: phase 1. Status: pending review
- AD-3 [planning, 2026-09-28] How does the re-run logic get the PR heads? — Picked: A — the enumerate step writes its PR snapshot (plus `head_sha`) to runner temp and a new step runs `scripts/ci_cancelled_rerun.py` over it. Alternatives: B — inline bash in the enumerate step; C — the script lists PRs again. Why: no second PR listing (§15), and a Python helper is unit-testable and keeps the workflow small (§27). Applied in: phase 1. Status: pending review
- AD-4 [planning, 2026-09-28] Fork PRs? — Picked: A — same-repository heads only (`reason=fork_head`). Alternatives: B — every PR. Why: §1, the automation never re-executes fork code on its own authority; fork PRs are rare here. Applied in: phase 1. Status: pending review
- AD-5 [planning, 2026-09-28] Honour the `[skip ai]` marker? — Picked: A — yes, skip with `reason=skip_ai_marker`. Alternatives: B — ignore it for CI re-runs. Why: it is the sweep's documented only opt-out, so the author keeps control. Applied in: phase 1. Status: pending review
- AD-6 [planning, 2026-09-28] `startup_failure` runs that `rerun-failed-jobs` refuses? — Picked: A — fall back once to `POST …/rerun`. Alternatives: B — log and skip. Why: such runs often have no jobs, so a full re-run loses nothing, and it is still one re-run per head (attempt becomes 2). Applied in: phase 1. Status: pending review
- AD-7 [planning, 2026-09-28] Consumer repos? — Picked: A — this repo only. Alternatives: B — extend to registered consumers. Why: `review_autofix_sweep.yml` is internal (not in `workflow-templates/`), and consumer CI workflow names are not uniform or known, which is the issue's condition for limiting it. Applied in: phase 1. Status: pending review
- AD-8 [planning, 2026-09-28] Manual-dispatch inputs `dry_run` and `head_ref_filter`? — Picked: A — honour both, as the dispatch loop does. Alternatives: B — ignore them. Why: an operator narrowing or dry-running the sweep expects no re-runs outside that scope. Applied in: phase 1. Status: pending review
- AD-9 [planning, 2026-09-28] Kill-switch parsing? — Picked: A — `1`/`true`/`yes`/`on` (case-insensitive) enable it, anything else disables it; the workflow defaults an unset variable to `true`. Alternatives: B — only an exact `false` disables it. Why: matches the `ALLOW_WORKFLOW_EDITS` normalisation in the same job, and an unrecognised value fails toward not re-running. Applied in: phase 1. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` printed `{"skip": false, "reason": "no skip label"}`.

## References

- Issue #4713; incident PR #4633; `main` CI runs 36367681221, 36368393442; PR #4706; issue #4707.
- GitHub REST: `GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs`, `POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs`, `POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun`.
