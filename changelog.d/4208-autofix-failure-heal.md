<!-- changelog: added -->
- **Repeated review/autofix failures now file workflow-heal issues.** When the AI review workflow fails on the same pull request for two runs in a row, the failing run reports itself to the workflow-failure-heal intake in `shubhodeep1/coding-workflows`, which opens a hotfix issue for the LLM pipeline instead of leaving the PR stuck behind a Telegram alert.

Until now a `review_autofix.yml` failure such as `editor_empty_noop` produced only the admin Telegram message and a PR comment, and the poller retried the run indefinitely if the cause was systemic. The reusable `review_autofix.yml` now has a `Report autofix failure to workflow failure heal` step in its failure path that counts the consecutive failure comments on the PR, waits until the streak reaches `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` (default 2, repo variable), and sends a `workflow-failure-heal` `repository_dispatch` with a new `source_kind: autofix_failure`. The intake fingerprints the report by workflow, failure reason (`autofix:<reason>`), and evidence signature, so one systemic cause opens one issue per lineage, and the issue targets `stable` like every other heal issue. Resolver escalations, closed PRs, branch-review mode, and smoke-test fixtures are never reported, and a consumer whose stable ref predates the reporter script simply skips the step.

| The numbers that matter | Value |
| --- | --- |
| Failure streak before a report | `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` = 2 runs |
| Extra GitHub API calls per report | 1 (`POST /dispatches`; PR payload and comments come from the run) |
| Evidence attached per report | up to 4000 chars (summary line, editor flags, log tails) |
| Reporter script | `scripts/workflow_failure_heal_autofix_report.sh` |

What this means for operators: a PR whose AI review keeps failing for the same reason now shows up as a `ai:workflow-heal` issue in coding-workflows after the second failed run, with the run's evidence attached, and the existing kill switch (`WORKFLOW_HEAL_ENABLED=false`) turns the reporter off together with the label-based reporters.

### For contributors

The reporter and the heal Python helper are staged through the `OPTIONAL_BOOTSTRAP_SCRIPTS` loop in `scripts/stage_workflow_support.sh`, so no consumer wrapper changes. The intake script reads `failure_reason`, `failure_streak`, and `failure_evidence` from the payload and feeds them to the diagnosis prompt as untrusted context; `tests/test_workflow_failure_heal.py` covers the streak counter, payload validation, issue composition, the intake path, the reporter's skip reasons, and the workflow wiring.
