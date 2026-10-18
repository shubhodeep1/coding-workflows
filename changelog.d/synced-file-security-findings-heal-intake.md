<!-- changelog: added -->
- **The workflow failure heal intake can file a consumer's security finding on a synced file as a heal issue in coding-workflows.** It is off until `WORKFLOW_HEAL_SYNCED_SECURITY_FINDING_ENABLED=true`, and nothing sends these reports yet.

A consumer's security audit already keeps a finding out of its own queue when the cited file is one the `@stable` sync overwrites and the file still matches the release copy. Nothing reported those findings to coding-workflows, which owns the files. This change adds the receiving side: a new report kind, `synced_security_finding`, for one such finding.

With the switch on, the intake:

- accepts the report only with a verified OIDC identity from coding-workflows' `security-audit.yml`, even while `WORKFLOW_HEAL_REQUIRE_REPORT_AUTH=false`;
- re-checks every field and treats the finding's exploit scenario and recommendation as untrusted text: each is cut to 600 characters, redacted, and stripped of scope markers and routing keys;
- maps the consumer path to the file coding-workflows ships (a wrapper to its `workflow-templates/` copy, a `.claude/commands/` file to its template, an audit-gate file to its `workflow-templates/audit-gate/` source) and skips live hooks, other `.claude/` files, `CLAUDE.md` and anything else with `skip reason=synced_security_finding_unmappable`;
- files the heal issue in coding-workflows with `ai:workflow-heal`, scoped to the mapped file plus tests and changelog fragments, and groups reports of the same file and finding category from every consumer into one lineage.

With the switch unset, empty or `false`, the intake logs `WORKFLOW_HEAL skip reason=synced_security_finding_disabled` and files nothing. Any other value is treated as off, with a warning.

What this means for operators: leave the switch off until the consumer-side sender (the next phase) ships, then set the repository variable `WORKFLOW_HEAL_SYNCED_SECURITY_FINDING_ENABLED=true` here.
