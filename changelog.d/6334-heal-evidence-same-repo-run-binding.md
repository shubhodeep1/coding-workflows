<!-- changelog: fixed -->
- **Workflow-heal evidence now verifies same-repository run links before reading logs.** Runs in the heal issue's repository must match the reported source repository, head SHA, and PR or branch, just like consumer runs. Unverifiable links are skipped and recorded in the evidence index, including cached runs and reports without a verifiable head SHA.
