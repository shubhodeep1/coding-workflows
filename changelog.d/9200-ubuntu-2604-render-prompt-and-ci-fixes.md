<!-- changelog: fixed -->
- **Clarify, plan, AI Review and the review-blocked judge run again on ubuntu-26.04 runners.** `scripts/render_prompt.py` no longer crashes at startup on Python 3.14, and the CI test that failed on the new image's dangling `/usr/bin/grub-ntldr-img` symlink now passes.

GitHub's `ubuntu-latest` runners moved to the `ubuntu-26.04` image (Python 3.14) on 2026-11-10. Python 3.14 argparse checks every help string with `%` formatting as each argument is added, and the `--skip-syntax-validation` help text held a bare `{%...%}`. Every `render_prompt.py` call then failed with `ValueError: badly formed help string` before any model call. That stopped clarify and plan on every issue, and the reviewer and judge steps on every pull request. The help text now escapes the `%` signs, and `python3 scripts/render_prompt.py --help` is covered by a regression test. `tests/test_codex_isolated_exec.py::test_missing_docker_fails_closed_and_never_runs_host_codex` now checks for an existing link with `os.path.lexists`, so a dangling system symlink no longer raises `FileExistsError` when the `/bin` entries repeat the `/usr/bin` ones.

| The numbers that matter | Value |
| --- | --- |
| Scripts with a bad argparse help string under Python 3.14 | 1 (`scripts/render_prompt.py`, every other argparse script checked) |
| PRs reported "autofix exhausted" because the judge crashed | 7 (#8960, #8967, #8992, #9010, #9011, #9015, #9057) |
| Issues closed by stall recovery after repeated clarify/plan crashes | 8 (#9189, #9191 to #9194, #9196, #9198, #9199) |

What this means for operators and consumer repos: after the next `@stable` sync, clarify, plan, review and judge runs work on Python 3.14 runners again. Issues and PRs that stalled or were closed during the outage need a retry, since nothing re-runs them automatically.
