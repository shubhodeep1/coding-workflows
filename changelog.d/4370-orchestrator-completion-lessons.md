<!-- changelog: added -->
- **Lessons learned now reach the prompts that plan, implement and review, and the orchestrator writes a lessons retrospective when every project completes, clean ones included.**

Lesson records from the judge, the review-blocked judge and review autofix were saved to the `ai-memory` branch but never read: memory retrieval only loaded candidate and canonical records. The planning, implementation and reviewer prompts now get a `LESSONS LEARNED` block with up to 5 of the newest lessons whose text or tags match the issue, and the block uses at most a quarter of the role's memory budget. The orchestrator also stops learning only from failures. `scripts/orchestrate_poll_process.sh` records causes as they happen: a judge fix-up issue, a validation `needs_fixes` diagnosis, reported security findings, and a stall recovery. When the project completes, it writes one lesson per cause type, plus a summary of the recovery, stall, review-blocked, validation and security counters. The wave judge skips clean projects, but this retrospective still runs for them; a project with nothing to report writes nothing.

| The numbers that matter | Value |
| --- | --- |
| Roles that see lessons | `planning`, `implementation`, `reviewer` |
| Lessons per prompt | at most 5, newest keyword matches first |
| Budget share | at most 25% of the role's token budget, only when lessons match |
| Causes kept in orchestrator state | newest 20 `lesson_events`, 400 characters each |
| New GitHub API calls | none |
| Kill switches | `AI_MEMORY_ENABLED`, `LESSONS_LEARNED_ENABLED` (default `true`) |

What this means for operators: planners and reviewers now see what earlier projects learned, and each orchestrator project adds a short record of what went wrong on the way to done. A lesson whose text trips the memory prompt-injection patterns is never shown to a prompt. Set `LESSONS_LEARNED_ENABLED=false` in the job environment to turn lessons off.

### For contributors

Retrieval lives in `retrieve_memory_context` (`scripts/ai_memory_lib.py`). With no matching lesson its output is unchanged, and the retrieve JSON and telemetry gain `lessons_selected`. Completion lessons are built by `orchestrate_lib.build_completion_lessons` and written by `emit_orchestrator_completion_lessons` on all four `status = "complete"` paths. They use phase `orchestrator_completion`, kind `project_retrospective` and deterministic record ids, so a repeated completion tick writes nothing.
