<!-- agents: section="Pre-push affected-test gate" -->
- **Sandbox tools and `/tmp`.** The gate container mounts `/tmp` as
  `rw,exec,nosuid,nodev,size=512m` (`scripts/prepush_test_gate_run.sh`).
  Docker's `--tmpfs` defaults to `noexec`, and shell tests that write a stub
  script to a temp dir and run it failed or hung there on both trees. The three
  images the gate runs in install `jq`, which most `scripts/*.sh` call:
  - codex-isolated, generated in `scripts/codex_isolated_exec.sh`: implement gate;
  - `scripts/review_sandbox/Dockerfile`: review autofix and conflict-resolver gates;
  - `scripts/clarify_sandbox/Dockerfile`: heal gate.

  `tests/test_prepush_affected_tests.py` pins both.
