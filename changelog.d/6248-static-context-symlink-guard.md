<!-- changelog: security -->
- **Static prompt assembly skips symlinked checkout files.** Review, clarification, planning and implementation no longer inline a symlinked README or local agents file; targeted file context also excludes symlinked paths and Git metadata.
