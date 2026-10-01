# Implement-Plan Log — Check-run collector: never report empty or unparseable API output as `ready`

- Plan: docs/plans/issue-5880-check-run-collector-malformed-output-plan.md
- Source issue: shubhodeep1/coding-workflows#5880 (https://github.com/shubhodeep1/coding-workflows/issues/5880)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-5880-check-run-collector-malformed-output   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: project branch opened from main; phase 1 starting.

## Phases
1. [ ] Phase 1 — collector fix (`gh_retry_to_file`, malformed output never `ready`), tests, docs, changelog fragment

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] Where should the concatenated retry output be stopped? — Picked: A — in the collector, by switching its call to `gh_retry_to_file` (per-attempt truncation, existing helper and shim pattern). Alternatives: B — change `gh_retry` to buffer each attempt's stdout for every caller; C — both. Why: §5 smallest change that fixes this issue; B changes a shared helper used by every `gh_retry … | jq` pipe and belongs with #5873. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What status should empty, unparseable, or `check_runs`-less output produce? — Picked: A — re-poll inside the existing wait budget, then `collection_status: api_error`. Alternatives: B — `api_error` at once with no re-poll; C — a new `parse_error` status. Why: A can still recover a clean round from a one-off bad read at no extra budget, and `api_error` is already in the documented value set (§6) and handled as not ready by the gate and both prompts. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How strict is the payload check? — Picked: A — every page must be an object with a `check_runs` list, and there must be at least one page. Alternatives: B — accept the payload if any page has a `check_runs` list. Why: a partial payload could hide runs and read as a clean, complete snapshot; strict is the fail-closed choice (§1). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Which existing tests change? — Picked: A — update the static assertion that pins `gh_retry gh api --paginate --slurp`, and change the two module-level tests' fake `"[]"` payload to one valid empty page. Alternatives: B — keep `"[]"` valid as "no pages". Why: `gh api --slurp` always returns at least one page, so a zero-page payload is malformed output; those tests exercise the writer-error paths, not the payload shape. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (`/implement-issue-claude`, CLAUDE.md §28.A). Security pass: run (`security_pass_skip.py`: no skip label).
- Hypothesis from the issue reproduced on main 1084dfd with the collector test harness before any change: failed attempt + successful retry → `ready`, `total_check_runs: 0` for a 4-run head.
