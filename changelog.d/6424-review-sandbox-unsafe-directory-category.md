<!-- changelog: changed -->
- **Review sandbox rejections now say which rule fired, without printing the path.** Unsafe directories emit `reason=unsafe_directory`, a fixed `category=` token and bucketed `depth=`. Annotated snapshot, refresh and transfer failures emit fixed reason tokens; unannotated errors retain their legacy reason or `unknown`. No previously refused directory, symlink or file is accepted now. Before this change, the failure behind issue #6424 (PR #6288, run 37264822053) could not be traced to a rule.

  | Field | Values |
  |---|---|
  | `category=` | `symlink`, `invalid_name`, `dot_github_subtree`, `env_like`, `sensitive_name`, `key_material_suffix`, `excluded_name_variant`, `other` |
  | `depth=` | `1`, `2`, or `3+` path segments |

  What this means for operators: the review/autofix failure headline and workflow-failure-heal fingerprint distinguish the rejection class while keeping the sandbox-controlled path out of logs. The line's prefix, exception name and exit codes are unchanged.

  For contributors: `UnsafeWorkspaceDirectory` records only the fixed category and depth bucket that `main()` emits.
