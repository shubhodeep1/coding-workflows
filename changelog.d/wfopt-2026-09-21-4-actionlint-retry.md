<!-- changelog: fixed -->
- **CI retries the actionlint download on transient network errors.**

The `Install actionlint` step of the `static-checks` job downloaded the pinned actionlint release with a single `curl`, so one HTTP 5xx or connection reset failed the whole CI run (run 35615284953 failed in 82 seconds on an HTTP 504). The download now uses `--retry 3 --retry-all-errors --retry-delay 0 --connect-timeout 20 --max-time 120`. The pinned version, the URL and the `sha256sum -c -` check are unchanged, so a bad download is still rejected. Test failures are never retried. `tests/test_ci_actionlint_install_retry.py` pins the retry flags, the timeout, the checksum check after the download and the version and checksum pins.

What this means for operators: nothing to set.
