<!-- changelog: fixed -->
- **Clarify stops after one attempt when the isolated runner rejects the source snapshot, instead of retrying the same rejection three times.**

`scripts/clarify_isolated_run.sh` copies the checkout into the clarify container before it starts any engine. When it refuses that copy it prints the fixed line `clarify source snapshot rejected` and exits 1, and the checkout does not change between attempts, so every retry gets the same answer. Before this change the `Run Codex` step of `.github/workflows/clarify.yml` retried it three times with backoff (about 46 seconds of waiting on run 44703147149 for issue #9835). The step now captures each attempt's stderr on both the Claude and the codex path. When a failed attempt's stderr holds that exact line, it prints `::error::<engine> clarify stopped after attempt <n>: scripts/clarify_isolated_run.sh rejected the source snapshot, and a retry gets the same result.` and fails the step at once. Every other failure keeps the three attempts. The step still fails with exit code 1, and the `CLARIFY_ENGINE_ATTEMPT_FAILED` line is unchanged. The helper prints the same line when a tracked file changes while it is being copied (`source changed during snapshot`). The step now stops on that case too. Nothing writes to the checkout during clarify, so a retry is not expected to clear it.

What this means for operators: a clarify run blocked by its source snapshot fails sooner, and its last error line names the helper, so a heal report points at it. The underlying rejection still fails the run until the helper change below lands.

### Deferred, outside this heal's scope

The heal scope of #9837 did not cover `scripts/clarify_isolated_run.sh`, so these changes still need their own issue:

- Print one stderr line `CLARIFY_SNAPSHOT_REJECTED reason=<token>`, with the token from a fixed set (`unsafe_path`, `unsafe_file`, `oversized`, `limit_exceeded`, `changed`, `missing`, `duplicate`, `undecodable`, `os_error`), and never a file's content or an unsanitised path.
- Exit with a distinct code on a snapshot rejection, and have `clarify.yml` stop retrying on that code as well as on the fixed line. Once the reason token exists, the stop rule can leave `reason=changed` to the normal retries.
- Skip and count tracked paths missing from the worktree in the legacy snapshot set too; today only the all-text pass-2 set skips them. Tracked files over 2 MiB are already skipped and counted (`CLARIFY_SNAPSHOT_EXCLUDED ... oversize=<n>`) while `CLARIFY_SNAPSHOT_ALL_TEXT_ENABLED` is on (the default), and still reject the snapshot when it is `false`. Keep rejecting non-regular files, symlinked parent directories, a source that changes during the copy, and the 5,000-file and 64 MiB caps.
- Add a snapshot regression test that builds a temporary git repository and shows that an oversized tracked file is skipped and a symlinked parent directory is still rejected with `reason=unsafe_path`.
- Give `.github/workflows/orchestrate_clarify_respond.yml`, which calls the same helper in its own retry paths, the same stop-on-rejection rule.

### For contributors

`tests/test_clarify_snapshot_reject_no_retry.py` runs the attempt loop with a stub helper and checks that the helper's `SystemExit` text and the line the workflow matches are the same string, so a wording change in the helper fails CI instead of silently bringing the retries back. The match is on the whole line only (`grep -a -qx`); a forged line can only make the run fail sooner.
