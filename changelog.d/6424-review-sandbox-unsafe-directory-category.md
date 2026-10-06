<!-- changelog: changed -->
- **Review sandbox rejections now say which rule fired, without printing the path.** When the review editor's disposable workspace is refused at snapshot, refresh or transfer, the `::error::Review isolation snapshot or transfer rejected (ValueError)` line now ends with a fixed `reason=` token. An unsafe directory also gets a `category=` token and a bucketed `depth=`. The rejection is exactly as strict as before: no directory, symlink or file that was refused is accepted now. Before this change, the failure behind issue #6424 (PR #6288, run 37264822053) could not be traced to a rule, because the line deliberately omitted the path.

  | Field | Values |
  |---|---|
  | `reason=` | `unsafe_directory`, `entry_limit`, `size_limit`, `unsafe_result_path`, `symlink_in_path`, `unsafe_file`, `file_changed`, `host_baseline_changed`, `result_conflicts_host` |
  | `category=` (only `unsafe_directory`) | `symlink`, `invalid_name`, `dot_github_subtree`, `env_like`, `sensitive_name`, `key_material_suffix`, `excluded_name_variant`, `other` |
  | `depth=` (only `unsafe_directory`) | `1`, `2`, `3+` |

  What this means for operators: the review/autofix failure headline and the workflow-failure-heal fingerprint for this failure now carry the category. The next occurrence will show whether the editor wrote under `.github/<other>`, created a case-variant build directory such as `Build/`, or left a directory symlink. The line's prefix, the exception name and the exit codes are unchanged.

  For contributors: tokens are attached to the plain `ValueError` by `_rejection()` in `scripts/review_untrusted_workspace.py`, and `_rejection_line()` prints only tokens from fixed allowlists. Container-controlled names never reach the log.
