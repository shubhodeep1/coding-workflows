<!-- changelog: fixed -->
- **The Claude-fixer convergence project's conformance audit fixed the defects it found before the project merges.** The GPT judge, the evidence check, and the hold and check-in helpers now fail safe in the edge cases the audit found.

The GPT judge now holds for a human when the model answers `hold` or `close_and_reissue` without rulings, instead of labelling the PR `ai:review-blocked`, and it reads the model's action without regard to case, so `Hold` holds too. Model text can no longer add marker or checklist lines to the verdict comment or the follow-up issue, or extra lines to the judge's fix prompt and `[judge-fix]` commit body. Sticky rulings follow only judge markers in collaborators' comments. Evidence from a run whose head repository is a fork or missing is now rejected, on the default branch as well as the pull-request head, so a fork branch named `main` cannot pass as a default-branch run. The merge check's early check-run read no longer polls for 300 seconds on every sweep dispatch.

| The numbers that matter | Value |
| --- | --- |
| Early check-run wait on a merge-check run | up to 300 s → 0 s |
| Trusted comment authors for sticky-ruling markers | OWNER, MEMBER, COLLABORATOR |
| `CLAUDE_CHECK_IN_HELD_RETRY_MINUTES=inf` | crash → 180 |

What this means for operators: a judge that cannot rule now asks a human (`ai:needs-human`) rather than starting another blocked-PR stage.

### For contributors

The `.claude/` changes (`stale_sessions.py` title matching, `claude_fix_claim.py`, `check_in_status.py`, `implement-plan-claude.md`, `fix-claude-pr.md`) were made in their `workflow-templates/.claude/` twins first and reach `.claude/` with the twin sync. `claude_fix_claim.py --reason` now strips nested `<!--` openers. The `/implement-plan-claude` duplicate-resume guard records the stage together with the stage session. CLAUDE.md §26.D accepts a checker title with a state note. The marker-chosen evidence run is a documented residual risk (agents.md).
