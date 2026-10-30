<!-- changelog: added -->
- **The heal evidence pack now lists the runs started for the issues a failed job names, and the commits on `main` that are not on `stable`.** Both come with the existing `WORKFLOW_HEAL_EVIDENCE_PACK_ENABLED=true` switch; it is still off by default.

During the 2026-10-27..29 release-gate incident, the working implement run had been started for the smoke-test issue the failed release job named, and one heal plan said `main`'s source was unreadable in the planning workspace. With the switch on, a `workflow_run` heal issue's `## Evidence pack` now adds a `Runs started for issue #N` subsection for up to two issues named in the failed jobs' logs: every run created during the failed job's time window whose title matches the issue's, with its status, timestamps, `head_sha` and `head_branch`. For this repository it also adds a `Branch divergence` subsection from one compare of `stable...main`: the changed files that are valid scope paths and the newest commits with their PR numbers. The compare does not say which commit touched which file, so the two lists are kept separate. Titles, branch names, commit subjects and file names are escaped and labelled untrusted, and the whole body is still checked by the routing validator before the issue is filed.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_EVIDENCE_PACK_ENABLED=false`) |
| Extra GitHub API calls per heal report (switch on) | at most 5: 2 issue reads, 2 runs-list pages, 1 compare |
| Issues followed per report | 2 |
| Rendered per subsection | 10 runs per issue; 20 files and 10 commits for the divergence |
| New `evidence_pack` reason tokens | `issue_unreadable`, `runs_unreadable`, `runs_truncated`, `compare_unreadable`, `compare_malformed`, `job_window_unavailable`, `requests_failed` |

What this means for operators: with the switch off, heal issues are byte-identical to before and no call is made. With it on, a failed read never blocks the heal issue; it shows up as `WORKFLOW_HEAL evidence_pack outcome=partial reason=<token>`.
