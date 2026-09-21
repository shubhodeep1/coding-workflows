<!-- changelog: fixed -->
- **Poller and security-audit Python processes no longer execute checkout startup hooks.** Inline parsing, staged prompt rendering, and model-broker setup now start with an empty allowlisted environment under isolated mode, and both source and consumer audits use immutable out-of-tree support before publishing findings.

Repository-controlled `sitecustomize.py`, `usercustomize.py`, `PYTHONPATH`, or user-site packages can no longer run inside inline poller or audit parsing processes with workflow credentials. Security-audit filtering imports helpers only from canonical staged support, and final `security_audit_findings.v1` packaging runs from the runner-owned runtime directory while preserving atomic publication.

### For contributors

Route new poller inline Python through `poller_run_isolated_python` and audit-owned Python through the isolated audit helpers; contract tests reject raw `python3 -` and `python3 -c` launch sites.
