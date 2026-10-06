<!-- changelog: security -->
- **Scope overrides cannot authorize protected automation paths.** The unblock judge now refuses the entire verdict when any requested override reaches protected automation code.

Paths under `.github/`, `.claude/` or `workflow-templates/` are rejected in every repository for both scope-blocked and destructive-blocked issues. In this repository, `scripts/` remains forbidden, while destructive overrides continue to refuse canonical workflow sources. Consumer repositories can still approve other scope paths, but mixing an allowed path with a protected path cannot partially authorize the verdict.
