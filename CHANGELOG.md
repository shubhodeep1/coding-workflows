# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/) per `docs/release-policy.md`.

## [Unreleased]

### Changed
- review/autofix now caches PR `closingIssuesReferences(first: 50)` once per job in `LINKED_ISSUES_JSON` and reuses it for linked-issue status/label updates and Telegram single-issue links, preserving existing PR title/body REST fallback and downstream behavior.
- Completed H1 migration for remaining workflow surfaces by replacing
  support-file GitHub Contents API fetch loops in `validate.yml` and
  `issue_pr_status.yml` with checkout-based staged support transport,
  preserving existing gate behavior and `${SCRIPT_REF} -> main` fallback.
- Extended staged AI memory schema lists to include the new cache schema
  entries for actions runs and workflow log analysis (best-effort staging
  until files exist on support refs).
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
