<!-- changelog: changed -->
- **Review/autofix failure comments now say what failed, and heal no longer accepts a retry as the fix for a repeating failure.** A new CI check also rejects duplicate `CLAUDE.md` section numbers.

The "AI review/autofix failed" comment now names the failed step and quotes the first specific error line, with credentials redacted. Before, it said only that the workflow "encountered an error". The conflict resolver's stderr is now captured and fingerprinted like the editor's, so a resolver failure is no longer reported as a generic `workflow_failure`. Workflow failure heal treats a failure as deterministic when the identical-failure cap tripped or an earlier heal of the same lineage merged a fix and the failure came back. For those failures the intake never files a `transient` verdict, and the heal issue requires a fix that removes the cause, backed by a regression test that reproduces it. `tests/test_claude_md_section_numbers.py` fails CI when two `## §N.` headings share a number.

| The numbers that matter | Value |
| --- | --- |
| Identical failures on PR #4443 before a human found the cause | 6 |
| Extra API calls per failed review run | 1 (jobs list, fail-open) |
| Longest first-error line quoted in a comment | 300 characters |

What this means for operators: a blocked PR's comment names the failing step and error, so the cause is visible without opening the run log. A heal issue for a repeating failure asks for a real fix with a test, not a retry.

### For contributors

New `scripts/workflow_failure_heal.py` subcommands: `failure-headline` and `is-deterministic`. New stable log prefix: `AUTOFIX_FAILURE_HEADLINE`. The intake logs `classification_remapped from=transient to=inconclusive reason=deterministic_failure`.
