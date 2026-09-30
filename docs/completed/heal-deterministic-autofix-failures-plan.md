# Workflow failure heal: catch deterministic review/autofix failures

## Summary

Make the review/autofix pipeline stop retrying failures that reproduce
identically on the same head, and make the workflow failure heal path
actually receive and correctly route them. Four independently mergeable
phases: P1 fixes the `repository_dispatch` envelope that has silently
rejected every heal report so far and makes the rejection visible; P2 adds
an identical-failure fingerprint cap that routes a PR to `ai:review-blocked`
after three identical failures on one head; P3 teaches the heal intake a
"self-inflicted" classification that sends the fix to the branch that owns
the crash instead of `stable`; P4 adds an editor preflight and a
main-pinned-script divergence check so a deterministic editor failure costs
seconds, not a 75-minute reviewer pass.

## §18 Automation surface (plan output requirements, CLAUDE.md §18.E)

- **Scripts:** every phase modifies existing code only. P1 edits
  `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_report.sh`,
  `scripts/workflow_failure_heal_autofix_report.sh` and
  `.github/workflows/workflow-failure-heal-intake.yml`. P2 edits
  `scripts/workflow_failure_heal.py` and `.github/workflows/review_autofix.yml`.
  P3 edits `scripts/workflow_failure_heal.py`,
  `scripts/workflow_failure_heal_intake.sh`,
  `scripts/workflow_failure_heal_autofix_report.sh` and
  `prompts/mode-workflow-failure-heal.txt`. P4 edits
  `scripts/review_apply_fixes.sh`, `scripts/stage_workflow_support.sh` and
  `.github/workflows/review_autofix.yml`. No standalone manual scripts
  (§18.A) and no new script files.
- **Scheduler / trigger entry point:** the existing review triggers,
  `.github/workflows/internal-review.yml` (this repo, `pull_request` and
  `workflow_dispatch`) and the consumer wrapper `workflow-templates/ai-review.yml`,
  both calling the reusable `.github/workflows/review_autofix.yml`; the
  existing dispatchers that re-run it (`.github/workflows/review_autofix_sweep.yml`
  every 30 minutes, `scripts/orchestrate_poll_process.sh` stall recovery, the
  post-commit continuation dispatch in `review_autofix.yml`); and the heal
  intake `.github/workflows/workflow-failure-heal-intake.yml`
  (`repository_dispatch`, event `workflow-failure-heal`). No wrapper edits.
- **Supervisor:** none. Every piece runs inside an existing per-PR or
  per-report one-shot job.
- **DB operations:** none. No MongoDB collections, indexes or contracts are
  touched (§10 not applicable).
- **§18.F registry:** no entry. No new script, supervisor or single-use job is
  introduced.

## Context

Two review/autofix runs on 2026-09-22 failed the same way for hours and
neither retry loop nor the heal path could end it. All line references are
to `main` at `4b9c501` (2026-09-22 08:42 UTC).

**PR #4259 (`ai/issue-4255`, run 35713627310 and six earlier runs).** The
PR's own change to `scripts/opencode_helpers.sh` saves the provider key and
runs `unset OPENROUTER_API_KEY` at source time; the base branch's editor
guard `: "${OPENROUTER_API_KEY:?…}"` in `scripts/review_apply_fixes.sh`
(line 168 on that branch) runs after that source and was not updated. Every
run spent about 75 minutes in reviewers and the consolidator, then the editor
exited in one second, the "Post editor summary comment" step
(`review_autofix.yml:4501`) posted "AI review/autofix produced no output —
will retry", exported `AUTOFIX_EDITOR_EMPTY_NOOP=true` and exited 1. That
flag suppresses "Mark linked issues review-blocked (workflow failure)"
(`review_autofix.yml:7285`), so nothing escalated; the sweep and the stall
poller re-dispatched roughly hourly (issue #4255 sits at `ai:done`, whose
recovery cap `MAX_STALL_RECOVERIES_DONE` defaults to 99,
`scripts/orchestrate_poll_process.sh:1653`). Seven identical failures on head
`f9d7196`.

