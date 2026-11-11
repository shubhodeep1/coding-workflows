<!-- changelog: fixed -->
- **Prompt rendering works again on Python 3.14 runners.** `scripts/render_prompt.py` crashed on every call with `ValueError: badly formed help string` after GitHub's `ubuntu-latest` runners moved to Ubuntu 26.04 (Python 3.14).

Python 3.14 argparse checks each help string when `add_argument()` runs. The `--skip-syntax-validation` help text had a bare `{%...%}`, which is not valid `%`-formatting, so `build_parser()` raised before any argument was parsed. Every step that renders a prompt failed before the model call: AI Plan (`Run Codex planning`), the review/autofix reviewers ("AI review/autofix produced no output"), and the review-blocked judge. Those failures then went on to file workflow-failure-heal issues whose own plan runs hit the same crash (#9189 to #9200), and to post "autofix exhausted" escalations such as PR #8992. The help text now escapes the percent signs as `{%%...%%}`, so `--help` output is unchanged.

| The numbers that matter | Value |
| --- | --- |
| First failing runs | 2026-11-10, about 15:55 UTC |
| Runner image | `ubuntu-26.04` 20261101.186.1, `/usr/lib/python3.14` |
| Scripts affected | 1 (`scripts/render_prompt.py`; a scan of all argparse scripts found no other bad help string) |

What this means for operators: plan, review and judge runs on this repo recover as soon as this merges to `main`. Consumer repos recover on the next `@stable` sync. Heal issues and review-blocked PRs filed during the outage can be re-run; their failure was this crash, not the work itself.

### For contributors

`tests/test_render_prompt_foundation.py` now runs `render_prompt.py --help`, which fails on every Python version while a help string is malformed. CI pins Python 3.12, which only expands help strings on `--help`, so the existing suites passed there while every Python 3.14 render failed.
