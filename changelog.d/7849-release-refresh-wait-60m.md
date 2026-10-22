<!-- changelog: changed -->
- **The release gate now waits up to 60 minutes for its validation-refresh smoke run.** `orphan-workflows-test` in `test-and-mark-stable.yml` gave up after 40 minutes, while refresh runs now take 31 to 44 minutes.

Release run 39694107256 (v1.37.2) was refused because the test stopped waiting at 00:15:11 and the refresh run it watched succeeded at 00:15:33. Four of the last eleven refresh runs went over 40 minutes, so the old limit could block any release regardless of what it contained. The wait now allows 60 minutes and the job limit goes from 110 to 130 minutes. A release takes no longer, because this job still ends well before `e2e-smoke-test`.

| The numbers that matter | Value |
| --- | --- |
| Refresh wait (`--completion-timeout-secs`) | 2400 to 3600 |
| Refresh step limit | 42 to 62 minutes |
| `orphan-workflows-test` job limit | 110 to 130 minutes |
| Recent refresh run durations | 31.2 to 43.5 minutes |

What this means for operators: a release is no longer refused just because the validation-refresh smoke run took more than 40 minutes.
