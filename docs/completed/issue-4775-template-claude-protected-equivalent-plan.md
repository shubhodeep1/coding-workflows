# Treat workflow-templates/.claude as protected-equivalent for unattended authorization

Source issue: shubhodeep1/coding-workflows#4775 (https://github.com/shubhodeep1/coding-workflows/issues/4775)
Base branch: claude/implement-plan-issue-4678-edit-files-without-python-heredocs
Security pass: skip (ai:security: automation-produced issue)

## Summary

The security audit of the issue #4678 project branch found that CLAUDE.md §23.I
(line 1382) now says `workflow-templates/.claude/**` "is not protected". The
`@stable` sync copies that tree into every consumer's `.claude/`, where
unattended sessions follow it. So an unattended agent steered by issue text
could change a consumer's commands, hooks, or settings without the approval a
root `.claude/**` edit needs. This plan makes the template tree
protected-equivalent for authorization, in CLAUDE.md and in a deterministic
CI parity check that fails closed on template-only changes.

## Context

- Issue #4775 (`ai:security`, filed by `.github/workflows/security-audit.yml`,
  `Refs #3576`): finding `template-protected-path-bypass`, A01 Broken Access
  Control, severity high, confidence 8/10, location `CLAUDE.md:1382`,
  integration branch `claude/implement-plan-issue-4678-edit-files-without-python-heredocs`.
  Recommendation: "Treat `workflow-templates/.claude/**` as
  protected-equivalent in automated authorization. Fail closed on
  template-only command, hook, or settings changes unless provenance-scoped
  authorization and deterministic parity checks pass."
