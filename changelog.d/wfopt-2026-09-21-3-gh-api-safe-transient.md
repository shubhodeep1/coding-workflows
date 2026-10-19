<!-- changelog: fixed -->
- **Release-gate GitHub reads now survive a single 5xx or network blip.** `gh_api_safe` in `scripts/comprehensive_test_and_release_gh_api.sh` retried only rate-limit errors, so one HTTP 502, connection reset or timeout could fail a long `test-and-mark-stable.yml` or `comprehensive-test-and-release.yml` run.

`gh_api_safe` now also retries transient errors, within its existing budget of 4 attempts:

- it counts as transient a stderr that names any `HTTP 5xx` status (500 to 599, as the shared retry helpers do) or a bare 502, 503 or 504 (only as a whole word, with no letter, digit or underscore next to it), a connection reset, an `i/o timeout`, a `timeout awaiting` response, a TLS handshake timeout or an EOF;
- it waits 5, 10, then 20 seconds between attempts, and `RATE_LIMIT_BACKOFF` is unchanged by these retries;
- it retries only reads. A call with `-X`/`--method` other than GET, or any of `-f`, `-F`, `--field`, `--raw-field` or `--input`, still makes one attempt, so a dispatch or comment is never sent twice;
- each retry writes `GH_API_SAFE_RETRY attempt=<n>/<max> class=transient` to stderr, also in quiet mode. The line carries no arguments or token.

Rate-limit retries, return codes, `GH_API_SAFE_OUTPUT`, quiet-mode reporting and the `gh_api_safe_print`, `gh_api_safe_quiet` and `gh_api_safe_quiet_print` wrappers are unchanged. The helper still does not source `scripts/gh_helpers.sh`, because the release jobs that source it may not have that file staged.

What this means for operators: a persistent GitHub outage now takes up to about 35 seconds longer per read before the gate reports it; search the job log for `GH_API_SAFE_RETRY` to see when a read was retried.