**PR #4273 (`ai/issue-4270`, run 35719487197).** The integration branch
`orchestrator/project-4139` (commit `125e602`, PR #4261) added a
linked-issue metadata digest step to `review_autofix.yml` and, in the same
PR, added `review_collect_pr_metadata.sh` to `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS`
(`scripts/stage_workflow_support.sh:68`), which stages the `main` copy and
ignores the branch copy (`:90-100`). `main`'s copy never writes the file,
so the digest step fails in every direct `review_autofix.yml` dispatch on a
project-4139 branch (the post-commit continuation dispatch,
`review_autofix.yml:6184`, runs the branch YAML; `internal-review.yml:55`
runs `review_autofix.yml@main` and passes). The same failure hit PR #4261's
own branch (run 35692710132). agents.md "Review self-repo support staging
runs under main's workflow YAML" records the earlier PR #4174 incident of
this class.

**The heal path never heard about either.** Run 35713627310 logged
`WORKFLOW_HEAL_AUTOFIX_REPORT skip reason=dispatch_denied pr=4259
failure=editor_empty_noop streak=6`. The reporter
(`scripts/workflow_failure_heal_autofix_report.sh:203`) discards the POST's
stderr. The dispatch body is `{event_type, client_payload: <report>}` where
the report (`build_autofix_failure_payload`,
`scripts/workflow_failure_heal.py:396-443`) has 23 top-level keys and the
issue reporter's (`build_issue_payload`, `:287`) has 20. GitHub's
`repository_dispatch` accepts at most 10 top-level `client_payload`
properties and answers 422, which `gh_retry` classifies as permanent. The
intake workflow has zero `repository_dispatch` runs in its last 100; every
autofix report so far ended in `reporter_missing` or `dispatch_denied`.

**Why plain wiring is not enough.** The intake opens `ai:workflow-heal`
issues with `Target branch: stable` (`scripts/workflow_failure_heal_intake.sh:567-580`).
Both defects above live on unmerged PR or integration branches and do not
exist on `stable` or `main`; a `stable`-targeted issue would fix nothing and
could loop (heal issue → implement PR → same review failure → heal report).

**Relationship to the existing plan.** `docs/plans/review-autofix-deterministic-editor-failure-resume-plan.md`
(unimplemented; no code or flags from it exist on `main`) proposed in its P1
an elapsed-time heuristic for fast editor exits. This plan's P2 supersedes
that P1: a fingerprint of the failure itself is a stronger signal than
elapsed time and covers failures outside the editor step. That plan's P2
(resume-round recovery) is untouched and still valid.

Constraints that bind the design: §6 (every env var, log prefix, payload
field, comment marker and classification token is an identifier: add, never
rename or remove), §15 (one new API call in P2, justified below; everything
else reuses data the run already fetched), §18 (no new scripts, no manual
steps), §19 (`Refs #N` in every body that mentions an orchestrator tracking
issue), §20 (one changelog fragment per phase PR), §23.C (no PR is merged
and no workflow is dispatched from the planning session).

## Decisions

All decisions were taken in the clarification round; the orchestrator has
no open choice.

### D1 — Cap enforced inside `review_autofix.yml`, not in the dispatchers (Q1: A)

The identical-failure check runs in the review workflow itself, so every
dispatcher (sweep, poller, continuation dispatch, `pull_request`) is covered
by one mechanism. It runs in the `gate` job, before `codex-agent` starts,
because a deterministic failure can occur in `codex-agent` before PR comments
would otherwise be read (PR #4273 failed in "Collect PR metadata", which is
the step that fetches them). Alternative considered: a check in
`review_autofix_sweep.yml` and the poller (one extra comments call per open
PR per tick and no coverage of the continuation or `pull_request` paths).

### D2 — Fingerprint = head SHA + failure reason + normalised error signature (Q2: A)

The failure comment the workflow already posts carries an HTML marker
`<!-- review-autofix-failure:v1 head=<sha> reason=<failure_reason> fp=<sha256> -->`.
`fp` is `fingerprint("review_autofix", failure_reason, error_signature(evidence))`
using the existing helpers (`scripts/workflow_failure_heal.py:593,610`).
Evidence is the bounded stderr tail of the failing stage that the workflow
captures (P2 step 4). When no evidence file exists the signature is empty
and the marker records `degraded=1`; the cap then compares head + reason
only. Alternative considered: head + reason only (coarser).

### D3 — Three identical failures trip the cap (Q3: B)

`REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL` defaults to `3`. The cap counts
marker comments whose `head` equals the current head SHA and whose `fp`
equals the newest marker's `fp`; a productive commit changes the head and
resets the count by construction (same design as the poller's noop sweep,
`orchestrate_poll_process.sh:23086-23110`).

### D4 — Cap outcome: review-blocked on the linked issues, or on the PR itself (Q4: A)

When the cap trips the gate stops the run (`should_run=false`,
`fingerprint_cap=true`) and a sibling job labels the linked issues
`ai:review-blocked` (existing stall recovery then dispatches the
review-blocked judge), or, when the PR has no linked issue, labels the PR
itself `ai:review-blocked` and posts the review-blocked comment. The same
job sends the autofix heal report so the intake (P1/P3) sees the failure.
Note: `ai:review-blocked` is not in `HUMAN_NEEDED_LABELS`
(`scripts/workflow_failure_heal.py:44-52`), so the label alone does not
trigger heal; the report does. Alternative considered: comment only.

### D5 — Self-inflicted routing in two sub-cases (Q5: A)

`pr-self-inflicted`: the crash file is in the PR's own diff → the diagnosis
is posted as a PR comment for the review-blocked judge; no issue.
`base-self-inflicted`: the crash file differs between `origin/main` and the
PR's base branch but is not in the PR's diff → an `ai:workflow-heal` issue
targeting that base branch, with orchestrator lineage lines so plan and
implement resolve the integration branch. Both apply only when
`source_repo` is this repository (consumers stage scripts at `@stable`, so a
consumer PR cannot break its own review scripts). Alternatives considered:
PR comment only; skip entirely.

### D6 — The diagnosis model still runs for self-inflicted reports (Q6: A)

The intake adds the ownership facts (crash file, owning branch, PR diff
membership) to the prompt context and keeps the same model and reasoning
effort; the comment is what the judge acts on. Alternative considered:
deterministic evidence only, no model call.

### D7 — Editor preflight as a `--preflight` mode of `review_apply_fixes.sh` (Q7: A)

The script gains a `review_apply_fixes_preflight()` function that runs every
precondition guard the script relies on before any model call, and a
`--preflight` argument that runs only that function. The existing
"Preflight: Verify required files before reviewer invocation" step
(`review_autofix.yml:3380`) invokes it when the staged copy advertises the
mode and fails open otherwise. A CI contract test asserts that every
`: "${VAR:?…}"` guard in the script is covered by the preflight function, so
the PR #4259 class cannot recur silently. Alternative considered: YAML-only
env checks.

### D8 — Main-pinned divergence notice plus a CI contract test (Q8: A)

`stage_workflow_support.sh` logs `STAGE_MAIN_PINNED_DIVERGENCE` when a
`MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` branch copy differs from the main copy. A
CI test compares, for every such script, the `${RUNTIME_DIR}` outputs the
branch copy writes against those the `origin/main` copy writes and fails
when the branch copy introduces an output the main copy lacks. Alternatives
considered: notice only; drop the check.

### D9 — This plan supersedes P1 of the deterministic-editor-failure plan (Q9: A)

Recorded in this plan's References and as a note at the top of
`docs/plans/review-autofix-deterministic-editor-failure-resume-plan.md`
(the note is the only change to that file and ships in this plan PR).

### D10 — On by default, one kill switch per phase (Q10: A)

`REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED` (P2),
`WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED` (P3) and
`REVIEW_EDITOR_PREFLIGHT_ENABLED` (P4) default to `true`. P1 has no flag: a
dispatch that always fails has no behaviour to preserve.

### D11 — Four phase PRs (Q11: A)

P1 dispatch envelope; P2 fingerprint cap; P3 self-inflicted routing; P4
preflight and divergence check. Each is shippable and revertible alone.

### D12 — Dispatch-failure cause recorded as accepted pending capture (R1: A)

The 422 explanation is inferred from GitHub's documented limit and the
payload's key count, not from captured stderr. P1 captures stderr first; if
a different cause appears, the fallback is a `GH_PAT` permission check. No
live dispatch was made from the planning session.

## Goals

- A review/autofix report reaches the heal intake: after P1, one failed
  review run on a PR with `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` prior
  failures produces a `repository_dispatch` run of
  `workflow-failure-heal-intake.yml` with `source_kind=autofix_failure`, and
  a rejected dispatch logs the HTTP status and first 300 characters of
  stderr.
- A deterministic failure stops looping: after P2, the fourth review run on
  a head whose last three failure comments carry the same fingerprint ends
  in the gate with `AUTOFIX_FINGERPRINT_CAP_TRIPPED`, labels the linked
  issues (or the PR) `ai:review-blocked`, posts one comment, sends one heal
  report, and no reviewer or editor model call is made. Later dispatches on
  the same head log `AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED` and post
  nothing.
- A self-inflicted failure is routed to its owner: after P3, a report whose
  crash file is in the PR diff produces a PR comment and no issue; one whose
  crash file changed only on the base branch produces an `ai:workflow-heal`
  issue with `Target branch:` set to that base branch and orchestrator
  lineage lines; `stable` is never targeted for either.
- A deterministic editor precondition fails before reviewers run: after P4,
  a run whose staged `review_apply_fixes.sh` would fail a `:?` guard fails
  in the preflight step within seconds of it, with
  `REVIEW_EDITOR_PREFLIGHT result=fail`, and the CI contract tests reject a
  guard without preflight coverage and a main-pinned script whose branch
  copy introduces a runtime output.
- Every threshold and switch is a defaulted repo variable; existing
  identifiers, comment texts and log prefixes are unchanged.

## Non-goals

- Fixing PR #4259 or `orchestrator/project-4139` themselves. Those are branch
  fixes outside this plan (the P2 cap and P3 routing will surface them to the
  judge and the integration branch respectively once shipped).
- Changing `review_autofix_sweep.yml` or the poller's dispatch decisions
  (Q1: A). They keep dispatching; the gate ends each dispatch cheaply.
- Making the digest step on `orchestrator/project-4139` tolerant of a missing
  file. That YAML is not on `main`; when the integration branch merges, the
  main copy of `review_collect_pr_metadata.sh` writes the file and the pair is
  consistent.
- Implementing P2 of the deterministic-editor-failure plan (resume-round
  recovery). It remains a separate plan.
- Consumer-repo registry changes (§14). Consumers receive P2 and P4 through
  the reusable `review_autofix.yml@stable` and the scripts staged from
  `stable`; P1 and P3 run only in this repository's intake and the reporters
  staged from `stable`. `.github/ai/consumer_repos.json` is unchanged.
- Any change to `MAX_STALL_RECOVERIES_DONE`, `MAX_AUTOFIX_ITERATIONS` or the
  review-blocked judge's actions.

## Constraints

- **§6 naming immutability.** No existing env var, workflow input, payload
  field, comment text, marker, label, log prefix or classification token is
  renamed or removed. New identifiers (checked for collisions against
  `review_autofix.yml`, the heal scripts, README and agents.md):
  `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED`,
  `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`,
  `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED`,
  `REVIEW_EDITOR_PREFLIGHT_ENABLED`; payload fields `base_branch`,
  `script_ref`, `crash_file`, `changed_files`, `failure_fingerprint`; envelope
  field `report`; classification tokens `pr-self-inflicted`,
  `base-self-inflicted`; comment markers `review-autofix-failure:v1` and
  `review-autofix-failure-cap:v1`; log prefixes `AUTOFIX_FINGERPRINT`,
  `AUTOFIX_FINGERPRINT_CAP_TRIPPED`, `AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED`,
  `AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED`, `REVIEW_EDITOR_PREFLIGHT`,
  `STAGE_MAIN_PINNED_DIVERGENCE`; `failure_reason` values
  `identical_failure_cap` and `editor_preflight_failed`; script argument
  `--preflight`; `workflow_failure_heal.py` subcommands
  `wrap-dispatch`, `unwrap-dispatch`, `autofix-failure-fingerprint`,
  `autofix-identical-failure-count`, `classify-crash-ownership`. Log
  prefixes are registered in agents.md "Stable log prefixes (contractual)"
  with `LOG_PREFIX.name=` entries (`tests/test_workflow_failure_heal.py:187`
  asserts the pattern).
- **§15 API hygiene.** P2 adds exactly one `GET
  repos/{repo}/issues/{pr}/comments` (paginated, 100 per page) in the gate
  job, and extends the existing conditional terminal-marker fetch
  (`review_autofix.yml:605-609`) into that same unconditional call by widening
  its `jq` filter to keep both `REVIEW_AUTOFIX_PARTIAL_V1` and
  `review-autofix-failure:v1` comments, so the gate issues one comments call
  instead of two. Justification: the cap must run before any `codex-agent`
  step can fail, and the gate has no comments data today; the alternative
  (zero-call placement after "Collect PR metadata") leaves the PR #4273 class
  uncapped. The sibling job reuses the gate's outputs (count, fingerprint,
  linked issue numbers from the PR payload the gate already fetched) and
  issues only the label and comment writes. P3's ownership check uses the PR
  files the review run already wrote to `PR_CHANGED_FILES_FILE` (carried in
  the payload, bounded) and `git diff --name-only` on the intake's own
  checkout; no new GitHub reads. P1 and P4 add no API calls.
