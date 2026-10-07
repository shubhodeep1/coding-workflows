<!-- changelog: fixed -->
- **Check-failure triage now runs diagnosis without GitHub credentials and posts issues with a separate, narrowly scoped token.**

The workflow collects failure context before running Codex read-only from trusted support, with checkout credentials removed. It treats PR-head instructions as diagnostic data and redacts known secrets from the issue body before posting. Credential-bearing collection, posting, and failure-notification steps use only trusted helpers, including their event-emission dependencies. If trusted support is missing, triage stops rather than executing PR-head scripts.

What this means for consumer maintainers: add `CHECK_TRIAGE_ISSUES_TOKEN` to each repository before the next `@stable` sync. It must be a fine-grained PAT with Issues: write and Metadata: read on that repository; without it, the required reusable-workflow secret prevents triage from starting. The separate token preserves downstream `issues: opened` automation without giving the diagnosis step access to `GH_PAT`.
