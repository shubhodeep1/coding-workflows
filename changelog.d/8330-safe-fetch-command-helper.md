<!-- changelog: security -->
- **`/investigate-issue`, `/analyze-log` and `/apply-url` now download external URLs through a helper that refuses internal network addresses (#8330).** They used to fetch with `curl --fail-with-body -sSL`. That follows redirects to any address, so a crafted log or PDF link could reach loopback services, private ranges or the cloud metadata endpoint from a session.

  Each download now runs `(cd .claude && python scripts/safe_fetch_url.py --url <url> --output /tmp/<name>)`. The helper accepts HTTPS only. It refuses a URL, or any redirect, that resolves to a non-public address, and pins curl to the address it checked. It also caps redirects, size and the time of each request. It prints the final HTTP status and exits 1 on any non-2xx response, leaving no file behind. In `/apply-url`, a PDF is downloaded through the helper and then read with `pdftotext`. The `Bash` bullet under Tool Access names the helper and no longer allows `curl`. The helper is ported unchanged from `shubhodeep1/radateeree-resort.com` (Refs shubhodeep1/radateeree-resort.com#151).

  | The numbers that matter | Value |
  | --- | --- |
  | Consumer path | `.claude/scripts/safe_fetch_url.py` (delivered by the existing `.claude/` sync) |
  | Commands converted | 3 (`investigate-issue.md`, `analyze-log.md`, `apply-url.md`) |
  | Default limits | 5 redirects, 100 MiB, 10 s connect, 120 s per request (each redirect hop is a new request) |

  What this means for consumer repos: the next `@stable` sync adds `.claude/scripts/safe_fetch_url.py` and updates the three command files. Nothing needs to be configured. The helper bypasses the HTTPS proxy, so in a session whose network only allows traffic through the proxy, these downloads fail and the URL is reported as inaccessible. radateeree-resort.com can turn on its command test (`SAFE_FETCH_COMMAND_GRANT_UNSET_OPERATOR_STEP=true`) once the sync has landed.
