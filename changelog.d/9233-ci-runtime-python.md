<!-- changelog: added -->
- **CI now tests on the Python interpreters the pipelines actually run.** Two new blocking `ci.yml` jobs run the tests on the runner's own `/usr/bin/python3` and inside the sandbox images, so a Python break that only shows on those interpreters fails `CI / lint` before it reaches the pipelines.

Every CI test job pinned `actions/setup-python` 3.12, while the pipelines run the runner's system `python3` (3.14 on `ubuntu-26.04`), and their sandbox containers run Debian bookworm's 3.11. The 2026-11-10 `render_prompt.py` crash broke only on 3.14, so CI stayed green while every clarify, plan and review run failed. `tests-system-python` builds a venv from `/usr/bin/python3`, checks that `sys.base_prefix` is `/usr`, and runs the whole pytest-collected `tests/` suite in 24 shards. `tests-sandbox-python` resolves the `clarify`, `review` and `codex-isolated-codex` images the way the pipelines do and runs the tests that cover each in-container Python entry point, in a container with no network, no credential and no root. A new guard, `tests/test_sandbox_python_coverage.py`, fails when a launch script gains an in-container entry point without covering tests.

| The numbers that matter | Value |
| --- | --- |
| New CI jobs gating `CI / lint` | 2 (`tests-system-python`, 24 shards; `tests-sandbox-python`, 3 images) |
| In-container entry points covered | 9 |
| New test files | 3 |

What this means for consumer repos: nothing; `ci.yml` is not synced to consumers.