- **§18.** Everything runs inside existing jobs; no manual steps; no new
  scripts; no registry entry.
- **§19.** The P3 issue body and every comment use `Refs #N` for the tracking
  issue derived from `orchestrator/project-<N>`.
- **§20.** One fragment per phase PR: `changelog.d/<pr>-heal-dispatch-envelope.md`,
  `changelog.d/<pr>-review-failure-fingerprint-cap.md`,
  `changelog.d/<pr>-heal-self-inflicted-routing.md`,
  `changelog.d/<pr>-review-editor-preflight.md`. This plan PR is docs only
  and ships no fragment.
- **Security.** Marker values are reduced to `[A-Za-z0-9_.-]` before they
  are logged or compared; comment bodies are untrusted input to the gate
  parser (same posture as the existing partial-marker parser, which also
  authenticates the marker author against `gh api user`; the cap parser
  reuses that author check). Evidence tails are truncated to
  `FAILURE_EVIDENCE_LIMIT` and pass through `sanitize_text`. The intake keeps
  treating every payload field as untrusted for the model. No token is
  logged: the dispatch stderr is logged after `_gh_actions_escape`, and
  `gh_retry` already redacts token-bearing stderr.
- **Self-repo staging.** New shell logic that a self-repo PR branch may
  predate lives in `workflow_failure_heal.py` (already backfilled from the
  main snapshot by `REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS`,
  `review_autofix.yml:249,1838`) or in the YAML; the P4 preflight mode is
  advertised by the staged script and the YAML fails open when it is absent
  (agents.md "Review self-repo support staging runs under main's workflow
  YAML").

## Approach

P1 wraps the report in a one-property envelope and unwraps it in the intake,
accepting both shapes. P2 stamps every failure comment the review workflow
posts with a fingerprint marker, counts identical markers per head in the
gate, and hands a tripped cap to a sibling job that applies the existing
review-blocked outcome and sends the heal report. P3 adds ownership facts
to the heal payload, two classification tokens to the prompt and parser,
and two routing branches in the intake. P4 gives the editor script a
preflight mode, invokes it from the existing preflight step, adds the
divergence notice to the staging helper, and pins both with CI contract
tests. Designs considered and rejected are recorded under Decisions.

## Phases & Merge Strategy

Each phase is one PR against `main`. No phase depends on another having
merged; each is production-safe and revertible alone.

1. **P1 — Heal dispatch envelope and visible rejection.** Scope: both
   reporters send `client_payload: {schema_version, report}`; the intake
   unwraps `report` when present and otherwise uses the payload as-is; a
   rejected dispatch logs status and stderr. Files:
   `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_report.sh`,
   `scripts/workflow_failure_heal_autofix_report.sh`,
   `.github/workflows/workflow-failure-heal-intake.yml`, tests, README,
   agents.md, changelog fragment. Done: unit tests pass; a report with
   streak ≥ 2 produces an intake run (verified on the first real failure
   after release, or by a manual `workflow_dispatch` of the intake with an
   enveloped payload). Rollback: revert the PR; reporters return to the flat
   shape and the intake still accepts it.
2. **P2 — Identical-failure fingerprint cap.** Scope: marker on failure
   comments, evidence capture, gate-side count, sibling job that labels,
   comments and reports; flag and threshold variables. Files:
   `.github/workflows/review_autofix.yml`, `scripts/workflow_failure_heal.py`,
   tests, README, agents.md, changelog fragment. Done: contract tests pass;
   a synthetic PR with three identical marker comments on its head is
   stopped in the gate. Rollback: set
   `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED=false` (the marker keeps being
   written; the cap is not evaluated), or revert the PR.
3. **P3 — Self-inflicted classification and routing.** Scope: ownership
   fields in the autofix report, ownership computation in the intake, two
   prompt tokens, two routing branches, flag. Files:
   `scripts/workflow_failure_heal.py`, `scripts/workflow_failure_heal_intake.sh`,
   `scripts/workflow_failure_heal_autofix_report.sh`,
   `prompts/mode-workflow-failure-heal.txt`, tests, README, agents.md,
   changelog fragment. Done: intake tests cover both sub-cases and the
   consumer-repo no-op. Rollback: set
   `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED=false` (tokens route as
   `workflow-defect`, today's behaviour), or revert the PR. Without P1 the
   report never arrives and P3 is inert but harmless; without P2 the report
   arrives after `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` plain failures as it
   does today.
4. **P4 — Editor preflight and main-pinned divergence check.** Scope:
   `--preflight` mode, preflight step invocation, divergence notice, two CI
   contract tests, flag. Files: `scripts/review_apply_fixes.sh`,
   `scripts/stage_workflow_support.sh`, `.github/workflows/review_autofix.yml`,
   tests, README, agents.md, changelog fragment. Done: contract tests pass;
   a run whose staged script lacks a required env var fails in the preflight
   step. Rollback: set `REVIEW_EDITOR_PREFLIGHT_ENABLED=false`, or revert
   the PR.

## Implementation Steps

### P1 — Heal dispatch envelope and visible rejection

1. `scripts/workflow_failure_heal.py` (after `_cmd_skip_reason`, `:978`):
   add `wrap_dispatch(payload) -> dict` returning
   `{"event_type": "workflow-failure-heal", "client_payload": {"schema_version": SCHEMA_VERSION, "report": payload}}`
   and `unwrap_dispatch(client_payload) -> dict` returning
   `client_payload["report"]` when it is a dict whose `schema_version`
   matches, else `client_payload` unchanged (flat shape stays accepted).
   Expose both as subcommands `wrap-dispatch --payload-json <file>` and
   `unwrap-dispatch --payload-json <file>`; register in `build_parser()`
   (`:1065`). Add a unit assertion that the wrapped `client_payload` has at
   most 10 top-level keys (`DISPATCH_CLIENT_PAYLOAD_MAX_KEYS = 10`, module
   constant with a comment citing the GitHub limit).
2. `scripts/workflow_failure_heal_report.sh:186-192` and
   `scripts/workflow_failure_heal_autofix_report.sh:199-206`: build
   `DISPATCH_FILE` with `python3 "${HEAL_PY}" wrap-dispatch --payload-json
   "${PAYLOAD_FILE}"` instead of the inline `jq -n`; capture the POST's
   stderr to `${REPORT_DIR}/dispatch_error.txt`; on failure log
   `skip reason=dispatch_denied … detail=$(head -c 300 dispatch_error.txt | tr '\n' ' ')`
   in the autofix reporter and `error dispatch_failed … detail=…` in the
   issue reporter (existing prefixes and exit codes unchanged; the issue
   reporter stays red on failure as `test_report_script_dispatch_failure_is_red`
   requires).
3. `.github/workflows/workflow-failure-heal-intake.yml:108-111`
   ("Materialize the report payload", `repository_dispatch` branch): write
   `CLIENT_PAYLOAD_JSON` to `${RUNTIME_DIR}/client_payload.json`, then
   `python3 scripts/workflow_failure_heal.py unwrap-dispatch --payload-json
   "${RUNTIME_DIR}/client_payload.json" > "${WORKFLOW_HEAL_PAYLOAD_FILE}"`.
   The `workflow_dispatch` manual path is unchanged (flat payload).
4. Tests (`tests/test_workflow_failure_heal.py`): extend
   `test_report_script_dispatches_thin_payload` (`:652`) and the autofix
   reporter test to assert `dispatch["body"]["client_payload"]["report"]`
   validates and that the client payload has ≤ 10 keys; add
   `test_unwrap_dispatch_accepts_flat_and_enveloped`; extend
   `test_report_script_dispatch_failure_is_red` and the autofix skip-path
   test to assert the `detail=` fragment is present; add an intake test
   feeding an enveloped `repository_dispatch` payload through the
   materialize step's logic.
5. Docs: README "Workflow Failure Heal" (`:1224-1240`): one sentence on the
   envelope and that rejection detail is logged; agents.md item 14
   (`:72-100`): same. `changelog.d/<pr>-heal-dispatch-envelope.md`
   (`<!-- changelog: fixed -->`).

### P2 — Identical-failure fingerprint cap

6. `scripts/workflow_failure_heal.py`: add
   `autofix_failure_fingerprint(*, failure_reason, evidence_text) -> dict`
   returning `{"fp": fingerprint("review_autofix", failure_reason, error_signature(evidence_text)), "degraded": evidence_text.strip() == ""}`,
   `render_failure_marker(head_sha, failure_reason, fp, degraded) -> str`
   producing `<!-- review-autofix-failure:v1 head=<sha> reason=<r> fp=<hex> degraded=<0|1> -->`,
   `parse_failure_markers(comments, *, head_sha, author_login) -> list`, and
   `count_identical_failures(comments, *, head_sha, author_login) -> dict`
   returning `{"count": n, "fp": <newest fp>, "reason": <newest reason>}`
   where `n` is the number of trailing marker comments (newest first, same
   author, same head) sharing the newest marker's `fp`; a non-marker
   failure comment or a success comment ends the scan (same rule as
   `count_autofix_failure_streak`, `:369`). Subcommands
   `autofix-failure-fingerprint --failure-reason <r> --evidence-file <f>`
   (prints `fp=… degraded=…`) and
   `autofix-identical-failure-count --comments-json <f> --head-sha <sha> --author-login <login>`
   (prints `count=… fp=… reason=…`). Every printed value is reduced to
   `[A-Za-z0-9_.-]`.
7. `.github/workflows/review_autofix.yml` "Apply fixes with editor model"
   (`:4254` and `:4336`): run the editor script as
   `bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh" 2> >(tee -a "${RUNTIME_DIR}/editor_stage_stderr.txt" >&2)`
   so the stage's stderr is available to later steps (bounded by a
   `tail -c 65536` when read). Apply the same capture to "Collect PR
   metadata" (`:2275`, file `collect_metadata_stderr.txt`). These two
   stages are where both observed deterministic failures occurred; other
   stages fall back to the degraded marker.
8. `.github/workflows/review_autofix.yml`: new `always()` step "Assemble
   failure evidence" immediately before "Mark linked issues review-blocked
   (workflow failure)" (`:7285`), gated on `failure() || env.EDITOR_NOOP_SUSPICIOUS == 'true' || env.EDITOR_CHANGES_LOST == 'true'`:
   concatenate the tails of `editor_stage_stderr.txt`,
   `collect_metadata_stderr.txt` and `review_autofix_run_summary_line.txt`
   (whichever exist) into `${RUNTIME_DIR}/failure_evidence_tail.txt`;
   derive `failure_reason` with the same precedence the reporter uses
   (`workflow_failure_heal_autofix_report.sh:117-127`); run subcommand
   `autofix-failure-fingerprint`; export `AUTOFIX_FAILURE_FP`,
   `AUTOFIX_FAILURE_REASON`, `AUTOFIX_FAILURE_MARKER` to `GITHUB_ENV`; log
   `AUTOFIX_FINGERPRINT pr= head= reason= fp= degraded=`. Fail open: when
   `workflow_failure_heal.py` is missing, export an empty marker and log
   `AUTOFIX_FINGERPRINT degraded=1 reason=helper_missing`.
9. `.github/workflows/review_autofix.yml`: append `${AUTOFIX_FAILURE_MARKER}`
   (when non-empty) as the last line of the bodies posted by "Post editor
   summary comment" (no-output path, `:4501-4535`; this step runs before
   step 8, so it computes the marker inline with the same subcommand and
   `editor_stage_stderr.txt`), "Post review-blocked comment on PR (workflow
   failure)" (`:7380`), and the `Editor changes lost` / `Editor no-op
   suspicious` comment steps (`:7069` region). Comment texts are otherwise
   unchanged, so `AUTOFIX_FAILURE_COMMENT_MARKERS` (`:87`) and the poller's
   noop sweep keep matching.
10. `.github/workflows/review_autofix.yml` `gate` job ("Evaluate review
    gate", `:269`): read repo vars `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED`
    (`|| 'true'`) and `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`
    (`|| '3'`, integer-validated, fallback 3). When enabled and the event
    targets a PR with a known head SHA: make the comments fetch at
    `:605-609` unconditional and widen its `--jq` to keep comments containing
    either `<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->` or
    `<!-- review-autofix-failure:v1`; keep the existing `gh api user`
    author lookup (`:598`) as the marker authenticator; run
    `autofix-identical-failure-count`; when `count >= max`, log
    `AUTOFIX_FINGERPRINT_CAP_TRIPPED pr= head= fp= count= max=`, set outputs
    `fingerprint_cap=true`, `fingerprint_cap_fp`, `fingerprint_cap_reason`,
    `fingerprint_cap_count`, and `should_run=false` with
    `skip_reason=fingerprint_cap`. On a fetch or parse failure log
    `AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED reason=…` and continue with the
    normal gate result (fail open). The gate's checkout of
    `workflow_failure_heal.py` uses the same support-source checkout the
    `deterministic-skip-merge` job pattern uses (`SCRIPT_REF`, falling back
    to the main snapshot).
11. `.github/workflows/review_autofix.yml`: new job `fingerprint-cap-block`
    (`needs: gate`, `if: needs.gate.outputs.fingerprint_cap == 'true'`,
    env `GH_TOKEN: ${{ secrets.GH_PAT }}`), modelled on
    `deterministic-skip-merge`: (a) fetch the PR once
    (`repos/{repo}/pulls/{n}`) and resolve linked issues with the same
    body/title fallback the review-blocked step uses (`:5890-5915`);
    (b) idempotency: if the gate's comment scan already saw a
    `<!-- review-autofix-failure-cap:v1 head=<sha> -->` marker for this head
    (gate output `fingerprint_cap_already_applied=true`), log
    `AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED` and exit 0; (c) label each
    linked issue `ai:review-blocked` via `set_issue_phase_label_resilient`
    (`label_helpers.sh`), or, with no linked issue, label the PR
    `ai:review-blocked` (`ensure_label_exists` first); (d) post one comment
    "**AI review/autofix stopped: identical failure repeated** …" carrying the
    count, reason, fingerprint, run URL and the cap marker; (e) run
    `workflow_failure_heal_autofix_report.sh` with
    `AUTOFIX_FAILURE_REASON=identical_failure_cap`,
    `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK=1` (the cap is the streak) and the
    gate's marker data as evidence (`REPORT_SUMMARY_LINE_FILE` absent is
    already tolerated). Telegram: one WARNING via `tg_send_msg` when
    available.
12. `scripts/workflow_failure_heal_autofix_report.sh:117-127`: honour
    `AUTOFIX_FAILURE_REASON` as the first precedence when set (new, optional
    env; existing precedence unchanged otherwise) and carry
    `failure_fingerprint` (`AUTOFIX_FAILURE_FP`, optional) into the payload
    via a new optional `--failure-fingerprint` argument of
    `build-autofix-payload`; `validate_payload` accepts the field when it is
    a 64-hex string and ignores its absence.
13. Tests: `tests/test_workflow_failure_heal.py` — fingerprint stability
    across two evidence texts differing only in run ids, SHAs and temp
    paths; degraded marker when evidence is empty; `count_identical_failures`
    with mixed heads, a foreign author, and a success comment ending the
    scan. `tests/test_review_autofix_review_pipeline_contract.py` — the gate
    reads both variables with the documented defaults; the widened `jq`
    keeps both markers; the `fingerprint-cap-block` job is gated on the gate
    output and declares `GH_TOKEN`; every failure comment step appends
    `AUTOFIX_FAILURE_MARKER`; the `2> >(tee …)` capture is present on both
    editor invocations and on Collect PR metadata.
14. Docs: README variables table (bottom, under the anchor):
    `REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED`, `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL`;
    README "Orchestrator PR autofix flow" failure modes (`:1837-1841`): new
    bullet for the cap; agents.md stable log prefixes: the four
    `AUTOFIX_FINGERPRINT*` prefixes with `LOG_PREFIX.name=` entries; agents.md
    item 7 (`:35-44`): one sentence. `changelog.d/<pr>-review-failure-fingerprint-cap.md`.

### P3 — Self-inflicted classification and routing

15. `scripts/workflow_failure_heal.py`: extend `build_autofix_failure_payload`
    with optional keyword arguments `base_branch`, `script_ref`,
    `changed_files` (list of repo-relative paths, capped at 200 entries and
    120 characters each), and add the crash-file extraction
    `extract_crash_file(evidence_text) -> str | None` matching
    `…/scripts/<name>: line N:` (returns `scripts/<name>`) and
    `::error::…` lines naming a `scripts/` or `.github/workflows/` path;
    payload field `crash_file`. `validate_payload` accepts all four as
    optional, validating `base_branch` with `is_valid_branch`, `script_ref`
    as a SHA or `stable`, paths against `^[A-Za-z0-9_./-]{1,120}$` with no
    `..` segment. Add `classify_crash_ownership(*, crash_file, changed_files,
    base_changed_files) -> str` returning `pr`, `base` or `none`, and
    subcommand `classify-crash-ownership --payload-json <f> --base-changed-files <f>`.
    Add `pr-self-inflicted` and `base-self-inflicted` to the token set
    `parse_classification` accepts (`:727`).
16. `scripts/workflow_failure_heal_autofix_report.sh`: pass
    `--base-branch "$(jq -r '.base.ref' PR_JSON_FILE)"`,
    `--script-ref "${REPORT_WRAPPER_SHA:-}"` and
    `--changed-files-file "${PR_CHANGED_FILES_FILE:-}"` (the file the run
    already wrote; absent is tolerated) to `build-autofix-payload`. The
    `.github/workflows/review_autofix.yml` reporter step (`:8525-8548`) adds
    `PR_CHANGED_FILES_FILE: ${{ env.PR_CHANGED_FILES_FILE }}` to its env.
17. `scripts/workflow_failure_heal_intake.sh`: after the payload is
    validated and before the model call, when
    `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED` (default `true`),
    `source_kind == autofix_failure`, `source_repo == SELF_REPO` and
    `crash_file` is set: `git fetch --depth=1 origin "${base_branch}"
    main` on the intake's checkout (`fetch-depth: 1` today,
    `.github/workflows/workflow-failure-heal-intake.yml:84`), compute
    `base_changed_files` with `git diff --name-only origin/main...origin/<base_branch>`
    (empty when the base is `main`), run `classify-crash-ownership`, and
    append an `## Ownership facts` block to the prompt context (crash file,
    ownership, PR head/base, script ref, whether the file is in the PR diff).
    Log `WORKFLOW_HEAL crash_ownership=<pr|base|none> crash_file=… base=…`.
