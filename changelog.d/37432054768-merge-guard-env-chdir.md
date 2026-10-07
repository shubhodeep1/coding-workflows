<!-- changelog: fixed -->
- **The merged-PR guard now asks for confirmation when `env -C` names an unresolved commit directory.**

An `env -C` wrapped `git commit` no longer checks the session checkout when the requested directory is missing or cannot be resolved. That checkout might be a different branch, so its PR history cannot authorize the commit. Other ambiguous shell-control commits retain their warning-only behavior; unresolved pushes still check the session checkout before requesting confirmation. Consumer repos receive the matching live and template hook changes at the next `@stable` sync.

What this means for operators: an unresolved explicit `env -C` directory cannot be mistaken for a verified checkout during the merged-PR check.
