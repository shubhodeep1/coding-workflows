<!-- changelog: changed -->
- **Workflow log analysis now names failed jobs that never ran a step.** The collector reports `step_name: "pre_runner_failure"` for a failed job whose step list is empty or whose steps were all skipped or never concluded, instead of no step.

Such a job failed while queued or before a runner picked it up. Before this, run 35452034565 failed in `review / codex-agent` after 128 minutes and looked the same as an unknown failure. Run summaries now show `pre_runner_failure` instead of `unknown`. A failed job with a finished step (for example a successful `Set up job`) still reports no step, rows already in the analysis cache are not rewritten, and no GitHub API calls are added.