- The base branch is project #4678 (draft final PR #4684 into `main`). Its
  phase 1 (#4696) changed the CLAUDE.md §23.I triage clause to "a
  protected-path edit (the repository root's `.claude/**`;
  `workflow-templates/.claude/**` is not protected)" and added the same
  statement to `agents.md` ("Unattended helpers and permission prompt
  reports"). Its AD-1/AD-3 goal was narrow: an `ai:permission-prompt` issue
  about the template twins must be fixed, not closed as by design, because
  Claude Code does not prompt on Edit/Write there.
- Claude Code protects only the project root's `.claude/` and `~/.claude/`.
  `workflow-templates/.claude/**` is an ordinary path, so nothing in the
  harness stops an unattended Edit there. CLAUDE.md §28.C is the rule that
  stops an unattended phase that edits `.claude/**` until an operator records
  a `Protected-path approval:` line. That recorded operator answer is the
  provenance-scoped authorization the chain has; after #4678 the text reads as
  if template edits do not need it.
- `.github/workflows/update_workflows.yml` ("Sync .claude/ assets from
  upstream", line 190) mirrors `workflow-templates/.claude/` over the
  consumer's `.claude/`.
- Template parity today is per file: `test_template_parity` in
  `tests/test_permission_prompts.py`, `test_gh_api_write_guard.py`,
  `test_stale_routines.py`, `test_pr_check_in_reminder.py`,
  `test_pr_watch_guard.py`, `test_dispatch_workflow.py`,
  `test_implement_issue_claude_command.py`, `test_check_in_status.py`,
  `test_audit_plans_command.py`, `test_edit_comment.py`, and
  `test_implement_plan_claude_command.py`. Commands such as
  `fix-claude-pr.md`, `write-plan.md`, `seed-repo.md`, and `apply-url.md` have
  no parity test, so a template-only edit to them passes CI.
- Measured on the base branch (2026-09-28): every file under
  `workflow-templates/.claude/` has a root twin; all are byte-identical except
  five consumer-variant commands (`analyze-log.md`, `deploy-activate.md`,
  `investigate-issue.md`, `validate-consumer-issue.md`,
  `verify-activation.md`), which differ on purpose (consumer vs. upstream
  side). `.claude/commands/claude-issue-pickup.md` is root-only. There are no
  symlinks in the template tree.

## Goals

- CLAUDE.md §23.I keeps #4678's triage outcome (a prompt issue about the
  template twins is fixed, not closed as by design) and states that
  `workflow-templates/.claude/**` is protected-equivalent for authorization,
  with the reason (the `@stable` sync) and issue #4775.
- CLAUDE.md §28.C's protected-path stop names `workflow-templates/.claude/**`
  alongside `.claude/**`.
- A new contract test, `tests/test_claude_template_parity.py`, fails CI when:
  a file under `workflow-templates/.claude/` has no root `.claude/` twin; a
  twin differs from its root copy and is not a listed divergence; a listed
  divergence's template SHA-256 no longer matches its pin; a listed
  divergence no longer exists or is now identical; or the template tree holds
  a symlink. Its failure messages name CLAUDE.md §28.C.
- The test runs in `.github/workflows/ci.yml`.
- `tests/test_permission_prompts.py` pins the new §23.I and §28.C wording.
- `agents.md` and a `changelog.d/` fragment record the change.

## Non-goals

- No edit to `.claude/**` or `workflow-templates/.claude/**` (commands,
  hooks, scripts, `settings.json`), so this phase needs no protected-path stop.
- No change to `/implement-plan-claude` step 3's wording (AD-3).
- No new auto-merge gate in `review_autofix.yml` (AD-2).
- No change to the #4678 changelog fragment (AD-4) or to the
  `permission_prompts.py` issue body (#4678 AD-7).
- No change to the `update_workflows.yml` sync.

## Constraints

- §1: security first; the new check fails closed.
- §5: CLAUDE.md wording in two places, one test file, one test update, one
  `ci.yml` line, `agents.md`, one fragment.
- §6: no identifier, heading, or section number changes;
  `tests/test_claude_md_section_numbers.py` must pass. The #4678 test names
  stay; only the pinned clause text changes.
- §9: tabs in Python; `ci.yml` stays 2-space YAML.
- §14: CLAUDE.md is synced to consumers. The new test lives only in this repo.
- §20: a `security` fragment, because consumers receive the CLAUDE.md change.
- §28.C: the phase touches no `.claude/**` or `workflow-templates/.claude/**`
  path.

## Approach

1. CLAUDE.md §23.I: replace the triage clause's parenthetical with "the
   repository root's `.claude/**`, which Claude Code itself protects", and add
   one sentence right after it. The sentence says that
   `workflow-templates/.claude/**` is not a Claude Code protected path, so an
   Edit or Write there does not prompt and a prompt issue about it is fixed.
   It is still protected-equivalent for authorization, because the `@stable`
   sync copies it into every consumer's `.claude/`. So the §28.C
   protected-path stop covers it, and `tests/test_claude_template_parity.py`
   fails CI on a template-only change (issue #4775).
2. CLAUDE.md §28.C: "A phase that must edit `.claude/**` or its
   `workflow-templates/.claude/**` twins (hooks, …)", plus one sentence giving
   the twins' reason (the sync, not a Claude Code block).
3. `tests/test_claude_template_parity.py` [new]: walks
   `workflow-templates/.claude/` and checks each file against its root twin
   with `TEMPLATE_DIVERGENCE = {relative path: sha256 of the template copy}`
   holding the five consumer variants. It checks the listed divergences both
   ways, flags a template symlink, and pins the CLAUDE.md sentences. The
   failure messages tell the author that updating the list is a
   protected-equivalent change needing a recorded `Protected-path approval:`
   (CLAUDE.md §28.C) in an unattended project.
4. `.github/workflows/ci.yml`: add the new test to the existing "Unattended
   helper and permission prompt report tests (CLAUDE.md §23.I)" step.
5. `tests/test_permission_prompts.py`: update the #4678 triage-clause pin to
   the new wording.
6. `agents.md` and `changelog.d/4775-template-claude-protected-equivalent.md`.

Alternatives considered: an auto-merge gate in `review_autofix.yml` that
requires an owner approval on any PR touching either `.claude/` tree (AD-2 C).
It is the stronger control but changes the documented auto-merge contract for
every project and consumer. That is a §12.D tradeoff beyond the finding's
scope. Reverting #4678's clause (AD-1 B) would reopen the triage problem #4678
fixed.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
wording, the test that pins it, and the CI wiring only make sense together.

1. **Phase 1 — protected-equivalent template tree: CLAUDE.md §23.I/§28.C and
   the template parity contract test.**
   - Files: `CLAUDE.md`, `tests/test_claude_template_parity.py` [new],
     `tests/test_permission_prompts.py`, `.github/workflows/ci.yml`,
     `agents.md`, `changelog.d/4775-template-claude-protected-equivalent.md`
     [new].
   - Done when: the §23.I and §28.C wording is in CLAUDE.md; the new test
     passes on the branch and fails on each mutation in Tests; the
     `ci.yml` step lists it; `tests/test_permission_prompts.py`,
     `tests/test_claude_md_section_numbers.py`, and
     `tests/test_changelog_fragment_contract.py` pass; no `.claude/**` or
     `workflow-templates/.claude/**` file changed.
   - Rollback: revert the phase PR.

## Implementation Steps

1. `CLAUDE.md` §23.I (~1379-1384): new triage clause and protected-equivalent
   sentence (Approach 1).
2. `CLAUDE.md` §28.C (~2233-2240): name the template twins and their reason
   (Approach 2).
3. `tests/test_claude_template_parity.py` [new] (Approach 3).
4. `.github/workflows/ci.yml` (~432-437): add the test path to the §23.I step.
5. `tests/test_permission_prompts.py` (~399-404): update the pinned clause.
6. `agents.md` (~1104-1117): name the template twins in the §28.C bullet and
   qualify the "only protected path" sentence.
7. `changelog.d/4775-template-claude-protected-equivalent.md` [new],
   `<!-- changelog: security -->`.

## Files & Modules

- `CLAUDE.md` (and so `workflow-templates/CLAUDE.md`, a symlink to it)
- `tests/test_claude_template_parity.py` [new]
- `tests/test_permission_prompts.py`
- `.github/workflows/ci.yml`
- `agents.md`
- `changelog.d/4775-template-claude-protected-equivalent.md` [new]

## Tests

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_claude_template_parity.py tests/test_permission_prompts.py tests/test_claude_md_section_numbers.py tests/test_changelog_fragment_contract.py`, plus every existing
  `test_template_parity` file.
- Mutation checks, run on a scratch copy of the repository and never
  committed: append a byte to a twin template, add a template-only file, edit
  a divergent template, make a divergent template identical to its root copy,
  replace a template file with a symlink. Each must fail the new test.
- `yamllint -s .github/workflows/ci.yml` and `actionlint` on it.

## Risks & Mitigations

- Every legitimate edit to a consumer-variant command now also updates its
  pinned digest. That is intended: the edit is protected-equivalent, and the
  failure message says how to update the pin. Two in-flight PRs that edit the
  same variant conflict on one line.
- The test cannot see who made a change. Provenance stays with the §28.C
  `Protected-path approval:` line and with review. The test only makes a
  template-only change fail loudly instead of passing silently.
- `/implement-plan-claude` step 3 still says `.claude/**`. CLAUDE.md §28.C,
  which it cites and every session reads, now names the template twins. The
  #4678 conformance run found that the loose reading already stops such a
  phase (it fails safe).

## Rollout

Instructions plus a CI test. The test gates PRs in this repo on merge. The
CLAUDE.md wording reaches consumer repos on the next `@stable` sync. Nothing
to activate.

## Auto-decisions

- AD-1 [plan, 2026-09-28] How should CLAUDE.md treat `workflow-templates/.claude/**`? — Picked: A — keep "not a Claude Code protected path" for prompt triage (a prompt issue about it is fixed) and declare it protected-equivalent for authorization in §23.I and §28.C, since the sync copies it into consumer `.claude/`. Alternatives: B — revert #4678's clause to the unqualified `.claude/**`; C — leave the text as is. Why: A closes the authorization gap the audit found without undoing #4678's triage fix; C leaves a high-severity finding open. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Which deterministic control backs the rule? — Picked: A — a new CI contract test over the whole template tree: every template file needs a root twin and is byte-identical to it unless it is in an explicit divergence list whose template SHA-256 is pinned; no symlinks; wired into the existing §23.I `ci.yml` step. Alternatives: B — the same list by name only, with no digest; C — an auto-merge gate in `review_autofix.yml` that requires an owner approval for PRs touching either `.claude/` tree; D — wording only. Why: A fails closed on every template-only command, hook, or settings change and needs no protected-path edit. B lets a divergent command change silently. C changes the documented auto-merge contract for every project and consumer (§12.D), beyond §5 for this finding. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Also change `/implement-plan-claude` step 3 (and its template twin) to name `workflow-templates/.claude/**`? — Picked: A — no. CLAUDE.md §28.C, which step 3 cites and every session reads, names the twins, and the parity test is the deterministic backstop. Alternatives: B — yes, as a root `.claude/**` edit that stops this unattended chain for a watched session (§28.C); C — file a separate issue. Why: A is the smallest change (§5) and needs no protected-path stop. The #4678 conformance run found that step 3's loose reading already stops such a phase. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-28] How is the change recorded in the changelog? — Picked: A — a new `security` fragment `changelog.d/4775-template-claude-protected-equivalent.md` that qualifies the #4678 entry; the #4678 fragment stays as is. Alternatives: B — also rewrite the #4678 fragment. Why: the #4678 statement stays true for Claude Code's own protection, one fragment per PR (§20.B), and B edits another project's entry. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- The base branch's final PR #4684 is open (draft), so the base has not moved.

## References

- Issue #4775; audit tracker #3576; project #4678 (final PR #4684, phase PR
  #4696, conformance fix #4728)
- CLAUDE.md §14, §20, §23.I, §28.C
- `.github/workflows/update_workflows.yml` ("Sync .claude/ assets from upstream")
- https://code.claude.com/docs/en/permission-modes.md#protected-paths
