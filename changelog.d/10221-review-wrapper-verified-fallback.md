<!-- changelog: fixed -->
- **Review failure-path steps no longer run scripts committed in the PR.** Refs #3576 (security finding on PR #10167; issue #10221).

  - In `review_autofix.yml`'s `codex-agent` job, a step whose body lives in `scripts/` looks for the staged support copy first. Its fallback to the workspace `.codex-workflow-src/scripts/` copy now needs two things: `WORKFLOW_SUPPORT_SOURCE_VERIFIED=true`, which "Ensure workflow support source checkout" sets only after the checkout's HEAD matched `SCRIPT_REF`, and that HEAD still matching `SCRIPT_REF` when the step runs.
  - Before this change, a PR could commit `.codex-workflow-src/scripts/...` and force an early failure, for example by adding a `.opencode` path. "Assemble failure evidence", "Mark linked issues review-blocked (workflow failure)" and "Post review-blocked comment on PR (workflow failure)" would then load the PR's copy while `GH_PAT` and the comment-signing secret were set. Those steps now report that the script is missing: a warning for the `always()` step, an error for the others.
  - Runs where support staging succeeds behave as before.

What this means for consumer repos: nothing to change. After the next `@stable` sync, a review run that fails before its support checkout is verified skips the scripted failure-reporting steps instead of running workspace copies. The inline Telegram failure alert still fires.
