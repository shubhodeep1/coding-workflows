<!-- changelog: changed -->
- **`/implement-plan-claude` now checks the merged project against its plan before the security pass, and the lessons each project learns reach AI memory.**

Until now, the audit that maps every plan acceptance criterion to the merged code ran only after the completion PR had moved the plan to `docs/completed/`, so its fix PRs skipped the security audit and runtime validation. A new `conformance <k>/3` stage now runs `/verify-activation — scope conformance` right after the last phase merges, before the security pass. It runs again before completion if validation needed a Claude-written fix. After completion, the `verify-activation` stage checks only activation (`— scope activation`). A project that was already in flight has no conformance run on record, so it gets the full check at the end, as before. Each stage also records surprises (a conformance finding, a security follow-up, a validation fix, a blocked-PR intervention, a departure from the plan) in a new `## Lessons` section of `docs/implement-plan/<slug>.md`. `issue_pr_status.yml` copies those lessons into the `ai-memory` branch when the PR carrying them merges.

| The numbers that matter | Value |
| --- | --- |
| Conformance runs per project | at most 3, shared by the pre-security and post-validation runs |
| New `/verify-activation` scopes | `— scope conformance`, `— scope activation` (no scope: full check, unchanged) |
| Ingestion trigger | merge of a `claude/implement-plan-*` or `claude/verify-activation-*` PR |
| Lesson record | `lessons_learned_record.v1`, phase `implement_plan`, kind `project_retrospective` |
| Kill switches | `AI_MEMORY_ENABLED`, `LESSONS_LEARNED_ENABLED` (repo vars, default `true`) |

What this means for operators: conformance defects are now fixed before the security audit and validation, and before the plan is archived, so no unaudited fix lands after the project is marked done. Lesson ingestion is fail-open and never turns the `AI Issue PR Status Sync` job red. Set `LESSONS_LEARNED_ENABLED=false` to stop it.

### For contributors

`scripts/ingest_implement_plan_lessons.py` reads every `docs/implement-plan/*.md` (except `README.md`) at the merge commit, so a lesson lost to an `ai-memory` push race is written by the next implement-plan merge. Record ids hash the plan slug, source and text, and `record_lessons_learned` accepts an optional `record_ids` list, skipping ids already written, so re-reading every log never duplicates. Prompt retrieval does not read `lessons_learned/` records yet.
