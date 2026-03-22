# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/) per `docs/release-policy.md`.

## [Unreleased]

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
