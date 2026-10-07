<!-- changelog: security -->
- **`/deploy-activate` no longer runs Cloudflare preflight checks on unmerged project code with session credentials.** Worker deployment steps now require a verified, protected default-branch commit.

The command and its consumer template require an unmerged project PR to be merged before a Worker deploy step. They prefer CI check-runs and allow local checks only in a credential-free, no-egress sandbox. A confirmed deploy uses the verified commit and strips unrelated session credentials. This tightens the command's deployment path without loosening the existing approval or secret-handling rules.

What this means for operators: merge the project before approving a Worker deployment; if commit protection cannot be verified, the command will guide you through the step rather than deploying itself. Account-scoped Cloudflare tokens must be narrowed through external credential provisioning where possible.
