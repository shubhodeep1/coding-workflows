<!-- changelog: added -->
- **The unblock judge can now hold a blocked item until something outside it happens, without spending a round.** A new `wait` verdict, and an automatic hold after an operator-step fix-up merges, replace the immediate retry that used to block again.

On 13 October the judge closed #6831 and #6887 on its two-round cap even though neither had failed again on its own: #6887 needed #7066 to reach `stable`, and #6831 needed a person to edit `CLAUDE.md`. Each time, the judge resumed the item at once, it blocked for the same reason, and the second blocked round hit the cap. Now the judge can answer `wait` with a condition it checks itself: a pull request merged and on a branch, another issue or pull request closed, or an operator step done (its `_UNSET_OPERATOR_STEP` placeholder gone from the default branch, or set as a repository variable or secret). While the condition is unmet the item keeps its block, no resume command is posted and no round is counted. Once it holds, the judge posts the stop's usual resume command. When the fix-up of an `operator_step` verdict merges and its placeholder needs a person, the item now waits for that step instead of resuming.

| The numbers that matter | Value |
| --- | --- |
| Holds per item | at most 3, never twice on a condition already met |
| Hold lifetime | 14 days (`UNBLOCK_JUDGE_WAIT_MAX_DAYS`), then the judge decides again |
| Re-check interval | every `UNBLOCK_JUDGE_RETRY_HOURS` (6) from the hold's last check |
| Off switch | `UNBLOCK_JUDGE_WAIT_ENABLED=false` |

The judge also runs the check-triage resolution gate that clarify runs (#7095) on an `ai:check-triage` issue before judging it: once the pull request merged and the failure is verified fixed on the default branch, the issue is closed with evidence and no round is used. #7332 sat blocked after PR #7259 merged because only clarify ran that gate. The gate itself also failed to verify #7332: GitHub empties a run's pull-request list once the PR merges, and #7259's head had moved past the failed commit, so the gate could not tie the failed run to the PR (`run_identity_mismatch`). It now also accepts a run on the triage issue's trusted Head SHA when the run is on the PR's head branch.

What this means for operators: items waiting on a promotion, another pull request or an operator step stay blocked quietly instead of being closed by the round cap, and resume on their own once the condition holds. The comment `Waiting for …` on the item says what it waits for.

### For contributors

The hold marker reuses the `ai:unblock-wait:v1` prefix (`hold=<kind> ref=<ref>[ done| expired]`), so the unblock scan already rate-limits held items. `scripts/unblock_ledger.py` adds `hold-marker`, `decide --wait-disabled`, and `holds_used` / `holds_met` in the decision; `scripts/unblock_actions.py` adds `hold-followup`.
