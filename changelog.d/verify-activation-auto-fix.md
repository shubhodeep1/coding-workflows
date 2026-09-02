<!-- changelog: changed -->
- **`/verify-activation` now fixes what it finds.** After grading a project LIVE / DORMANT / INCOMPLETE, the command applies every evidence-based audit finding and every code-only activation gap on a branch, verifies each fix, and opens one ready-for-review PR that lists each issue, why it was an issue, and the fix applied.

The command used to stop at the diagnosis: it reported defects at `file:line` and pointed the operator at `/investigate-issue` or `/code-review --fix` for the remedy. It now carries the remedy itself. A new fix step runs after the verdict, selects the findings that qualify under a written Fix Policy, applies the smallest change that removes each defect, re-runs the check that demonstrated it, and ships the result as a single PR on `claude/verify-activation-<slug>`. The chat report gains `Fix PR:`, `Fixes applied`, and `Not fixed` sections, and the PR body carries a table with the same four columns per fix: the finding, why it is an issue, what changed, and the check that proves it. The verdict is still graded against the default branch before any fix is applied, so a fix PR never upgrades INCOMPLETE to LIVE in the same run, and `/deploy-activate` keeps gating on the reported verdict.

| The numbers that matter | Value |
| --- | --- |
| Findings fixed automatically | EVIDENCE-BASED BLOCKER and CONCERN, plus `CODE-FIXABLE` activation gaps |
| Findings never fixed automatically | HYPOTHESIS, §6 renames, §10 contract changes, §12.D tradeoffs, `OPERATOR` gaps |
| PRs per run | 1, reused across re-runs while it stays open |
| Operator steps performed | 0 — repo-vars, secrets, pin bumps, merges, `@stable` tags stay under `To activate` |
| New report sections | `Fix PR:`, `Fixes applied`, `Not fixed` |

What this means for operators: a `/verify-activation` run on a defective project now ends with a PR to review instead of a list of follow-up commands to type. Activation gaps that need a repo setting, a secret, a merge, or a release tag are still enumerated for you and never performed. Anything the command chose not to fix is listed under `Not fixed` with the reason, and with a Q/A question when a decision would unblock it.

### For contributors

All three copies changed: the repo-local command, the consumer template under `workflow-templates/.claude/commands/`, and consumer copies received on the next `.claude/` sync. The consumer template fixes the `[CONSUMER]` side only: `[UPSTREAM]` findings and the upstream half of a `[BOTH]` finding are reported with a proposed fix at `UPSTREAM_SHA` and routed via `/validate-consumer-issue`, and a consumer session never pushes to `shubhodeep1/coding-workflows` even when that checkout is attached. The fix step runs under CLAUDE.md §12 (PR Review Mode): §6 naming immutability and §10 contracts stay hard rules, every fix must be verified before it is pushed, and the PR body follows §19 (`Refs #N`, never an auto-close keyword) and §20 (a `changelog.d/` fragment when a fix changes observable behaviour). The `/deploy-activate` intro in all three copies now describes its companion as diagnose-and-fix rather than diagnose-only.
