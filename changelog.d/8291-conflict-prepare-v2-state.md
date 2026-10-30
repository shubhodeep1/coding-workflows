<!-- changelog: fixed -->
- **The integration-sync conflict resolver now sees which sub-issues have merged, and its fingerprint check runs again.** `scripts/review_conflict_prepare.sh` reads the orchestrator's current `ORCHESTRATOR_STATE_V2` state instead of only the legacy V1 comment. A new switch, `CONFLICT_INTEGRATION_V2_STATE_ENABLED` (default `true`), turns this off.

When an `orchestrator/project-*` integration branch hits a merge conflict, the prepare step puts the project's merged sub-issues and their `merged_issue_fingerprints` into the resolver prompt and into the file the resolver's fingerprint check reads. It read only `ORCHESTRATOR_STATE_V1` comments, but the poller writes only the chunked V2 format. So the newest V1 comment was the initial snapshot that `orchestrate.yml` posted before anything merged: the resolver got an empty merged list and a `{}` fingerprints file, and `scripts/review_conflict_resolve.sh` skips its fingerprint check for a file that small. Tracking issues created after `orchestrate.yml` moved its first post to V2 (refs #8291) have no V1 comment at all. The script now reads the newest complete V2 chain by the pipeline login, the same way `review_conflict_resolve.sh` already does, and falls back to V1 only when there is no V2 state.

| The numbers that matter | Value |
| --- | --- |
| Fingerprints file size below which the resolver skips its check | 2 bytes (`{}`) |
| `CONFLICT_INTEGRATION_V2_STATE_ENABLED` default | `true` |
| Workflow that forwards the switch | `.github/workflows/review_autofix.yml` (merge-conflict prepare step) |

What this means for operators: integration-sync conflict resolution on orchestrator projects now gets the real list of merged work, and a resolution that breaks a merged sub-issue's fingerprint is caught and retried instead of passing unchecked. If that check starts blocking integration syncs you need to land, set the repository variable `CONFLICT_INTEGRATION_V2_STATE_ENABLED` to `false` to go back to the old V1-only read. Comments by other users are still ignored, an unavailable pipeline login still stops the run, and missing state still fails open as before.

### For contributors

The helper lookup matches `_resolver_test_repair_context`: `${SUPPORT_SCRIPTS_DIR}/orchestrate_state_v2.py`, then `${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/orchestrate_state_v2.py`, absolute paths only, no symlinks, run with `python3 -I -B`. Both conflict scripts run in the `codex-agent` job of `review_autofix.yml`, which checks out `.codex-workflow-src`. `tests/test_review_conflict_prepare_state_author.py` covers V2-only, multi-chunk, stale V1 plus V2, forged V2, missing helper, symlinked helper, relative path and switch values.
