<!-- changelog: fixed -->
- **A `files_touched:` list written flush with its key is now read as the issue's allowlist instead of being rejected as malformed.**

The orchestrator judge wrote #7296's list as `files_touched:` followed by `- scripts/...` lines at the same indent. That is valid YAML, but the plan gate held it with `malformed_allowlist`, and the implement-time guard would have granted none of its `scripts/` paths. `scripts/files_touched_scope_guard.py` and `scripts/review_reject_verify.sh` now read that form. The unblock judge extends such a list at the same indent. The judge prompt (`prompts/mode-judge.txt`) now asks for the two-space form.

What this means for operators: issues with a flush list, such as #7296, get their exact grant on the next gate check. Paths the list does not name, such as `.github/workflows/ci.yml` in #7296's plan, still need a human `/approved`.
