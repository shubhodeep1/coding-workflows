<!-- changelog: fixed -->
- **The planner no longer stops for a human on a false "integration branch mismatch".**

Issue #7172, wave 3 of project #7126, waited for a person from 11:14 UTC on 2026-10-11 because the planner answered `BLOCKED: integration branch mismatch`. The workflow had checked out `orchestrator/project-7126` and recorded `Integration branch mismatch: false`. The model instead ran `git branch --show-current` inside its sandbox. That sandbox is a snapshot on a local branch named `isolated` with its own commit ID, so it never matches. The planner prompt now asks for that block only when the workflow's own line reads `true`, and says why the sandbox git differs. If a model still answers it while the workflow found no mismatch, `scripts/run_plan_codex.sh` retries with a note added to the prompt and logs `PLAN_REF_MISMATCH_RETRY`. A genuine mismatch still blocks on the first answer.

| The numbers that matter | Value |
| --- | --- |
| Hours #7172 waited on the false block | about 14 |
| Planner attempts per run (unchanged) | 3 |
| Retry note added per run | at most 1 |

What this means for operators: planning no longer pages a person for a branch mismatch the workflow itself ruled out.
