<!-- changelog: fixed -->
- **Workflow-log analysis agents no longer run beside GitHub write credentials.**

The analysis, deep-audit, API-redundancy, weekly-retro, and consumer-retro model passes use a read-only container with no external network or GitHub credentials. Separate publisher jobs validate bounded model-result artifacts before committing reports or posting comments. Untrusted CI logs, including fork-origin runs, remain available for analysis as data; failures never fall back to host Codex execution.
