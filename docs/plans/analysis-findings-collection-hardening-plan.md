# Analysis findings: delivery, coverage and validation hardening

## Summary

The promote cycle's proving and verifying runs each hand one
`analysis/workflow-optimization-*.md` doc to the orchestrator. Those docs come
from `.github/workflows/workflow-log-analysis.yml`, dispatched by the stable
gate in `.github/workflows/test-and-mark-stable.yml`. This plan fixes five
problems in that chain.

- **No report has reached `main` since 2026-10-25.** The "Commit and push
  report" step pushes straight to `main`. Every push is now refused with
  `GH013: Repository rule violations ... 2 of 2 required status checks are
  expected` (for example run 42540318556, 2026-11-09). The analysis runs, but
  the report is lost because it is not uploaded as an artifact.
- **Consumer repos are never analysed.** The gate dispatches with
  `repos_override=${TEST_REPO}`, so the 13 repos in
  `.github/ai/consumer_repos.json` are never covered.
- **Collection gaps are silent.** The following leave a green run:
  - a consumer that returns 403/404 or hits the rate limit;
  - a repo whose run listing stops at the 1,000-run cap;
  - a missing full-log directory;
  - a near-empty report.
- **The purge deletes unread docs.** The 30-day retention purge removes
  docs that were never processed.
- **Findings are checked only after the work starts.** The orchestrator checks
  each finding against current code, but nothing makes that the first step.

Newest-first selection in `scripts/apply_analysis_on_main.sh` (`sort -rV`)
ships in the same PR that adds this plan. It is a separate change and is not
one of this plan's phases.

## Context

- **Report path today.** The `analyze-commit-notify` job writes the report and
  then runs "Commit and push report" (`workflow-log-analysis.yml`, about lines
  1445-1608). That step:
  - purges docs older than `WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS`
    (default 30), sparing docs that open projects read, via
    `APPLY_ANALYSIS_IN_FLIGHT_DOCS=true bash scripts/apply_analysis_on_main.sh`;
  - commits, rebases and pushes `HEAD:main` up to 5 times.

  `deep-audit` ("Commit and push deep audit section", about line 2156) and
  `api-redundancy` ("Commit and push API redundancy section", about line
  2754) each pull `main` to fetch the report, append a section and push to
  `main` the same way. Both jobs only run after the first push succeeds, so
  today they never run.
- **Precedent for a protected `main`.**
  `.github/workflows/nightly-validation-selftest.yml` (about lines 380-475)
  already handles the same rule. It:
  - pushes a fresh `auto/validation-selftest-status-<run_id>-<attempt>` branch;
  - opens a PR with `gh pr create`;
  - logs `VALIDATION_SELFTEST_STATUS outcome=...` (including
    `reason=push_rejected_by_ruleset`);
  - closes PRs it supersedes.

  The existing review pipeline then merges those PRs (for example #9092).
- **Gate dispatch.** `test-and-mark-stable.yml` (about line 4250) passes
  `--field lookback_days=1 --field "repos_override=${TEST_REPO}" --field
  alert_msg_level=SILENT` with `--dispatch-fail-open`. The gate runs 2-4 times
  a day, so 1-day windows overlap on busy days. On a day the push fails, that
  day's window is lost for good.
- **Repo list.** The `resolve_repos` step (about lines 94-153) already reads
  the full roster when `repos_override` is empty and the event is not the
  06:00 PAT-budget schedule.
- **Collector.** `scripts/collect_workflow_logs.py`:
  - records per-repo failures in `errors[]` and moves on to the next repo
    (about lines 2127-2135);
  - exits 1 only when no repo succeeded (about line 2394);
  - sets `page_limit_reached` (about lines 690 and 717), but only the PAT-budget
    path reads it.
- **Report check.** The analyze step accepts any report that contains a
  non-whitespace character (about line 1393). The prompt
  (`prompts/mode-workflow-analysis.txt`, built from
  `prompts/_templates/mode-workflow-analysis.txt`) requires 11 `## ` headings
  in a fixed order. It also requires a `###` subsection per repository under
  `## Per-Repo Breakdown`. Report `2026-10-25-3` opens by saying the full-log
  directory was not mounted, so that run's findings had no log evidence.
- **Findings check.** `scripts/apply_analysis_on_main.sh` builds the
  `project_description`, which tells the orchestrator to "validate every
  recommendation against the current code". The processing report shows the
  check does happen, but only as prose. Implementation issues can be planned
  before every finding has a verdict.

## Goals

- G1: Every completed analysis lands on `main` through a PR, and is kept as a
  run artifact even when that PR cannot be opened (Q4: A).
- G2: The gate's analysis covers this repo plus every consumer in
  `.github/ai/consumer_repos.json` (Q5: A).
- G3: The window starts where the last report on the branch ended, so gate
  runs leave no gap while that report is at most 7 days old (Q6: A). An
  older window end is clamped to 7 days, and with no readable marker the
  window falls back to `lookback_days`; both are logged. Overlaps are
  accepted (see P4). The outage since 2026-10-25 is not backfilled.
- G4: A repo that could not be read, a capped run listing, a missing log
  directory, or a report that breaks the required structure is reported
  loudly, never silently (Q6: A).
- G5: The purge never deletes an unprocessed doc (Q6: A, Q7: B).
- G6: Every apply-analysis project records a verdict for each finding before
  it plans any implementation issue. A doc with no valid findings becomes a
  single closing PR (Q3: A).

## Non-goals

- Changing the analysis prompt's sections or the models used.
- Changing the weekly retro fan-out (`scripts/workflow_retro_fanout.sh`) or
  the 06:00 PAT-budget run.
- Changing the repository ruleset or giving any account a bypass.
- Backfilling reports lost between 2026-10-26 and this plan's merge.
- A separate model call that checks findings before dispatch (Q3: B was not
  chosen).

## Constraints

- §6: keep every existing input, output, env var, log prefix and step
  `name:`/`id:`. New names are added alongside the old ones.
  `lookback_days` and `repos_override` keep working for manual dispatches.
- §4: every new input and repo variable has a default.
- §15:
  - the PR route adds 1 branch push, 1 `gh pr create` and 1 open-PR list per
    run;
  - the window lookup reads the local checkout (0 API calls);
  - consumer coverage adds the collector's existing per-repo calls (13 more
    repos).
