<!-- changelog: security -->
- **Workflow-heal evidence now verifies intake provenance before fetching run logs.** Heal issue bodies and occurrence comments must be authored and edited only by the intake account; consumer-repository runs must also match their reported repository and head or pull request. Unverifiable runs are skipped without blocking other evidence.
