# Checkpoint the environment re-queue comment scan so a comment flood cannot disable recovery

Source issue: shubhodeep1/coding-workflows#5136 (https://github.com/shubhodeep1/coding-workflows/issues/5136)
Base branch: claude/implement-plan-issue-4938-environment-blocker-self-heal
Security pass: skip (ai:security: automation-produced issue)

## Summary

The queue watchdog's environment re-queue (issue #4938) reads every comment of a blocked Claude issue and refuses to decide once a thread passes 1,000 comments. Anyone who can comment can therefore flood a blocked issue and switch its automatic recovery off for good. This plan checkpoints the verified scan state between hourly runs so pagination resumes past the per-run cap, and the watchdog still never decides from an incomplete thread.

## Context

- `scripts/claude_issue_route.py` `_read_issue_comments` (base branch, around line 1230) reads at most `ENV_REQUEUE_COMMENT_PAGES_MAX` (10) pages of 100 comments. On a full tenth page it raises `RuntimeError("… has more than 1000 comments; the latest blocker is past the read cap")`, and `env_requeue_plan` lists the issue under `errors` and skips it. The next hourly run starts again from page 1 and fails the same way, forever.
- The comments endpoint lists oldest first and takes no sort, so the latest blocker is on the last page. The fixed cap exists so the watchdog never decides on an older blocker (#4938 design). That property must stay.
- `env_requeue_decision` only reads **trusted** comments that carry a blocker marker, a re-queue marker, an exhausted marker, or a trusted `/reclarify`. Every other comment is irrelevant to the decision, so the state worth checkpointing is small.
- The watchdog runs hourly from `.github/workflows/claude-issue-queue-watchdog.yml` (only in coding-workflows), step "Re-queue Claude issues blocked by an environment failure", which runs `scripts/claude_issue_queue_watchdog.sh` in `env-requeue` mode. That step calls `claude_issue_route.py env-requeue-plan`.
- The base branch is the #4938 project branch (final PR #5000, still open), because this finding is a follow-up from that project's security audit (`Refs #3576`).

## Goals

- A thread with more than 1,000 comments is read across successive hourly runs from a persisted cursor, and it is decided once the scan reaches the end of the thread.
- No decision is ever made from a partially read thread: an issue whose scan is still in progress is reported as pending and left alone for that run.
- The decision for a fully scanned thread is identical to the decision the current code makes when reading the whole thread in one run.
- Deleted comments before the cursor (which shift page boundaries) never cause a comment to be skipped.
- Per-run API cost per blocked issue stays at most `ENV_REQUEUE_COMMENT_PAGES_MAX` comment reads (§15).
- A missing, expired, or corrupt checkpoint fails open: the scan restarts from page 1, as today.

## Non-goals

- No change to `env_requeue_decision` semantics, the markers, retry counts, the window, or the alert.
- No change to the search stage (`env_requeue_search_queries`, its paging cap, its errors).
- No change to the queue-stale mode of the watchdog.
- Not defending against an adversary who can post more comments per hour than the per-run budget reads (see Risks).

## Constraints

- §1 security first; §5 minimal change set: the fix lives in the env-requeue read path, the CLI, the watchdog step, and their tests and docs only.
- §6 naming immutability: `_read_issue_comments`, `env_requeue_plan`, `ENV_REQUEUE_COMMENT_PAGES_MAX`, the `env-requeue-plan` CLI flags, the plan JSON keys (`actions`, `skipped`, `errors`, `searches`), and the `CLAUDE_ISSUE_QUEUE_WATCHDOG` log keys stay as they are. New names are additive and checked for collisions.
- §4 new env var `CLAUDE_ISSUE_ENV_REQUEUE_CHECKPOINT` defaults to empty (stateless, today's behaviour).
- §9 tabs in Python and shell, 2-space YAML.
- §15 no new API call beyond the comment pages already read; the batching contract in `env_requeue_plan`'s docstring is updated.
- §18 no manual script: the checkpoint is wired into the existing hourly watchdog workflow.
- §20 one changelog fragment.
- §27 the watchdog workflow stays far below the 480,000-byte guard.

## Approach

1. **Checkpoint state per issue** (`owner/repo#N`, lower-cased): `page` (the page holding the last consumed comment), `last_id` (id of the last consumed comment), `relevant` (the compact trusted decision-relevant comments seen so far), `complete`, and `updated_at`. A file holds `{"version": 1, "issues": {…}}`.
2. **Resumable scan** (`_scan_issue_comments`): starting from the checkpoint, re-read the cursor page. If it is past page 1 and its first comment is newer than `last_id` (or the page is empty), earlier comments were deleted and the page boundaries moved, so step back one page. This check runs only on the resume read. Then read forward, keeping only comments newer than `last_id`, until a short page ends the thread or the per-run budget (`ENV_REQUEUE_COMMENT_PAGES_MAX` reads) runs out. Progress is written into the state after every page, so a read error keeps what was consumed.
3. **Decide only on a complete scan.** `env_requeue_plan` gains an optional `checkpoint` argument. With it, a complete scan feeds `relevant` to `env_requeue_decision`; an incomplete scan lands in a new `pending` list (`repo`, `issue_number`, `issue_url`, `cursor_page`) and no action. A scan always gets at least two reads per run, because the resume read of a full cursor page adds nothing new. Without it, the function behaves exactly as today (the stateless read and its cap error).
4. **Prune**: entries for issues that are no longer candidates are dropped, unless a search failed in this run (then the old entries are kept).
5. **Persistence**: `env-requeue-plan --checkpoint <file>` loads and atomically rewrites the file. The watchdog workflow restores the latest `claude-env-requeue-checkpoint-` Actions cache entry before the step and saves a new entry (key suffixed with the run id and attempt) after it. Both cache steps are `continue-on-error`. The shell passes `--checkpoint` when `CLAUDE_ISSUE_ENV_REQUEUE_CHECKPOINT` is set, and logs each pending issue as `env_requeue_scan_pending`.

Alternatives: see AD-1 to AD-5.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for a standalone issue.

1. **Phase 1 — checkpointed comment scan, wired into the watchdog.**
   - Files: `scripts/claude_issue_route.py`, `scripts/claude_issue_queue_watchdog.sh`, `.github/workflows/claude-issue-queue-watchdog.yml`, `tests/test_claude_issue_route.py`, `README.md`, `agents.md`, `changelog.d/5136-env-requeue-comment-checkpoint.md`.
   - Done when: a flooded thread (more than 1,000 comments) is decided after enough runs through the checkpoint, never before; the deletion step-back and the equivalence with a one-shot read are tested; the watchdog passes the checkpoint and restores and saves it; the full test suite passes.
   - Rollback: revert the PR. An orphaned cache entry is harmless and expires after 7 days without access.

## Implementation Steps

Phase 1:

1. `scripts/claude_issue_route.py`: add `ENV_REQUEUE_CHECKPOINT_VERSION`, `ENV_REQUEUE_CHECKPOINT_BODY_MAX`, `_env_relevant_comment` (compact a trusted decision-relevant comment or return `None`), `_scan_issue_comments` (resumable scan), `load_env_requeue_checkpoint` / `save_env_requeue_checkpoint` (validated, fail-open load; atomic write), and the `checkpoint` argument, `pending` output, and pruning in `env_requeue_plan`. Update its batching-contract docstring.
2. Same file: `env-requeue-plan --checkpoint <file>`; the CLI output gains `checkpoint` (`loaded` / `missing` / `invalid` / `off`).
3. `scripts/claude_issue_queue_watchdog.sh`: document and read `CLAUDE_ISSUE_ENV_REQUEUE_CHECKPOINT` (default empty); pass `--checkpoint`; log `env_requeue_scan_pending` per pending issue and `env_requeue checkpoint status=…`.
4. `.github/workflows/claude-issue-queue-watchdog.yml`: `actions/cache/restore@v5` before and `actions/cache/save@v5` after the env-requeue step (both `continue-on-error: true`), and set `CLAUDE_ISSUE_ENV_REQUEUE_CHECKPOINT` on the step. The stale check stays the last step.
5. `tests/test_claude_issue_route.py`: the tests listed below.
6. `README.md` and `agents.md`: replace the "more than 1,000 comments … left alone" sentence with the checkpoint behaviour; `changelog.d/5136-env-requeue-comment-checkpoint.md` (`security`).

## Files & Modules

- `scripts/claude_issue_route.py`
- `scripts/claude_issue_queue_watchdog.sh`
- `.github/workflows/claude-issue-queue-watchdog.yml`
- `tests/test_claude_issue_route.py`
- `README.md`
- `agents.md`
- `changelog.d/5136-env-requeue-comment-checkpoint.md` [new]

## Tests

Unit (`tests/test_claude_issue_route.py`):
- A 1,050-comment thread whose latest blocker is on page 11: the first run reports it `pending` with no action, and the second run (checkpoint carried over) re-queues it on the latest blocker.
- A thread whose earlier comments were deleted between runs: the step-back re-reads the earlier page and no comment is skipped.
- Equivalence: for varied threads, the decision from the compact `relevant` list equals the decision from the full comment list.
- Checkpoint load: missing file, invalid JSON, wrong version, and malformed entries all start from page 1; save is atomic and round-trips.
- Pruning: non-candidates are dropped, and kept when a search failed.
- The existing stateless cap test keeps passing unchanged (no `checkpoint`).
- CLI: `env-requeue-plan --checkpoint` creates and updates the file.
- Shell: with `CLAUDE_ISSUE_ENV_REQUEUE_CHECKPOINT` set, the watchdog passes the checkpoint and logs `env_requeue_scan_pending`.
- Workflow: the restore step precedes and the save step follows the env-requeue step, both `continue-on-error`, and the step sets the checkpoint path.

Full suite: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q`.

## Risks & Mitigations

- An attacker who posts more than about 1,000 comments per hour to one issue keeps the scan pending indefinitely. ACCEPTED: GitHub's content-creation limits (about 500 per hour per account) keep one account below the per-run budget, the failure mode is a delayed decision rather than a wrong one, and every pending run is logged.
- A trusted comment edited or deleted after it was consumed is not re-read. ACCEPTED: blocker and re-queue markers are posted by automation and not edited; a deleted marker only makes the watchdog more conservative for the exhausted case.
- A trusted author's association that changes after consumption keeps its earlier verdict. ACCEPTED: the state is the verified state at read time, as the issue asks.
- Cache eviction or a corrupt checkpoint: the scan restarts from page 1, as today (fail open).
- Cache poisoning: only default-branch runs write cache entries that the scheduled run restores, and the loader validates every field.

## Rollout

Ships with the #4938 project's final PR (#5000) into `main`. The first scheduled run after that restores nothing and starts every scan from page 1. No flag, no migration. Roll back by reverting.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where should the checkpoint persist between hourly runs? — Picked: A — the Actions cache in the watchdog workflow (restore the latest `claude-env-requeue-checkpoint-` entry, save a new one per run). Alternatives: B — a hidden checkpoint comment on each issue; C — a commit to a state branch. Why: no new permission or write surface; B needs a thread read to find it, the very thing that fails; C needs `contents: write`. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How should pagination resume safely? — Picked: A — a page cursor (page and last comment id) that re-reads the cursor page and steps back when earlier deletions moved page boundaries. Alternatives: B — a `since=` timestamp cursor; C — remove the cap and read the whole thread every run. Why: B lets edited old comments and id ordering skip comments across a partial read; C makes a flood cost unbounded API calls every hour (§15). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] What should the checkpoint store? — Picked: A — only trusted, decision-relevant comments (markers and trusted `/reclarify`) with the fields `env_requeue_decision` reads, bodies trimmed to 256 characters after the marker's leading whitespace. Alternatives: B — every comment. Why: the decision provably ignores every other comment, and B grows without bound. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] How is a scan still in progress reported? — Picked: A — a new additive `pending` list in the plan JSON, logged as `env_requeue_scan_pending`, with no action. Alternatives: B — keep reporting it under `errors` (`env_requeue_read_failed`). Why: it is expected progress, not a failure, and the existing keys stay unchanged (§6). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] What does `env-requeue-plan` do without `--checkpoint`? — Picked: A — exactly today's stateless read and cap error. Alternatives: B — an in-memory checkpoint that reports `pending`. Why: backward compatibility for any other caller and the existing tests (§6); the watchdog always passes the flag. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` verified the issue as automation-produced (`ai:security: created and labelled by the issue automation`).

## References

- Issue #5136 (this finding), #4938 (environment blocker self-heal) and its final PR #5000, security audit tracker #3576.
