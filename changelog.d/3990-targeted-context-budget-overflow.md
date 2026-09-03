<!-- changelog: fixed -->
- **The targeted-file-context budget now bounds the whole prompt block, not just the inlined files.** Implement runs whose plan named several large files could build a prompt over the editor model's 1,048,576-character stdin cap and fail every attempt.

`scripts/targeted_file_context.py` pre-loads plan-named files into the editor prompt. When a file was too large to inline, the overflow path asked semble for chunks and rendered them, adding the rendered size to the running total only afterwards. Once the total passed `TARGETED_FILE_CONTEXT_MAX_BYTES`, nothing stopped the next overflow file from rendering another full payload, so the cap bounded the inlined files but not the emitted block. Overflow representations are now charged against the remaining headroom and rendered only when they fit; once the headroom is gone the semble subprocess is skipped and the remaining files get the existing "read with read tool" marker. The implement workflow also logs the assembled prompt size and warns when it reaches the stdin cap, so an overshoot is visible instead of surfacing as an opaque model error.

| The numbers that matter | Value |
| --- | --- |
| Budget (`TARGETED_FILE_CONTEXT_MAX_BYTES`) | 102,400 bytes |
| Block emitted before the fix (issue #3990) | ~1,164,000 bytes |
| Overflow content rendered before the fix | 1,281,280 bytes across 10 files |
| Editor stdin cap | 1,048,576 characters |
| Failing run | 33796624872 |

What this means for operators: implement, review-autofix, and conflict-resolver runs on plans that name large files no longer die with `turn/start failed: Input exceeds the maximum length of 1048576 characters` and the downstream "no actionable output" bail. The editor reads those files with its own read tool instead, which is the behaviour the marker path always intended.

### For contributors

The read-fallback branch already computed remaining headroom and emitted a marker at zero; the semble branch simply lacked the same guard. New telemetry reason code `budget-exhausted` on `SEMBLE_FALLBACK` distinguishes a budget rejection from a semble failure. Two contract tests in `tests/test_targeted_file_context.py` pin the whole-block bound and the skip-the-subprocess-at-zero-headroom behaviour.
