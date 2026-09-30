# validate.yml authorizes a stable target only for a verified workflow-heal issue bound to the PR

Source issue: shubhodeep1/coding-workflows#4791 (https://github.com/shubhodeep1/coding-workflows/issues/4791)
Base branch: claude/implement-plan-issue-4734-validate-stacked-stable-targets
Security pass: skip (ai:security: automation-produced issue)

## Summary

The `stable` case of the `Authorize explicit validation target` step in
`.github/workflows/validate.yml` (added by project #4734) authorizes a
`claude/implement-plan-issue-<n>-*` target whose PR goes into `stable` when
issue `<n>` merely carries the `ai:workflow-heal` label. A label is mutable and
the branch name is chosen by whoever opens the PR, so a collaborator can label
an unrelated issue, or reuse a genuine heal issue's number, and get a
secret-bearing validation run for any head (security finding
`stable-target-mutable-label-authorization`, issue #4791). This plan makes the
`stable` case fail closed unless issue `<n>` has verified heal-automation
provenance and the target PR was opened by the account that filed the issue.

## Context

- `.github/workflows/validate.yml:176-270` on the base branch (`f1dbe12`): the
  step lists the target's open PRs, requires exactly one trusted same-repo PR
  with a 40-hex head, and classifies its base. The `stable` branch
  (`validate.yml:254-262`) reads `repos/<repo>/issues/<n>` and only checks
  "not a pull request" and "has `ai:workflow-heal`".
- Heal issues are filed by `scripts/workflow_failure_heal_intake.sh`
  (`_open_issue`, `gh issue create --label ai:workflow-heal` with the
  `GH_PAT` account, so the label is applied at creation by the author) with a
  body from `scripts/workflow_failure_heal.py` `compose_issue_body`, which
  starts with `<!-- workflow-failure-heal:fp=<fp> -->` and carries
  `- **Target branch:** \`<branch>\``.
- `.claude/scripts/security_pass_skip.py` (issue #4623) already defines
  heal-automation provenance for the same label: author is
  `github-actions[bot]` (Bot) or a User with `author_association: OWNER`; every
  `labeled` event for the label is by the author, the first within 120 s of
  creation; the body carries the marker line; at most one 100-item events page
  (a full page is unverifiable).
- Real case checked: issue #4665 (User `shubhodeep1`, OWNER, label applied 2 s
  after creation by the author, fp marker, `Target branch: stable`) and its
  final PR #4667 (author `shubhodeep1`, OWNER, base `stable`) satisfy every
  rule below.
- The step runs before `actions/checkout` in the caller's repository, so the
  logic stays inline (the base project's AD-1).

## Goals

1. A `stable`-base target is authorized only when issue `<n>`:
   1. is an issue (no `pull_request` key) carrying `ai:workflow-heal`;
   2. was authored by the heal automation: a `User` with
      `author_association` `OWNER`, or the `github-actions[bot]` `Bot`;
   3. has a body line `<!-- workflow-failure-heal:fp=<token> -->`;
   4. names `stable` as its branch: the first `Integration branch:` line, else
      the first `Target branch:` line, parsed with the same patterns as
      `extract_integration_branch` in `scripts/resolve_integration_ref.sh`;
   5. had `ai:workflow-heal` applied only by its author, the first time within
      120 s of creation. Every events page is read (`--paginate --slurp`, one
      read per 100 events) and every `ai:workflow-heal` labeled event on every
      page is checked (AD-5; this replaced the first draft's single 100-item
      page, which failed any issue with 100 or more events).
2. **PR binding.** The target PR's author login equals issue `<n>`'s author
   login, so only the account that filed the heal issue can open a `stable`
   target for it. The pinned 40-hex head SHA from the PR listing stays the
   checkout SHA.
3. Default-branch and project-branch targets are unchanged.
4. Every failure keeps the existing
   `::error::Explicit validation target is not authorized.` and exits 1 before
   `sha=` is written.
5. GitHub reads: 1 for a default base, 2 for a project-branch base, and for a
   `stable` base the target listing, the issue, and one read per 100 of its
   events (3 for a typical heal issue; AD-5). The step comment documents the
   count.

## Non-goals

- The project-branch (stacked) case, the `target_ref` regex, and
  `Verify authorized checkout`.
- A commit-level binding for the head SHA (AD-2).
- Editing the #4734 plan doc or its progress log (AD-4).
- `.claude/**` (the provenance rules are re-stated inline, not imported).

## Constraints

- §1: fail closed on every missing, malformed, or unreadable field.
- §5: one workflow step, its test file, docs, and the changelog fragment.
- §6: the step keeps its `name:`, `id:`, env names, and `sha` output; the
  existing shell variables and jq defs are unchanged. New names
  (`target_author`, `heal_events_json`) were checked unused in `validate.yml`.
- §9: YAML 2-space; Python tabs.
- §15: extra reads only for the `stable` case (the issue, plus one per 100
  events; AD-5); no earlier call in the job returns the issue's author, body,
  or label history.
- §20: the #4734 fragment is amended in place (AD-3).
- §27: `validate.yml` is 69,061 bytes; check `wc -c` after the change.

## Approach

1. The classifying jq pass emits `stable <sha> <n> <pr author login>` for the
   `stable` case, and fails when the PR author login is not a plain GitHub
   login: alphanumeric segments joined by single hyphens, an optional `[bot]`
   suffix, at most 39 characters in all
   (`^[A-Za-z0-9](?:-?[A-Za-z0-9])*(\[bot\])?$`; AD-6, AD-7).
2. `stable`: read the issue, and in one jq pass require goals 1.1–1.4 and the
   author-login match (goal 2), emitting nothing on success.
3. Read every page of `repos/<repo>/issues/<n>/events` with `per_page=100`
   (`--paginate --slurp`, which yields `[[event, ...], ...]`) and require goal
   1.5 across all pages, comparing timestamps with `fromdateiso8601` (AD-5).
4. Only then write `sha=<head sha>`.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
change is one workflow step and its tests.

1. **Phase 1 — verify heal provenance and PR binding for stable targets.**
   - Files: `.github/workflows/validate.yml`,
     `tests/test_validate_target_ref_input.py`, `README.md`, `agents.md`,
     `changelog.d/4734-validate-stacked-and-stable-targets.md`.
   - Done when: every case in [Tests](#tests) passes, the rest of
     `tests/test_validate_target_ref_input.py`,
     `tests/test_workflow_checkout_integration_ref_audit.py`, and
     `tests/test_workflow_file_size_limit.py` pass, `actionlint` is clean,
     and `wc -c` stays under 480,000.
   - Rollback: revert the PR; the `stable` case returns to label-only.

## Implementation Steps

1. `validate.yml`: extend the classifier output, add the provenance and
   binding checks to the `stable` case, add the events read, update the §15
   comment.
2. Tests: the `gh` stub answers the events read (`EVENTS_JSON`); PR fixtures
   carry `user`; the stable test covers every goal.
3. `README.md` (`target_ref` input) and `agents.md` (validation hook
   paragraph): describe the provenance and binding rules and the read count.
4. Amend the #4734 changelog fragment.

## Files & Modules

- `.github/workflows/validate.yml`
- `tests/test_validate_target_ref_input.py`
- `README.md`
- `agents.md`
- `changelog.d/4734-validate-stacked-and-stable-targets.md`

## Tests

The step's `run:` body runs with a stub `gh`:

- genuine heal issue + PR by the issue author: allowed, three calls, the third
  is the events read with `per_page=100`;
- rejected: label missing; issue is a PR; author a `User` that is not `OWNER`
  (`COLLABORATOR`, `MEMBER`); a `Bot` other than `github-actions[bot]`; PR
  author differs from issue author; no fp marker (or marker only mid-line);
  `Target branch:` names another branch or is missing; an `Integration
  branch:` line naming another branch while `Target branch:` says `stable`;
  label applied by another account (even after the author's); label applied
  more than 120 s after creation or before creation; no labeled event; a
  re-label by another account or a late first label on a later events page;
  events output that is not an array of page arrays; events read fails;
  unreadable timestamps; a PR author login with spaces or other characters,
  double or edge hyphens, or over 39 characters with `[bot]` counted;
- a label history over 100 events is read across pages and accepted (AD-5);
- the bold and plain `Target branch:` forms and a `Target branch:` with
  trailing prose after the backticked name are accepted;
- existing default and project-branch cases unchanged (one and two calls).

## Risks & Mitigations

- A collaborator pushes commits onto a genuine heal project branch: the head
  SHA pin is the same trust boundary as every other target kind (AD-2).
- A heal issue filed by a different automation identity than the one that
  opens the final PR would be refused: fails closed; in this repo both are the
  `GH_PAT` / GitHub App account (`shubhodeep1`, verified on #4665 / #4667).
- The #4734 project's later conformance re-run reads a plan that says
  "carries `ai:workflow-heal`": the new rule is strictly narrower, and the
  #4734 changelog fragment, README, and agents.md describe it.

## Rollout

Lands on the #4734 project branch and ships with that project's final PR and
the next `@stable` tag. No wrapper or variable change.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Which provenance rules decide a genuine heal issue? — Picked: A — the `security_pass_skip.py` rules (automation author, label applied at creation by the author, fp marker) plus the issue naming `stable` as its branch. Alternatives: B — author and marker only, no events read; C — a new provenance record written by the heal intake. Why: reuses the rules already reviewed for #4623 at one extra read; C needs a new producer and still would not bind the PR. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How is the target PR and its head bound to the issue? — Picked: A — the PR author login must equal the issue author login; the head stays pinned to the PR listing's 40-hex SHA. Alternatives: B — also require the head commit's author to be the issue author (one more read; commit author metadata is set by whoever commits); C — require a workflow-posted record naming the head SHA (no workflow writes one today). Why: A blocks the collaborator-opened PR in the finding at no extra read; B adds no real assurance and C needs a new producer. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Which changelog fragment records the change? — Picked: A — amend `changelog.d/4734-validate-stacked-and-stable-targets.md` in place. Alternatives: B — add `changelog.d/4791-…` with a `security` entry. Why: the label-only rule never shipped (it lives only on the unmerged #4734 project branch), so the fragment that ships should describe the final rule; a separate entry would announce a fix for a release that never had the flaw. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Update the #4734 plan doc's Goal 3 and Risks? — Picked: A — leave the #4734 plan and log to that project's chain. Alternatives: B — edit them in this PR. Why: they are that project's records and its chain records its security follow-ups itself; §5. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #4791 (finding `stable-target-mutable-label-authorization`); Refs #3576 (audit tracker).
- Base project #4734, final PR #4746, phase PR #4758.
- Provenance precedent: #4623, `.claude/scripts/security_pass_skip.py`.
