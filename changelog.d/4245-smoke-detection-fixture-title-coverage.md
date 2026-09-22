<!-- changelog: fixed -->
- **Release runs no longer page operators with per-phase clarify and plan alerts.** The smoke-fixture detectors missed two of the gate's four fixture title shapes, and missed orchestrator-decomposed children entirely.

Silencing a release run has three links: detect the fixture, export `ALERT_MSG_LEVEL=SILENT`, and have every Telegram step read that job-level value. The earlier fix repaired the third link across 18 step declarations, but left the first one narrow, so correctly-plumbed steps kept sending alerts at the repo's default `DEBUG`. `clarify.yml`, `plan.yml`, `implement.yml` and `review_autofix.yml` matched the literal `[E2E Smoke Test]`, which never matched `[E2E Clarify Negative Test]` (different middle token) or `[E2E Smoke Test alt-model]` (no `]` directly after `Test`). All four now use `^\[E2E `, covering every shape `test-and-mark-stable.yml` builds. `plan.yml` additionally resolves the parent tracking issue when a child's own title carries no marker: the `orchestrate-decompose-test` job hands a `[E2E Orchestrate Smoke <run_id>]` project description to the decomposer, which writes its own child titles, so no title pattern can match them. It reuses the `^\[Orchestrator\] E2E ` parent signal `orchestrate_clarify_respond.yml` already relies on.

| The numbers that matter | Value |
| --- | --- |
| Detection sites corrected | 6 across 4 workflows |
| Fixture title shapes now matched | 4 of 4 (was 2 of 4) |
| Extra API calls per plan run | at most 1, only when the title check misses and a `Tracking issue: #N` ref is present |
| Contract test | `tests/test_smoke_alert_silencing_contract.py` (12 tests, was 7) |

What this means for operators: a release or promote run posts its `Release … SUCCEEDED` / `FAILED` message and nothing else. Run 35672590166 sent a `🚨 CRITICAL: Clarification required` for the negative-test fixture plus two `🔍 DEBUG: Implementation plan generated` pings for the decomposed canary children; all three are now suppressed. Alerts on real issues are unchanged, and the parent lookup fails open, so an unreachable tracking issue leaves a genuine project's alerts switched on rather than silencing it.

### For contributors

Anchoring at `^` also closes a false positive the previous `\[E2E Smoke Test\b` pattern had in `implement.yml`: a production issue titled e.g. "Fix [E2E Smoke Test] flake" was treated as the fixture itself and silenced. `review_autofix.yml`'s PR title/body check stays deliberately unanchored, because a smoke PR is titled "AI implementation for issue #N" and carries the fixture tag mid-body; only its linked-issue lookup is anchored. The contract test now pins the detection half of the chain as well as the plumbing: it derives the fixture tag set from `test-and-mark-stable.yml` itself, so a newly added fixture shape that no detector matches fails CI instead of reaching an operator's phone. Narrowing either the detector constant or any workflow's use of it fails a test.
