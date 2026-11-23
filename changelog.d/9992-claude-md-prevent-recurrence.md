<!-- changelog: added -->
- **`CLAUDE.md` §34, Prevent Recurrence.** Interactive Claude Code sessions must now check, for every problem they solve, whether it can happen again, and put a lasting fix in place.

A session that unsticks a PR, repairs a failed check or works around a pipeline defect has to name the cause from evidence, look for other PRs, issues or runs in the same state, and then fix the cause. A small fix is made in the session; in PR review mode a fix that fits the PR lands in the PR under review. Anything larger becomes a pipeline issue in the repository the session works in, opened in the same turn, with the plan shown in the reply. An issue in any other repository, consumer repos included, and a fix that needs several PRs still wait for the owner's go. When only the owner can act (a variable, a ruleset, a setting), the session asks and names the exact change. The workaround used in the session, such as a hand merge, never counts as the fix.

| The numbers that matter | Value |
| --- | --- |
| New section | `CLAUDE.md` §34 (A to E) |
| PRs found in one deadlock after the first was fixed by hand | 18 (#9987) |
| Repeat review runs found in the same investigation | 181 in 7.5 hours (#9991) |

What this means for operators and consumer repos: the section reaches consumers on the next `@stable` sync, because `workflow-templates/CLAUDE.md` is the same file. Expect sessions to end a fix with a short list of causes and prevention items, and expect more fix issues labelled for the pipeline. Unattended pipelines are not affected; they read `unattended_system_instructions.md`.

### For contributors

§34.B treats the rule as the owner's standing approval to open one prevention issue in the repository the session works in (§23.C, §31.B step 3). It does not cover an issue in another repository, a multi-PR plan PR or the `/implement-plan-claude` dispatch. Ask-first actions elsewhere in `CLAUDE.md` keep their own approval.
