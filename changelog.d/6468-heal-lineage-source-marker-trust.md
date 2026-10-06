<!-- changelog: security -->
- **Workflow heal no longer trusts lineage markers on ordinary issues.** The intake checks reported generation and root against an authenticated, labelled heal issue before inheriting them. Unverified claims fall back to recorded fingerprint or source lineage instead of prematurely exhausting the heal budget.
