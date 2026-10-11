<!-- changelog: fixed -->
- **Runtime validation no longer fails on a consumer that does not install PyYAML.** The `python-repo-checks` and `python-mongo-repo-checks` import audits now check standard-library imports only.

Both families' `20_import_audit.sh` imported `yaml`, `jsonschema` and `jinja2`, which are the harness renderer's own modules, not the consumer's. binance-blessings#249's validation run 38107224535 ended as a harness error on `ModuleNotFoundError: No module named 'yaml'` while its 685 repository tests passed. The audits now import `argparse`, `json`, `pathlib` and `subprocess`, as the Node families do. The repository checks still exercise the consumer's own imports. `python-mongo-flask` keeps its `flask` / `pymongo` audit.

What this means for consumer repos: a Python repository without PyYAML gets a real validation verdict instead of `ai:harness-broken`.
