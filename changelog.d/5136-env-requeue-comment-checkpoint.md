<!-- changelog: security -->
- **Flooding a blocked Claude issue with comments no longer switches off its automatic re-queue.** The queue watchdog now reads a long comment thread across several hourly runs instead of giving up on it for good.

The environment re-queue step of `claude-issue-queue-watchdog.yml` reads every comment of an `ai:claude-blocked` issue to find its latest blocker. It used to stop at 1,000 comments and skip the issue on every run, so anyone able to comment could disable recovery for that issue (#5136). The scan now keeps a checkpoint in the Actions cache: the page and id of the last comment read, plus the trusted marker and `/reclarify` comments seen so far. Each run reads up to 1,000 more comments from there, and the watchdog decides only once it has reached the end of the thread. A missing or unusable checkpoint restarts the scan from the first page, as before.

| The numbers that matter | Value |
| --- | --- |
| Comments read per issue per hourly run | up to 1,000 (10 pages), resumed on the next run |
| Reads per run for a quiet, fully scanned issue | 1 |
| Checkpoint | `CLAUDE_ISSUE_ENV_REQUEUE_CHECKPOINT`, cache key `claude-env-requeue-checkpoint-<run id>-<attempt>` |
| New log lines | `env_requeue_scan_pending`, `env_requeue checkpoint status=<loaded, missing, invalid, off>` |

What this means for operators: a flooded issue now recovers after a delay of one extra hourly run per 1,000 comments, and one more per 1,000 comments deleted before the scan's position. `env_requeue_scan_pending` in the watchdog log means a scan is still in progress, not that it failed. The watchdog never acts on a partly read thread.

### For contributors

`env-requeue-plan --checkpoint <file>` loads and atomically rewrites the checkpoint. Without the flag, the command behaves exactly as before, including the 1,000-comment error. The plan JSON gains `pending` and, from the CLI, `checkpoint`. The scan (`_scan_issue_comments` in `scripts/claude_issue_route.py`) re-reads the cursor page on resume and steps back when deleted comments have moved the page boundaries, so no comment is skipped.
