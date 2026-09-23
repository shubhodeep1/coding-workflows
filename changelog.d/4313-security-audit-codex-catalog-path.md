<!-- changelog: fixed -->
- **The weekly Security Audit runs again in coding-workflows itself.** From 2026-07-05 every run died at the Codex call with `Error: No such file or directory (os error 2)`, because Codex was handed a relative model-catalog path.

`.github/workflows/security-audit.yml` passed `--catalog-path "${SECURITY_AUDIT_SUPPORT_DIR:-.}/scripts/codex_model_catalog.json"`. In the source repo the support dir is unset, so `./scripts/codex_model_catalog.json` was written into `~/.codex/config.toml` as `model_catalog_json`, and Codex resolves a relative value there against `CODEX_HOME` rather than the working directory. The workflow now falls back to `${GITHUB_WORKSPACE}`, and `scripts/write_codex_config.sh` turns any relative `--catalog-path` into an absolute path under the caller's working directory before writing it, so no other caller can hit the same failure.

| The numbers that matter | Value |
| --- | --- |
| Consecutive failed runs | 13 (2026-07-05 to 2026-09-23, last one workflow_dispatch run 35821734999) |
| Introduced by | PR #3575 |
| Codex CLI version reproduced on | 0.114.0 |

What this means for operators: the scheduled `Security Audit` in this repo needs no action and will run on its next Sunday 08:00 UTC slot. Consumer repos calling the reusable workflow were not affected, since their support dir is an absolute `$RUNNER_TEMP` path.
