<!-- changelog: fixed -->
- **Validation no longer fails with a harness error when the renderer's Python packages were installed with `pip install --user`.** `scripts/validate_process.sh` now installs them into a private venv when its isolated interpreter cannot import them.

The validation renderer (`scripts/render_validation_templates.py`) runs through the isolated Python launcher (`python3 -I` under `env -i`), which ignores user site-packages. Main's `validate.yml` installs `pyyaml`, `jsonschema` and `jinja2` with `pip install --user`, so validating a branch that already ships the isolated launcher failed every time with exit 14, "Missing dependency 'PyYAML'", and the run was classified `harness_error`. Project #3965 hit this on every revalidation. The renderer now checks the three imports first; if they fail, it creates a venv under `RUNNER_TEMP` once per run, installs them there and runs from it. The venv stays out of the uploaded runtime directory.

| The numbers that matter | Value |
| --- | --- |
| Renderer exit code this removes | 14 (`harness_error`) |
| Venv location | `${RUNNER_TEMP}/validate-renderer-deps-venv-<run id>-<pid>` |
| Settings passed to `pip` | `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` (either case), `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_TRUSTED_HOST`, `PIP_CERT`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` |

What this means for operators: validation works whether the calling `validate.yml` installs the renderer's packages into a venv on `PATH` or with `pip install --user`. A runner that cannot reach a package index gets the same exit-14 failure as before, with a log line naming the venv that could not be prepared.

### For contributors

The probe block's closing marker is now printed with `printf '%s\n'`. The old `printf '--- end ...'` parsed the leading dashes as an option and logged `printf: --: invalid option`. Tests: `tests/test_validate_process_renderer_deps.py`.
