<!-- changelog: changed -->
- **A failed Claude review-panel slot now leaves evidence of why it failed.** A rejected reviewer answer is kept in the run's reviewer logs, and a Claude run that crashes logs the first 400 characters of the CLI's result text.

PR #7104's review failed twice because the Claude pool slot's pass-2 answer was rejected as malformed and its Haiku fallback crashed within a second. Neither cause could be confirmed, because the rejected answer was deleted and a crash logged only `outcome=crashed reason=result_success`. A pool attempt that does not succeed now keeps up to 64 KiB of its output as `rejected_<pass>_<slot>_attempt<N>.txt` and logs `REVIEWER_POOL_REJECTED_OUTPUT` with its size and reason. A crashed run logs `CLAUDE_POOL crash_detail ... result=<text>`. Both files are uploaded with the reviewer logs and the failure logs.

What this means for operators: the next review-slot failure of this kind can be diagnosed from the run's artifacts and job log, without rerunning it.
