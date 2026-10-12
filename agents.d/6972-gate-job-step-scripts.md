<!-- agents: section="Workflow file size limit" -->
- Steps in the `gate` job of `review_autofix.yml` run before support
  staging, so their wrapper has no `SUPPORT_SCRIPTS_DIR` or
  `.codex-workflow-src` fallback. Add the script to the SHA-pinned
  "Checkout head-gate helper" sparse checkout and to the file check in
  "Verify head-gate helper identity". Resolve only
  `${GITHUB_WORKSPACE}/.codex-head-gate-src/scripts/<script>`, and require
  `HEAD_GATE_HELPER_VERIFIED=true`, failing the step otherwise.
  `REVIEW_AUTOFIX_GATE_JOB_STEP_SCRIPTS` in
  `tests/review_autofix_step_scripts.py` lists these scripts. The first is
  the "Evaluate review gate" body (`review_autofix_step_evaluate_gate.sh`,
  #6972).
