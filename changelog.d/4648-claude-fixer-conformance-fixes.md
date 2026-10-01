<!-- changelog: fixed -->
- **The Claude-fixer convergence project's conformance audit fixed the defects it found before the project merges.** The session janitor now recognises numbers-first session titles, and the GPT judge, the evidence check, and the hold and check-in helpers now fail safe in the edge cases the audit found.

The hourly Claude issue pickup's janitor (`.claude/scripts/stale_sessions.py`) matched only the old session titles. Titles that carry `#<issue> · PR #<pr> — ` in front of `implement-plan …` or `issue … — implement` (#4943) counted as not ours and were never archived. On a live listing of 100 sessions it recognised 5; it now recognises 62. The GPT judge now holds for a human when the model answers `hold` or `close_and_reissue` without rulings, instead of labelling the PR `ai:review-blocked`. Model text can no longer add marker or checklist lines to the verdict comment or the follow-up issue. Sticky rulings follow only judge markers in collaborators' comments. Evidence from a pull-request-head run with no head repository is now rejected. The merge check's early check-run read no longer polls for 300 seconds on every sweep dispatch.

| The numbers that matter | Value |
| --- | --- |
| Live sessions the janitor recognises (100-session page) | 5 → 62 |
| Early check-run wait on a merge-check run | up to 300 s → 0 s |
| Trusted comment authors for sticky-ruling markers | OWNER, MEMBER, COLLABORATOR |
| `CLAUDE_CHECK_IN_HELD_RETRY_MINUTES=inf` | crash → 180 |

What this means for operators: finished stage and checker sessions of issue-mode projects are archived 24 hours after their source issue closes, as the janitor intended. A judge that cannot rule now asks a human (`ai:needs-human`) rather than starting another blocked-PR stage.

### For contributors

The `.claude/` changes (`stale_sessions.py`, `claude_fix_claim.py`, `check_in_status.py`, `implement-plan-claude.md`, `fix-claude-pr.md`) were made in their `workflow-templates/.claude/` twins first and reach `.claude/` with the twin sync. `claude_fix_claim.py --reason` now strips nested `<!--` openers. The `/implement-plan-claude` duplicate-resume guard records the stage together with the stage session. CLAUDE.md §26.D accepts a checker title with a state note. The marker-chosen evidence run is a documented residual risk (agents.md).
