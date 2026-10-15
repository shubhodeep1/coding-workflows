<!-- changelog: fixed -->
- **Validation runs again: the PID-namespace launch no longer refuses every start with `selfcheck probe_failed`.** Since #7349 shipped, every validation on `main` failed closed before running a test ("Isolated validation harness unavailable … selfcheck probe_failed"), which blocked project #7355 after its last wave merged.

Before dropping to the sandbox user, the namespace launch in `scripts/validation_harness_sandbox.sh` bind-mounts `/dev/null` over every socket in the sandbox user's runtime dir, then lists the sockets again and refuses to start if any is left. That second listing used `find -type s`, which reads the type from the directory entry. A bind mount does not change that entry, so the masked rootlesskit `api.sock` and containerd sockets were still listed, and the launch exited 125 with no output on every run. The namespace self-check probe had the same check and would have failed with `user_manager_reachable`. Both now use `! -type l -xtype s`, which stats each entry. A socket left unmasked still stops the launch.

| The numbers that matter | Value |
| --- | --- |
| Validation runs on `main` that failed this way | 4 (runs 38646893482, 38649799512, 38652432980, 38660958249) |
| Last passing validation | run 38502066227, before #7360 |

What this means for operators: validation of orchestrator projects and the nightly harness self-test runs tests again with no setting to change. The `VALIDATION_HARNESS_SELFCHECK_RUNNER_UNSET_OPERATOR_STEP` notice for #7355 needs no action.

### For contributors

The existing launch tests fake `mount` by replacing the socket with a regular file, which also changes the directory entry, so they could not catch this. `tests/test_validation_harness_sandbox.py` now also runs the real launch script and the real probe under a real bind mount in a private mount namespace. These tests skip on hosts that cannot create one.
