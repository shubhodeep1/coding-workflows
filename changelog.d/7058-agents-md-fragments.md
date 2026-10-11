<!-- changelog: added -->
- **PRs now document `agents.md` through `agents.d/` fragments that release automation folds in, so concurrent PRs stop conflicting on the agents file.**

`agents.md` was the most common merge-train conflict: on 2026-10-10 it conflicted in 111 of the train's 149 blocked PR pairs, and was the only conflict in 25 of them. Each PR now adds `agents.d/<issue-or-pr>-<slug>.md`, whose `<!-- agents: section="<heading>" -->` blocks are appended to the named section; new log prefixes land at the end of both registries. `scripts/assemble_agents.py` folds the fragments in the release job (same commit as the changelog) and in consumer repos on the daily `update_workflows.yml` sync, and CI fails a fragment that would not fold. The model's static context, the AGENTS.md materiality checks and the scope guards treat a pending fragment like an `agents.md` edit, so documentation is not lost between releases and orchestrator sub-issues that list `agents.md` are not scope-blocked. CLAUDE.md §30 sets the rule.

| The numbers that matter | Value |
| --- | --- |
| Blocked merge-train pairs with an `agents.md` conflict (2026-10-10) | 111 of 149 |
| Pairs where `agents.md` was the only conflict | 25 |
| Fold points | release job, consumer sync |

What this means for operators and consumer repos: fewer merge-train holds and resolver runs on documentation; consumer `AGENTS.md` files gain the same fragment workflow on the next `@stable` sync.

### For contributors

Fragments are append-only; correcting existing text still edits the file directly. Use ` new` in the marker only to create a section. `tests/agents_doc.py` is the reader for tests that assert documented content.
