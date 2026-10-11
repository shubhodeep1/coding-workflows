<!-- changelog: changed -->
- **A CI heal may now fix the code its failing tests cover.** The workflow-failure heal intake adds the failing tests a verified run reports, and the subject files they cover, to the frozen edit scope, and the heal prompt decides between a test and its code from the documented behaviour.

A heal issue's edit scope is frozen before the diagnosis runs (issue #6463), from the run's workflow paths, the crash file and the ownership facts, plus `tests/**` and `changelog.d/*.md`. For a "CI failed on main" heal that confined the implementer to tests, so when a test and the code disagreed it rewrote the test to match the code: heal PRs #6907 and #6916 flipped the security-pass cap rule that #6906 had restored. `scripts/workflow_failure_heal_intake.sh` now extracts the failing test names and files from the verified runs' own filtered job logs (`heal-scope failing-tests`), and `heal-scope render` resolves each name to the test file that defines it at the scope commit and that file's stem to `scripts/<stem>.sh`, `scripts/<stem>.py` or `.github/workflows/<stem>.yml`, only when the file exists there and passes the usual path rules. The diagnosis still cannot add a path. `prompts/mode-workflow-failure-heal.txt` tells the healer to pick the wrong side from README, agents.md, changelog fragments and the introducing issues and PRs, never from "the test should match what the code does now".

| The numbers that matter | Value |
| --- | --- |
| Failing tests read per heal | up to 20 names and 20 files |
| Subject candidates per test file | `scripts/<stem>.sh`, `scripts/<stem>.py`, `.github/workflows/<stem>.yml`, `.github/workflows/<stem-with-dashes>.yml` |
| Paths never added | `.claude/**`, `.github/ai/**` |

What this means for operators: a red main caused by a code change no longer produces a heal that rewrites the test to hide it; the heal can change the script the test covers, and the implement guard's automation-path rules still apply to `scripts/` and workflow files.

### For contributors

`tests/test_workflow_failure_heal_scope_tests.py` covers the log shapes, the mapping, the render CLI against a scope commit and the intake wiring; it runs in the heal CI step.
