<!-- changelog: fixed -->
- **A review finding that only one reviewer raises, and that most of the other reviewers explicitly reject, no longer blocks a `claude/*` PR. Claude-fixer hold comments now say why the fixer stopped.**

On conformance-fix PR #4575, one reviewer (`google_gemini-3_1-flash-lite`) flagged a missing backtick at `README.md:1261`, and the other five reviewers called it a false positive. The Claude-fixer hand-off counted every ledger entry as blocking, so the finding went to a fixer session. With no verdict bot configured, the fixer had to hold for a human, and the #4550 project stopped for about 8.5 hours. Pass-2 reviewers can now reject a pass-1 entry with a structured `REJECTED_FINDING:` line. Before the hand-off step in `review_autofix.yml` counts the ledger, `scripts/review_claude_fixer_nonblocking.py` moves each rejected single-reviewer finding into a `NON-BLOCKING FINDINGS` ledger block, which is still posted on the PR. Hold comments written by `.claude/scripts/claude_fix_claim.py` take a `--reason` and mention the hand-back cap only when the cap was reached. On #4575 no cap was involved: both claims there were `review` claims, which the cap does not count.

| The numbers that matter | Value |
| --- | --- |
| Rejections needed to demote a finding | a strict majority of the other successful pass-2 reviewers, and at least 2 |
| Match between a rejection and a finding | same file, line ranges within 3 lines, same flagging reviewer |
| Hold reasons (`claude_fix_claim.py --reason`) | `cap`, `review-no-verdict-bot`, `conflict-decision`, `ci-outside-pr`, `workflow-failure`, `needs-human` |
| New log lines | `CLAUDE_FIXER_NONBLOCKING demoted=<n> successful_reviewers=<m>`, `CLAUDE_FIXER_NONBLOCKING_ENTRY`, and `nonblocking=<n>` on `CLAUDE_FIXER_HANDOFF` |

What this means for operators and Claude sessions: a `claude/*` PR whose only finding is a false positive raised by one reviewer, and rejected by most of the others, auto-merges once its checks are fresh and green, and the rejected finding stays readable in the posted ledger. A finding the other reviewers did not address still blocks, and so do task gaps and findings raised by two or more reviewers. A fixer that sees a `NON-BLOCKING FINDINGS` block leaves it alone. When a hold does happen, its comment names the real cause, so nobody goes looking for a cap that was never reached. The claim marker `<!-- ai:claude-fix-claim:v1 head=… kind=hold by=… -->` is unchanged.

### For contributors

The rule applies only in Claude-fixer mode, and only in `scripts/review_autofix_step_claude_fixer_handoff.sh`, which works on a filtered copy (`${RUNTIME_DIR}/reviewer_consensus_claude_fixer.txt`). The GPT editor path, the memory-record step, and `REVIEWER_CONSENSUS_FILE` itself are unchanged. Rejections are read from each reviewer's raw `review_<slug>.txt`, counted only when `status_review_<slug>.txt` reads `success`, and never taken from the summariser's informational `rejected_by:` line. A missing or failing filter keeps the original ledger, so every finding stays blocking. The new script is staged through `REQUIRED_BOOTSTRAP_SCRIPTS` in `scripts/stage_workflow_support.sh`. `.claude/commands/fix-claude-pr.md` now names the `--reason` at every hold site.
