<!-- changelog: added -->
- **The heal intake now takes security findings on synced files from consumers.** A new report kind, `synced_security_finding`, lets a consumer's security audit send its high and critical findings on files the `@stable` sync manages and the consumer has not modified. The intake files one `ai:workflow-heal` issue in coding-workflows per file, category and line area, shared by every consumer, so the fix lands in the file's source here instead of in a consumer copy that the next sync overwrites.

The issue carries only the file, line, OWASP 2021 or STRIDE category, severity, confidence, finding id and consumer repository. coding-workflows is public, so a report that carries any other field, such as the exploit scenario or the recommendation, is rejected; those stay in the consumer's tracker comment.

A report is accepted only when all of these hold:

- it carries a verified OIDC identity from the consumer's `security-audit.yml` run, at the release SHA the report names;
- that SHA is on `stable`;
- every finding's file is a managed path of that release;
- it lists at most 50 findings, each `high` or `critical`.

Otherwise it is rejected with a `report_auth=rejected` log line and a WARNING, and nothing is filed.

Findings in the same file and category whose lines fall in the same bucket (`HEAL_SYNCED_SECURITY_LINE_BUCKET`, default 25) share a fingerprint. Each fingerprint takes the existing dedupe, occurrence, budget and open path, so a second consumer reporting the same area adds an occurrence comment. No run log is read and no diagnosis model runs. The issue gets a heal scope covering the file's source in `workflow-templates/`, so it goes through the normal plan and implement flow and its scope guard. The run logs `WORKFLOW_HEAL received kind=synced_security_finding findings=N opened=X occurrences=Y skipped=Z`.

What this means for operators: consumer security audits send these reports through the "Report synced-file security findings upstream" step, which ships with this change (see the sender entry). To turn the intake side off, set the repository variable `HEAL_SYNCED_SECURITY_FINDING_ENABLED=false`.
