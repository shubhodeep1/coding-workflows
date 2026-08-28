<!-- changelog: fixed -->
- **python-repo-checks validation no longer aborts as `harness_error` when `.ai/validate.yml` uses a command-style `entry`.** The generated container healthcheck now accepts script paths and commands such as `sh scripts/run_validation_repo_checks.sh`, `bash -lc "scripts/run_validation_repo_checks.sh"`, or `python -m pytest`.

Consumer repos whose `.ai/validate.yml` selects `type: python-repo-checks` with a command-style `entry` previously failed every AI Validate run before their repo checks executed. The template `workflow-templates/validation-harness/python-repo-checks/docker-compose.test.yml.j2` interpolated the entry into `test -f /workspace/<entry>`, so `entry: sh scripts/run_validation_repo_checks.sh` rendered an invalid file test (`test: /workspace/sh: unexpected operator`), the app container stayed unhealthy, and validation reported `harness_error`. The healthcheck now first verifies `/workspace`, then checks each whitespace-separated entry token under `/workspace`; a path-like token must resolve to an existing file or directory, while entries without path-like tokens pass when their command target is on `PATH`. Command execution remains in the existing repo-check test. First observed on shubhodeep1/drhyg_ecommerce_automation run 33128774884.

| The numbers that matter | Value |
| --- | --- |
| Template fixed | `workflow-templates/validation-harness/python-repo-checks/docker-compose.test.yml.j2` |
| Failing consumer run | shubhodeep1/drhyg_ecommerce_automation Actions run 33128774884 |
| Pull request | #3862 |

What this means for consumer repos: after the next `@stable` sync regenerates `validation/docker-compose.test.yml`, python-repo-checks validation with a command-style entry proceeds to the actual repo checks instead of dying unhealthy at container startup. No consumer-side change is required; existing path-style entries behave exactly as before.

### For contributors

Regression coverage lives in `tests/test_family_python_repo_checks.py`, which simulates the rendered healthcheck the way docker runs it for path, unquoted-command, quoted-command, directory-argument, and command-only entries. The golden fixture `tests/fixtures/validation_harness/python_repo_checks/docker-compose.test.yml` was regenerated. The fix landed on `stable`; `forward-merge-stable-to-main.yml` propagates it to `main` automatically on merge.
