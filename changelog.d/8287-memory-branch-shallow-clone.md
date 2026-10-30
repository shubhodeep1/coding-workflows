<!-- changelog: fixed -->
- **Memory writes now clone only the `ai-memory` branch tip, so memory maintenance finishes inside its 20-minute limit again.** Release v1.40.3 failed because the `internal-memory-maintenance` run that the release gate's `orphan-workflows-test` job dispatches (run 40752760153) hit its job timeout and was cancelled.

Every memory write (`record-candidate`, `promote`, compaction and the other `persist_memory_operation` callers in `scripts/ai_memory.py`) used to run a full `git clone` of the repository: more than 4,000 branches plus the whole `ai-memory` history of over 260,000 commits. Each clone took 135 to 209 seconds in CI, and the "Extract repository learnings (fail-open)" step makes two writes per learning, so three learnings used up the job. `_clone_for_memory_branch` in `scripts/ai_memory_lib.py` now clones only the memory branch tip (`--single-branch --depth 1 --branch ai-memory`), the same way the memory read path and the `memory_helpers.sh` force-tick helpers already do. When the branch does not exist yet, or the shallow clone fails, it falls back to the previous full clone unchanged. The push-retry fetch and rebase keep working from the shallow clone. The fallback is silent by default, because in-process callers such as the integration fingerprint verifier expect an empty stderr; set `AI_MEMORY_CLONE_DIAGNOSTICS=1` to print one redacted line when the branch probe or the shallow clone fails.

The extraction step in `.github/workflows/memory_maintenance.yml` also gets `timeout-minutes: 10`. Before, a slow extraction ran into the job timeout, so `continue-on-error` never applied and "Run memory compaction" never started. Now a slow extraction fails only its own step and compaction still runs.

| The numbers that matter | Value |
| --- | --- |
| Clone time per memory write, before (run 40752760153) | 135 to 209 s |
| Shallow `ai-memory` clone, measured against this repository | about 25 s |
| Extraction step cap | 10 min, inside the unchanged 20-minute job limit |

What this means for operators: memory maintenance and the release gate's `orphan-workflows-test` stop timing out on memory clones, and every other workflow that writes memory (clarify, plan, implement, review) spends less time cloning. Refs #8287.
