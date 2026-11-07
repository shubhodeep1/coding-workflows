<!-- agents: section="Stable log prefixes (contractual)" -->
- `ISSUE_TITLE_NEWLINE_STRIPPED` (`.github/workflows/clarify.yml` step `Fetch issue metadata` and `.github/workflows/orchestrate_clarify_respond.yml` step `Fetch issue and tracking context`: `::warning::ISSUE_TITLE_NEWLINE_STRIPPED issue=<int>`, emitted when the API-sourced issue title contained CR or LF. Those characters were replaced with spaces before the single-line `ISSUE_TITLE=` write to `$GITHUB_ENV` (SEC-002). The title text is never logged and the job does not fail.)
LOG_PREFIX.name=ISSUE_TITLE_NEWLINE_STRIPPED