18. `prompts/mode-workflow-failure-heal.txt:46-61`: add the two tokens with
    their definitions ("the failing run executed scripts from the pull
    request head; the crash is in a file the pull request itself changed"
    and "… in a file the base branch changed relative to main and the pull
    request did not"), and an instruction that the tokens apply only when the
    `## Ownership facts` block is present and reports `pr` or `base`.
19. `scripts/workflow_failure_heal_intake.sh:567-627`: two new `case`
    branches. `pr-self-inflicted`: `_comment_on_source` with the full
    diagnosis and a first line "**Workflow failure heal: this failure is
    caused by this pull request's own changes**"; no issue; log
    `no_issue classification=pr-self-inflicted …`; Telegram DEBUG.
    `base-self-inflicted`: `_open_issue "${SELF_REPO}" "${base_branch}"`
    where `compose_issue_body` (`:787`) gains optional lineage arguments so
    the body carries `**Target branch:** <base_branch>`, and, when the base
    matches `^orchestrator/project-([0-9]+)$`, `**Tracking issue:** #N`,
    `**Integration branch:** <base_branch>` and `Refs #N` (§19; never
    `Fixes`); the issue gets labels `ai:workflow-heal` and
    `ai:orchestrator-managed` (mirroring the `merge_with_followup` lineage
    convention, README `:1826`). When the flag is off, both tokens are routed
    through the existing `workflow-defect|inconclusive` branch and the
    ownership block is not added to the prompt. Budget, dedup, lineage and
    the source comment are unchanged for both.
20. Tests: `tests/test_workflow_failure_heal.py` — payload round trip with
    the new fields; `extract_crash_file` on the two observed error lines
    (`review_apply_fixes.sh: line 168: OPENROUTER_API_KEY …` and
    `##[error]Could not publish the linked-issue metadata integrity digest.`
    → `None`); `classify_crash_ownership` for `pr`, `base`, `none`;
    `parse_classification` accepts the new tokens; intake harness tests for
    each routing branch, for the consumer-repo no-op (`source_repo` not
    self), and for the flag off. `test_prompt_declares_classification_tokens`
    (`:178`) extended to the new tokens.
21. Docs: README "Classification and routing" (`:1241-1252`): the two tokens
    and their outcomes; README variables table:
    `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED`; agents.md item 14.
    `changelog.d/<pr>-heal-self-inflicted-routing.md`.

### P4 — Editor preflight and main-pinned divergence check

22. `scripts/review_apply_fixes.sh`: immediately after the helper sourcing
    block (`:12-100`) define `review_apply_fixes_preflight()` that checks,
    without side effects and without any model or network call: every
    variable the script guards with `: "${VAR:?…}"` (the function is the
    single list; the contract test in step 26 keeps it complete), the
    presence and readability of `OPENCODE_HELPERS_PATH`,
    `OPENCODE_CONFIG_WRITER_PATH`, `CODEX_HELPERS_PATH`, the `opencode`
    binary, write access to `RUNTIME_DIR`, and, when a function named
    `setup_editor_isolation` is defined on the staged copy, its
    prerequisite checks in a dry-run form (the branch that introduced it
    adds them to this function in the same change; `main` today has none).
    Add at the top of the script's main flow: `if [ "${1:-}" = "--preflight" ]; then review_apply_fixes_preflight; exit $?; fi`.
    Add the marker comment `# supports: --preflight` on its own line for the
    YAML capability probe. Output one line per check
    (`REVIEW_EDITOR_PREFLIGHT check=<name> result=<ok|fail> detail=…`) and a
    final `REVIEW_EDITOR_PREFLIGHT result=<ok|fail> checks=<n> failed=<m>`.
23. `.github/workflows/review_autofix.yml` "Preflight: Verify required
    files before reviewer invocation" (`:3380-3535`): add step env
    identical to the editor step's explicit entries (`:4187-4190`,
    `TOOL_CALL_BUDGET_JUDGE`) so the preflight sees what the editor sees;
    when `REVIEW_EDITOR_PREFLIGHT_ENABLED` (`|| 'true'`) and
    `grep -q '^# supports: --preflight' "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh"`,
    run `bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh" --preflight
    2> >(tee -a "${RUNTIME_DIR}/editor_stage_stderr.txt" >&2)`; on non-zero
    exit, export `EDITOR_PREFLIGHT_FAILED=true`, log
    `REVIEW_EDITOR_PREFLIGHT result=fail`, and `exit 1` (the run then takes
    the ordinary failure path: comment with marker, review-blocked label
    suppressed only by `AUTOFIX_EDITOR_EMPTY_NOOP`, which this path does not
    set, heal reporter with `failure_reason=editor_preflight_failed` via
    the P2 precedence or, without P2, via `finalize_reason`). When the
    staged copy lacks the marker, log `REVIEW_EDITOR_PREFLIGHT skip
    reason=unsupported script_ref=${SCRIPT_REF}`.
