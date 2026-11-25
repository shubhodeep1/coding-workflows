<!-- agents: section="Implement self-repo staged-support ledger" -->
- With `SUPPORT_SCRIPTS_UNIFIED_ENABLED=true` (the default, stuck-states plan P2b),
  `implement.yml` stages executable helpers through
  `scripts/stage_support_for_phase.sh implement` into the `RUNNER_TEMP` bundle
  (`SUPPORT_SCRIPTS_DIR`) and copies them from there into
  `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`; nothing is installed under `scripts/` in the
  checkout, so the ledger only covers the prompt, schema and instruction-file
  installs. Pre-editor steps call `"${SUPPORT_SCRIPTS_DIR:-scripts}/<helper>"`.
  The in-tree `scripts/` installs and their ledger entries happen only under the
  kill switch (`SUPPORT_SCRIPTS_UNIFIED_ENABLED=false`) or when the support ref's
  helper has no `implement` case (the `--list` probe falls back with a warning).
  The optional persistence helpers (`attempt_journal.py`, `gh_api_retry.py`,
  `acceptance_check.py`, `evidence_pack.py`, plus `evidence_broker.py` and
  `targeted_file_context.py` for plan and clarify) are optional bundle entries
  of the clarify, plan and implement cases, so the `PERSIST_*` features keep
  working under unified staging; clarify also copies the evidence-pack set to
  `CLARIFY_HOST_PYTHON_DIR` when the bundle holds it. `run_plan_codex.sh`
  resolves them through `RUN_PLAN_CODEX_SUPPORT_DIR`, and the plan and clarify
  steps through `"${SUPPORT_SCRIPTS_DIR:-scripts}/<helper>"`.
