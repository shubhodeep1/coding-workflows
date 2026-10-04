<!-- changelog: added -->
- **Blocked work no longer waits for a person.** An unblock judge now picks up every issue, pull request and orchestrator project that stopped at a point that needed a human, and gets it moving again or closes it with a report.

Once per poll tick, `orchestrate_poll_process.sh` searches for items carrying a block label (`ai:blocked`, `ai:needs-human`, `ai:scope-blocked`, `ai:destructive-blocked`, the `ai:*-failed` labels, the escalated triage, heal and resolver labels) and for failed projects, and dispatches `unblock_judge_dispatch.yml` for the oldest ones. The judge (`scripts/unblock_judge.sh`, role UNBLOCK_JUDGE) reads the evidence and picks one verdict: retry with a narrower fix, answer the open question, descope, override the scope or bulk-delete guard, reissue, accept with a follow-up, record an operator step, or close. The verdict is carried out with the commands a person would use (`/revalidate`, `/re-security-pass`, `/judge_resume`, `/approved`, `/answer`, `/reclarify`, a review dispatch). Hard limits are enforced in code. The judge never repeats a verdict for the same failure, never waives a security finding or a failed validation, and never touches `.github/workflows/**`, `.claude/**` or `scripts/**` in this repository.

| The numbers that matter | Value |
| --- | --- |
| Judge runs started per poll tick | at most 5 (`UNBLOCK_JUDGE_MAX_DISPATCH_PER_TICK`) |
| Time blocked before the judge looks | 30 minutes (`UNBLOCK_JUDGE_MIN_BLOCKED_MINUTES`) |
| Time between verdicts on one item | 6 hours (`UNBLOCK_JUDGE_RETRY_HOURS`) |
| Rounds per item / per project | 2 / 6 |
| Still blocked after the last round | closed after 24 hours |
| Fix-up wait before deciding again | 72 hours (`UNBLOCK_JUDGE_FIXUP_WAIT_HOURS`) |
| New label | `ai:unblock-closed` |
| New wrapper | `workflow-templates/unblock_judge_dispatch.yml` (standard and full profiles) |

What this means for operators: blocked items resolve themselves or end closed with `ai:unblock-closed` and one Telegram CRITICAL. A closed project is marked `abandoned` and its tracking issue closed. Steps only you can take arrive in the `ai:operator-step` issue, with the work kept off behind a flag until you act. You can still act on any blocked item yourself. Set `UNBLOCK_JUDGE_ENABLED=false` to turn the judge off.

### For contributors

Three give-up exits that used to end in a Telegram alert alone now hand over to the scan.
- **Project judge.** `JUDGE_OUTPUT_FAILURE_MAX` (default 3) consecutive runs with no usable output fail the project with `ai:blocked`.
- **Merge deferrals.** The first time `MAX_MERGE_DEFERRALS` is reached, the PR gets `ai:needs-human`.
- **Review-blocked judge.** Its terminal skips (`llm_failed`, `json_parse_failed`, `missing_followup_details`, `merged_pr_unsafe_action`) add `ai:needs-human` to the PR.
- **Workflow-failure heal.** An escalation with no prior issue labels the failure report itself.

`override_guard` on the destructive latch leaves a one-shot `override=bulk_delete` marker. `implement.yml` spends it on the issue's next run, and only when no canonical workflow source is among the deletions. The selection and action logic is pure Python (`scripts/unblock_scan.py`, `scripts/unblock_ledger.py`, `scripts/unblock_actions.py`). `tests/test_unblock_judge.py` and `tests/test_unblock_scan.py` run in their own `ci.yml` step.
