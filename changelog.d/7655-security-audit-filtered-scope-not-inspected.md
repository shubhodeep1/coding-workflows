<!-- changelog: fixed -->
- **A project whose diff touches a credential-filtered file such as `.env.example` no longer has its security pass blocked on every poll.** The file is still withheld from the auditor, and it is now reported as not inspected.

The orchestrator's project security pass audits every file the project changed. When one of those files matched the isolated read-only filter (any `.env*` path, `*.pem`, `secrets.*`, or a hidden `CLAUDE.md`), `scripts/codex_isolated_workspace.py export-oversized` rejected the audit with `scoped file excluded by read-only filter`. The filter never admits such a file, so the pass could never succeed. The poller reported `engine_unavailable` and retried forever, which held fun-token-multi-chain#579 at the security pass over four `.env*` files. Now the file is skipped and named in four places:

- the auditor's prompt;
- the findings-json `coverage.scoped_filtered_skipped` list and `scoped_filtered_skipped_count`;
- the default-branch tracker comment, as a coverage note;
- a `::warning::` together with the `security-audit: oversized ... filtered_scoped=<n>` log line.

| The numbers that matter | Value |
| --- | --- |
| Filtered files #579's diff contained | 4 (`.env.example`, `lobby-frontend/.env.example`, `lobby-frontend/.env.production`, `services/session-server/.env.example`) |
| Withheld files listed in the export manifest | first 50, plus a total count |
| New GitHub API calls | 0 |

What this means for operators: nothing to set. A blocked project security pass resumes on the next poll once the consumer runs this `@stable` release. Withheld files stay unread by the model, so a secret committed inside an env template is not caught by this audit; the coverage note names every such file so a person can check it. Scoped oversized files above the export caps still stop the audit.

### For contributors

The export manifest gains `filtered_scoped` (entries `{path, reason}`, reason `credential_filter` or `hidden`) and `filtered_scoped_count`. `oversized_readonly_export.v1` and `security_audit_findings.v1` keep their schema versions because both additions are additive. Withheld files never mark coverage partial: that flag forces full re-scans until coverage completes, and these files can never be covered.