- §18: there are no manual scripts. The one new script runs from the
  workflow. No DB work. No supervisor.
- §19: report PR bodies must not use auto-close keywords.
- §27: `workflow-log-analysis.yml` is about 142,800 bytes today. Any large new
  `run:` body goes into `scripts/` so the file stays far below 480,000 bytes.
- §20 / §30: one changelog fragment per phase, and `agents.d/` fragments for
  new log prefixes.

## Approach

### Report delivery (P1)

- **Run branch.** Each run publishes to its own branch:
  `auto/workflow-log-analysis-<run_id>-<run_attempt>`.
- **"Commit and push report"** keeps its `name:` and `id: commit_report`. It
  keeps the purge, the commit and the same-name rename logic. The push target
  changes:
  - it creates the run branch from the commit;
  - it keeps the rename-on-collision check against `origin/main`;
  - it pushes `HEAD:refs/heads/<run branch>`.

  It outputs `report_branch=<run branch>` next to `committed=` and
  `report_file=`. The push uses `secrets.GH_PAT`, as the self-test precedent
  does; without it the step logs `WORKFLOW_LOG_ANALYSIS_PUBLISH
  outcome=failed reason=gh_pat_missing` and fails.
- **Ruleset rejections.** A push refused by a ruleset (`GH006|GH013|protected
  branch|push declined|repository rule`, the same match the self-test uses)
  logs `WORKFLOW_LOG_ANALYSIS_PUBLISH outcome=failed
  reason=push_rejected_by_ruleset branch=<b>` and fails the step.
- **`deep-audit` and `api-redundancy`** read the report from the run branch
  (`needs.analyze-commit-notify.outputs.report_branch`) instead of `main`.
  They push their section commits to that same branch, using a plain
  fast-forward push.
- **New `publish-report` job.** It runs after the three jobs, with
  `if: always() && needs.analyze-commit-notify.outputs.report_branch != ''`.
  - It opens one PR from the run branch to the dispatch branch, titled
    `chore: add workflow log analysis report`. The body names the run, the
    report file, which sections were added, and which were skipped or failed.
  - It logs `WORKFLOW_LOG_ANALYSIS_PUBLISH outcome=pr_opened pr=<n>
    branch=<b>`.
  - Section jobs that failed do not block the PR. The report alone is still
    worth merging.
  - The existing review pipeline merges it, the same way as the self-test
    status PRs.
  - `gh pr create` uses `secrets.GH_PAT`. A PR opened with `github.token`
    starts no workflows, so its required checks never report and it cannot
    merge. An empty `GH_PAT` logs `WORKFLOW_LOG_ANALYSIS_PUBLISH
    outcome=failed reason=gh_pat_missing` and fails the job.
- **Artifact.** The analyze job uploads the report file as artifact
  `workflow-log-analysis-report` with `if: always()` (retention 30 days)
  before "Commit and push report". If every later step fails, the findings
  still exist. That copy lacks the deep-audit and API-redundancy sections, so
  `publish-report` uploads the run branch's final report again as
  `workflow-log-analysis-report-final` (`if: always()`) before it opens the
  PR.
- **Repo variable `WORKFLOW_LOG_ANALYSIS_PUBLISH_MODE`.** It takes `pr`
  (default) or `push` (the old direct push to the dispatch branch, kept as the
  rollback path).

### Report structure check (P2)

- **New script `scripts/check_workflow_analysis_report.py`.** It takes
  `--report <path> --collect-report workflow_log_report.json` and checks that:
  - all 11 required `## ` headings appear, in order;
  - `## Per-Repo Breakdown` has a `### ` subsection whose heading contains
    each repository in `scope.repositories`, except repositories listed in
    `coverage.repositories_failed` (P3) when that key is present;
  - the report is at least `--min-bytes` long (default 2,000).

  It prints `WORKFLOW_LOG_ANALYSIS_REPORT_CHECK outcome=ok|invalid
  missing_headings=<n> missing_repos=<csv> bytes=<n>` and exits 0 or 1.
