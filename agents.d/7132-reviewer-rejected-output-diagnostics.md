<!-- agents: section="Review pipeline consolidator + ledger contract" -->
- A Claude pool reviewer attempt that does not succeed (`run_pool_reviewer` in `scripts/review_run_reviewers.sh`) keeps up to 64 KiB of its output as `PREVIOUS_REVIEWS_DIR/rejected_<prefix>_<slot>_attempt<N>.txt` (an attempt with no output removes any stale file of that name), outside the `<prefix>_*.txt` globs the consensus summariser reads; both reviewer-log artifacts upload `rejected_*.txt`. A crashed Claude run in `scripts/review_untrusted_sandbox.sh` also logs `CLAUDE_POOL crash_detail role= account= exit_code= result=<first 400 printable characters of the CLI result text|empty|no_result_event>`.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `REVIEWER_POOL_REJECTED_OUTPUT` (`run_pool_reviewer` in `scripts/review_run_reviewers.sh`: `slot= pass= attempt= model= bytes= reason= kept_bytes= truncated=true|false`; `bytes=` is the full output size and `kept_bytes=` the size of the kept file, capped at 65536, one line per unsuccessful Claude pool reviewer attempt; the kept output is `rejected_<pass>_<slot>_attempt<N>.txt`)
LOG_PREFIX.name=REVIEWER_POOL_REJECTED_OUTPUT
