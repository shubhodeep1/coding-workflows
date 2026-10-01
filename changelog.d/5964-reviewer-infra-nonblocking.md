<!-- changelog: changed -->
- **A reviewer slot that fails for infrastructure reasons no longer blocks a clean `claude/*` review round.** When at least half of the active reviewer panel returns `success` and those reviewers report no findings and no task gaps, the round is clean and auto-merge proceeds; before, one failed slot handed the whole PR to a Claude session.

In Claude-fixer mode the consensus ledger used to carry the failure notice of every reviewer slot that failed (non-retryable error, output-token cap, empty output, retryable-failure limit) or was skipped for an unmapped model. The summariser turned that notice into a `FINDINGS FROM <slot>` block that was not `(No findings reported.)`, so the round counted as not clean and cost a Claude fix session. `scripts/summarize_reviewer_consensus.sh` now skips every reviewer input whose `status_<prefix>_<slot>.txt` is not `success`; an input with no status file is kept as before. `scripts/review_autofix_step_claude_fixer_handoff.sh` then requires `REVIEWERS_SUCCESSFUL * 2 >= active` before it calls a round clean, and logs `CLAUDE_FIXER_PANEL_FLOOR successful=<n> active=<m> floor_met=<true|false|unknown>`.

| The numbers that matter | Value |
| --- | --- |
| Clean-round floor | at least 50% of the active panel returned `success`, rounded up |
| 6 active reviewers | 3 successes needed |
| 5 active reviewers | 3 successes needed |
| Active panel size | larger of the `status_review_*.txt` count and the `reviewer_active_models.txt` line count |
| Workflow files changed | 0 (`review_autofix.yml` is untouched) |

A reviewer slot skipped for budget is not covered on its own. When it is the pass's only non-success slot, the runner still requests a partial finalize and the round finishes in a later run, as it did before. Beside a hard failure it is dropped like the others.

What this means for operators: `claude/*` PRs whose reviewers hit a rate limit or a token cap should auto-merge when enough of the panel still reviewed them clean, with no Claude fix session. A real finding or task gap from any successful reviewer still blocks, and so does a round below the floor. When the active panel size cannot be read (`floor_met=unknown`) the floor is not applied.

### For contributors

The summariser change applies to every caller, not only Claude-fixer mode: the pass-1 cross-pollination ledger and the pass-2 ledger that feeds the GPT editor and the memory-record step no longer include failure notices for non-`success` slots. The old `failed after retries` filter is kept. `tests/test_review_autofix_claude_fixer_mode.py` runs the real `run_reviewer_pass` to pin the budget-skip boundary. Tests: `tests/test_review_autofix_claude_fixer_mode.py` and `tests/test_summarize_reviewer_consensus_prompt.py`.
