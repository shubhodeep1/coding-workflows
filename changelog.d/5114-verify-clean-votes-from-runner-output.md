<!-- changelog: security -->
- **Claude-fixer review no longer counts a reviewer as clean on the consensus ledger's word when a reviewer slot failed.** Each clean vote on that path now has to be backed by the reviewer's own output file, so a ledger that reads clean over a reviewer's finding cannot auto-merge the PR.

In Claude-fixer mode, a review with a failed reviewer slot is clean when at least `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` other reviewers came back clean (issue #4835). `scripts/review_autofix_step_claude_fixer_handoff.sh` counted a reviewer as clean when its block in the consensus ledger read `(No findings reported.)` and its `status_review_<slug>.txt` read `success`. The ledger is written by a summariser model, and `success` only means the reviewer finished, so a mislabelled block could turn a reviewer's finding into a clean vote and five such votes could enable auto-merge (security audit finding #5114). A clean vote now also needs the runner-written `review_<slug>.txt` to be an unambiguous no-findings result. It must have at least one `NONE` line and no finding or task-gap field (`File:`, `Problem:`, `Requirement:`, `Evidence of absence:`, `SEVERITY:` and the rest of the reviewer format), and, when the reviewer used the lens checklist, each of the nine lenses must be followed by `NONE`. Anything else keeps the round as a hand-off, with a warning naming the reviewer and the reason.

| The numbers that matter | Value |
| --- | --- |
| Reviewer outputs used to size the rule | 30, from 5 review runs on 2026-09-29 |
| Clean reviewers still counted on the fully clean run | 5 of 6 (the sixth wrote only a narration line) |
| Outputs carrying a finding that were counted clean | 0 |
| New variables, log keys, GitHub API calls | none |

What this means for operators and consumer repos: a `claude/*` PR can only auto-merge past a failed reviewer slot when the remaining reviewers' own outputs say they found nothing. A reviewer that writes something other than the `NONE` verdict now counts as a missing clean vote, so such a round is handed to the Claude session more often than before. Reviews with no failed slot are unchanged. Consumers get this with the next `@stable` release, with no wrapper change.

### For contributors

The lens headings live in `claude_fixer_checklist_lens_headings`, and `tests/test_review_autofix_claude_fixer_mode.py` pins them to `prompts/review-reviewer-checklist.txt`, so renaming a lens in the prompt fails CI until the script follows. The classifier is plain awk and is tested under both mawk and gawk. It reads the file through a here-string, not a pipe, so a failing read can never end the step under `set -euo pipefail`.
