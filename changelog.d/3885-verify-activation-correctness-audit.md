<!-- changelog: changed -->
- **`/verify-activation` now audits the code before it will call a project COMPLETE.** A new `Correctness: PASS / CONCERNS / FAIL` axis runs on every invocation, and a FAIL downgrades `Implemented:` to PARTIAL.

The command used to grade implementation by presence: it checked that the files existed and the linked PRs were merged, then spent its depth on the activation question. A feature that was fully wired, correctly triggered, and subtly wrong came back LIVE. The implementation check now maps every acceptance criterion to the code that satisfies it, and a new audit step reads that code against plan conformance, security, error paths, concurrency and idempotency, naming immutability (§6), DB contracts (§10), API budget (§15), automation bias (§18), test coverage, and docs. It also runs the repo's existing tests and linters against the local checkout, in a form that changes nothing: no edits, commits, pushes, workflow dispatches, or network mutations, with formatters in check mode only.

Findings are ranked BLOCKER or CONCERN and classified EVIDENCE-BASED or HYPOTHESIS, so an unverifiable concern cannot pass silently as PASS, and a check that could not run is reported as `could-not-run` instead of being dropped. The verdict vocabulary is unchanged: `LIVE / DORMANT / INCOMPLETE` and `COMPLETE / PARTIAL / NOT` keep their meanings, and a FAIL reaches `/deploy-activate` through the PARTIAL to INCOMPLETE path its completeness gate already stops on.

| The numbers that matter | Value |
| --- | --- |
| Audit surface | Files the linked merged PRs touched, files the plan names, immediately reachable call sites |
| New report lines | `Correctness:`, `Audit findings`, `Checks run` |
| Verdict tokens added | 0 — the axis is separate from LIVE / DORMANT / INCOMPLETE |
| `/deploy-activate` changes needed | 0 |
| Extra API calls | One `pulls/<N>/files` call per linked PR |

What this means for operators: a `/verify-activation` run takes longer and tells you more. LIVE now means implemented, audited, and running, rather than wired and running. A project whose code does not do what its plan said comes back INCOMPLETE with the defect cited at `file:line`, and `/deploy-activate` refuses to emit a runbook for it.

### For contributors

Both copies of the command changed. The consumer template copy is side-aware: it audits at the ref for the side under test (`THIS_REPO@main` or the resolved `UPSTREAM_SHA`, never upstream `main` when the consumer is pinned to a release), adds a wrapper-to-upstream input contract check, tags findings `[CONSUMER]` or `[UPSTREAM]`, and records upstream checks it cannot execute as `could-not-run` rather than treating unread code as correct. The command stays read-only and never fixes what it finds; remediation routes to `/investigate-issue`, `/code-review --fix`, or `/validate-consumer-issue` for an upstream defect.
