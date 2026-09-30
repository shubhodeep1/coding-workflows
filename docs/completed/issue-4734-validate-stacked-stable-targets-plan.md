# validate.yml authorizes explicit targets whose final PR goes into a project branch or stable

Source issue: shubhodeep1/coding-workflows#4734 (https://github.com/shubhodeep1/coding-workflows/issues/4734)
Base branch: main
Security pass: run

## Summary

The `Authorize explicit validation target` step of `.github/workflows/validate.yml`
only accepts a `target_ref` whose one open PR goes into the default branch, so
issue-mode `/implement-plan-claude` projects whose final PR targets another
project branch or `stable` stop at validation. This plan adds two narrowly
bound cases, a one-level stacked project branch and a `stable` base for a
workflow-heal issue, and keeps every existing check.

## Context

- `.github/workflows/validate.yml:176-211` (on `main` at `3b19418`) lists the
  open PRs with `head=<owner>:<target_ref>` **and** `base=<default>`, then
  requires exactly one, same-repo head and base, an `OWNER` / `MEMBER` /
  `COLLABORATOR` author, `.base.ref == <default>`, and a 40-hex `head.sha`.
- Issue mode (`.claude/commands/implement-plan-claude.md`, Issue Mode) builds a
  project on the branch the issue names: another project's branch for a
  follow-up, `stable` for a workflow-heal issue. Its final PR then targets
  that branch, and the validation stage dispatches `target_ref` = the project
  branch. The step rejects it as "missing or ambiguous".
