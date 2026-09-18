<!-- changelog: fixed -->
- **The implement phase no longer overflows codex-cli's prompt cap when the Semble overflow fallback fires.** Targeted-file-context chunk blocks are now clamped to the same total byte budget as inlined files, and `implement.yml` logs the assembled prompt size so an overshoot is visible instead of opaque.

An orchestrator security-pass fix issue was closed without a merged pull request because every implementation attempt died before the editor ran. `scripts/targeted_file_context.py` renders Semble chunks for files too large to inline, but chunk size is set by the search index rather than by the file that triggered the query, and the rendered block was never clamped to the remaining budget. One 11,800-byte source file pulled in a 129,000-byte block against a 102,400-byte budget, and each subsequent overflowing path added another unclamped block on top. The assembled prompt passed codex-cli's 1,048,576-character `turn/start` stdin cap, so both attempts failed with `Input exceeds the maximum length`, the retry loop read that as an ordinary no-actionable-output bail, stall recovery retriggered the same deterministic failure twice, and the stall judge closed the issue. The poller then correctly fail-closed the project on `ai:security-pass-failed`.

| The numbers that matter | Value |
| --- | --- |
| Semble block size per overflowing file, before | ~129,000 bytes |
| `TARGETED_FILE_CONTEXT_MAX_BYTES` default | 102,400 bytes |
| Overflow blocks emitted in one prompt | 6+ |
| codex-cli `turn/start` stdin cap | 1,048,576 characters |
| Affected implement run / issue | 33796624872 / #3990 |

What this means for operators: `TARGETED_FILE_CONTEXT_MAX_BYTES` is now enforced as a true total across inlined files and Semble chunk blocks alike. A clamped block says so in its header and tells the model to read the rest with its read tool, and once the budget is spent no further Semble query is issued. The implement job also prints `Implement prompt assembled size: NN characters (MM bytes)` on every run and raises a workflow warning when the character count is at or over the cap, so a future overshoot is diagnosable from the run log rather than from a stalled issue.

### For contributors

The new `IMPLEMENT_PROMPT_CODEX_STDIN_CAP_CHARS` repo variable (default `1048576`) only tunes that warning threshold; it never truncates the prompt and never fails the step. The guard compares in characters because that is the unit codex-cli enforces; `wc -m` runs under `C.UTF-8`, and a non-UTF-8 locale degrades it to a byte count, which is never smaller than the character count and so can only warn early. The clamp is the prevention, the warning is the diagnosis. This mirrors the guards already in `scripts/review_run_reviewers.sh` and `scripts/review_rb_judge.sh`. The issue-comment block in the implement prompt remains uncapped and is a separate latent overflow source.
