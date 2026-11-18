<!-- changelog: fixed -->
- **The security audit no longer files a finding again under a new id while its follow-up is still open, and the CI budget watch no longer re-files a job's issue right after the previous one closed.**

The weekly and per-branch security audit (`.github/workflows/security-audit.yml`) deduplicated follow-up issues only by the `finding_id` the model writes, and the model words that id differently on each run. One finding at `scripts/render_prompt.sh:326` on one branch was filed as #9399, #9431 and #9465 within a day, each while the earlier issues were still open. A finding that names the same file and the same category within 25 lines of an open follow-up of the same audit scope now reuses that follow-up's id, so nothing new is filed and the tracker report names the issue it repeats. A finding that comes back under a new id after its issue closed is still filed again; a finding reported under the exact id of any earlier follow-up, open or closed, is skipped as before. Separately, the `budget-watch` job in `ci.yml` opened a new `ai:ci-budget` issue for `orchestrate-poll` three times in one week, each a few hours after the last one closed, for a job sitting just over the warning ratio. It now waits 7 days after an issue for the same job closes, unless the job reaches 90% of its timeout.

| The numbers that matter | Value |
| --- | --- |
| `SECURITY_AUDIT_SIMILAR_FINDING_LINE_WINDOW` | `25` lines (`0` turns the check off) |
| `CI_BUDGET_REFILE_COOLDOWN_DAYS` | `7` days (`0` turns the cooldown off) |
| `CI_BUDGET_REFILE_URGENT_RATIO` | `0.9` of the limit, filed even inside the cooldown |
| Extra API calls | security audit: none; budget watch: one closed-issue listing, only when a finding below 90% has no open issue |

What this means for operators: one open security follow-up per defect and branch, in place of a new issue on each audit cycle. Consumer repositories get the security-audit change on their next `@stable` sync. A CI job that hovers around 75% of its timeout produces one issue per week at most, and a job that is really about to time out still gets one at once.

### For contributors

The category key is the OWASP code (`A01` to `A10`) when the category has one, otherwise the category's letters and digits in lower case. Closed follow-ups are left out on purpose: suppressing on them would hide a regression and would leave the single-issue security pass waiting for a follow-up that is never filed. Queue wait was already left out of the job duration by #9559, and one open `ai:ci-budget` issue per job was already enforced by its marker; the cooldown covers the closed-issue case. The tracker report comment stays one comment per run: it is posted with the job token and starts no workflow.
