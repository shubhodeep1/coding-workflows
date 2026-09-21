<!-- changelog: fixed -->
- **Stable release workflows now recover safely after partial publication failures.** Both workflows prefer the repository PAT for release API operations.

`mark-stable.yml` and `test-and-mark-stable.yml` retain an existing immutable version tag only when it points to the intended release commit. They treat an existing matching GitHub Release as complete only when it is published, non-draft, and not a prerelease; drafts, prereleases, conflicting tags, and non-404 lookup errors fail closed. Operators can rerun after tag publication without deleting or retargeting immutable tags.
