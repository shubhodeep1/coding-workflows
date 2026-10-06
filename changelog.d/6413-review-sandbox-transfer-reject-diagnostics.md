<!-- changelog: fixed -->
- **Rejected review-editor transfers now name the path and rule that caused them.** When the review/autofix editor's sandbox result is refused, the `::error::` lines from `scripts/review_untrusted_workspace.py` and `scripts/review_apply_fixes.sh` now end with `reason=<reason> rule=<rule> path=<path>`. Previously they gave only the exception type, so the three failed review runs behind issue #6413 could not be traced to a cause. Which paths are allowed has not changed: `Build/`, `.github/ai/` and directory symlinks still abort the transfer, and root `.claude/` and lowercase `build/` are still skipped quietly. Before printing, the path is cleaned: characters outside `A-Za-z0-9._/+@-` become `?`, a segment that looks like a secret becomes `<redacted>`, and the result is cut to 200 characters. A file or directory name therefore cannot inject a workflow command or leak a secret.

  | Item | Value |
  |---|---|
  | Reasons | `unsafe_directory`, `unsafe_result_path`, `symlink_path`, `other` |
  | Rules | `symlink`, `invalid_name`, `excluded_name`, `excluded_case_variant`, `env_name`, `secret_name`, `secret_suffix`, `dot_subdir`, `disallowed`, `disallowed_suffix` |
  | Runtime file | `${RUNTIME_DIR}/review_sandbox_transfer_reason` (`REVIEW_SANDBOX_TRANSFER_REASON_FILE`, default unset = not written) |
  | Archived copies | `review_sandbox_transfer_reason_<attempt>.txt` and `editor_attempt_<attempt>.err` under the previous-reviews directory |

  The editor prompt now says that root dot-directories such as `.claude/` are not in its workspace and must not be recreated.

  What this means for operators: the next rejected transfer shows the offending path in the job log and in the archived attempt files, so a targeted fix can follow without reading the sandbox by hand.
