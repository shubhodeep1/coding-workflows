<!-- changelog: fixed -->
- **Claude read-profile runs can no longer use Git output options to overwrite trusted files.** Security audits now check the support scripts before reporting a single-issue result.

Read-profile Bash commands are checked for shell control syntax and write-capable git options, including abbreviated `--output` and the `git grep` pager flag. The security audit removes GitHub credentials and runner command-file paths from the Claude call while leaving the reporting code's credentials available. Before executing the single-issue reporter, the workflow compares the trusted support scripts against a fingerprint recorded before the audit. A mismatch leaves the PR's security pass pending rather than executing altered support code.

What this means for operators: a changed support script stops single-issue security-pass reporting and logs `SECURITY_AUDIT_SUPPORT_INTEGRITY outcome=mismatch` for investigation.
