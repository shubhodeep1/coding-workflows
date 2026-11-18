<!-- changelog: fixed -->
- **Signed job-token comments now cover orchestrator state and the review pipeline's comments.** With `AI_COMMENT_SIGNING_SECRET` set, most new pipeline comments were still posted with `GH_PAT` and kept starting the four `issue_comment` wrappers.

Part of it was temporary: runs queued before the secret existed run with it empty. Two gaps were bugs. Full orchestrator state parts were packed so close to GitHub's 65,536-character comment cap that the signature line pushed them over, so only the last part of each state was signed; `scripts/orchestrate_state_v2.py` now leaves room for it. Scripts that post comments in a step where no earlier step had loaded the `gh` shim, such as the single-issue security pass in review_autofix's `deterministic-skip-merge` job, called the real `gh`; every comment-posting script now installs the shim itself.

| The numbers that matter | Value |
| --- | --- |
| New comments that should have been signed but went out with `GH_PAT`, 10:30 to 12:47 UTC on 2026-11-17 (backlog runs included) | 96 of 128 |
| Orchestrator state parts kept on the PAT for size | 59 |
| State framing headroom | 256 → 768 bytes |
| Scripts that now install the shim themselves | 17 bash, 5 Python (stable line; `main` also has 2 bash scripts that stable does not) |

What this means for operators: nothing to configure. Comment-driven wrapper runs drop further once this reaches `promote-candidate` (this repository) and the next `@stable` release (consumers). Large orchestrator states can take one more comment part than before.
