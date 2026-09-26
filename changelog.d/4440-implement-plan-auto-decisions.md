<!-- changelog: changed -->
- **`/implement-plan-claude` no longer stops mid-project to ask clarification questions.** It picks the RECOMMENDED answer, records it, and lists every such decision for human review at the verify-activation and deploy-activate stages.

A new CLAUDE.md §28 covers every `/implement-plan-claude` session after its start-up checks (step 0 permission mode, step 1 plan resolution, step 3 phase checklist). Inside that scope, an intent or design question that §0/§2 would turn into a stop is written in the usual Q/A format and answered with its `(RECOMMENDED)` option. That includes an ambiguous plan step, an edge case the plan leaves open, and a `/verify-activation` finding that needs a decision (the chain now passes `— unattended` to its conformance and activation runs). The work then continues. Each pick is written as an `AD-<n>` line in a new `## Auto-decisions` section of `docs/implement-plan/<slug>.md` and listed in the body of the PR that carries it. The step-12 `/verify-activation` report and the `/deploy-activate` opening message show the whole list and never ask about it. A `change AD-<n> → <letter>` reply is implemented as one PR on `claude/implement-plan-<slug>-decision-changes`, and `/deploy-activate` holds its runbook until that PR merges.

| The numbers that matter | Value |
| --- | --- |
| Questions that still stop the chain | start-up checks (steps 0, 1, 3); used-up caps; security or validation runs that did not succeed; terminal validation classes; §22.B / §23.C / §24.D operations |
| Where decisions are recorded | `## Auto-decisions` in `docs/implement-plan/<slug>.md`, plus the carrying PR's body |
| Where they are reviewed | `/verify-activation` activation-stage report, `/deploy-activate` opening message |
| Notifications per decision | none; the completion `PushNotification` carries the count |

What this means for operators: once you start a Claude-orchestrated project and answer the start-up checks, you are needed at completion, plus any failure escalation. Read the auto-decision list there. Reply `change AD-<n> → <letter>` for any choice you want different; unchanged entries are marked `confirmed` when `/deploy-activate` reaches LIVE. Consumer repos get the same behaviour through the `@stable` sync of `CLAUDE.md` and `.claude/commands/`.

### For contributors

`scripts/ingest_implement_plan_lessons.py` reads only `## Lessons`, so auto-decisions never reach AI memory (covered by `tests/test_ingest_implement_plan_lessons.py`). `tests/test_implement_plan_claude_command.py` pins the §28 text and the review wording in both copies of `verify-activation.md` and `deploy-activate.md`. The AI orchestrator's own clarify auto-answer (`[auto-answered-by-orchestrator]`) is unchanged.
