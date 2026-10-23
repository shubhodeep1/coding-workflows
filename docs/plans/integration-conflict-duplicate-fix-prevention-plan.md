# Integration conflicts from duplicate fixes: scope security-pass fixes, let the default branch supersede

> **Status: proposed, awaiting operator approval (2026-10-23).** Nothing in
> this plan is implemented. Items 1-3 of the same incident follow-up (the
> `.ai/validate.yml` take-base, REST retry-state persistence and the
> integration park / judge context) ship in the PR that adds this file; see
> `changelog.d/3933-integration-conflict-recovery.md`.

## Summary

Two orchestrator projects in tele-funtoken-msg-scoring, #3928 and #4038, each
ran a mandatory security pass. Both passes found the same **pre-existing**
weaknesses in shared referral, payout and fantasy-pot code, and each project
fixed them on its own integration branch, with a different design. #4038's
final PR #4045 merged to `main` at 01:04Z on 2026-10-23. #3928's final PR
#3933 then conflicted on 24 files, and no automated path could reconcile two
independent rewrites of the same security control.

This plan prevents that in two places:

- **D1 (item 4): scope the fix.** A security-pass finding drives a fix-up on
  the integration branch only for the project's own lines. Pre-existing
  defects in shared code, including the "sibling code paths" the fix-up issue
  currently asks the implementer to sweep, go to **one** standalone fix on the
  default branch, shared by every project that reports them.
- **D2 (item 5): let the default branch win when it already carries the
  fix.** The integration judge gets a `supersede_with_base` verdict. It names
  the merged sub-issues whose intent the default branch's design replaces.
  Their intent fingerprints can then be dropped and the conflict resolved to
  the default branch's version, but only after a label or a human ack.

## Automation & Wiring (§18.E)

- **New script vs extension:** no new standalone script. D1 extends the
  security-pass fix-up builder and finding classification in
  `scripts/orchestrate_poll_process.sh` (`create_security_pass_fix_issue`
  ~L7219, `security_pass_record_line_ownership_advisories` ~L7579,
  `create_security_pass_advisory_followup` ~L7660). D2 extends
  `invoke_judge_for_integration_conflict` and `heal_integration_branch_conflict`
  in the same file, plus `scripts/verify_integration_fingerprints.py` and
  `scripts/review_conflict_prepare.sh` (fingerprint file filtering).
- **Scheduler entry point:** the existing orchestrator poller,
  `.github/workflows/orchestrate_poll.yml` (cron `*/5 * * * *` through
  `internal-orchestrate-poll.yml` here and the synced
  `workflow-templates/ai-orchestrate-poll.yml` in consumers), and the existing
  `review_autofix.yml` resolver dispatch. No new trigger.
- **Long-running supervisor:** none; the poller is the supervisor.
- **DB work:** none (state lives in the tracking-issue state comment and
  ai-memory, as today).
- **Removal registry (§18.F):** no single-use or long-running script is added,
  so no `docs/scripts-pending-removal.md` entry.

## Background: why the existing line-ownership routing did not stop it

`security_pass_record_line_ownership_advisories` already waives a finding
whose *cited line* predates the project (`git blame` outside
`merge_base..head`) and files a deferred `ai:security` advisory follow-up.
That did not help here, for two reasons:

1. The fix-up issue body (`scripts/orchestrate_poll_process.sh` ~L7250) tells
   the implementer: "Fix every instance of each finding's defect class across
   this project's changes, not only the cited line: the re-audit checks
   sibling code paths ... and re-opens the loop if any remain." A finding cited
   on a project-owned line therefore leads to edits in pre-existing sibling
   code, which line ownership never sees. Example: #4710 (project #4038, fix
   cycle 3) asked for provenance stamping on every `manual_unblocks` writer.
2. Each project's advisory follow-ups are deferred until *that* project
   merges, and they are keyed per project. Two projects that hit the same
   pre-existing weakness get two independent remedies, and no shared fix
   exists on `main` to rebase onto.

## D1: scope security-pass fixes to the project's own diff

### Behaviour

1. **Split each blocking finding's remedy by line ownership.** Keep the
   blocking fix-up for edits on project-owned lines (the D2′ rule of
   `docs/plans/security-pass-convergence-plan.md` is unchanged: a project line
   keeps blocking). Replace the "sibling code paths" instruction with:
   "Fix the defect on the lines this project wrote. If the same defect class
   exists in code the project did not write, do not edit it; list it under
   `Pre-existing instances` in the PR body." The re-audit treats a sibling
   instance on a pre-existing line as advisory (the line-ownership rule
   already does this for cited lines), not as a re-open.
2. **One shared default-branch fix per pre-existing defect.** The advisory
   follow-up for a pre-existing finding is filed **immediately**, not
   deferred until the project merges, as a standalone `ai:security` issue
   targeting the default branch. It is deduplicated across projects by a
   stable key: `<!-- ai:security-shared-fix:v1 key=<sha256(file, defect class, enclosing symbol)> -->`.
   A second project reporting the same key comments on the existing issue
   ("also reported by project #N") instead of filing a new one.
