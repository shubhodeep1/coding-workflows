<!-- changelog: fixed -->
- **Three orchestrator stall-recovery dead ends are closed: clarification auto-answers now parse the option formats Codex actually emits, the editor-changes-lost review recovery fires on every trigger path with a per-head retry bound, and consumer poll runs get the integration-readiness script they call.**

Orchestrator-managed clarifications no longer park for an hour and then discard the model's recommended answers. The `(RECOMMENDED)` parsers in `plan.yml` and in the poller's `extract_recommended_answers` accept bullet-less option lines (`A. text (Recommended)`), and clarification comments are matched by their body marker instead of a `[bot]` author login — pipeline comments are posted with the `GH_PAT` under a human login, so the login filter meant the poller's `auto_respond_clarify` never found the questions at all. On `tele-funtoken-msg-scoring#3754` that combination stalled a financial-payout clarification for 67 minutes and then answered it with "the issue description is deemed sufficient", leaving the planner to invent payout tables.

The `review_autofix.yml` editor-changes-lost recovery was unreachable on `workflow_dispatch` runs, which is every autofix iteration after the first, so a lost editor pass posted "All automated retry attempts have been exhausted" without making any attempt and the PR sat blocked until generic stall recovery re-triggered it about two hours later (`tele-funtoken-msg-scoring#3757`, run 32659591000). The step now keys on the job-level PR number and bounds itself with `autofix_changes_lost_head_retry_consumed` (one branch-scoped list-runs call, fail-closed): one automated retry per head SHA, since a changes-lost run pushes no commit and the head cannot advance.

`orchestrate_poll.yml` also stages `scripts/check_integration_pr_readiness.py`, which `orchestrate_poll_process.sh` already invoked; consumer poll runs previously failed the call with "No such file or directory" and never refreshed the `orchestrator/integration-pr-not-ready` commit status from the poller (run 32657328962).

| The numbers that matter | Value |
| --- | --- |
| Clarification stall before forced auto-answer (#3754) | 67 minutes |
| Review dead-end before generic stall recovery (#3757) | 123 minutes |
| Automated editor-changes-lost retries per head SHA | 1 |
| Extra API calls per changes-lost re-dispatch decision | 1 |

What this means for operators: the two Telegram warnings that motivated this change ("Stall recovery: auto-responded to clarification", "Stall recovery: re-triggered review") should become rare; when a clarification does auto-answer it now carries the model's own recommended `Q1: A` selections instead of "deemed sufficient", and an "Editor changes lost … exhausted" PR comment now means a retry really ran.

### For contributors

`tests/test_plan_auto_answer_recommended_parser.py` pins the bullet-less forms (including the verbatim #3754 Q1 block) and the marker-only comment selection end-to-end; `tests/test_editor_changes_lost_redispatch_budget.py` pins the re-dispatch guard and the budget helper; `tests/test_orchestrate_poll_stages_readiness_script.py` pins that every unguarded `python3 scripts/<f>.py` call in the poller is staged.
