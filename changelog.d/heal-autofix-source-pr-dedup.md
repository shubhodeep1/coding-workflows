<!-- changelog: fixed -->
- **A pull request whose review keeps failing now gets one workflow heal issue, not one per failed run.** Heal fix PRs whose own review fails now count toward the lineage cap instead of starting a new chain.

The workflow failure heal intake (`scripts/workflow_failure_heal_intake.sh`) de-duplicated review/autofix failure reports only by fingerprint, and that fingerprint hashes each run's own failure evidence, so it changed from run to run. PR #4323 opened five `ai:workflow-heal` issues and PR #4348 opened three, two of them (#4409 and #4416) open at the same time. Reports also never carried a lineage generation, so a heal fix PR (branch `ai/issue-<N>`) whose review failed opened a fresh generation-1 heal issue, which got its own fix PR, and the loop went on (#4390 → PR #4394 → #4407 → PR #4408 → #4411 → PR #4413 → #4417). An `autofix_failure` report is now also keyed on its pull request through the heal issue's `<!-- workflow-failure-heal:source=owner/repo#N -->` marker. An open heal issue from the same PR gets an occurrence comment (log `duplicate … match=source`) and no diagnosis model run. A closed one continues its lineage, and so does the heal issue that an `ai/issue-<N>` head branch fixes. `WORKFLOW_HEAL_MAX_LINEAGE_DEPTH` therefore stops the loop.

| The numbers that matter | Value |
| --- | --- |
| Heal issues from review/autofix failures, 2026-09-23 to 2026-09-25 | 18 |
| Same reports replayed through the new decision | 10 opened, 6 occurrence comments, 2 escalations |
| Extra GitHub API calls | 0 (the intake already lists every `ai:workflow-heal` issue) |
| Caps (unchanged) | lineage depth 3, 10 open, 20 per UTC day |

What this means for operators: a failing PR produces one heal issue plus occurrence comments, and a heal-fix chain escalates to a human (`ai:workflow-heal-escalated` + Telegram CRITICAL) at generation 4 instead of looping. Release-run and escalation-label reports are unchanged.

### For contributors

`budget_decision` in `scripts/workflow_failure_heal.py` takes two optional inputs, `source_key` and `linked_heal_issue`. The `budget` CLI takes them as `--source-key` and `--source-head-branch`, and `heal_fix_branch_issue` parses `ai/issue-<N>`. The intake passes both only for `autofix_failure` payloads. A fingerprint duplicate still wins over a source duplicate, and an inherited `source_gen` still wins over both lineage sources.
