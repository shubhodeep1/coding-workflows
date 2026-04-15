# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/) per `docs/release-policy.md`.

## [Unreleased]

### Changed
- review/autofix now caches PR `closingIssuesReferences(first: 50)` once per job in `LINKED_ISSUES_JSON` and reuses it for linked-issue status/label updates and Telegram single-issue links, preserving existing PR title/body REST fallback and downstream behavior.
- Reduced default reasoning effort where deep reasoning is unnecessary:
  - `THINKING_LEVEL_CLARIFY_RESPOND`: `medium` -> `low`
  - `THINKING_LEVEL_VALIDATE`: `xhigh` -> `high`
  - `THINKING_LEVEL_CONFLICT_RESOLVER`: `xhigh` -> `medium`
- Added adaptive judge reasoning in `orchestrate_poll_process.sh`: keep `THINKING_LEVEL_JUDGE` for cycles 1-3 and force `high` from cycle 4 onward.
- Added a regression test in `tests/test_orchestrate_poll_process.py` to assert adaptive judge reasoning logic remains in place.
- Lowered implement issue-summary generation effort by temporarily overriding `model_reasoning_effort` to `low` for the summary `codex exec` invocation and restoring config afterward.
- Trimmed static prompt assembly in planning, implementation, and review/autofix workflows to reduce token overhead.
- Updated review/autofix prompt assembly to inline pre-assembled static context directly into editor/reviewer prompts (removed runtime "read pre_assembled_static.txt first" round-trip instructions).
- Updated README thinking-level defaults and judge adaptive behavior notes to match workflow/script behavior.

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
