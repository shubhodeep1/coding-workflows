<!-- changelog: fixed -->
- **The review-autofix editor can now run a repository's pytest suites instead of improvising import shims.** `review_autofix.yml` installs pytest when the repository declares pytest configuration and the runner's interpreter cannot import it.

The "Install project dependencies (best-effort)" step could report success while installing nothing. A `pyproject.toml` that carries only a tool table, such as `[tool.pytest.ini_options]`, has no `[project]` table, so setuptools' legacy fallback builds an `UNKNOWN-0.0.0` package and `pip install -e .` exits 0. The step's `install_failed` guard stayed false, no warning was logged, and pytest was still missing when the editor started. Every autofix round then reported "pytest is unavailable" and validated its own edits through a hand-rolled no-install import shim rather than the repository's real suites. The step now detects declared pytest configuration, installs pytest through `python3 -m pip` so the install and the importability probe share one interpreter, and warns when the bootstrap still does not take.

| The numbers that matter | Value |
| --- | --- |
| Workflow fixed | `.github/workflows/review_autofix.yml` |
| Autofix rounds that hit the gap | 8 of 8 on PR #4029, 2026-09-07 05:52 to 23:45 UTC |
| Config markers detected | `pytest.ini`, `conftest.py`, `[tool.pytest.ini_options]`, `[tool:pytest]`, `[pytest]` |
| Regression tests | 4, in `tests/test_review_autofix_review_pipeline_contract.py` |

What this means for consumer repos: an AI review-autofix round on a Python repository that declares pytest now validates its fixes against the suites the repository actually ships, so a regression the tests would catch is caught before the round pushes. Repositories with no pytest configuration are untouched and no extra install runs for them.

### For contributors

The gap is silent by construction, which is why it survived: pip's exit code says success, so neither the workflow log nor the `::warning::` path showed anything wrong, and only the editor's own prose reported it. The bootstrap is deliberately gated on declared configuration rather than on the presence of `tests/test_*.py`, so a unittest-only repository does not pay for an install it will not use. `implement.yml` handles the same need differently, by instructing the editor to run `pip install pytest` itself, and is left as is.