- **In the analyze step's retry loop**, a report that fails the check counts
  like empty output. The new failure mode `report_structure_invalid` goes
  through the existing `handle_log_analysis_codex_failure`, so retries,
  failure markers and alerts are unchanged. A report that is still invalid
  after the last attempt is not committed, but the P1 artifact still holds it.
  P2 therefore depends on P1 and must merge after it.
- **Failed repos need P3.** The failed-repo exemption reads
  `coverage.repositories_failed`, which only P3 writes. Without that key the
  checker exempts nothing, so a report for a run where one repo could not be
  read would fail the per-repo check and be held back. P2 therefore also
  depends on P3 and must merge after it.
- **Missing logs.** When the `workflow-log-output` download (already
  `continue-on-error`) is missing, the step logs
  `WORKFLOW_LOG_ANALYSIS_COVERAGE logs=missing` and adds a fixed line to the
  top of the report:
  `> Data gap: full workflow logs were unavailable for this run.`
  The analysis is still published, because its index-level findings are
  still useful, but the gap is visible.
- **Repo variable `WORKFLOW_LOG_ANALYSIS_REPORT_CHECK_ENABLED`** (default
  `true`). Set to `false`, the step goes back to today's non-empty check.

### Collection coverage (P3)

- **`collect_workflow_logs.py`** adds a top-level `coverage` object to
  `workflow_log_report.json`:
  `{"repositories_requested": [...], "repositories_failed": [{"repository",
  "message"}], "repositories_truncated": [...], "page_limit_reached": bool}`.
  - `repositories_failed` lists repos whose run listing raised (the
    collector's exception path), not every `errors[]` entry with
    `scope == "runs"`: a `max-runs` cap appends an entry with the same scope
    and counts only as truncated.
  - `repositories_truncated` lists repos where `capped` or
    `run_meta["page_limit_reached"]` was true.
  - Exit codes do not change (exit 1 only when every repo failed).
- **The collect step** reads `coverage` and logs one line:
  `WORKFLOW_LOG_COLLECT_COVERAGE repos=<n> failed=<n> truncated=<n>
  failed_repos=<csv> truncated_repos=<csv>`. It raises a `::error::`
  annotation for each failed repo and a `::warning::` for each truncated
  repo.
- **Alerts.** It sends one alert through the workflow's existing notify path,
  at WARNING level, so the gate's `alert_msg_level=SILENT` still holds it
  back. It also writes `coverage` into the analysis context the agent already
  reads.
- **Prompt.** The prompt's existing data-gap rule ("If evidence is incomplete,
  state the exact gap") gets one sentence: failed or truncated repositories
  from `coverage` must be named in `## Executive Summary`.
- **The job still passes** when some repos fail, so one dead consumer cannot
  block the findings for the rest. Collection only fails when every repo
  fails. "Fail loudly" here means an annotation, a log line, an alert and a
  line in the report.

### Consumer coverage and window (P4)

- **New `workflow_dispatch` input `since_last_report`** (string, default
  `"false"`). When it is `"true"`, and `since` is empty and this is not the
  PAT-budget run, the collect step reads the window end from the newest
  `analysis/workflow-optimization-*.md` added on the checked-out branch, found
  in git history (`git log --diff-filter=A`, then `git show <commit>:<path>`).
  Reading history matters because an apply-analysis project's final PR
  deletes its doc, and newest-first selection processes the newest doc first.
  The step fetches at least 8 days of history (`git fetch --shallow-since`).
  Then it:
  - passes `--since <window_end>` when the window end is in the past and at
    most 7 days old, however recent;
  - passes `--since <now minus 7 days>` when the window end is older (the
    window is clamped to 7 days, logged as `clamped=true`);
  - falls back to `--lookback-days` when there is no marker, the value cannot
    be parsed, the timestamp is in the future, or a git command fails.

  It logs `WORKFLOW_LOG_ANALYSIS_WINDOW source=last_report|lookback
  since=<ts> report=<path>`.
- **Window marker.** The analyze step adds a first line to every report:
  `<!-- workflow-log-analysis: window_start=<ISO> window_end=<ISO> run=<id>
  -->`. `window_end` is the collect job's start time.
- **Gate dispatch.** In `test-and-mark-stable.yml`:
  - drop `--field "repos_override=${TEST_REPO}"`, so `resolve_repos` reads the
    roster plus this repo;
  - add `--field since_last_report=true`;
  - keep `lookback_days=1` as the fallback;
  - keep `alert_msg_level=SILENT`.
- **Overlaps.** Report PRs merge some time after their run. A run that starts
  before the previous report merged reads an older window end, which gives an
  overlap, never a gap.
- **Repo variable `WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS`** (default `true`).
  Set to `false`, the gate dispatch passes `repos_override=${TEST_REPO}` as
  before.

### Purge (P5)

The purge code stays in "Commit and push report". Under D5 it deletes only
docs that are past the retention window **and** recorded in
`analysis/recommendation-processing-report.md`. It finds those with the same
fixed-string basename match `scripts/apply_analysis_on_main.sh` already uses
(`grep -qF -- <basename> <report>`): a local read with no API calls, which
also works while `APPLY_ANALYSIS_ON_MAIN_ENABLED=false`.

- An older doc the report does not record is kept and logged on the existing
  prefix with a new reason value:
  `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc=<path> reason=unprocessed`.
- If the processing report is missing or unreadable, the purge is skipped
  for this run (fail closed), logged as
  `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED reason=processing_report_unavailable`.
- The existing in-flight guard (`reason=in_flight_project`) is unchanged and
  runs first.

The retention window (`WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS`, default
30) now applies only to docs that were processed but never deleted.
Normally the project's final PR deletes those docs, so the purge rarely
deletes anything.

### Check findings first (P6)

`scripts/apply_analysis_on_main.sh` keeps its `project_description` layout and
its first line, which the orchestrator uses as the tracking-issue title. The
validation paragraph becomes a required first step:

> Step 1, before planning any implementation issue: read the source doc in
> full and record a verdict for every finding against the current default
> branch: `applied-candidate` (still valid and safe), `already-implemented`,
> or `rejected` (with the reason). Plan implementation issues only for
> `applied-candidate` findings. If no finding is `applied-candidate`, the
> project is a single PR that deletes the source doc and appends its verdict
> table to the processing report.

The current paragraph also carries the repository guardrails ("never rename
or remove existing identifiers without an alias; MongoDB changes ship their
/db/contracts update; no standalone manual scripts, wire work into existing
automation; never use auto-close keywords against ai:orchestrator-tracking
issues"). The new text keeps that guardrail clause byte-for-byte, after the
verdict step. The rest of the text (scope, report entry, never touch the
state files) is unchanged. The marker comment does not change, so the loop guard and the
poller's marker parser are not affected.

P6 also bounds the marker search, because D5 keeps every unprocessed doc.
`unprocessed_docs` keeps two separate counters and stops at whichever limit
comes first:

- printed docs, capped at `APPLY_ANALYSIS_MAX_CANDIDATES` (default 3, enough
  for the deleted-on-tip fallback). The script raises the effective cap to
  `PROMOTE_CYCLE_MIN_DOCS` when that variable is a larger positive integer
  and logs `APPLY_ANALYSIS_CANDIDATE_CAP raised_to=<n>
  reason=promote_cycle_min_docs`, so a capped list-only count can never fall
  below the promote cycle's minimum on its own;
- docs skipped because they carry a trusted dispatch marker, capped at
  `APPLY_ANALYSIS_MAX_MARKER_SEARCHES` (default 20). Only these skips count
  toward this cap, so the docs it prints never use it up.

Each run therefore makes at most the effective candidate cap plus
`APPLY_ANALYSIS_MAX_MARKER_SEARCHES` searches, whatever the backlog size.

Marker-carrying docs can still fill the front of the newest-first scan:
under D5, docs from failed or closed projects keep their markers (the lookup
matches closed tracking issues too) and are never purged. The scan reports
that case instead of an empty backlog. "Capped" means the marker cap stopped
the scan while at least one candidate was still unexamined; a cap reached on
the last candidate is a complete scan, so a backlog of exactly 20
marker-carrying docs still ends as `all_docs_processed`.

How the capped state leaves `unprocessed_docs`: both callers run it as
`docs_output="$(unprocessed_docs)"` (lines 405 and 448), so stdout stays the
doc list and nothing else. A global flag would be lost in the subshell, and
return code 2 keeps meaning `guard_unavailable`. So:

- stdout carries only doc paths, as today, including any docs printed
  before the cap stopped the scan;
- `APPLY_ANALYSIS_CANDIDATE_CAP` and the per-doc marker-skip warnings go to
  stderr, which both callers let through to the job log;
- return codes: 0 means the scan ended normally, either because every
  candidate was examined or because the candidate cap was reached (that is
  the expected stop, not a capped scan); 3, a new code, means only that the
  marker cap stopped the scan with at least one candidate still unexamined;
  2 still means a failed lookup. Command substitution keeps the function's
  exit status, so the callers' existing `docs_rc=$?` line reads it.

The callers handle return code 3 as follows:

- list-only mode counts the printed docs as before and appends
  `capped_behind_markers=true` to its `APPLY_ANALYSIS_CANDIDATES` line (it
  writes that line itself, at line 418);
- dispatch mode dispatches normally when docs were printed. With no doc
  printed it logs `APPLY_ANALYSIS_SKIPPED reason=scan_capped_behind_markers
  searches=<n>` instead of `all_docs_processed`.

`scripts/promote_main_cycle.sh` already reads the `APPLY_ANALYSIS_CANDIDATES`
line from stdout. When that line carries `capped_behind_markers=true` and the
count is below `PROMOTE_CYCLE_MIN_DOCS`, which includes a capped scan that
printed one doc, it skips with `PROMOTE_CYCLE_SKIPPED
reason=scan_capped_behind_markers available=<n> required=<m>` instead of
`insufficient_docs`. It also prints that line as a `::warning::` annotation.
The script and its workflow step have no Telegram credential, so the
annotation and the log line are the whole alert, and no workflow change is
needed. The docs that used up the cap are named by the dispatcher's existing
per-doc `::warning::... was dispatched to the orchestrator before` lines,
which reach the same job log through stderr. The fix is a human decision, as
D5 intends: record each abandoned doc in the processing report (which also
makes it eligible for the P5 purge), or raise
`APPLY_ANALYSIS_MAX_MARKER_SEARCHES`.

At the defaults a run makes at most 3 + 20 = 23 `search/issues` calls. Each
search returns up to 20 tracking issues (`per_page=20`), and
`doc_dispatched_before` makes one comments read per returned issue until one
has a trusted marker, so a run makes up to 20 comments reads per searched doc
(460 at the defaults). Hitting the search rate
limit mid-scan fails that lookup, and the run skips as `guard_unavailable`,
the existing fail-closed outcome.

The bound applies in both modes. A failed lookup still fails closed as
`guard_unavailable` (the loop guard must never dispatch a doc it could not
check). The bound limits how many lookups can fail per run; it does not
change that rule.

## Decisions

### D1 — Report delivery (Q4: A)
Reports go through a PR from a per-run branch, mirroring
`nightly-validation-selftest.yml`, and are always uploaded as an artifact.
`push` mode stays available as a rollback.

### D2 — Consumer coverage (Q5: A)
The gate's dispatch covers the roster plus this repo. A kill switch can bring
back self-only coverage.

### D3 — "Fail loudly" for collection
Annotations, a stable log line, a WARNING alert and a line in the report. The
job fails only when every repo fails. Failing the whole run for one
unreachable consumer would cost every other repo's findings.

### D4 — Invalid report
An invalid report is retried through the existing loop, then held back from
`main` but kept as an artifact. A missing log directory is published with a
data-gap line, not rejected.

### D5 — Purge of unprocessed docs (Q7: B)
An unprocessed doc is never deleted. "Unprocessed" means the processing
report does not record it. Docs dispatched to a project that failed or closed
without finishing are therefore kept too, which matches the dispatcher's
loop guard: those are a human decision. The backlog under `analysis/` grows
for as long as new docs arrive faster than about 2 a day are processed. That
was accepted when Q7 was answered.

### D6 — Findings check (Q3: A)
The check happens inside the project as a required first step. It does not
add a separate model call before dispatch.

### D7 — Phase split
Six phases, each in its own PR. Every phase except P2 can merge on its own;
P2 must merge after P1 and P3. P1 and P5 both edit "Commit and
push report", and P4 and P2 both edit the analyze step. The merge train orders
those edits. Two orderings are required: P2 needs P1's report artifact and
P3's `coverage.repositories_failed`, so P2 merges after both P1 and P3.

## §18.E Automation summary

- **Scripts:** one new script, `scripts/check_workflow_analysis_report.py`
  (P2), run by the analyze step of `workflow-log-analysis.yml`. It is not
  single-use or long-running and is not a supervisor, so it needs no
  `docs/scripts-pending-removal.md` entry. Other edits extend existing code:
  `scripts/collect_workflow_logs.py`, `scripts/apply_analysis_on_main.sh` and
  the two workflows.
- **Scheduler entry points:**
  - `.github/workflows/test-and-mark-stable.yml` (the `workflow_dispatch` gate
    job that dispatches the log analysis);
  - `.github/workflows/workflow-log-analysis.yml` (`workflow_dispatch`);
  - `.github/workflows/promote-main-to-stable.yml` (daily `0 0 * * *` cycle)
    and `.github/workflows/orchestrate_poll.yml` (verifying run), which both
    call `scripts/apply_analysis_on_main.sh`.
- **Long-running supervisor:** none.
- **DB work:** none.
- **Removal registry:** no entry needed.

## Phases & Merge Strategy

| Phase | Scope | Files | Rollback |
| --- | --- | --- | --- |
| P1 | Report via PR, run branch, `publish-report` job, report artifact | `workflow-log-analysis.yml`, new tests | `WORKFLOW_LOG_ANALYSIS_PUBLISH_MODE=push` |
| P2 | Report structure check, log-gap line | new `scripts/check_workflow_analysis_report.py`, `workflow-log-analysis.yml`, tests | `WORKFLOW_LOG_ANALYSIS_REPORT_CHECK_ENABLED=false` |
| P3 | Collector `coverage`, coverage log line, alert, prompt sentence | `scripts/collect_workflow_logs.py`, `workflow-log-analysis.yml`, `prompts/_templates/mode-workflow-analysis.txt` (+ regenerated `prompts/mode-workflow-analysis.txt`), tests | revert; the extra JSON key is ignored by readers |
| P4 | `since_last_report` input, window marker, gate covers all repos | `workflow-log-analysis.yml`, `test-and-mark-stable.yml`, tests | `WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS=false`; the input defaults to off |
| P5 | The purge keeps every unprocessed doc (D5) | `workflow-log-analysis.yml`, tests | revert |
| P6 | Check findings first in the project description; bound the marker searches | `scripts/apply_analysis_on_main.sh`, `scripts/promote_main_cycle.sh`, `tests/test_apply_analysis_on_main.py`, `tests/test_promote_main_cycle.py`, `README.md` | revert |

Each phase is safe in production at merge:

- New behaviour is on by default with a repo-variable switch, or is additive.
- The system works whichever subset of phases has merged, as long as P2
  merges after P1 and P3.
- P1 alone fixes the delivery outage, so it should be merged first. P2 must
  merge after P1, because it withholds an invalid report from `main` and
  relies on P1's artifact to keep it. P2 must also merge after P3, because
  its failed-repo exemption reads `coverage.repositories_failed`. No other
  phase depends on another.

## Implementation Steps

### P1
1. In `analyze-commit-notify`, add "Upload workflow log analysis report"
   (`actions/upload-artifact`, the same pinned version as the collect-logs
   uploads, `if: always() && steps.analyze.outputs.report_file != ''`,
   `retention-days: 30`). Place it before "Commit and push report".
2. Move the body of "Commit and push report" to
   `scripts/workflow_log_analysis_publish_report.sh`, following §27: keep
   `name:`, `id:`, `if:` and `env:` in the YAML and pass `${{ }}` values
   through `env:` (add `REPORT_FILE`, `RUN_ID`, `RUN_ATTEMPT`, `PUBLISH_MODE`).
   In `pr` mode:
   - keep the purge and the rename-on-collision against `origin/<dispatch
     branch>`;
   - replace `git pull --rebase` and `git push HEAD:<branch>` with a push to
     `refs/heads/auto/workflow-log-analysis-<run_id>-<run_attempt>`;
   - output `report_branch`.

   In `push` mode, run today's loop unchanged.
3. Add `report_branch` to the `analyze-commit-notify` job outputs.
4. In "Commit and push deep audit section" and "Commit and push API redundancy
   section", and in the sync steps that pull `main` to fetch the report (about
   lines 1839 and 2438):
   - when `report_branch` is set, fetch and check out that branch;
   - push to it with `git push origin HEAD:refs/heads/<report_branch>`.

   Otherwise keep today's behaviour.
5. Add a `publish-report` job (env `GH_TOKEN: ${{ secrets.GH_PAT }}`, failing
   with `reason=gh_pat_missing` when it is empty; `needs: [analyze-commit-notify, deep-audit,
   api-redundancy]`, `if: always() && needs.analyze-commit-notify.outputs.report_branch != ''`,
   `permissions: contents: read, pull-requests: write`). Model it on the
   self-test PR step:
   - run `gh pr create` with a body file;
   - lint the body with `scripts/lint_pr_body_auto_close.py`;
   - log `WORKFLOW_LOG_ANALYSIS_PUBLISH outcome=pr_opened|failed reason=`;
   - read 1 open-PR list for an existing PR from the same head before
     creating one.
6. Add the new log prefix to `## Stable log prefixes (contractual)` through an
   `agents.d/` fragment, and add a README row for
   `WORKFLOW_LOG_ANALYSIS_PUBLISH_MODE`.

### P2
1. Add `scripts/check_workflow_analysis_report.py` (stdlib only), as in the
   Approach above.
2. In "Run workflow log analysis", after the non-empty check, run the checker
   when `WORKFLOW_LOG_ANALYSIS_REPORT_CHECK_ENABLED` is `true`. On exit 1 set
   `codex_failure_mode="report_structure_invalid"` and continue the retry
   loop. After the last attempt, call `handle_log_analysis_codex_failure` with
   that mode.
3. Before the analysis, find out whether the `workflow-log-output` directory
   exists. If it does not, log `WORKFLOW_LOG_ANALYSIS_COVERAGE logs=missing`.
   After the analysis, put the data-gap line under the window marker (P4), or
   at the top when there is no marker.
4. Add an `agents.d/` fragment, a README row and a changelog fragment.

### P3
1. In `collect_workflow_logs.py`, collect `repositories_failed` and
   `repositories_truncated` while looping over repos, and add `coverage` to
   the report that `build_report` writes. Exit codes stay the same.
2. In the collect step, after the collector runs, log
   `WORKFLOW_LOG_COLLECT_COVERAGE` from the report with `jq`, plus one
   annotation per failed or truncated repo.
3. Send the alert through the workflow's existing Telegram helper at WARNING
   level.
4. Add the prompt sentence to `prompts/_templates/mode-workflow-analysis.txt`
   and regenerate `prompts/mode-workflow-analysis.txt` with `make generate`.
5. Add an `agents.d/` fragment and a changelog fragment.

### P4
1. Add the `since_last_report` input (string, default `"false"`).
2. In the collect step, add the branch before the `lookback_days` branch, as
   in the Approach. Only parse the first line, matched with an anchored
   regex. A timestamp in the future falls back to `lookback_days`; one more
   than 7 days old is clamped to 7 days.
3. In the analyze step, add the window marker as the report's first line. The
   collect job outputs its start time as `window_end` and the computed
   `window_start`.
4. In `test-and-mark-stable.yml`, change the gate dispatch, gated on
   `vars.WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS` (default `true`).
5. Make sure the P2 checker, if it is present, ignores an HTML-comment first
   line.
6. Add an `agents.d/` fragment, README rows and a changelog fragment.

### P5
1. In the age-based purge loop of "Commit and push report" (or in the
   moved script, if P1 merged first), check every candidate after the
   in-flight check and before `git rm`:
   - when `analysis/recommendation-processing-report.md` does not contain the
     doc's basename (`grep -qF --`), log
     `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc=<path> reason=unprocessed` and
     `continue`.
2. Before the loop, if the processing report file is missing or unreadable,
   set `purge_guard_ok="false"` and log
   `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED reason=processing_report_unavailable`.
3. Update the inline comment above the purge and the README row for
   `WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS`, and add the new reason
   values to the agents file through an `agents.d/` fragment. Add a changelog
   fragment.

### P6
1. Replace the validation paragraph of `project_description` in
   `scripts/apply_analysis_on_main.sh` with the text in the Approach. Keep the
   first line, the guardrail clause, the scope paragraph, the source-doc
   lines and the marker comment byte-for-byte.
2. Extend `tests/test_apply_analysis_on_main.py`. The description must:
   - still start with `Apply analysis recommendations from <basename>\n`;
   - contain `before planning any implementation issue` and
     `If no finding is \`applied-candidate\``;
   - still contain the guardrail clause (`never rename or remove existing
     identifiers without an alias`, `/db/contracts`, `no standalone manual
     scripts`, `never use auto-close keywords against
     ai:orchestrator-tracking issues`);
   - keep the marker lines unchanged.
3. In `unprocessed_docs`, in both modes, stop the scan once the effective
   candidate cap of docs has been printed (`APPLY_ANALYSIS_MAX_CANDIDATES`,
   default 3, raised to `PROMOTE_CYCLE_MIN_DOCS` when that is larger) or
   `APPLY_ANALYSIS_MAX_MARKER_SEARCHES` docs (default 20) have been skipped
   for a dispatch marker, whichever comes first. Both are positive integers
   defaulted in the script. Keep stdout to doc paths only and send the cap
   log to stderr. Return 0 when the candidate cap stopped the scan or every
   candidate was examined, and return 3 only when the marker cap stopped the
   scan with at least one candidate unexamined (see the Approach). Update both callers: list-only
   mode treats 3 as success and appends `capped_behind_markers=true`;
   dispatch mode dispatches the printed docs, or logs
   `reason=scan_capped_behind_markers` when none was printed. Make
   `scripts/promote_main_cycle.sh` skip with
   `PROMOTE_CYCLE_SKIPPED reason=scan_capped_behind_markers` and a
   `::warning::` annotation when the candidates line is capped and its count
   is below `PROMOTE_CYCLE_MIN_DOCS`. Test that:
   - a backlog of 10 unprocessed docs makes 3 searches in either mode;
   - with `PROMOTE_CYCLE_MIN_DOCS=4`, a backlog of 10 unprocessed docs
     prints 4 and logs `APPLY_ANALYSIS_CANDIDATE_CAP raised_to=4`;
   - a backlog of 25 docs that all carry a dispatch marker makes 20 searches,
     prints none and logs `reason=scan_capped_behind_markers`, not
     `all_docs_processed` or `insufficient_docs`;
   - exactly 20 docs that all carry a dispatch marker end as
     `all_docs_processed`, not `scan_capped_behind_markers`;
   - 1 unprocessed doc followed by 25 marker-carrying docs prints that doc,
     lists `count=1 ... capped_behind_markers=true`, and in dispatch mode
     dispatches it;
   - no cap log line appears on stdout in either mode;
   - marker-carrying docs ahead of an unprocessed doc within the marker cap
     do not hide it;
   - list-only mode still reports `count=2` or more when at least 2
     unprocessed docs exist within the bound.

   In `tests/test_promote_main_cycle.py`, next to
   `test_insufficient_docs_skips_before_the_smoke_gate`, test that a capped
   candidates line with `count=0` and one with `count=1` (below the default
   minimum of 2) both skip with `reason=scan_capped_behind_markers` and a
   `::warning::` line, and that an uncapped `count=1` still skips with
   `insufficient_docs`.
4. Add README rows for `APPLY_ANALYSIS_MAX_CANDIDATES` and
   `APPLY_ANALYSIS_MAX_MARKER_SEARCHES`, add both to the env-var header
   comment of `scripts/apply_analysis_on_main.sh`, add the
   `scan_capped_behind_markers` reason to the header of
   `scripts/promote_main_cycle.sh`, and register `APPLY_ANALYSIS_CANDIDATE_CAP`
   through an `agents.d/` fragment.
5. Add a changelog fragment.

## Files & Modules

- `.github/workflows/workflow-log-analysis.yml`: P1, P2, P3, P4, P5
- `.github/workflows/test-and-mark-stable.yml`: P4
- `scripts/workflow_log_analysis_publish_report.sh` (new, moved body): P1
- `scripts/check_workflow_analysis_report.py` (new): P2
- `scripts/collect_workflow_logs.py`: P3
- `prompts/_templates/mode-workflow-analysis.txt`,
  `prompts/mode-workflow-analysis.txt`: P3
- `scripts/apply_analysis_on_main.sh`: P5 (read only), P6
- `scripts/promote_main_cycle.sh`: P6 (new skip reason)
- Tests: `tests/test_collect_workflow_logs.py`,
  `tests/test_workflow_log_analysis_failure_contract.py`,
  `tests/test_apply_analysis_on_main.py`, `tests/test_promote_main_cycle.py`,
  and new
  `tests/test_workflow_log_analysis_publish_report.py` and
  `tests/test_check_workflow_analysis_report.py`
- Docs: `README.md` rows, `agents.d/` and `changelog.d/` fragments per phase

## Data Model / Index Changes

None.

## Tests

- **P1:** a mocked-`git`/`gh` harness for the moved publish script:
  - `pr` mode pushes only `refs/heads/auto/workflow-log-analysis-*` and never
    `HEAD:main`;
  - a GH013 refusal logs `reason=push_rejected_by_ruleset` and exits non-zero;
  - `push` mode keeps today's loop;
  - a name collision is still renamed.

  YAML contract tests check that the artifact upload runs `if: always()`,
  that `publish-report` is `always()`-gated, and that the section jobs push to
  `report_branch`.
- **P2:** the checker's unit tests cover:
  - a valid report;
  - a missing heading;
  - headings out of order;
  - a missing repo subsection;
  - a report that is too small;
  - an HTML-comment first line.

  A YAML contract test checks for the `report_structure_invalid` failure mode
  and the kill switch.
- **P3:** collector tests with a 403 consumer and a capped repo check the
  `coverage` object and that the exit code stays 0. A YAML test checks the
  `WORKFLOW_LOG_COLLECT_COVERAGE` line.
- **P4:** unit tests for the window branch:
  - a valid marker;
  - no marker;
  - a future timestamp (falls back);
  - more than 7 days old (clamped to 7 days);
  - under 1 day old (passed through);
  - a marker found only in history (doc deleted on the tip);
  - `since` set explicitly, which wins.

  A contract test checks that the gate dispatch drops `repos_override` and
  passes `since_last_report=true` when the switch is on.
- **P5:** a purge harness with three docs older than the cutoff: one in the
  processing report (purged), one not in it (kept, `reason=unprocessed`) and
  one named by an in-flight project (kept, `reason=in_flight_project`). A
  missing processing report must purge nothing and log
  `reason=processing_report_unavailable`.
- **P6:** the description tests, the search-bound tests and the
  `tests/test_promote_main_cycle.py` skip tests above.
- **Every phase:** run `python3 scripts/assemble_agents.py check --repo-root
  .`, `tests/test_workflow_file_size_limit.py`, and the existing
  `tests/test_workflow_untrusted_input_contract.py`.

## Risks & Mitigations

- **Report PRs pile up when the review pipeline is slow.** The `publish-report`
  job closes older open `auto/workflow-log-analysis-*` PRs only when their
  report file also exists on `main` (already delivered). Every other open
  report PR is kept, because each one holds a different report.
- **More repos mean a larger prompt and more model cost.** The collector's
  existing `--max-log-runs` (15) and the 4,000-character excerpt caps still
  apply across all repos. P3's coverage line makes truncation visible.
  `WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS=false` is the cost switch.
- **The PAT cannot read a private consumer.** That consumer appears in
  `repositories_failed` with an `::error::` annotation (P3), not silently.
- **A report merges after the next run starts, so windows overlap.** The
  overlap repeats findings, which the apply-analysis check classifies as
  already-implemented. It never leaves a gap.
- **The `analysis/` backlog grows without limit (D5).** It is bounded only
  by how fast the promote cycle processes docs. The growth adds checkout size
  and also API calls: `unprocessed_docs` calls `doc_dispatched_before` (one
  `search/issues` call) for every doc not in the processing report, and one
  failed lookup makes the dispatch skip as `guard_unavailable`. P6 therefore
  bounds the number of searches in both modes (see P6). A failed lookup still
  skips the run, by design. Abandoned docs at the front of the scan can still
  use up the marker cap; that is logged as `scan_capped_behind_markers` with
  a `::warning::` annotation, never as an empty backlog.
- **The section jobs fail after the report branch exists.** `publish-report`
  still opens the PR, and the PR body says which sections are missing.
- **The P6 wording changes how the orchestrator decomposes a project.** Only
  the validation paragraph changes. The first line, the scope paragraph and
  the marker comment stay byte-identical, so the poller's parsing and the
  tracking title are unchanged.

## Rollout

All switches default to the new behaviour and take effect on merge. Rollback
is a repo variable for P1, P2 and P4, or a revert for P3, P5 and P6. Watch:

- `WORKFLOW_LOG_ANALYSIS_PUBLISH outcome=pr_opened` on the next gate run, and
  a new `analysis/workflow-optimization-*.md` on `main` after that PR merges;
- `WORKFLOW_LOG_COLLECT_COVERAGE repos=14` on the first gate run after P4;
- `WORKFLOW_LOG_ANALYSIS_REPORT_CHECK outcome=ok` on the first run after P2.

## References

- Run 42540318556 (GH013 push refusal, 2026-11-09)
- `.github/workflows/nightly-validation-selftest.yml` (PR pattern)
- `analysis/recommendation-processing-report.md`
- `docs/plans/pin-promote-cycle-to-candidate-plan.md` (promote cycle)
