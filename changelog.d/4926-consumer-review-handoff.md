<!-- changelog: security -->
- **The Claude PR checker now accepts review hand-offs from `ai-review.yml` runs that the poller and the merge train dispatch in consumer repos.** This fixes security finding #4926 (`consumer-review-handoff-unrecognized`, medium, denial of service).

Since #4701, the orchestrator poller and the merge train start the consumer wrapper `ai-review.yml` from the default branch and name the run `AI Review [pr:<N>]`. `.claude/scripts/check_in_status.py` only recognized `internal-review.yml` runs titled `Internal: AI Review & Autofix [pr:<N>]`. So in a consumer repo, a Claude-fixer hand-off whose review run was one of those dispatches stayed at `waiting for verified completed review run` forever: the §26 checker never woke the fixer, and the §26.H catch-all sweep (`scripts/claude_pr_sweep.py`) never queued one. The active-run count missed those runs too, so a running dispatch did not hold a hand-off back. The checker now accepts both wrappers, each only with its own run name, in the hand-off check and in the active-run count.

| The numbers that matter | Value |
| --- | --- |
| Accepted (workflow, run name) pairs | 2: `internal-review.yml` / `Internal: AI Review & Autofix [pr:<N>]`, `ai-review.yml` / `AI Review [pr:<N>]` |
| API calls for the dispatch listing | at most 1 (`actions/runs?event=workflow_dispatch&per_page=100`), only when the head-branch reads found nothing |
| Listing window | the newest 100 `workflow_dispatch` runs of the repo, the same window the poller uses |

What this means for consumer repos: after the next `@stable` sync, `claude/*` pull requests whose review ran as a poller or merge-train dispatch get their Claude fixer again. A consumer still on an `ai-review.yml` without the `AI Review [pr:<N>]` run name behaves as before.

### For contributors

`PR_NAMED_REVIEW_DISPATCHES` holds the pairs and `PR_NAMED_REVIEW_RUNS_PATH` the repo-wide listing; `_is_pr_named_review_pair` matches a run's path (without `@ref`) and `display_title` against them. `DISPATCHED_REVIEW_WORKFLOW`, `DISPATCHED_REVIEW_TITLE`, and `DISPATCHED_REVIEW_RUNS_PATH` are kept. A failed listing read now raises `ReadError` (`action: retry`) instead of counting an `internal-review.yml` 404 as zero, because the repo-wide listing has no per-workflow 404 case.
