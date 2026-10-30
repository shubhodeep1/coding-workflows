<!-- changelog: changed -->
- **Tests: the cancel-on-PR-close sweep test now parses the workflow and pins the job's concurrency group to the per-PR key (#8411).** The old substring check also passed against the repository-wide group, so it did not catch a regression.
