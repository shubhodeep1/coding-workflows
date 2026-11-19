<!-- changelog: changed -->
- **Repeat heal reports update one comment instead of adding a new one each time, and a heal issue's own failed run no longer opens another heal issue.** Release-gate fixtures titled `[E2E Orchestrate Smoke ...]` are skipped by the heal intake as well.

Heal issue #9200 collected 271 `<!-- workflow-failure-heal:occurrence -->` comments in two days, and each comment posted with the pipeline account started four `issue_comment` wrapper runs. The Workflow Failure Heal Intake now keeps one occurrence comment per heal issue and edits it: a count, the first and latest report times, and the newest 10 reports. An edit starts no wrapper run. The intake also stops filing heal issues for heal issues (#9200 for #9190, #9192 for #9189, #9238 for #9234): when a heal issue's own clarify, plan or implement run fails, the report is recorded on that issue and no new issue is opened. The same defect still gets a heal issue from any report whose source is not a heal issue. Reports from `[E2E Orchestrate Smoke ...]` and `[E2E Clarify Negative Test ...]` fixtures are skipped like `[E2E Smoke Test ...]` ones (#9236 was filed for the fixture #9210).

| The numbers that matter | Value |
| --- | --- |
| Occurrence comments per heal issue | 1, edited in place (was 1 per report) |
| Reports kept in the comment | newest 10, at most 60,000 characters |
| Extra API calls per repeat report | 1 paginated comments read, then 1 `PATCH` in place of the `POST` |
| Counter posted again as a new comment | when 15 or more newer comments follow it |
| New switches (repository variables, default `true`) | `HEAL_OCCURRENCE_IN_PLACE_ENABLED`, `HEAL_SOURCE_DEDUPE_ENABLED` |

What this means for operators: a heal issue's thread stays short, and the count of repeats is in one place. Set either variable to `false` to return to the earlier behaviour. Heal issues that already hold single-report occurrence comments keep them; the first new repeat starts the counter at their number plus one.

### For contributors

The comment's first line is unchanged, so readers that match it by prefix are unaffected. `_recovery_occurrence_runs` reads the entries one by one, and the stale auto-close anchor (`heal_stale_decision`) also uses the count line's `last=` time, because an edited comment keeps its original `created_at`. The poller reads only an issue's newest 20 comments for the stale auto-close, which is why a counter buried under 15 newer comments is posted again and the old one deleted. Two intakes that handle the same issue at the same moment can lose one report's entry and one count. The heal-of-heal check compares the report's issue number with the GitHub list of `ai:workflow-heal` issues, not with the labels in the report; such a report is recorded without a diagnosis and without escalation, and only a run that fails with the heal issue's own fingerprint (or cause) is escalated, as before.
