# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/) per `docs/release-policy.md`.

## [Unreleased]

### Added
- Stable-branch release flow. `test-and-mark-stable.yml` and `mark-stable.yml` are now both restricted to dispatches from the `stable` branch — a new `source` job in each rejects any other ref with a clear error pointing at `promote-main-to-stable.yml`. This prevents accidentally tagging `main` as stable and dragging in untested in-flight work. `resolve-version` (in `test-and-mark-stable.yml`) patch-bumps from the latest `vX.Y.Z` tag reachable from `stable`'s HEAD (`git tag --merged HEAD …`), so a stable release on top of `v1.4.2` becomes `v1.4.3` even when `main` is on a higher version. The `validate` and `release` jobs check out `stable` and the GitHub Release is created with `--target stable`. The moving `stable` git tag — what consumer repos pin to via `@stable` — and the `coding-workflows-stable-released` repository_dispatch are unaffected; consumers automatically pick up stable patch releases with no changes.
- New `forward-merge-stable-to-main.yml` — on every push to `stable`, attempts a clean direct merge into `main` so bug fixes can't be lost when `main` is later promoted to stable. Falls back to opening a PR if the merge has conflicts or branch protection rejects the direct push. `ci.yml` now also runs on push/PR to `stable` so the release-gate "Verify CI passed on source branch" check has data to inspect.
- `test-and-mark-stable.yml` now has a `sync-to-main` job that dispatches `forward-merge-stable-to-main.yml` after a successful stable release. Skipped on dry runs. This is belt-and-braces — the forward-merge already runs on every push to `stable` (typically via the bug-fix PR merge), but explicitly tying a sync to the release event ensures `main` can never silently drift behind a tagged stable release.
- New `promote-main-to-stable.yml` — single-shot workflow for promoting `main` to the new stable baseline (typical for minor/major version bumps). Validates that `stable` is fast-forwardable to `main`, fast-forwards `stable` to `main`'s HEAD, and dispatches `test-and-mark-stable.yml` on the freshly-updated `stable` branch. Forwards all `test-and-mark-stable.yml` inputs (`version_tag`, `test_repo`, `skip_e2e`, `dry_run`, `phase_timeout`, `review_timeout`) for parity. Refuses to run if `stable` has commits not on `main` (operator should investigate the divergence first; do not bypass via force-push). For BUG-FIX patch releases on the existing stable line, dispatch `test-and-mark-stable.yml` directly from `stable` instead.

- Pre-flight MCP handshake probe (`scripts/mcp_handshake_probe.py`) plus `probe_mcp_handshake` helper in `scripts/setup_serena.sh`. For each enabled optional MCP server (Context7, Git), the setup script now performs a JSON-RPC `initialize` exchange before writing its `[mcp_servers.<name>]` block to `~/.codex/config.toml`. Servers that fail the probe (timeout, EOF mid-handshake, malformed/error response, id mismatch) are omitted, preventing Codex from emitting a `tools[N]` entry whose `function` field is `undefined` — the failure shape that some OpenRouter back-ends (notably Azure) reject with HTTP 400 and that previously caused `implement.yml`, `validate.yml`, and `review_autofix.yml` retries to fail. Defence-in-depth alongside the `@upstash/context7-mcp@2.1.8` pin from #1705. Gated by the new `MCP_HANDSHAKE_PROBE_ENABLED` env var (default `true`); timeout configurable via `MCP_HANDSHAKE_PROBE_TIMEOUT` (default `15`, in seconds). Reproduction harness lives at `tests/fixtures/mcp_handshake/mock_mcp_close_on_init.py` (closes connection during `initialize`); end-to-end coverage in `tests/test_mcp_handshake_probe.py` exercises probe success/timeout/EOF/spawn-failure/invalid-JSON/error-response/id-mismatch paths plus the bash-level `setup_serena.sh` gate (block written iff probe passes; opt-out via `MCP_HANDSHAKE_PROBE_ENABLED=false`).

