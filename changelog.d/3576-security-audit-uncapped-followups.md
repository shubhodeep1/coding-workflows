<!-- changelog: changed -->
- **The security audit now opens an `ai:security` follow-up issue for every new finding.** The limit of 3 follow-up issues per UTC week is gone, so no finding is silently deferred.

`scripts/security_audit.sh`, which runs weekly from `.github/workflows/security-audit.yml` in this repo and in every consumer through `workflow-templates/ai-security-audit.yml`, used to stop filing follow-ups once 3 `ai:security` issues with a finding marker existed for the current UTC week. It then logged the rest on the tracker as "Findings deferred by the weekly cap". Deferred findings were never filed later: on 2026-09-24 run 35996690244 surfaced 5 findings on tracker #3576, opened #4398, #4399 and #4400, and left 2 that had to be filed by hand as #4431 and #4432. Every finding that passes the confidence gate and the exclusion catalog now gets its own issue unless an `ai:security` issue, open or closed, already carries its `<!-- ai:security-finding:<id> -->` marker. The existing-issue lookup now reads every `ai:security` issue through one paginated REST listing instead of the first 200. Without the cap, a repo can pass 200 such issues, and a truncated list would re-file old findings.

| The numbers that matter | Value |
| --- | --- |
| Follow-up issues per week | was at most 3, now one per new finding |
| Existing issues read for the duplicate check | was the first 200, now all (1 REST call per 100 issues) |
| Tracker comment lines removed | "Existing follow-up issues this UTC week", "Findings deferred by the weekly cap" |
| Repos affected | this repo and the 13 consumers in `.github/ai/consumer_repos.json`, on the next `@stable` sync |

What this means for operators: expect one `ai:security` issue per distinct finding after each audit, including weeks with more than three. There is no knob to bring a cap back. `SECURITY_AUDIT_ENABLED=false` still turns the audit off for a repo. Findings deferred before this change are not filed retroactively. They show up again only if a later audit re-reports them.

### For contributors

The follow-up planner in `scripts/security_audit.sh` no longer takes the `MAX_FOLLOWUP_ISSUES_PER_WEEK` argument, and it skips pull requests returned by the issues endpoint. `tests/test_security_audit_workflow_contract.py` covers the uncapped filing, duplicate checks across more than one page, and the absence of the cap. The planned `SECURITY_PASS_ADVISORY_FOLLOWUP_CAP` in `docs/plans/security-pass-convergence-plan.md` is dropped as well, so pre-existing advisories are planned without a cap.
