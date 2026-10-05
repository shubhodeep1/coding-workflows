<!-- changelog: fixed -->
- **The merged-PR guard blocks git writes whose repository depends on unresolved shell-state overrides.** Appended `GIT_DIR` or `GIT_WORK_TREE` assignments and earlier shell-level changes to those variables can direct a commit or push to a different repository than the session checkout. The guard no longer checks the wrong checkout or asks for permission to proceed: it blocks these writes with instructions to use a literal, resolvable override.

The hook and its consumer-template copy stay in sync. Literal repository overrides continue to use the existing PR check; no operator action is required.
