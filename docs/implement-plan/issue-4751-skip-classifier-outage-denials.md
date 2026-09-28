# Implement-Plan Log — Stop filing Auto-mode classifier outages as permission-prompt issues

- Plan: docs/plans/issue-4751-skip-classifier-outage-denials-plan.md
- Source issue: shubhodeep1/coding-workflows#4751
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4751-skip-classifier-outage-denials   Final PR: opened right after this commit (draft; number recorded by the next stage)
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: none (protected-path approval for phase 1, asked on issue #4751)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (no wait armed while blocked)
- Last updated: 2026-09-28
- Last note: plan written; phase 1 edits .claude/scripts/permission_prompts.py and its workflow-templates copy (protected paths), so it stops before it starts (CLAUDE.md §28.C) and asks on the issue how to run it.

## Phases
1. [ ] Phase 1 — report classifier outages without filing them   — protected paths: .claude/scripts/permission_prompts.py, workflow-templates/.claude/scripts/permission_prompts.py
   - [ ] `TRANSIENT_DENIAL_REASONS` + `is_transient_denial` in permission_prompts.py; `transient` counts in `group_patterns` / `report`; outage records left out of filing in `file_patterns`, listed under `not_filed_transient`; docstring updated
   - [ ] byte-identical copy in workflow-templates/.claude/scripts/permission_prompts.py
   - [ ] tests in tests/test_permission_prompts.py: outage-only log files nothing; mixed pattern files only real occurrences; match rules (case, whitespace, suffix, PermissionDenied only); report counts
   - [ ] CLAUDE.md §23.I and agents.md describe the rule
   - [ ] changelog.d/4751-skip-classifier-outage-denials.md
   - Done when: tests/test_permission_prompts.py passes in full (parity included) and a `file --dry-run` over an outage-only log files nothing.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How should issue #4751 be resolved, given that its command already matches exact allow rules and was denied only because the classifier was unavailable? — Picked: A — stop filing classifier-outage denials as issues, and keep them in the report. Alternatives: B — close #4751 as not planned with no code change; C — add or reshape allow rules for the command. Why: the rules already match, so only the filing rule can stop the next outage from spawning a project per command shape. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Which records count as a classifier outage? — Picked: A — `PermissionDenied` records whose reason, stripped and lower-cased, starts with `classifier unavailable`. Alternatives: B — exact string only; C — also denials with no reason. Why: this is the narrowest match that survives a suffix or a case change, and a denial with no reason may be a real block. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Where does the filter live? — Picked: A — in `permission_prompts.py file`, with the records kept in the report. Alternatives: B — skip logging them in `permission_prompt_logger.py`. Why: the stage report must still show that an outage denied calls. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] What happens to the sibling outage issues #4749, #4750, #4759, #4760, #4761, #4762, #4767? — Picked: A — leave them alone and list them in this issue's progress comment for a human to close. Alternatives: B — close them as duplicates from this session. Why: one issue per chain, and closing an issue this session did not open is §23.C ask-first. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Permission mode of the invoking session: auto.
- Security pass: run (`security_pass_skip.py` → no skip label).
- Phase 1 is marked protected paths; no `Protected-path approval: phase 1` line yet. Blocked-ask posted on issue #4751 with the `ai:claude-blocked` label (CLAUDE.md §28.C, Issue Mode "Stops are reported on the issue").
