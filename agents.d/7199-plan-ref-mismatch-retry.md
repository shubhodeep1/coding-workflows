<!-- agents: section="Workflow architecture" -->
The plan workflow decides whether the checkout matches the issue's integration branch before the model runs ("Capture planning ref context", `PLANNING_REF_MISMATCH=true|false|unknown`) and shows the result in PLANNING REF CONTEXT. The planner prompt (`scripts/run_plan_codex.sh`, `prompts/mode-plan.txt`) asks for `BLOCKED: integration branch mismatch` only when that line reads `true`. The model's own git is the isolated snapshot from `scripts/codex_isolated_workspace.py` (branch `isolated`, a fresh commit ID), so it never matches the planning ref. When an attempt still answers that block while `PLANNING_REF_MISMATCH=false`, `run_plan_codex.sh` retries within its 3 attempts and appends one `=== RETRY NOTE ===` to the prompt. On the last attempt it keeps the answer, so `plan.yml` blocks as before. The check reads the first `BLOCKED:` line after removing closed code fences, the same way `plan.yml`'s parser does.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `PLAN_REF_MISMATCH_RETRY` (`scripts/run_plan_codex.sh`: `attempt= outcome=retry|kept planning_ref_mismatch=false`)
LOG_PREFIX.name=PLAN_REF_MISMATCH_RETRY
