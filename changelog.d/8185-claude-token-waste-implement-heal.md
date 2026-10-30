<!-- changelog: fixed -->
- **Implement in coding-workflows no longer sends `agents.md` twice, and the heal intake no longer diagnoses engine-fallback reports it can never file.** Both changes cut Claude pool usage without changing what the pipelines produce.

In coding-workflows, `implement.yml` staged the support checkout's `agents.md` as `agents_canonical.md`, and `scripts/build_static_context.sh` then added it next to the checkout's own copy. Every self-repo implement prompt carried the ~620 KB file twice. The canonical copy is now staged only in consumer repos, which still get it ahead of their own agents file. In the workflow-failure heal intake (`scripts/workflow_failure_heal_intake.sh`), an `engine_fallback` report whose reporting run is missing or does not bind can never get a heal scope. With `HEAL_INTAKE_SCOPE_GUARD_ENABLED` on (the default), the intake now skips it before the `WORKFLOW_HEAL` diagnosis instead of after, and logs `WORKFLOW_HEAL skip reason=scope_unavailable_no_runs stage=pre_diagnosis`.

| The numbers that matter | Value |
| --- | --- |
| Self-repo implement prompt size, 2026-10-19..26 | 1.19–1.34 MB in 21/21 logged runs |
| Duplicate cache-write tokens per self-repo implement run | ~255K |
| Run-less engine_fallback heal diagnoses, 2026-10-21/22 | 104 |
| Heal runs that diagnosed one SECURITY_AUDIT fallback fingerprint that week | 73 |

What this means for operators: self-repo implement prompts drop back under codex's 1 MiB stdin cap, and a burst of unbound fallback reports no longer runs one Opus diagnosis per report. The DEBUG Telegram note for an `already-fixed` verdict on such reports no longer arrives; the reporting job's own `AI_ENGINE_FALLBACK_REFUSED` error and fallback alert are unchanged. With the creation gate off, the intake's compose-only path still diagnoses these reports.
