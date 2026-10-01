# Require a trusted target and a same-repository fix PR in permission_prompts.py duplicate-check

Source issue: shubhodeep1/coding-workflows#5809 (https://github.com/shubhodeep1/coding-workflows/issues/5809)
Base branch: claude/implement-plan-issue-4867-close-permission-prompt-duplicates
Security pass: skip (ai:security: automation-produced issue)

## Summary

`permission_prompts.py duplicate-check` lets an unattended `/implement-issue-claude` session close a pipeline-filed `ai:permission-prompt` issue as a duplicate. It accepts any issue as the target and any pull request that mentions the target as its fix, so in this public repository an outsider can open a target issue with a forged class marker and a fork PR whose title names it, and the session closes a genuine issue with no verified fix. This plan makes the check require a target written by a trusted account and a same-repository fix PR by a trusted account.

## Context

- Security audit finding #5809 (`A01:2021-Broken Access Control`, high, confidence 9/10) at `.claude/scripts/permission_prompts.py:920`, filed by `.github/workflows/security-audit.yml` against the project branch of issue #4867 (`Refs #3576`, the audit tracker).
- The code lives on the issue base `claude/implement-plan-issue-4867-close-permission-prompt-duplicates` (draft final PR #4883), which added `duplicate-check` and the CLAUDE.md §23.C carve-out / §23.I "Closing pipeline-filed duplicates" rules. Nothing of it is on `main` yet.
- `decide_duplicate_close` (`permission_prompts.py:865-944`) checks condition 1 (the issue is pipeline-filed) strictly, but condition 2 only checks the target's state and that the fix PR "references" the target by head branch name or `#<M>` in its title or body (`_references_issue`, `:857-862`). Neither the target's author nor the PR's head repository or author is checked. The `target_class` and `target_occurrences` evidence fields are read from the target body, which its author controls.
- `shubhodeep1/coding-workflows` is public: anyone can open issues and fork PRs.
- `check_in_status.FIX_CLAIM_TRUSTED_ASSOCIATIONS` (`OWNER`, `MEMBER`, `COLLABORATOR`) is the existing trust set for fix claims (CLAUDE.md §26.H, issue #4622); `permission_prompts.py` already imports `check_in_status`.

## Goals

1. `duplicate-check` is not eligible when the target issue's `author_association` is not `OWNER`, `MEMBER`, or `COLLABORATOR`.
2. `duplicate-check` is not eligible when the fix PR's head repository is missing (a deleted fork) or differs from its base repository (a fork PR).
3. `duplicate-check` is not eligible when the fix PR's `author_association` is not `OWNER`, `MEMBER`, or `COLLABORATOR`.
4. The `target_class` and `target_occurrences` evidence fields are `null` when the target fails goal 1, so an untrusted body's markers never reach the session's evidence.
5. The read budget stays at most five REST GETs (§15): every new field comes from the issue and PR objects already read.
6. CLAUDE.md §23.I condition 2, `agents.md`, and a `changelog.d/` fragment state the new rule.

## Non-goals

- No change to condition 1 (the closed issue's own provenance), the class routing in `file`, or the filer.
- No requirement that the target itself be pipeline-filed (AD-1): a maintainer-filed root-cause issue such as #4858 stays a valid target.
- No change to `/implement-issue-claude` step 5a's text: it already defers to the script's verdict.
- No change to `.claude/settings.json` or any allow rule.

## Constraints

- §1: security first; the check fails closed (a missing field is untrusted).
- §5: minimal change in `decide_duplicate_close` and the docs that state its rules.
- §6: no rename; `decide_duplicate_close`'s signature and the JSON verdict keys are unchanged (no new key).
- §15: no new API call.
- §20: security fix → one `changelog.d/5809-…` fragment.
- §28.C / `/implement-plan-claude` step 4: `.claude/scripts/permission_prompts.py` is a protected path, so the phase runs under the interim twin-first default: only the `workflow-templates/.claude/scripts/permission_prompts.py` twin is edited, and the `.claude/` copy follows at the twin sync.

## Approach

In `decide_duplicate_close`, after the existing condition-2 checks:

- Target trust: `target.get("author_association") in check_in_status.FIX_CLAIM_TRUSTED_ASSOCIATIONS`, else the reason `#<M> author <login> (<association>) is not an owner, member, or collaborator`. The target evidence markers are read only from a trusted target's body.
- Fix PR same-repository: `head.repo.full_name` and `base.repo.full_name` both present and equal (case-insensitive), else `PR #<P> is not a same-repository PR (head <head repo or unknown>)`.
- Fix PR trust: `fix_pr.get("author_association")` in the same set, else `PR #<P> author <login> (<association>) is not an owner, member, or collaborator`.
- The existing `_references_issue` binding stays (AD-3): with the PR now same-repository and trusted-authored, its branch name and text are written by an account with write access.

Alternatives are recorded as auto-decisions below.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the fix is one function, its tests, and the docs that state its rule.

1. **Phase 1 — trusted target and same-repository fix PR in `duplicate-check`.**
   - Files: `workflow-templates/.claude/scripts/permission_prompts.py` (twin; `.claude/scripts/permission_prompts.py` at the twin sync), `tests/test_permission_prompt_duplicates.py`, `CLAUDE.md`, `agents.md`, `changelog.d/5809-duplicate-check-trusted-target-and-fix.md` [new].
   - Protected paths: `.claude/scripts/permission_prompts.py` (edited through its `workflow-templates/.claude/` twin).
   - Done: goals 1-6 hold; the new tests fail against the base twin and pass against the change; `tests/test_permission_prompt_duplicates.py` passes; after the twin sync `tests/test_permission_prompts.py` passes; ruff is clean.
   - Rollback: revert the phase PR; the check returns to the base behaviour.

## Implementation Steps

1. `workflow-templates/.claude/scripts/permission_prompts.py`: add the three checks and the trusted-target evidence rule in `decide_duplicate_close`; update the module docstring's `duplicate-check` paragraph and the function docstring.
2. `tests/test_permission_prompt_duplicates.py`: give `_target` and `_fix` the trusted fields (`author_association: OWNER`, same-repository `head.repo` / `base.repo`), add failing cases for a fork PR, a deleted-fork head, an untrusted PR author, an untrusted target author, and the null target evidence, and one assertion that the five-read budget still holds.
3. `CLAUDE.md` §23.I condition 2 and `agents.md` "Duplicate close": state the trust rule.
4. `changelog.d/5809-duplicate-check-trusted-target-and-fix.md` [new], section `security`.

## Files & Modules

- `workflow-templates/.claude/scripts/permission_prompts.py`
- `.claude/scripts/permission_prompts.py` (twin sync only)
- `tests/test_permission_prompt_duplicates.py`
- `CLAUDE.md`
- `agents.md`
- `changelog.d/5809-duplicate-check-trusted-target-and-fix.md` [new]

## Tests

- Unit: `tests/test_permission_prompt_duplicates.py` (loads the twin), new parametrized cases plus the evidence-field case; run with `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider`.
- Parity: `tests/test_permission_prompts.py` stays red until the twin sync copies the twin into `.claude/scripts/` (expected under twin-first), then passes.
- Lint: `ruff check` on the changed Python files.

## Risks & Mitigations

- A legitimate fix PR authored by a bot account (association `NONE` / `CONTRIBUTOR`) is now refused. ACCEPTED: the check fails closed and the session asks on the issue, which is the pre-#4867 behaviour; pipeline and Claude PRs here are authored by the owner account.
- A legitimate fix PR from a fork is refused. ACCEPTED: fork PRs never carry pipeline fixes in this repository.
- The twin sync is a supervising-session step. Mitigated by the twin-first blocker, which lists the exact copy and the tests to run.

## Rollout

Ships with the issue #4867 project (final PR #4883) when that reaches `main`; consumer repos receive it on the next `@stable` sync of `.claude/`. No flag; revert the phase PR to roll back.

## Auto-decisions

- AD-1 [plan, 2026-10-01] What provenance must the duplicate target have? — Picked: A — its `author_association` is `OWNER`, `MEMBER`, or `COLLABORATOR` (no new read). Alternatives: B — the target must itself be pipeline-filed (condition-1 checks on the target, a sixth read for its events); C — both. Why: A shuts out an outsider-forged target within the five-read budget and keeps §23.I condition 2's documented scope, where a maintainer-filed root-cause issue (#4858) is a valid target; B changes that contract (§5, §12.D). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What must the fix PR satisfy beyond referencing the target? — Picked: A — a same-repository head (`head.repo.full_name` equals `base.repo.full_name`) and a trusted `author_association`. Alternatives: B — same-repository head only; C — trusted author only. Why: both fields are in the one PR read, and together they refuse fork PRs and PRs by accounts without write access (§1). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] Keep the title/body `#<M>` reference as a binding, or require the head branch `issue-<M>-`? — Picked: A — keep both bindings, now only on a trusted same-repository PR. Alternatives: B — head branch only. Why: once the PR is trusted-authored, its text is written by an account with write access; B would refuse fix PRs whose branch does not carry the number (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] What happens to `target_class` / `target_occurrences` for an untrusted target? — Picked: A — report `null`, so an untrusted body's markers never reach the evidence. Alternatives: B — report them as before (the verdict is ineligible anyway). Why: the finding asks that causes not be read from editable markers; A costs one condition and no new key (§6). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-01] Edit `.claude/scripts/permission_prompts.py` directly, or twin-first? — Picked: A — twin-first under the interim automatic default (`/implement-plan-claude` step 4, CLAUDE.md §28.C), with a hold claim and the twin-sync blocker on #5809. Alternatives: B — ask how to run the phase. Why: coding-workflows has `workflow-templates/.claude/`, the plan does not require a watched session, and the default applies. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5809` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5809 (this finding), #4867 (the duplicate-close project), #4883 (its final PR), #3576 (security audit tracker), #4622 (trusted fix claims), #4858 (inline-interpreter class).