24. `scripts/workflow_failure_heal_autofix_report.sh:117-127`: add
    `EDITOR_PREFLIGHT_FAILED=true → failure_reason=editor_preflight_failed`
    ahead of the `finalize_reason` fallback (after the P2 override when
    present; both changes are additive and order-independent).
25. `scripts/stage_workflow_support.sh:90-100` (main-primary loop): when the
    branch copy `.codex-workflow-src/scripts/${f}` exists and `cmp -s`
    against the staged main copy fails, log
    `::notice::STAGE_MAIN_PINNED_DIVERGENCE script=${f} script_ref=${SCRIPT_REF}`.
    No behaviour change.
26. Tests: `tests/test_review_autofix_review_pipeline_contract.py` —
    (a) `review_apply_fixes.sh` declares `# supports: --preflight` and every
    `: "${VAR:?` guard name in the file appears inside
    `review_apply_fixes_preflight()`; (b) the preflight step invokes
    `--preflight` behind the flag and the capability probe and declares the
    editor step's explicit env entries; (c) main-pinned divergence: for each
    name in `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS`, when the working-tree copy
    differs from `git show origin/main:scripts/<name>`, the set of
    `${RUNTIME_DIR}/<file>` and `*_FILE` runtime outputs the working-tree
    copy writes (`> "${…}"`, `printf … > …`, `tee`) must be a subset of the
    main copy's; the test fetches `origin/main` with `git fetch --depth=1
    origin main` and skips with a warning when the ref is unavailable
    (fail open in offline runs); (d) `stage_workflow_support.sh` contains the
    `STAGE_MAIN_PINNED_DIVERGENCE` notice inside the main-primary loop.
    `tests/test_workflow_failure_heal.py`: `editor_preflight_failed`
    precedence.
27. Docs: README variables table: `REVIEW_EDITOR_PREFLIGHT_ENABLED`;
    README "Orchestrator PR autofix flow" run summary contract: mention of
    `REVIEW_EDITOR_PREFLIGHT`; agents.md stable log prefixes:
    `REVIEW_EDITOR_PREFLIGHT`, `STAGE_MAIN_PINNED_DIVERGENCE`; agents.md
    "Review self-repo support staging runs under main's workflow YAML": one
    sentence pointing at the divergence notice and the contract test.
    `changelog.d/<pr>-review-editor-preflight.md`.

## Files & Modules

- `.github/workflows/review_autofix.yml` — P2 (steps 7-11), P3 (step 16),
  P4 (step 23)
- `.github/workflows/workflow-failure-heal-intake.yml` — P1 (step 3)
- `scripts/workflow_failure_heal.py` — P1 (step 1), P2 (steps 6, 12), P3
  (step 15)
- `scripts/workflow_failure_heal_report.sh` — P1 (step 2)
- `scripts/workflow_failure_heal_autofix_report.sh` — P1 (step 2), P2
  (step 12), P3 (step 16), P4 (step 24)
- `scripts/workflow_failure_heal_intake.sh` — P3 (steps 17, 19)
- `prompts/mode-workflow-failure-heal.txt` — P3 (step 18)
- `scripts/review_apply_fixes.sh` — P4 (step 22)
- `scripts/stage_workflow_support.sh` — P4 (step 25)
- `tests/test_workflow_failure_heal.py` — P1, P2, P3, P4
- `tests/test_review_autofix_review_pipeline_contract.py` — P2, P4
- `README.md` — every phase (variables table, heal section, autofix flow)
- `agents.md` — every phase (stable log prefixes, items 7 and 14, staging
  note)
- `changelog.d/<pr>-heal-dispatch-envelope.md` [new] — P1
- `changelog.d/<pr>-review-failure-fingerprint-cap.md` [new] — P2
- `changelog.d/<pr>-heal-self-inflicted-routing.md` [new] — P3
- `changelog.d/<pr>-review-editor-preflight.md` [new] — P4
- `docs/plans/review-autofix-deterministic-editor-failure-resume-plan.md` —
  supersession note (this plan PR only)

## Tests

- **Unit (pytest, `tests/test_workflow_failure_heal.py`):** envelope wrap and
  unwrap, key-count bound, both reporters' dispatch bodies and rejection
  detail lines (P1); fingerprint stability and degradation, marker
  rendering and parsing, identical-count rules (P2); payload fields,
  crash-file extraction, ownership classification, token parsing, intake
  routing for both sub-cases, consumer no-op and flag off (P3);
  `editor_preflight_failed` precedence (P4).
- **Contract (pytest, `tests/test_review_autofix_review_pipeline_contract.py`):**
  gate variables and defaults, widened comment filter, sibling job gating,
  marker appended on every failure comment step, stderr capture on the two
  stages (P2); preflight marker and guard coverage, preflight step wiring,
  main-pinned output subset, divergence notice (P4).
- **Existing tests to update:** `test_report_script_dispatches_thin_payload`,
  `test_report_script_dispatch_failure_is_red`, `test_report_script_skip_paths`
  (P1); `test_prompt_declares_classification_tokens` (P3);
  `test_stable_log_prefixes_are_registered` pattern reused for the new
  prefixes (P2, P4).
- **End-to-end (after release, operator-observed, no manual scripts):** the
  first review failure with streak ≥ 2 on any PR produces an intake run
  (P1); the next PR with three identical failures shows
  `AUTOFIX_FINGERPRINT_CAP_TRIPPED` in its gate log and exactly one cap
  comment (P2); the intake log for that report shows
  `crash_ownership=pr` or `base` and the matching outcome (P3); a run on a
  branch that breaks an editor guard fails in the preflight step within its
  first minutes (P4). PR #4259 and any open project-4139 child PR are the
  natural first observations.

## Risks & Mitigations

- **Dispatch-failure cause is inferred, not captured** — ACCEPTED — pending
  P1 stderr capture (R1: A). If the captured error is not the property
  limit, the follow-up is a `GH_PAT` permission check; the envelope is
  correct regardless.
- **`client_payload` total size** — the report is bounded by the existing
  excerpt limits (about 4 KB each for issue excerpt, comments excerpt and
  evidence, plus the new `changed_files` list capped at 200 × 120 bytes);
  P1's test asserts the enveloped body stays under 60 KB with all fields at
  their limits.
- **A transient failure with a stable signature trips the cap** — the count
  needs three identical signatures on one head; `error_signature` strips
  ids, SHAs, URLs and temp paths but keeps the message, so a provider
  outage repeating the same message three times would trip it. Mitigation:
  the outcome is the review-blocked judge, which can re-dispatch; the cap
  comment names the fingerprint and the operator lever
  (`REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED=false`).
- **Marker forgery in PR comments** — the gate accepts markers only from the
  login `gh api user` returns for `GH_PAT`, the same check the partial-marker
  parser uses.
- **Older self-repo branches lack the new helper code** — every new shell
  path is guarded: the gate and the evidence step fail open without
  `workflow_failure_heal.py` (backfilled from the main snapshot in
  `codex-agent`; the gate uses its own checkout), and the preflight step
  skips without the capability marker.
- **The sibling job needs `label_helpers.sh` and the reporter** — it stages
  them from the support-source checkout exactly as `deterministic-skip-merge`
  stages what it needs; if either is missing it logs and still applies the
  label with a direct `gh api` call, so the escalation never depends on the
  report.
- **`ai:review-blocked` on a PR with no linked issue has no automated
  consumer today** — ACCEPTED — the comment, the heal report and the
  Telegram WARNING are the visible outcomes; the label makes the state
  greppable and matches the existing PR-label surface for standalone PRs.
- **Base-branch heal issues for a project the orchestrator is still
  running** — the issue carries `ai:orchestrator-managed` and lineage lines
  so it is picked up as a child of the project; dedup by fingerprint keeps
  one issue per defect even when every child PR reports it.
- **Contract test (c) depends on `origin/main` being fetchable in CI** — the
  test skips with a warning when the fetch fails; it never blocks an
  offline run.
- **Divergence notice noise on integration branches** — it is a
  `::notice::`, one line per diverged script per run, and carries no
  behaviour.

## Rollout

- Each phase ships on merge to `main` and reaches consumers on the next
  `@stable` release through the existing promotion workflows; no consumer
  wrapper, secret or variable is required. Consumers can set the three
  kill-switch variables per repository.
- Order is irrelevant for correctness. Observationally, P1 first gives the
  fastest signal (the next real failure either produces an intake run or a
  logged rejection detail).
- Rollback per phase: P1 revert the PR (intake keeps accepting both shapes,
  so a partial revert is safe); P2, P3, P4 set the phase's variable to
  `false` or revert the PR.
- No migrations, no DB, no data backfill.

## References

- PR #4259 (`ai/issue-4255`), run 35713627310 and runs 35705818736,
  35696455633, 35689046981, 35685250882, 35680228793
- PR #4273 (`ai/issue-4270`), run 35719487197; PR #4261 (`ai/issue-4260`),
  run 35692710132; integration branch `orchestrator/project-4139` at
  `125e602`
- Issues #4255, #4270 (both `ai:done`, `ai:orchestrator-managed`); tracking
  issues #3965 and #4139 (`Refs` only, §19)
- `docs/plans/review-autofix-deterministic-editor-failure-resume-plan.md` —
  its P1 is superseded by this plan's P2; its P2 stands
- `docs/plans/release-blocker-heal-reports-plan.md` — same intake, same
  payload schema conventions (`SOURCE_KINDS`, `Refs #N`)
- README "Workflow Failure Heal" (`:1174-1275`), "Orchestrator PR autofix
  flow" (`:1807-1859`); agents.md item 14 (`:72-100`), "Review self-repo
  support staging runs under main's workflow YAML" (`:330-353`), "Stable log
  prefixes (contractual)" (`:916`)
- GitHub REST API, "Create a repository dispatch event": `client_payload`
  is limited to 10 top-level properties
