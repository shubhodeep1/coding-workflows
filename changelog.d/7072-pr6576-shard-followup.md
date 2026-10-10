<!-- changelog: added -->
- **A one-time workflow settles PR #6576's unexplained `orchestrate-poll` shard failure.** The new `.github/workflows/pr6576-shard-followup.yml` runs on the next push to `main`, fetches the failed job log, reruns the failing tests on current `main`, and posts the full result on #7072.

The postmortem `docs/postmortems/2026-10-10-pr-6576-orchestrate-poll-shard-1.md` could not fetch the failed log, and most of the partition could not run without `jq` and PyYAML, so the original failure was never identified. The workflow reads #6576's recorded head from the pull request, finds its failed `orchestrate-poll (<g>)` CI jobs (including earlier attempts and earlier heads of `ai/issue-6573`), redacts their logs and extracts the failing test names with the heal intake's parser. When no log or no names are found, it reruns the `ci.yml` partition (4 groups x 4 local shards) at the recorded head instead. A test counts as reproduced only when it fails in both of two reruns on `main`, and only then is one standalone issue filed (marker `<!-- ai:pr6576-shard-followup-defect:v1 -->`). Failures seen only at the old head are recorded on #7072 and nothing is filed.

| The numbers that matter | Value |
| --- | --- |
| Result marker on #7072 | `<!-- ai:pr6576-shard-followup:v1 -->`, by the GH_PAT account |
| API calls on every later `main` push | 2 (identity plus one comments page), then the run stops |
| Logs read at most | 4 jobs, from at most 8 job listings and 3 run listings |
| Main reruns per failing test | 2 |

What this means for operators: nothing to start by hand. The first `main` push after this lands does the work once; later pushes skip. `ci.yml` and its aggregate `lint` gate are unchanged. Remove the workflow, its helper and test once the registry entry in `docs/scripts-pending-removal.md` says so.

### For contributors

The job that reruns old test code (`rerun`) holds no secrets and only `contents: read`; only `locate` and `report` see `GH_PAT`, and they run only on the default branch. Log text is neutralised before it reaches a comment or issue, so a test's output cannot forge `<!-- ai: -->` markers or workflow commands. `tests/test_pr6576_shard_followup.py` runs in the workflow's own `self-test` job on pull requests that touch these files.
