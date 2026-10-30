<!-- changelog: added -->
- **Heal issues can carry an evidence pack: the failed run's own step timeline and the linked runs' head commit and branch.** Turn it on with the repo variable `WORKFLOW_HEAL_EVIDENCE_PACK_ENABLED=true`; it is off by default.

During the 2026-10-27..29 release-gate incident, heal plans ended `HEAL_COUNTERFACTUAL: UNVERIFIED` because the issue named the failing step but showed neither how long each step of the failed run took nor which commit and branch the linked runs ran on. With the switch on, the heal intake adds an `## Evidence pack` section to the issue body. It holds the failed run's step timeline, with `[LONG]` marks, and the linked runs' timelines, each with a `head_sha` / `head_branch` line. The pack is built from run and job data the intake already downloaded, after the heal scope is frozen, so it adds no GitHub API calls and never changes the scope marker. Job, step, run and branch names are escaped and labelled untrusted, and the whole body is still checked by the routing validator before the issue is filed.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_EVIDENCE_PACK_ENABLED=false`) |
| Pack size cap | 12000 bytes by default (`WORKFLOW_HEAL_EVIDENCE_PACK_MAX_BYTES`, clamped to 1024-20000) |
| Extra GitHub API calls | 0 |
| Log line | `WORKFLOW_HEAL evidence_pack outcome=ok\|partial\|skipped bytes=<n> [reason=<token>]` |

What this means for operators: with the switch off, heal issues are byte-identical to before. With it on, a heal plan can read step durations and head refs straight from the issue. A pack that hits the cap is cut, closed cleanly and logged as `outcome=partial reason=truncated`.
