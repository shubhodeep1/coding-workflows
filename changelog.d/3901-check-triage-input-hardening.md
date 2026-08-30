<!-- changelog: security -->
- **Check-failure triage now verifies trigger inputs and PR origin before credential-bearing work begins.** Malformed identifiers, unsupported conclusions, invalid SHAs, and fork-origin PRs stop in a credential-minimal prerequisite job.

The reusable `AI Check Failure Triage` workflow now passes check metadata through step environments and references quoted shell variables only. Its read-only prerequisite validates input shape, confirms the PR head repository, and gates the secret-bearing `triage` job on a successful same-repository result. Check names are sanitized and bounded before Actions-log or Telegram display, and fork-skip notices retain the rejected PR and head-repository context for auditability. Existing same-repository triage and concurrency behavior remains unchanged.

What this means for consumer-repo operators: untrusted check-run metadata cannot reach checkout, model execution, or PAT-backed processing until the workflow has validated the trigger and rejected fork-origin PRs.
