<!-- changelog: fixed -->
- **coding-workflows' own `.claude/` now carries the pre-push test gate from #7146, which turns `main` CI green again.** Refs #7137.

#7146 added `workflow-templates/.claude/hooks/prepush_affected_tests.py` and its Bash hook entry in `workflow-templates/.claude/settings.json`, but the live copies under `.claude/` never followed. The `Sync live .claude copies` run for that merge (run 38364113936) built its sync branch and failed at the push, so `test_template_copies_are_identical`, `test_live_copies_match_their_templates` and the settings check in `tests/test_session_start_extract_repo_slug.py` failed on `main` and on every pull request from 07:40 UTC on 13 October. This change copies both files byte for byte.

| The numbers that matter | Value |
| --- | --- |
| Files copied | `.claude/hooks/prepush_affected_tests.py`, `.claude/settings.json` |
| CI jobs fixed | `tests-hooks-and-orchestrator`, `tests-release-and-log-analysis`, `lint` (roll-up) |

What this means for interactive sessions in this repository: a `git push` now asks for confirmation unless `python3 .claude/hooks/prepush_affected_tests.py stamp` recorded a passing run for the pushed commit. `CLAUDE_PREPUSH_TEST_GATE=off` turns the check off. Consumer repos already receive the template copy and are unaffected.