### Changed
- `update_workflows.yml` now defaults its in-workflow `ALERT_MSG_LEVEL` env to `SILENT` (was `DEBUG`), so the per-run `🔍 DEBUG: Workflow wrappers updated in <repo> …` Telegram notification fired by `tg_send_msg "${MSG}" "DEBUG"` at the end of the update step is suppressed by `tg_helpers.sh::_tg_should_send` (msg=DEBUG=0 < threshold=SILENT=99). Consumer repo overrides still take precedence: `vars.ALERT_MSG_LEVEL=DEBUG` (or any level ≤ DEBUG) re-enables the alert, and the `alert_msg_level` `workflow_dispatch` input continues to override per-run. README's `ALERT_MSG_LEVEL` row updated to call out the exception. No code paths other than this notification are affected — `update_workflows.yml` has no other helper-based Telegram sends, and the raw-curl fallback only fires when the runtime `tg_helpers.sh` fetch from `coding-workflows@stable` fails.
- `ensure_label_exists` in `scripts/validate_process.sh` no longer routes the "label already exists, skipping" duplicate-label case through `tg_notify` (DEBUG); it now emits a local `::debug::` stderr line, matching the existing behaviour in `scripts/label_helpers.sh` and `scripts/orchestrate_poll_process.sh`. Genuine label-create failures still raise a `tg_notify` WARNING. No Telegram alert is sent for expected duplicate-label races.
- H8: made reviewer watchdog PR-state polling interval configurable via `REVIEW_PR_STATE_POLL_INTERVAL_SECS` in `scripts/review_run_reviewers.sh` (default `10`, valid `10..3600`), with `rate_limit_audit_fallback` warning and fail-open fallback to default for invalid/out-of-range inputs.
- Added H4 PR comment hydration shim in `scripts/gh_helpers.sh`: `gh_pr_with_all_comments` now uses a single GraphQL call for PR metadata + issue/review comments with deterministic ordering, mandatory fail-open REST fallback, and shared legacy JSON output contract for judge consumers.
- review/autofix now caches PR `closingIssuesReferences(first: 50)` once per job in `LINKED_ISSUES_JSON` and reuses it for linked-issue status/label updates and Telegram single-issue links, preserving existing PR title/body REST fallback and downstream behavior.
- Completed H1 migration for remaining workflow surfaces by replacing
  support-file GitHub Contents API fetch loops in `validate.yml` and
  `issue_pr_status.yml` with checkout-based staged support transport,
  preserving existing gate behavior and `${SCRIPT_REF} -> main` fallback.
- Extended staged AI memory schema lists to include the new cache schema
  entries for actions runs and workflow log analysis (best-effort staging
  until files exist on support refs).
- H7: removed repo label-existence GET probes from `ensure_label_exists`
  in `scripts/orchestrate_poll_process.sh` and `scripts/validate_process.sh`,
  now relying on direct `gh label create` with idempotent `already_exists`/422
  handling (including DEBUG skip logging) across those scripts and
  `scripts/label_helpers.sh`.
- Added H6 cross-run workflow log analysis cache persistence in
  `scripts/collect_workflow_logs.py` + `scripts/ai_memory_lib.py` using
  ai-memory branch fail-open reads/writes, ETag-aware run collection, 304
  snapshot reuse, and 500-entry LRU seen-set trimming for jobs/log archives.
- Removed all cycle-based runtime reasoning effort downgrades — every phase now
  uses the configured `THINKING_LEVEL_*` as-is (`xhigh` by default) for all cycles:
  - Removed adaptive judge downgrade (`xhigh` → `high` after cycle 3) in `orchestrate_poll_process.sh`.
  - Removed adaptive validate downgrade (`xhigh` → `high` after cycle 3) in `validate_process.sh`.
  - Removed issue-summary generation reasoning override (forced `low`) in `implement.yml`.
  - Removed `REVIEW_REASONING_SCHEDULE` / `REVIEW_AUTODOWNGRADE_DISABLED` reviewer cycle schedule machinery in `review_autofix.yml`.
- Trimmed static prompt assembly in planning, implementation, and review/autofix workflows to reduce token overhead.
- Updated review/autofix prompt assembly to inline pre-assembled static context directly into editor/reviewer prompts (removed runtime "read pre_assembled_static.txt first" round-trip instructions).
- Updated README to remove all references to adaptive reasoning, reasoning schedules, and smoke test reasoning overrides.

### Added
- `test-and-mark-stable.yml`: E2E smoke test workflow that exercises all pipeline phases
  (clarify → plan → implement → review/edit) before marking a version stable. Creates a
  test issue, polls each phase to completion, verifies success, cleans up, then proceeds
  with the standard release process. Supports `skip_e2e`, `dry_run`, configurable
  `phase_timeout`, and testing against external repos via `test_repo`.

## [v1.1.0] - 2026-03-22

### Fixed
- `memory_maintenance.yml`: replaced inline Python with CLI command to fix branch-safe persistence
- `review_autofix.yml`: fixed ghost reference to removed `ai-auto-review-and-edit.yml`
- `ai_pipeline.md`: updated stale workflow name references to current names
- `research/`: updated pre-refactoring workflow names to current names
- `docs/compatibility-matrix.md`: corrected misleading setup-runtime action references
- `cancel_on_pr_close.yml`: fixed 404 due to incorrect workflow reference
- `ci.yml`: fixed JSON validation step failing when `schemas/` dir doesn't exist

### Added
- `ci.yml`: basic CI workflow with YAML lint, Python syntax check, JSON validation, and shell lint
- `CHANGELOG.md`: release tracking per `docs/release-policy.md`
- README quickstart section with full secrets/vars reference and all workflow wrappers
- Serena MCP integration across all workflows
- Internal caller workflows so all AI workflows run on this repo
- Comprehensive repository audit report

### Changed
- Disabled URL previews in all Telegram notifications
- Consolidated `TG_ADMIN_USERID` and `TG_ADMIN_CHAT_ID` into single variable
- Added yamllint config to disable line-length, document-start, and truthy rules
