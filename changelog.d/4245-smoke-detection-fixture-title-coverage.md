<!-- changelog: fixed -->
- **Release runs no longer page operators with per-phase clarify and plan alerts, and a PR that merely discusses the smoke fixtures is no longer mistaken for one.** The fixture detectors missed two of the gate's four title shapes, missed orchestrator-decomposed children entirely, and classified PRs from body prose.

Silencing a release run has three links: detect the fixture, export `ALERT_MSG_LEVEL=SILENT`, and have every Telegram step read that job-level value. The earlier fix repaired the third link across 18 step declarations, but left the first one narrow, so correctly-plumbed steps kept sending alerts at the repo's default `DEBUG`. `clarify.yml`, `plan.yml`, `implement.yml` and `review_autofix.yml` matched the literal `[E2E Smoke Test]`, which never matched `[E2E Clarify Negative Test]` (different middle token) or `[E2E Smoke Test alt-model]` (no `]` directly after `Test`). All four now use `^\[E2E `, covering every shape `test-and-mark-stable.yml` builds.

Orchestrator-decomposed children carry no marker at all: the `orchestrate-decompose-test` job hands a `[E2E Orchestrate Smoke <run_id>]` project description to the decomposer, which writes its own child titles, so no title pattern can match them. `plan.yml` and `implement.yml` now resolve the parent tracking issue instead, reusing the `^\[Orchestrator\] E2E ` signal `orchestrate_clarify_respond.yml` already relies on. In `implement.yml` this also restores the atomic `force-review` + `e2e-smoke-test` labels on such children's PRs, which `IS_SMOKE_TEST` gates.

`review_autofix.yml` now classifies a PR from that `e2e-smoke-test` label rather than by scanning the PR title and body for a fixture tag. The old scan matched any real PR that merely discussed the tags, which pinned reviewer and editor reasoning, silenced that PR's review alerts, and exported `IS_SMOKE_TEST` so `scripts/review_apply_fixes.sh` appended a "must call `apply_patch` on `tests/e2e_smoke_canary.txt`" directive to the editor prompt, on a PR that had nothing to do with the canary. The label is reliable because `implement.yml` applies it atomically at `gh pr create`, so it is already in the `pull_request: opened` payload the gate snapshots. The anchored linked-issue-title check stays as a second signal.

| The numbers that matter | Value |
| --- | --- |
| Detection sites corrected | 7 across 4 workflows |
| Fixture title shapes now matched | 4 of 4 (was 2 of 4) |
| Extra API calls | at most 1 per plan run and 1 per implement run, only when the title check misses and a `Tracking issue: #N` ref is present |
| Contract test | `tests/test_smoke_alert_silencing_contract.py` (14 tests, was 7) |

What this means for operators: a release or promote run posts its `Release … SUCCEEDED` / `FAILED` message and nothing else. Run 35672590166 sent a `CRITICAL` "Clarification required" for the negative-test fixture plus two `DEBUG` "Implementation plan generated" pings for the decomposed canary children; all three are now suppressed. Alerts on real issues are unchanged, and both parent lookups fail open, so an unreachable tracking issue leaves a genuine project's alerts on rather than silencing it or mislabelling its PR.

### For contributors

Anchoring at `^` also closes a false positive the previous `\[E2E Smoke Test\b` pattern had in `implement.yml`: a production issue titled e.g. "Fix [E2E Smoke Test] flake" was treated as the fixture itself and silenced. The parent lookup is resolved once in `implement.yml`'s precheck step and exported as `IS_SMOKE_PARENT`, so the main detect step reuses it instead of issuing a second call. `PR_TITLE` is retained in `review_autofix.yml`'s detect step although nothing reads it any more, because §6 forbids removing an existing identifier without the ask flow.

The contract test now pins the detection half of the chain as well as the plumbing: it derives the fixture tag set from `test-and-mark-stable.yml` itself, so a newly added fixture shape that no detector matches fails CI instead of reaching an operator's phone. Narrowing the detector constant, narrowing any workflow's use of it, reintroducing free-text PR-body matching, or dropping the parent lookup each fail a test.
