<!-- changelog: fixed -->
- **Workflow-heal evidence now verifies same-repository run links before reading logs.** Runs must match the reported source repository, head SHA and branch or PR; runs without that context are listed as skipped, including cached runs from earlier stages. This prevents unrelated run links in issue text from triggering privileged log and artifact reads.
