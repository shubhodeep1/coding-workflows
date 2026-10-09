<!-- changelog: security -->
- **Synthesised smoke tests, the validation harness and the review utility models can no longer bypass their isolation.** Security-pass fix cycle 4 for project #6664 closes the paths that still reached the host:
  - The validation driver and its fallback runner now recognise a synthesised test by a `synth_round_*.sh` name in any letter case, or by the generated-wrapper marker, so a renamed copy still runs only in the sandbox, or is skipped.
  - `validate_process.sh` regenerates `validation/validate.sh` just before launch and runs it only when it matches the canonical wrapper byte for byte. A freehand, self-healed or committed `validate.sh` is refused as a harness error instead of being run.
  - The harness environment now also drops `*_KEY`, `*_PASSWORD`, `*_PASSWD`, `*_CREDENTIAL(S)`, `*_COOKIE`, `GIT_CONFIG_*`, `GIT_ASKPASS`, `SSH_ASKPASS`, `SSH_AUTH_SOCK`, `ACTIONS_RUNTIME_*`, `ACTIONS_CACHE_*`, `ACTIONS_RESULTS_*` and `CLAUDE_*`. The generated smoke wrapper refuses to run its body while any variable matching the same policy is set.
  - The synthesised-test materializer refuses a symlinked `.ai` and any git-tracked file under `.ai/review_runtime`. It also reads cache files without following symlinks (1 MiB cap) and writes through a no-follow descriptor of `validation/tests`.
  - The consensus summariser, smoke synthesiser and interim judge load their helpers from their own directory when `SUPPORT_SCRIPTS_DIR` is unset or relative, never from the PR checkout.

What this means for operators: a project test that read a host variable now matched by these patterns (for example `DB_PASSWORD`) must get it from `validation/validate.env` instead. Refs #6664.
