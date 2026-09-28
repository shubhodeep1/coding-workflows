<!-- changelog: changed -->
- **The `CI` workflow's `lint` job may now run for 60 minutes instead of 45.**

The single `lint` job in `.github/workflows/ci.yml` grew to 40–45 minutes. On 2026-09-28, runs on `main` were cancelled at the 45-minute limit while every test was still passing. That left pull requests marked "unstable" and made clean Claude-fixer reviews miss their ready check snapshot. This is a stopgap until the job is split into parallel jobs.

| The numbers that matter | Value |
| --- | --- |
| `lint` `timeout-minutes` | 60 (was 45) |
| `main` runs cancelled at 45 minutes | 36367681221, 36368393442 |

What this means for operators: `CI` completes again instead of being cancelled near the end. A full run still takes about 45 minutes until the split lands.
