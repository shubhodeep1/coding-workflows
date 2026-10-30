<!-- changelog: changed -->
- **Tests: the cancel-on-PR-close sweep test now also asserts that the job's concurrency group is not the repository-wide group (#8411).** The test already parsed the workflow and checked the per-PR key; the new assertion fails directly if the group reverts to the repository-wide key.
