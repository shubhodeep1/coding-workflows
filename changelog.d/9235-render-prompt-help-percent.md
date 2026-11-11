<!-- changelog: fixed -->
- **`scripts/render_prompt.py` starts again on Python 3.14 runners.** The `--skip-syntax-validation` help text had a bare `{%...%}`, which argparse reads as a `%` format. Python 3.14 checks help strings when `add_argument()` runs, so `build_parser()` raised `ValueError: badly formed help string` and every `render_prompt.sh` call failed.

On runners with Python 3.14, the `review / codex-agent` step (`Run reviewer models`) and the AI Plan step stopped before any model ran, on every pull request and issue that reached them. That includes the plan for the heal issue filed for it (#9235). The help text now escapes the percent signs as `{%%...%%}`; `--help` still shows `{%...%}`, and no option name or behaviour changes.

| The numbers that matter | Value |
| --- | --- |
| Help strings affected | 1 (`scripts/render_prompt.py`, `--skip-syntax-validation`) |
| Consumer pin that still has the bug | `6955fe54` (`@stable`) |
| Python version that fails | 3.14 (earlier versions failed only on `--help`) |

What this means for consumer repos: AI reviews and plans that failed with two Python tracebacks and exit 1 recover after the next `@stable` release reaches them through `update_workflows.yml`. Re-running them before that fails the same way.

### For contributors

`tests/test_render_prompt_foundation.py` now builds the parser's full help text (`format_help()`, which expands every help string on every Python version) and runs `render_prompt.py --help`, so a bare `%` in any of its help strings fails CI before it reaches a 3.14 runner.
