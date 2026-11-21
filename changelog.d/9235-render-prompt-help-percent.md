<!-- changelog: fixed -->
- **CI now runs the `render_prompt.py --help` regression test and checks the parser's full help text.** The Python 3.14 fix itself (escaping `{%...%}` as `{%%...%%}` in the `--skip-syntax-validation` help text) is recorded in `9200-ubuntu-2604-render-prompt-and-ci-fixes.md`; this entry covers only the test gaps around it.

`tests/test_render_prompt_foundation.py` runs as a script in CI, so only the tests called from its `main()` execute. The `--help` regression test added with that fix, `test_render_prompt_py_help_strings_are_valid_format_strings`, was never called from `main()` and so never ran. It is now registered there. A new test, `test_render_prompt_py_help_strings_are_well_formed`, builds `build_parser().format_help()`, which expands every help string on every Python version, so a bare `%` in any of its help strings fails CI before it reaches a Python 3.14 runner. No option name or behaviour of `render_prompt.py` changes.

| The numbers that matter | Value |
| --- | --- |
| `--help` regression tests that never ran in CI | 1 (now registered in `main()`) |
| New tests | 1 (`test_render_prompt_py_help_strings_are_well_formed`) |
| Consumer pin that still has the original bug | `6955fe54` (`@stable`) |

What this means for consumer repos: nothing changes at runtime. AI reviews and plans that fail on Python 3.14 recover after the next `@stable` release that carries the fix in `9200-ubuntu-2604-render-prompt-and-ci-fixes.md` reaches them through `update_workflows.yml`.
