<!-- changelog: security -->
- **`/implement-plan-claude` uses a security or validation verdict only when the run covered the project's current code.** An audit of an older commit of the project branch can no longer be taken as this project's clean pass.

Steps 9 and 10 used to accept the first completed candidate whose log named the project branch. When `.claude/scripts/dispatch_workflow.py` could not name the run exactly, an older audit of the same branch could therefore stand in for code it never saw (issue #5841, found by the security audit of project #5016). A read-result stage now notes the project head it finds on origin and no longer merges the default branch into the project branch, not even cleanly; the next stage syncs. Before it uses a clean verdict it reads the project head again, and a head that moved in the meantime (a PR merged into the project branch) counts as a mismatch. It reads a run's verdict only when the log shows both the project branch and that head as the commit covered: `SECURITY_AUDIT_TARGET: branch … range <base>..<sha>` for `security-audit.yml`, `HEAD commit: <sha>` for the validate run. This holds even when GitHub named the run. With several candidates, the stage waits for all of them, and every matching run's verdict and follow-ups count. A mismatch triggers one re-dispatch, and a second mismatch stops the project at `Status: BLOCKED`. The helper's polling fallback also stops counting runs dispatched from another ref, since such a run executes a different copy of the workflow file.

| The numbers that matter | Value |
| --- | --- |
| Commits a verdict must match | 1, the project head the read-result stage finds on origin, re-read before a clean verdict is used |
| Sync merges in a read-result stage | 0 (the next stage syncs) |
| Re-dispatches on a mismatch | 1 per cycle, then `Status: BLOCKED` |
| New helper output fields | none (`new_runs` gains an optional `dispatched_ref` keyword) |
| GitHub API calls | unchanged |
| Workflow or consumer-wrapper changes | none |

What this means for operators: a project's security and validation gates now attest to the code that merges. Legacy projects, which audit the default branch, keep the ref-only check. Consumer repos pick the change up with the next `.claude/` sync.