- Blocked by this today: #4665 (workflow heal; final PR #4667,
  `claude/implement-plan-issue-4665-reissue-new-output-paths` → `stable`).
  #4687 / #4688 (final PRs #4695 / #4694 into
  `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason`)
  were answered to skip validation and are moving on (owner comment on #4734).
- The step runs before `actions/checkout`, in the caller's repository, so its
  body cannot come from a script file in this repository (AD-1).

## Goals

1. A `target_ref` whose single open PR goes into the default branch is
   authorized exactly as today.
2. **Project-branch base.** A `target_ref` whose single open PR goes into a
   `claude/implement-plan-*` branch is authorized only when that base branch
   has exactly one open PR, that PR goes into the default branch, and it
   passes the same same-repo and author-association checks. A base whose own
   PR goes anywhere else (a second level of stacking), or with no or several
   open PRs, is rejected.
3. **`stable` base.** A `target_ref` whose single open PR goes into `stable`
   is authorized only when `target_ref` matches
   `claude/implement-plan-issue-<n>-*` and issue `<n>` (an issue, not a pull
   request) carries `ai:workflow-heal`.
4. Every other base is rejected. 0 or 2+ open PRs for `target_ref` still fail;
   the listing never falls back to "any open PR for the head".
5. The step issues at most **two** GitHub reads: the existing (paginated) PR
   listing for `target_ref`, plus one parent-PR listing (case 2) or one issue
   read (case 3). A comment in the step documents the count.

> **Superseded for the `stable` case by #4791** (security follow-up, final PR
> #4793 merged into this project branch, AD-6). A label and a branch name are
> forgeable, so goal 3 now also requires verified heal-automation provenance
> for issue `<n>` and the target PR's author to be the issue's author, and the
> `stable` case reads every issue-events page (goal 5: three reads for a
> typical heal issue). See `docs/completed/issue-4791-validate-stable-heal-provenance-plan.md`.

## Non-goals

- Changing `Verify authorized checkout`, the `target_ref` regex, or
  `git check-ref-format`.
- Deeper stacking than one level.
- Editing `.claude/commands/implement-plan-claude.md` (AD-4).
- Posting `/reclarify` on #4665 after merge (AD-5: an operator step).

## Constraints

- §1: the new cases only widen the base the target PR may point at; the
  target binding (exactly one open same-repo PR, trusted author, 40-hex head)
  is unchanged and still decides the checked-out SHA.
- §5: one workflow step, its test file, docs, and a changelog fragment.
- §6: no identifier renamed or removed. The step keeps its `name:`, `id:`, the
  env names `GH_TOKEN` / `VALIDATE_TARGET_REF` / `VALIDATE_DEFAULT_BRANCH`,
  and the `sha` output. New shell variables (`target_binding`, `target_kind`,
  `target_base`, `heal_issue`, `parent_prs`, `heal_issue_json`) and the jq
  function `trusted_pr` were checked unused in `validate.yml`.
- §9: YAML stays 2-space; Python tests use tabs.
- §15: the base filter moves from the query into `jq`; at most one extra read
  per run, only for the new cases.
- §20: changelog fragment (`fixed`).
- §27: `validate.yml` is 65,523 bytes, far below the 480,000-byte guard;
  check `wc -c` after the change.

## Approach

1. List open PRs with `head=<owner>:<target_ref>` (no `base=` filter).
2. One `jq` pass requires exactly one PR that is open, same-repo head and
   base, trusted author, `head.ref == target_ref`, 40-hex `head.sha`, and
   classifies its base:
   - `== <default>` → `default <sha>`;
   - matches `^claude/implement-plan-[A-Za-z0-9][A-Za-z0-9._-]*$` →
     `project <sha> <base>`;
   - `== stable` and `target_ref` matches
     `^claude/implement-plan-issue-([1-9][0-9]*)-` → `stable <sha> <n>`;
   - anything else → error.
3. `project`: `git check-ref-format --branch <base>`, list open PRs with
   `head=<owner>:<base>` (no base filter) and require exactly one, open,
   same-repo, trusted author, `head.ref == <base>`, `base.ref == <default>`
   (AD-2).
4. `stable`: read `repos/<repo>/issues/<n>` and require an object with no
   `pull_request` key whose labels include `ai:workflow-heal` (AD-3).
5. Write `sha=<head sha>` only after every check passed. Every failure keeps
   the existing `::error::Explicit validation target is not authorized.` and
   exits 1.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
change is one workflow step and its tests.

1. **Phase 1 — authorize stacked project-branch and stable-heal targets.**
   - Files: `.github/workflows/validate.yml`,
     `tests/test_validate_target_ref_input.py`, `README.md`, `agents.md`,
     `changelog.d/4734-validate-stacked-and-stable-targets.md` [new].
   - Done when: every case in [Tests](#tests) passes, the rest of
     `tests/test_validate_target_ref_input.py` and
     `tests/test_workflow_checkout_integration_ref_audit.py` pass, the
     workflow parses as YAML, and `wc -c` stays under 480,000.
   - Rollback: revert the PR; the step returns to default-branch-only.

## Implementation Steps

1. `validate.yml` `Authorize explicit validation target`: drop `-f base=…`
   from the listing, replace the single `jq` check with the classifying pass,
   add the parent-PR and issue-label reads, and a comment documenting the
   call count (at most 2).
2. `tests/test_validate_target_ref_input.py`: replace the `gh` stub with one
   that answers by request (target listing, parent listing, issue read) and
   logs its arguments; keep every existing assertion; add the cases below and
   assert the listing carries no `base=` filter and at most two calls.
3. `README.md` (validate inputs, `target_ref`) and `agents.md` (validation
   hook isolation paragraph): describe the two new cases.
4. `changelog.d/4734-validate-stacked-and-stable-targets.md`.

## Files & Modules

- `.github/workflows/validate.yml`
- `tests/test_validate_target_ref_input.py`
- `README.md`
- `agents.md`
- `changelog.d/4734-validate-stacked-and-stable-targets.md` [new]

## Tests

Unit tests run the step's `run:` body with a stub `gh`:

- default-branch base: allowed (one API call);
- project-branch base with one open parent PR into the default branch:
  allowed (two calls);
- project-branch base with no parent PR, two parent PRs, a parent PR into
  another branch, a fork parent, or an untrusted parent author: rejected;
- `stable` base with `ai:workflow-heal` on the issue: allowed; without the
  label, with a head that is not `claude/implement-plan-issue-<n>-*`, or when
  `<n>` is a pull request: rejected;
- another base (`develop`): rejected;
- two PRs for the head: rejected;
- fork head: rejected;
- unauthorized author: rejected;
- the target listing carries no `base=` filter.

Plus `python3 -m pytest tests/test_validate_target_ref_input.py
tests/test_workflow_checkout_integration_ref_audit.py
tests/test_workflow_file_size_limit.py`, and `actionlint` when available.

## Risks & Mitigations

- A collaborator opens a PR from a `claude/implement-plan-*` branch into any
  `claude/implement-plan-*` branch to get it validated — mitigated: the parent
  must itself be a trusted, same-repo PR into the default branch, and the
  target's author must already be trusted, so the trust boundary is the same
  author-association check as today.
- Anyone who can label an issue could add `ai:workflow-heal` — ACCEPTED: the
  target PR must still be authored by an `OWNER` / `MEMBER` / `COLLABORATOR`,
  which is the existing trust boundary; the label only scopes which `stable`
  targets qualify, as the issue specifies. **Superseded:** the security pass
  found this acceptance insufficient (#4791); the `stable` case now verifies
  heal provenance and binds the PR author to the issue author (#4793).
- Consumer repos pick the change up with the next `@stable` sync — no wrapper
  change is needed (the inputs are unchanged).

## Rollout

Ships with the next `@stable` tag; no flag, no wrapper change. After merge the
owner posts `/reclarify` on #4665 (AD-5).

## Auto-decisions

- AD-1 [plan, 2026-09-28] Where does the new authorization logic live? — Picked: A — inline in the `validate.yml` step. Alternatives: B — move to `scripts/validate_authorize_target.sh`. Why: the step runs before checkout in the caller's repository, so a script file in this repo is not on disk there, and the workflow is 65,523 bytes, far under the §27 guard. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How is the project-branch base's own PR bound? — Picked: A — list open PRs for the base head without a base filter and require exactly one, into the default branch. Alternatives: B — filter the listing by `base=<default>` and require exactly one. Why: stricter; a base branch with a second open PR anywhere is ambiguous, and one level of stacking is enforced by the same check. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] What must issue `<n>` satisfy for a `stable` base? — Picked: A — an issue (no `pull_request` key) carrying `ai:workflow-heal`, any state. Alternatives: B — also require it to be open; C — label only. Why: the issue says "issue `<n>` carries `ai:workflow-heal`"; excluding pull requests keeps the number bound to an issue, and requiring open state would reject a re-run after an explicit close. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Update the "Validation target trust boundary" line in `.claude/commands/implement-plan-claude.md`? — Picked: A — leave `.claude/**` unchanged and document the new cases in `README.md` and `agents.md`. Alternatives: B — edit both `.claude/commands/` and `workflow-templates/.claude/commands/` copies. Why: a `.claude/**` edit stops an unattended phase (CLAUDE.md §28.C), and the existing line stays true for default-base projects. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Post `/reclarify` on #4665 after merge? — Picked: A — leave it to the owner and list it as the next step in the progress comment and final report. Alternatives: B — the chain posts it. Why: `/reclarify` starts an unattended pipeline, which CLAUDE.md §23.C keeps ask-first. Applied in: no code change. Status: pending review
- AD-6 [completion, 2026-09-29] The security follow-up #4791 replaced goal 3's label-only `stable` rule and the "ACCEPTED" label risk, so this plan no longer matches the shipped code. How to archive it? — Picked: A — add a short superseded note under the goals and on the risk, pointing at the #4791 plan. Alternatives: B — rewrite goals 3 and 5, the approach, and the tests to the final rule; C — archive it unchanged. Why: the final PR's reviewer panel reads the plan as the spec; a note fixes the misleading text without rewriting the history of what this project planned (§5). Applied in: completion PR. Status: pending review

## Notes

- `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`.

## References

- Issue #4734; blocked projects #4665 / #4667 (and #4687 / #4695, #4688 / #4694).
- Refs #4586.
