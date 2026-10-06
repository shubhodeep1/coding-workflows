<!-- changelog: fixed -->
- **Rejected review-editor transfers now archive the reason and report the rule class without exposing untrusted paths.** When the review/autofix editor's sandbox result is refused, the helper's `::error::` line includes a fixed `reason=` token for known rejections; unsafe directories also include fixed `category=` and bucketed `depth=` tokens. The editor wrapper repeats the reason from the transfer-reason file and archives it alongside the attempt's stderr. These diagnostics help identify the kind of rejected directory without printing container-controlled names. Excluded build and cache directories such as `Build/`, `Dist/`, and `Coverage/` are skipped case-insensitively. Directories under `.github/` other than `workflows/` and `actions/`, secret-like names, and directory symlinks still abort the transfer; root `.claude/` is still skipped.

  | Item | Value |
  |---|---|
  | `reason=` | `unsafe_directory`, `entry_limit`, `size_limit`, `unsafe_result_path`, `symlink_in_path`, `unsafe_file`, `file_changed`, `host_baseline_changed`, `result_conflicts_host` |
  | `category=` (unsafe directories only) | `symlink`, `invalid_name`, `dot_github_subtree`, `env_like`, `sensitive_name`, `key_material_suffix`, `excluded_name_variant`, `other` |
  | `depth=` (unsafe directories only) | `1`, `2`, `3+` |
  | Runtime file | `${RUNTIME_DIR}/review_sandbox_transfer_reason` (`REVIEW_SANDBOX_TRANSFER_REASON_FILE`, default unset = not written) |
  | Archived copies | `review_sandbox_transfer_reason_<attempt>.txt` and `editor_attempt_<attempt>.err` under the previous-reviews directory |

  The editor prompt now says that root dot-directories such as `.claude/` are not in its workspace and must not be recreated.

  What this means for operators: the next rejected transfer identifies the failure class in the job log and archived attempt files, without claiming to identify the exact path.
