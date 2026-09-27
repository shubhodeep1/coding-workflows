# Implement-Plan Log — Verify automation provenance before an issue-mode project skips its security pass

- Plan: docs/plans/issue-4623-verify-security-pass-skip-plan.md
- Source issue: shubhodeep1/coding-workflows#4623 (https://github.com/shubhodeep1/coding-workflows/issues/4623)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4623-verify-security-pass-skip   Final PR: #4635 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4645
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01FJKco7zdoJ1ez3JY6t8ui9   safety net and hand-back re-armed by the round-2 stage session session_012CkE7YGUj1z2aXiafbLS3B (ids in its report)
- Last updated: 2026-09-27
- Last note: review round 2: 1 finding fixed (allowlist contract test now asserts the bare `python3` entry as well as the `PYTHONDONTWRITEBYTECODE=1` one), 0 rejected

## Phases
1. [ ] Phase 1 — verified security-pass skip (`.claude/scripts/security_pass_skip.py`, commands, allowlist, tests, docs, changelog)   — PR #4645 open (waiting); review rounds: 2; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Where does the verification live? — Picked: A — a deterministic `.claude/scripts/security_pass_skip.py` the command runs, mirrored to consumers. Alternatives: B — decide in `clarify.yml` and carry the verdict through payload, queue, and start prompt; C — prose rules in the command. Why: one component, testable, available in consumer checkouts, and the model no longer decides from mutable data. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Which identity counts as the designated automation? — Picked: A — author `github-actions[bot]` (Bot) or a `User` with `author_association: OWNER`, and the skip label's first `labeled` event performed by that author within 120 s of creation. Alternatives: B — a configured login list; C — any OWNER/MEMBER/COLLABORATOR. Why: both signals are set by the creating token and cannot be produced by a triage-role user or a collaborator acting as themselves. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What proves the link to a valid automation record? — Picked: A — the label's own body marker, plus for `ai:security` a `Refs #T` tracker carrying `ai:security-audit`, the tracker marker, and the same author. Alternatives: B — marker only; C — also require a tracker comment naming the finding id. Why: satisfies the recommendation within 3 REST calls (§15). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] What happens on a read error or a failed check? — Picked: A — write `Security pass: run`. Alternatives: B — stop the project as BLOCKED. Why: §1, the safe option costs only an audit run. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] What happens to the router's label-only `skip_security_pass` field? — Picked: A — keep field and computation (§6), document it as advisory. Alternatives: B — tighten the router with the same checks. Why: no component decides from it; §5 minimal change. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-27] Does this project skip its own security pass? — Picked: A — skip: #4623 passes the new rule by hand. Alternatives: B — run it. Why: the issue is an automation-produced follow-up under both the old and the new rule; conformance, validation, and the reviewer panel still run. Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] Pick the earliest GitHub event by its `created_at`, never by list position: the REST events endpoints are chronological today but reviewers treat the order as unspecified, and a position-based check breaks silently if it changes. (files: .claude/scripts/security_pass_skip.py)
- [source:plan-deviation] Read label history from `issues/<N>/events` (label and state events only), not `issues/<N>/timeline`: the timeline mixes in comments and cross-references, so a 100-item page fills faster and hides later label events. (files: .claude/scripts/security_pass_skip.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#4623: start`); invoking session session_01WLQwN8LRQ8ih2NrbkY1ky6.
- Phase 1 used the issue-events endpoint instead of the timeline named in the plan (plan updated in the phase PR); a non-automation author now costs one REST read instead of two.
- Draft final PR #4635 opened 2026-09-27.
- Local verification: `tests/test_orchestrate_poll_process.py` (446 tests) runs >40 min on this container's Python 3.11 and was not finished when the PR opened; CI's sharded Python 3.12 run is authoritative for it. `tests/test_workflow_retro.py` cannot be collected on Python 3.11 (pre-existing, unrelated).
