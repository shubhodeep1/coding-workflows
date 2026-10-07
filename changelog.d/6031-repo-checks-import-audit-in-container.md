<!-- changelog: fixed -->
- **The `python-repo-checks` validation harness now runs its import audit inside the app container.** Before, `ai-validate.yml` failed with `No module named 'yaml'` on any runner without PyYAML.

The generated `validation/tests/20_import_audit.sh` ran `import_audit.py` with the runner's own `python3`. That script checks `yaml` and `jinja2`, which `Dockerfile.app` installs only in the app image, so the audit failed on hosts without those packages even when the app image was correct. Orchestrator project #6031 stopped at runtime validation for this reason (run 37315007990, labelled `ai:harness-broken`). The template now runs the audit with `docker compose exec` against the `app` service, the same way the `python-mongo-flask` family does. It keeps the isolated-subprocess check and the TAP result, and on failure it now prints the helper's output as TAP comments.

What this means for consumer repos: projects whose `.ai/validate.yml` selects `python-repo-checks` stop hitting this false validation failure after the next `@stable` sync. Nothing needs changing in the repo.
