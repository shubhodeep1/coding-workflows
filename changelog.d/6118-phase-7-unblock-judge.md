<!-- changelog: added -->
- **Blocked work no longer waits for a person.** An unblock judge now picks up every issue, pull request and orchestrator project that stopped at a point that needed a human, and gets it moving again or closes it with a report.

Once per poll tick, `orchestrate_poll_process.sh` searches for items carrying a block label (`ai:blocked`, `ai:needs-human`, `ai:scope-blocked`, `ai:destructive-blocked`, the `ai:*-failed` labels, the escalated triage, heal and resolver labels) and for failed projects, and dispatches `unblock_judge_dispatch.yml` for the oldest ones. The judge (`scripts/unblock_judge.sh`, role UNBLOCK_JUDGE) reads the evidence and picks one verdict: retry with a narrower fix, answer the open question, descope, override the scope or bulk-delete guard, reissue, accept with a follow-up, record an operator step, or close. The verdict is carried out with the commands a person would use (`/revalidate`, `/re-security-pass`, `/judge_resume`, `/approved`, `/answer`, `/reclarify`, a review dispatch). Hard limits are enforced in code. The judge never repeats a verdict for the same failure, never waives a security finding or a failed validation, and never extends an issue's scope to `.github/**`, `.claude/**` or `workflow-templates/**` in any repository, or `scripts/**` in this repository.

| The numbers that matter | Value |
| --- | --- |
| Judge runs started per poll tick | at most 1 (`UNBLOCK_JUDGE_MAX_DISPATCH_PER_TICK=0` disables dispatch) |
| Time blocked before the judge looks | 30 minutes (`UNBLOCK_JUDGE_MIN_BLOCKED_MINUTES`) |
| Time between verdicts on one item | 6 hours (`UNBLOCK_JUDGE_RETRY_HOURS`) |
| Rounds per item / per project | 2 / 6 |
| Still blocked after the last round | closed after 24 hours |
| Fix-up wait before deciding again | 72 hours (`UNBLOCK_JUDGE_FIXUP_WAIT_HOURS`) |
| Isolated model timeout | 1500 seconds (`UNBLOCK_JUDGE_TIMEOUT_SECS`; invalid values fall back to 1500) |
| New label | `ai:unblock-closed` |
| New wrapper | `workflow-templates/unblock_judge_dispatch.yml` (standard and full profiles) |

What this means for operators: blocked items resolve themselves or end closed with `ai:unblock-closed` and one Telegram CRITICAL. If the close succeeds but applying that label fails, the CRITICAL alert still fires so a closed item is not lost without notice. If a project's label write fails, a CRITICAL alert reports that closure is pending while it remains blocked. A closed project is marked `abandoned` and its tracking issue closed. Steps only you can take arrive in the `ai:operator-step` issue, with the work kept off behind a flag until you act. You can still act on any blocked item yourself. Set `UNBLOCK_JUDGE_ENABLED=false` to turn the judge off.

This release also adds activation verification after default-branch merges and orchestrator project completion. `scripts/activation_verify.sh` grades merged work `LIVE` or `DORMANT`, files code gaps for the normal pipeline, and records human-only gaps in the repository's single `ai:operator-step` issue. The model reads merged code without GitHub or Telegram credentials; `ACTIVATION_VERIFY_ENABLED=false` disables the check.

### For contributors

Three give-up exits that used to end in a Telegram alert alone now hand over to the scan.
- **Project judge.** `JUDGE_OUTPUT_FAILURE_MAX` (default 3) consecutive runs with no usable output fail the project with `ai:blocked`.
- **Merge deferrals.** The first time `MAX_MERGE_DEFERRALS` is reached, the PR gets `ai:needs-human`.
- **Review-blocked judge.** Its terminal skips (`llm_failed`, `json_parse_failed`, `missing_followup_details`, `merged_pr_unsafe_action`, `auto_merge_disabled`) add `ai:needs-human` to the PR.
- **Workflow-failure heal.** An escalation with no prior issue labels the failure report itself.

`override_guard` on the destructive latch leaves a one-shot `override=bulk_delete` marker. `implement.yml` spends it on the issue's next run, and only when no canonical workflow source is among the deletions. A failed review dispatch retains the PR's block label; failed prerequisite writes prevent later resume actions and success notifications. If posting a standalone fix-up's wait marker fails, the judge closes that new issue instead of leaving an untracked open fix-up (and logs a close failure for recovery). An issue must carry `ai:orchestrator-managed` and appear in the project's V2 state before its body can route a fix-up into that project; unavailable state defers the verdict. A PR reissue creates its replacement before closing the original PR so close-event cleanup cannot cancel the replacement. Parsed model verdicts redact the OpenRouter key before GitHub writes, including when the configured key has surrounding whitespace; an incomplete 30-comment window defers dispatch until a paginated REST history read verifies one rotating candidate per tick, so an older wait marker refreshed in place cannot be missed. The selection and action logic is pure Python (`scripts/unblock_scan.py`, `scripts/unblock_ledger.py`, `scripts/unblock_actions.py`). `tests/test_unblock_judge.py` and `tests/test_unblock_scan.py` run in their own `ci.yml` step.

Activation verification uses the ACTIVATION_VERIFY role from `prompts/mode-activation-verify.txt`. A merge that closes an activation-fix issue is not verified again, preventing recursive fix issues. Operator-step updates append keyed comments rather than patching a shared issue body; the highest-id trusted comment for a key is current, and earlier comments and legacy body sections remain as history. If a new tracker's label listing stays stale, the writer stops instead of posting to an unverified duplicate; `tests/test_activation_verify.py` covers the verifier and operator-step writer.

Issue and project resume commands are posted before their block label is removed, except `/approved`: the block label is removed before posting that command so the implementation gate sees no guard label. If posting fails, the judge restores the block label for the next scan even when another actor removed it first (HTTP 404), and alerts CRITICAL if restoration also fails.

The merged-PR guard also keeps numeric push refspecs in its branch check when output is redirected, while still recognizing adjacent unquoted file descriptors. Env-wrapped commits whose working directory cannot be resolved ask for confirmation instead of checking a different checkout.

Project state and reset commands now verify their comment authors: only the authenticated pipeline account can write state, and only that account or a repository-associated human can reset a failed project. The implement workflow also uses a collision-checked random `GITHUB_ENV` delimiter for issue text, so an issue containing `EOF` cannot inject job variables.
