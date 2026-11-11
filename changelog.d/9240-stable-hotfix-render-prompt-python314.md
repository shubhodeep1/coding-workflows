<!-- changelog: fixed -->
- **Clarify, plan, AI Review and the review-blocked judge run again in consumer repos on ubuntu-26.04 runners.** This hotfix brings the `scripts/render_prompt.py` fix from #9240 to the `stable` line.

GitHub's `ubuntu-latest` runners moved to the `ubuntu-26.04` image (Python 3.14) on 2026-11-10. Python 3.14 argparse checks every help string with `%` formatting as each argument is added, and the `--skip-syntax-validation` help text held a bare `{%...%}`. Every `render_prompt.py` call then failed with `ValueError: badly formed help string` before any model call, in every release from v1.45.0 to v1.47.1. Rolling back to an older release would not have helped. The help text now escapes the `%` signs, and `python3 scripts/render_prompt.py --help` is covered by a regression test.

| The numbers that matter | Value |
| --- | --- |
| Releases carrying the crash | v1.45.0 to v1.47.1 |
| Consumer repos affected | all 13 in `.github/ai/consumer_repos.json` |
| Lines changed in `scripts/render_prompt.py` | 1 |

What this means for consumer repos: after the next `@stable` sync, clarify, plan, review and judge runs work on Python 3.14 runners again. Issues and PRs that stalled or were closed during the outage need a retry, since nothing re-runs them automatically.

### For contributors

Only the `render_prompt.py` part of #9240 is on `stable`. The CI-only test fixes and the poller `EPOCHREALTIME` clock change reach `stable` with the next `main` → `stable` promotion.
