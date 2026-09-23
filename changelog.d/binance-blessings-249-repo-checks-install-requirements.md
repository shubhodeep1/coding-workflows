<!-- changelog: fixed -->
- **The `python-mongo-repo-checks` validation image now installs the consumer's `requirements.txt`.** Repo checks that import the consumer's own modules no longer fail with `ModuleNotFoundError`.

Until now `workflow-templates/validation-harness/python-mongo-repo-checks/Dockerfile.app.j2` installed only the harness tools (`pyyaml`, `jsonschema`, `jinja2`, `pytest`). Any consumer whose `custom_tests` ran unit tests against modules importing third-party packages failed runtime validation, and the failure was reported as `raw_status=harness_error`. The template now runs `pip install -r` on the consumer's requirements file after `COPY . /workspace`, the same way `python-mongo-flask` already does. The file defaults to `requirements.txt`, can be overridden with `slots.requirements_file` in `.ai/validate.yml`, and is skipped when absent.

| The numbers that matter | Value |
| --- | --- |
| Reported by | shubhodeep1/binance-blessings#249, validation run 35727922881 |
| Symptom | `Ran 61 tests`, `errors=9`, all `No module named 'requests'` |
| After the fix, same consumer head, `python:3.12-slim` | `Ran 677 tests`, `OK`, repo-check exit 0 |

What this means for consumer repos on `python-mongo-repo-checks`: validation now exercises your real dependency set. Consumers without a `requirements.txt` see no change. A requirements file that cannot install on `python:3.12-slim` (no compiler in the image) now fails the image build instead of failing later on imports.
