<!-- changelog: added -->
- **New runner concurrency rule for workflow changes.** Interactive sessions and the unattended pipeline now have to try to run workflow work on fewer runners before adding a job, without slowing delivery.

The account's concurrent-job limit is shared by every workflow in every repo, and from 2026-11-11 the Actions queue was jammed (487 queued runs in coding-workflows on 2026-11-15, `docs/plans/reduce-actions-run-volume-plan.md`). The new rule sits next to the GitHub API rate-limit rule. Before adding a job, fold the work into an existing job on the same trigger when its time limits allow. Filter at the trigger so empty runs are never created, coalesce superseded runs with `concurrency:` groups, and batch fan-out. A new job that runs on every PR, push, comment or schedule needs a stated reason and a before/after jobs-per-event count, or review blocks it. Jobs that are required checks or are cited by id stay as they are.

| The numbers that matter | Value |
| --- | --- |
| `CLAUDE.md` section (interactive Claude, synced to consumers) | §33 |
| `unattended_system_instructions.md` section (pipeline) | §26 |
| `codex.md` section (interactive Codex) | §16 |
| Headroom a merged job must keep below its `timeout-minutes` and the 360-minute hosted-job limit | 25% |

What this means for consumer repos: the next `@stable` sync delivers the rule in `CLAUDE.md` and `/write-plan`. Plans and PRs that add workflow jobs will now state which existing job could not take the work and why.

### For contributors

`/write-plan` asks a "Runner concurrency" clarification question and binds plans to §33. The workflow-audit prompt (`prompts/_templates/mode-workflow-audit.txt` and `prompts/mode-workflow-audit.txt`) checks for runner concurrency under its existing Section 5, so the section headings `workflow-log-analysis.yml` checks for are unchanged. Documentation only, no CI check counts jobs.
