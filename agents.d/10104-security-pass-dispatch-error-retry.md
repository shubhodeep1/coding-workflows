<!-- agents: section="Stable log prefixes (contractual)" -->
- `SINGLE_ISSUE_SECURITY_PASS` audit-dispatch error keys (#10104, `scripts/review_single_issue_security_pass.sh`). The gate's two failed-dispatch lines now end with ` dispatch_error=<class>`: `mode=gate pr= head= outcome=hold reason=dispatch_failed workflow= cycle= dispatch_error=<class>` and `... reason=dispatch_failed_exhausted cycle= dispatch_error=<class>`. `<class>` is one of `rate_limited`, `server_error`, `network`, `rejected`, `auth` or `other`:
  - `rate_limited`: HTTP 429, or HTTP 403 with a rate-limit message.
  - `server_error`: HTTP 5xx.
  - `auth`: HTTP 401, or HTTP 403 without a rate-limit message.
  - `rejected`: HTTP 404 or 422, or gh's "could not find any workflows" error.
  - `network`: any other non-empty text with no HTTP status.
  - `other`: any other HTTP status, or empty output.

  `rate_limited`, `server_error` and `network` are retried in the same run. Each retry logs `mode=gate pr= head= outcome=dispatch_retry attempt=<n> dispatch_error=<class>`. A run has one retry budget, `SECURITY_PASS_DISPATCH_RETRIES` (default 2, valid 0-5), shared by the delta dispatch and its plain fallback. The wait before each retry is `SECURITY_PASS_DISPATCH_RETRY_DELAY_SECS` (default 20, valid 0-60). When a run needed more than one dispatch call, ` dispatch_attempts=<n>` is appended to `outcome=dispatched`. Each failed attempt prints `::warning::security-audit dispatch attempt <n> failed (dispatch_error=<class>): <excerpt>`. The excerpt is the gh error with the `GH_TOKEN`/`GITHUB_TOKEN` values and token-shaped strings redacted and newlines removed, then cut to 200 characters. The PR comment names only the class, never the raw error.
