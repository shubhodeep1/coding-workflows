<!-- changelog: fixed -->
- **The pre-push test gate's sandbox can now run the repository's shell-script tests.** Its `/tmp` allows executables, and all three images it runs in now install `jq`.

The gate runs selected tests in a credential-free container. That container mounted `/tmp` with Docker's default `noexec`, so tests that write a stub script to a temp dir and run it failed or hung. The images also lacked `jq`, which most `scripts/*.sh` call. Both broke tests on the change and on the merge-base alike, so the gate reported noise. Its repair turn then "fixed" tests the change never touched. On PR #8371 the review editor and the gate's repair turn kept adding and removing the same fixture in `tests/test_promote_main_cycle.py`, until the review stopped with "editor changes lost".

| `tests/test_promote_main_cycle.py` in the gate sandbox | Result |
| --- | --- |
| Before (no `jq`, `/tmp` noexec) | 63 of 68 failed, 741 s |
| `/tmp` exec only | 63 of 68 failed (`jq: command not found`) |
| `jq` only | 63 of 68 failed, 741 s |
| After (both) | 68 of 68 passed, 286 s |

| Image | Gate it serves |
| --- | --- |
| codex-isolated, generated in `scripts/codex_isolated_exec.sh` | implement |
| `scripts/review_sandbox/Dockerfile` | review autofix and conflict resolver |
| `scripts/clarify_sandbox/Dockerfile` | heal |

What this means for operators: gate summaries should list far fewer "inherited" failures, and repair turns stop editing unrelated test files. The changed Dockerfiles produce new image tags. `publish-sandbox-images.yml` publishes them on the push to `main`, and until then runners build them locally.
