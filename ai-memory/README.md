# AI Memory System

This directory defines canonical memory for GitHub AI workflows.

## Branch and storage model

Persistent writes are stored on a dedicated branch (`ai-memory`) and use this layout:

- `global/canonical/<category>/*.json`: validated canonical memory records
- `tasks/issue-<n>/candidates/*.json`: per-task candidate records
- `tasks/issue-<n>/lineage/task_lineage.v1.json`: issue/PR/run lineage state
- `runs/<run-id>/ledger/events.jsonl`: per-run event ledger
- `archive/monthly/<YYYY-MM>/...`: monthly archival snapshots
- `schemas/*.json`: versioned JSON schemas
- `config/retrieval_profiles.v1.json`: fixed role budgets and ranking weights

## Record lifecycle

1. Workflows append run events (`record-run-event`).
2. Workflows write candidate records (`record-candidate`).
3. Promotion (`promote`) validates schema + governance checks:
- confidence threshold
- provenance presence
- sensitive-category source refs
- fingerprint duplicate prevention
4. Passing candidates become canonical `active` records.
5. Records can supersede previous canonicals using lineage `supersedes`.
6. Finalization (`finalize-task`) updates issue lineage on merge/close/cancel.

## Governance

Sensitive categories include `incidents`, plus security/reliability and high-impact decision content inferred from summary/details.

Fail policy:

- Retrieval/candidate failures are fail-open at workflow level (logged in run ledger).
- Promotion/finalization failures are fail-closed.

## Deterministic retrieval

`retrieve` scores records by:

- role-specific category weights
- confidence
- issue/PR scope boosts
- active-status boost

Selection is deterministic and bounded by fixed per-role token budgets and `max_records`.

## Retention and compaction

Canonical memory is retained indefinitely.
Candidate and run-ledger tiers can be archived monthly with `compact --month YYYY-MM`.

`compact --prune true` removes already-promoted/rejected/superseded candidates and archived run ledgers after copying to archive.

## Interfaces

CLI entrypoint: `.github/scripts/ai_memory.py`
Library: `.github/scripts/ai_memory_lib.py`

Workflows must use CLI subcommands and avoid duplicated inline memory logic.

## Environment variables

- `AI_MEMORY_ENABLED` (default `true`)
- `AI_MEMORY_BRANCH` (default `ai-memory`)
- `AI_MEMORY_ROOT` (default `.github/ai-memory`)
- `AI_MEMORY_RETRIEVAL_PROFILES` (default `.github/ai-memory/config/retrieval_profiles.v1.json`)
- `AI_MEMORY_PUSH_RETRIES` (default `5`)
