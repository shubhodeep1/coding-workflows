<!-- changelog: fixed -->
- **Memory maintenance no longer times out on full-repository clones.** Memory writes now clone only the existing `ai-memory` branch, and learnings extraction has a time limit of its own.

Every memory write used to clone the whole host repository before switching to `ai-memory`. In release gate run 40746491161 those clones took 135 to 209 seconds each, two per extracted learning. The `internal-memory-maintenance` run hit its 20-minute job timeout and was cancelled, which failed `orphan-workflows-test` and blocked the release. `_clone_for_memory_branch` in `scripts/ai_memory_lib.py` now checks for the memory branch and clones only that branch, with its full history. If the branch is missing, or the single-branch clone fails, it falls back to the previous full clone. The "Extract repository learnings (fail-open)" step in `.github/workflows/memory_maintenance.yml` now has `timeout-minutes: 10`, so a slow extraction fails only that step, `continue-on-error` applies, and memory compaction still runs.

| The numbers that matter | Value |
| --- | --- |
| Job timeout (unchanged) | 20 minutes |
| New extraction step timeout | 10 minutes |
| Full clone per write in run 40752760153 | 135 to 209 s |

What this means for operators: memory maintenance finishes inside its job budget again, including when the release gate starts it, and a slow extraction no longer cancels the run. Memory writes from every other workflow get the faster clone too.

### For contributors

The fallback path is the previous code, unchanged, so a missing `ai-memory` branch is still created from the default branch. A failed `ls-remote` probe costs one extra round trip and then behaves as before. Tests: `tests/test_ai_memory_git_env_isolation.py` (single-branch clone, fallback) and `tests/test_memory_maintenance_contract_noop.py` (step timeout below the job timeout).
