<!-- changelog: fixed -->
- **Rejected review-editor transfers now retain validated failure diagnostics with path-free directory classification.** When the review/autofix editor's sandbox result is refused, the helper's `::error::` line includes a fixed `reason=` token for known transfer rejections; unsafe directories also include fixed `category=` and bucketed `depth=` tokens. The wrapper validates and repeats the reason, then archives both the transfer diagnostic and the attempt's stderr. Exact excluded build and cache directories are skipped; case variants such as `Build/`, `Dist/`, and `Coverage/` still abort the transfer. Unadmitted directories under `.github/` and `.claude/`, secret-like names, and directory symlinks also abort.

  | Item | Value |
  |---|---|
  | `reason=` (transfer) | `admitted_inventory_missing`, `symlink_path`, `unsafe_file`, `file_changed`, `entry_limit`, `unsafe_directory`, `unsafe_result_path`, `size_limit`, `host_baseline_changed`, `result_conflicts_host`, `transfer_rollback_failed`; unclassified errors use `unknown` |
  | `category=` (unsafe directories only) | `symlink`, `invalid_name`, `dot_github_subtree`, `env_like`, `sensitive_name`, `key_material_suffix`, `excluded_name_variant`, `other` |
  | `depth=` (unsafe directories only) | `1`, `2`, `3+` |
  | Runtime file | `${RUNTIME_DIR}/review_sandbox_transfer_reason_<output-basename>` (per editor attempt) |
  | Archived copies | `review_sandbox_transfer_reason_<attempt>.txt` and `editor_attempt_<attempt>.err` under the previous-reviews directory |

  The editor prompt identifies the specific `.claude/` and `.github/ai/` paths that are admitted and warns against creating other paths there.

  What this means for operators: the next rejected transfer identifies its reason and rule class in the job log and archived attempt files without exposing a sandbox-controlled path.
