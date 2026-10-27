<!-- changelog: fixed -->
- **The validation refresh runner now closes validation discovery PRs that newer ones have replaced, so a stale seed PR no longer conflicts on `.ai/validate.yml` and blocks review/autofix.** Each time a consumer's default branch moved, the runner opened a new `automation/validate-discovery/<sha>/<type>` PR and left the older ones open. When one of them merged, the rest hit an add/add conflict on `.ai/validate.yml`. The conflict resolver refuses that host-only path, so the PR stopped with `conflict_resolver_sandbox_path_host_only` and `ai:needs-human` (shubhodeep1/btc_sweeper#196, after #235 merged).

  Each cycle, before the dedup and budget gates, the runner in `scripts/validation_refresh_runner.py` now lists the consumer's open discovery PRs once. It closes seed PRs once `.ai/validate.yml` is on the default branch, and type-change PRs whose proposed `type` is already committed. After it opens or reuses a discovery PR, it also closes every older one: a PR whose branch SHA is this run's consumer HEAD or an ancestor of it. It closes with a comment and keeps the branch. It does not close a PR that has a commit not authored by `codex-bot`, and it reads the commits again right before each close. In dry-run mode it only logs.

  | The numbers that matter | Value |
  | --- | --- |
  | Extra GitHub calls per consumer per cycle | 1 `gh pr list`, plus 1 `gh pr view` and 1 `gh pr close` per closed PR |
  | New env var | `VALIDATION_DISCOVERY_CLOSE_SUPERSEDED` (default `true`) |
  | New log prefix | `VALIDATION_DISCOVERY_SUPERSEDED repository= pr= branch= reason= outcome=` |

  What this means for operators: you no longer close superseded discovery PRs by hand. A PR someone has edited stays open (`outcome=kept_human_commits`). Set the repo variable `VALIDATION_DISCOVERY_CLOSE_SUPERSEDED=false` to turn the cleanup off.
