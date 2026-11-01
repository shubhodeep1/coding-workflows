<!-- changelog: added -->
- **Heal issues can now target the branch the failing code actually ran from.** Behind `WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED` (off by default) the heal intake looks at the child run the failed release or promotion run was watching, such as the implement run in the 2026-10-27 release-gate incident that ran code from `main` while the release run itself was on `stable`. When that child failed or stalled on another branch and its workflow is in the heal issue's frozen scope, the heal issue targets the child's branch instead, and its body says `Code ran from: <branch>@<sha>`.

The child comes from the runs the failed logs link to, or from the evidence pack's issue-linked runs. Fork runs, orchestrator integration branches, heal fix branches and the `promote-candidate` mirror are never used. The new target must still pass the issue-body routing check and must exist; otherwise the heal keeps the target it would have had before.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED=false`) |
| Extra GitHub API calls | 0 with evidence-pack data; otherwise at most 3 run reads per report |
| Log line | `WORKFLOW_HEAL code_ref outcome=<ok|skipped|partial> reason=<token>` |

What this means for operators: nothing changes until the switch is on, and heal issues already filed keep their target branch.
