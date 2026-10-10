<!-- changelog: changed -->
- **The workflow failure heal intake no longer files a heal issue it cannot implement, and a refused conflicted path now says what kind of path it was.**

Issue #6866 was filed with an empty scope marker, so implement refused it and nothing could fix it. Before filing a `workflow-defect`, `inconclusive` or `base-self-inflicted` issue, `scripts/workflow_failure_heal_intake.sh` now checks its own marker with `workflow_failure_heal.py heal-scope check-marker`, which uses the same parser and shape rules as implement. A missing or invalid marker files nothing: the intake logs `WORKFLOW_HEAL skip reason=scope_marker_unresolved`, sends a CRITICAL alert and fails the run, and the next report of the same failure retries.

The conflict resolver's `sandbox_path_unsupported` failure used to name no cause. It now first prints `::error::Conflict resolver: conflicted path(s) cannot enter the sandbox: unsafe=<n> host_only=<m> categories=<...> depths=<...>`, using only fixed categories (`symlink`, `symlink_in_path`, `special_file`, `unsafe_name`, `unreadable`, `other`) and `1|2|3+` depth buckets, never a path name. Admission rules, failure reasons and the fail-closed exit are unchanged.

| The numbers that matter | Value |
| --- | --- |
| `WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED` | default `true`; only `false` turns the check off |
| Marker file read cap | 8 KiB |
| Resolver report lines read | at most 500 |

What this means for operators: an unresolved heal scope now shows up as a red intake run and a CRITICAL alert instead of a stuck heal issue, and a failed conflict resolution's First error says which class of path blocked it.
