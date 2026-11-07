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