3. **Projects wait for the shared fix, not re-implement it.** When a
   project's blocking finding depends on a pre-existing instance being fixed
   first, the project fix-up carries `- Depends on: #<shared issue>` (the
   security dependency hold from issue #4934 already gates on this). When the
   shared fix merges, the next integration sync brings it into the branch
   through the normal merge, so there is one design to merge.

### Edge cases

- Shared issue closed without merge: the dependency hold releases only on
  `ai:merged` (existing behaviour), so the project stays held. It is routed to
  the unblock judge after the existing 24h window.
- Finding has no enclosing symbol (top-level code): the key falls back to
  `file + defect class + line bucket (line // 50)`.
- `SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED=true` today defers *all*
  advisories. D1 adds `SECURITY_PASS_SHARED_FIX_IMMEDIATE` (default `true`)
  for pre-existing-line advisories only; the deferral stays for the rest.

### API budget (§15)

The dedup lookup reuses the security audit's existing paginated `ai:security`
issue listing (`security_audit.sh` already reads it for the finding marker
dedupe). One issue create or one comment per new key. No per-finding reads.

## D2: `supersede_with_base` judge verdict

### Behaviour

1. **New judge action.** `invoke_judge_for_integration_conflict` accepts
   `{"action":"supersede_with_base","diagnosis":"...","superseded_sub_issues":[<issue numbers>],"resolution_guidance":"..."}`.
   It is valid only when every listed issue is a merged sub-issue in the
   tracking state, and when the judge cites, for each one, the default-branch
   commit or PR that carries the replacing design (validated: the SHA is an
   ancestor of `origin/<default>`, or the PR merged into it).
2. **Gate: label or human ack.** The poller never acts on the verdict alone.
   It posts a tracking comment with the verdict, the per-sub-issue
   fingerprints that would be dropped, and the files that would take the
   default branch's version. It applies `ai:supersede-pending` and waits for
   either (a) the operator adding `ai:supersede-approved` to the tracking
   issue, or (b) a trusted (OWNER/MEMBER/COLLABORATOR) `/approve-supersede`
   comment that names the verdict marker's head SHA. Either signal is checked
   against the current final-PR head; a new head invalidates the pending
   verdict.
3. **After approval.** The poller records
   `superseded_issue_fingerprints: {<issue>: {by: <sha|pr>, approved_by, at}}`
   in the tracking state and leaves `merged_issue_fingerprints` unchanged (it
   is the audit trail). `review_conflict_prepare.sh` writes the fingerprint
   file without the superseded issues, and the integration resolver prompt
   tells the model that those sub-issues' intent is replaced by the default
   branch's design, so it takes `main`'s side on those hunks.
   `verify_integration_fingerprints.py` skips superseded issues and logs
   `INTEGRATION_FINGERPRINT_SUPERSEDED issue= by=`.
4. **Completeness checks.** The project's completeness and activation
   verification read `superseded_issue_fingerprints` so a superseded
   sub-issue counts as delivered by the default branch, not as a gap.

### Edge cases

- Judge names a sub-issue that is not merged, or cites a commit not on the
  default branch: the verdict is rejected as `invalid` (the existing
  `INTEGRATION_JUDGE_INVALID` path; retried within the lifetime budget).
- Approval arrives after the head changed: ignored, with a tracking comment
  asking for re-approval of the new verdict.
- Partial supersede (some files need synthesis, others take `main`):
  `resolution_guidance` covers the rest, and the resolver still runs on the
  non-superseded conflicts.

### API budget (§15)

The approval check reads the tracking issue labels and comments this tick
already fetches. One label write and one comment per verdict.

## Related gap (not in D1/D2): re-arming a parked head

The park shipped with this plan (`_integration_conflict_park_if_stuck`) and
the resolver's first-failure escalation both hold until the final PR's head
changes. A newer default branch, or a newer review-support version that can
now resolve the same conflict (for example the `.ai/validate.yml` take-base),
does not re-arm them. The review gate already re-arms its non-retryable cap
once per new support SHA (#6911). A follow-up could record the default-branch
SHA and support SHA with the park and allow one resolver dispatch (not a judge
call) when either changes. Until then, a push to the integration branch,
which an operator or a branch rebuild makes, resumes healing.

## Tests

- D1: `tests/test_security_pass_shared_fix.py`. Covers the fix-up body
  wording; key derivation stability; dedup across two simulated projects
  (second one comments, does not file); the `Depends on` line; immediate
  filing with deferral on; the fallback key without an enclosing symbol.
- D2: extend `tests/test_orchestrator_judges_claude_engine.py` and
  `tests/test_integration_conflict_park.py`. Covers schema validation of the
  new action; rejection for unmerged or unknown sub-issues and off-branch
  commits; no action without approval; label approval; comment approval from
  trusted and untrusted authors; invalidation on a new head; the fingerprint
  file filter in `review_conflict_prepare.sh`; and the verifier skip log line.
- Both: wire new test files into `.github/workflows/ci.yml`
  (`tests/test_ci_wires_every_test_file.py`).

## Rollout

Both are behind switches that default on (`SECURITY_PASS_SHARED_FIX_IMMEDIATE`)
or off (`INTEGRATION_JUDGE_SUPERSEDE_ENABLED`, default `false` until one
supervised use on a consumer). Ship D1 first: it removes the source of the
duplicate designs. D2 is the escape hatch for projects already in flight.

## Open questions for the operator

> **Q1: Should D1 file shared default-branch fixes immediately, or keep the current "after the project merges" deferral and only add the cross-project dedup?**
>
> Choices:
> - **A**: File immediately and dedup across projects (projects wait on it through `Depends on`). (RECOMMENDED)
> - **B**: Keep the deferral; dedup only.
>
> Reply: `Q1: A`

> **Q2: Which approval signal gates a `supersede_with_base` verdict?**
>
> Choices:
> - **A**: Either the `ai:supersede-approved` label or a trusted `/approve-supersede <head-sha>` comment. (RECOMMENDED)
> - **B**: Label only.
> - **C**: Comment only.
>
> Reply: `Q2: A`

> **Q3: Ship D2 default-off behind `INTEGRATION_JUDGE_SUPERSEDE_ENABLED`?**
>
> Choices:
> - **A**: Yes, default `false` until one supervised consumer use. (RECOMMENDED)
> - **B**: Default `true` (the approval gate is the safety).
>
> Reply: `Q3: A`
