# Fail closed on direct `.claude/` guard-path changes in the twin sync-state check

Source issue: shubhodeep1/coding-workflows#5246 (https://github.com/shubhodeep1/coding-workflows/issues/5246)
Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
Security pass: skip (ai:security: automation-produced issue)

## Summary

The CI step "Claude twin sync state (CLAUDE.md §28.C)" accepts any `.claude/` change whose content equals the twin at the same commit. So an ordinary PR can change `.claude/hooks/**` or `.claude/settings.json` and its twin to the same content in one go, pass CI, and reach `main` through ordinary review and auto-merge, skipping the owner-only merge that `claude-twin-sync.yml` enforces for guard paths. This plan makes `scripts/claude_twin_sync.py check` fail closed on guard-path changes: on a PR into `main` they are allowed only from a same-repository `claude/claude-twin-sync-*` head whose guard files equal the twin already on the base commit, and on a push to `main` only when the new content equals the twin at the push's `before` commit.

## Context

- Security audit finding `guard-sync-provenance-bypass` (issue #5246, high, confidence 9/10, `scripts/claude_twin_sync.py:375`), filed against the #4785 project branch `claude/implement-plan-issue-4785-twin-first-claude-sync` (draft final PR #4804).
- `check_not_ahead` (`scripts/claude_twin_sync.py:345-380`) compares every changed `.claude/<rel>` with `workflow-templates/.claude/<rel>` **at head**. A PR that edits both copies together passes. It never looks at guard paths or at who opened the PR.
- The sync workflow itself refuses to merge guard paths (`merge_check`, `is_guard_path`), labels guard sync PRs `ai:claude-sync-approval`, and leaves them to the owner. The `review_autofix.yml` gate and `scripts/claude_pr_sweep.py` skip `claude/claude-twin-sync-*` heads. The bypass is a guard change that never goes through a sync PR.
- Claude-fixer mode (every PR-backed `claude/*` head) auto-merges only with a fresh, ready check-run snapshot with `failed_count: 0` (`scripts/review_autofix_step_claude_fixer_handoff.sh:130-150`), so a failing `lint` job blocks its auto-merge. The GPT review path feeds failing checks to reviewers and the editor (`CHECK_RUNS_AUTOFIX_ENABLED`) but has no hard gate, and `main` has no required status checks (plan #4785, "Facts about `main`").
- The deterministic small-diff / doc-only skip is already suppressed for any `.claude/*` path (`review_autofix.yml` gate, `PROTECTED_SKIP_SUPPRESSED`).
- Every PR and commit here is authored by the owner account (sessions through the App proxy, `GH_PAT` in Actions). No bot identity exists, so author or committer identity cannot prove provenance (#4785 plan, Risks: ACCEPTED).
- The CI step (`.github/workflows/ci.yml:194-225`) runs in the `lint` job on PRs into `main` (base: the merge commit's first parent) and pushes to `main` (base: `github.event.before`), and skips everything else.
- Binding rules: §1 (security first), §5 (minimal change), §6 (no renames; new identifiers unique), §9 (tabs; YAML 2-space), §15 (no new API calls), §19 (`Refs #5246`, never `Fixes`), §20 (changelog fragment), §23.C (rulesets are operator actions), §27 (`review_autofix.yml` is not touched).

## Goals

- G1: On a PR into `main`, `check` reports a violation for every changed `.claude/` guard path (`hooks/**`, `settings.json`, `settings.local.json`: `is_guard_path`) unless **all** of these hold:
  - the head ref starts with `SYNC_BRANCH_PREFIX` (`claude/claude-twin-sync-`);
  - the head repository equals the base repository (case-insensitive);
  - the guard file exists at head as a regular file whose blob and mode equal the twin's at the **base** commit.
- G2: On a push to `main` (and a local run without PR context), a changed guard path is a violation unless its head blob and mode equal the twin's at the **base** commit (`before`). A merged sync PR passes; one PR that changed both copies together fails.
- G3: Deleting a `.claude/` guard file is always a violation (the sync never deletes), including a deletion of both copies. Non-guard files keep today's rule unchanged (equal to the twin at head; both missing is a synced delete).
- G4: `ci.yml` passes the PR context to `check` through `env:` only (no `${{ }}` inside `run:` text): `--event pull_request --pr-head-ref … --pr-head-repo … --repo …` for PRs, `--event push` for pushes.
- G5: Tests reproduce the exploit (a PR editing a hook and its twin together fails) and cover every rule above, the CLI, and the `ci.yml` wiring.
- G6: `agents.md` ("Sync-state check"), `CLAUDE.md` §28.C, and a `changelog.d/5246-guard-sync-provenance.md` fragment (`security`) describe the rule.

## Non-goals

- Changing `review_autofix.yml` (AD-1), the ruleset, or required checks (§23.C operator steps).
- Changing the sync workflow, `merge_check`, `plan_sync`, or the non-guard rule.
- Proving provenance by commit author, committer, or PR author (AD-2).
- Editing `.claude/**` (this phase touches none).

## Constraints

- §6: no identifier is renamed or removed. `check_not_ahead(repo, base, head)` keeps its positional signature and output keys; new keyword-only parameters `event`, `pr_head_ref`, `pr_head_repo`, `base_repo` and new CLI flags `--event`, `--pr-head-ref`, `--pr-head-repo`, `--repo` (on `check` only) were checked against `scripts/claude_twin_sync.py` and are unused there. The new result keys are `event` and `sync_pr`.
- §15: no GitHub API call is added. Everything comes from git and the event payload.
- §9: tabs in Python; 2-space YAML.
- The `::error` line keeps its escaping (`escape_command_property` / `escape_command_data`) for PR-controlled paths; the head ref and repo come from the event payload through `env:` and are never printed into a workflow command without escaping.

## Approach

`check_not_ahead` keeps its loop over the changed `.claude/` paths. For a path whose `rel` is a guard path it applies the guard rule instead of the twin-at-head rule:

1. Missing at head → violation `guard path deleted; hooks and settings are never deleted through a sync, so the repository owner merges this by hand (CLAUDE.md §28.C)`.

   > **Extended by #5609** (2026-10-01): `.claude/scripts/**` is a guard path too (`GUARD_PATH_PREFIXES = ("hooks/", "scripts/")`, `scripts/claude_twin_sync.py:95`), and the violation now reads `guard path deleted; hooks, scripts, and settings are never deleted through a sync, so the repository owner merges this change by hand` (`scripts/claude_twin_sync.py:384`). The text above records what #5246 shipped.

2. `event == "pull_request"` and the PR is not a sync PR (`is_sync_pr_head(pr_head_ref, pr_head_repo, base_repo)` is false: wrong prefix, other repo, or empty values) → violation `guard path changed outside a claude-twin-sync PR; edit only workflow-templates/.claude/<rel> and let the owner merge the sync PR`.
3. Head blob or mode differs from the twin at `base`, or either side is not a regular file → violation `guard path differs from workflow-templates/.claude/<rel> on the base commit; only a copy of the twin already on the default branch may change it`.

`event` defaults to `"push"`. An unknown value is refused by argparse, and a PR run with no head ref or repo is treated as a non-sync PR (fail closed). Rejected alternatives are recorded as auto-decisions.

## Phases & Merge Strategy

A single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase for one issue.

1. **Phase 1 — guard-path rule in `claude_twin_sync.py check`, CI wiring, tests, docs.**
   - **Scope:** the Implementation Steps below.
   - **Done when:**
     - `tests/test_claude_twin_sync.py` passes locally, including the new exploit reproduction;
     - `ruff` and `yamllint` pass as CI runs them;
     - `python3 scripts/claude_twin_sync.py check --base <project branch base> --head HEAD` passes on the phase branch;
     - no path under `.claude/` differs from the base branch.
   - **Rollback:** revert the PR; the check returns to the twin-at-head rule.

## Implementation Steps

1. **`scripts/claude_twin_sync.py`:**
   - add `is_sync_pr_head(pr_head_ref, pr_head_repo, base_repo)`;
   - `check_not_ahead(repo, base, head, *, event="push", pr_head_ref="", pr_head_repo="", base_repo="")`: the guard rule above for guard paths; unchanged rule otherwise; add `event` and `sync_pr` to the result;
   - `check` subcommand: `--event {push,pull_request}` (default `push`), `--pr-head-ref`, `--pr-head-repo`, `--repo`; pass them through;
   - module docstring: describe the guard rule under `check`.
2. **`.github/workflows/ci.yml`** step "Claude twin sync state (CLAUDE.md §28.C)": add `PR_HEAD_REF`, `PR_HEAD_REPO`, `REPOSITORY` to `env:`; call `check` with `--event pull_request --pr-head-ref "${PR_HEAD_REF}" --pr-head-repo "${PR_HEAD_REPO}" --repo "${REPOSITORY}"` on PRs and `--event push` on pushes; update the step comment.
3. **`tests/test_claude_twin_sync.py`:** ordinary PR editing a hook and its twin together (fails, the reproduction); the same in push mode (fails); `settings.json`; a sync PR copying the base twin (passes, both modes); a sync-prefixed PR from another repo (fails); a sync PR whose guard file equals the twin at head but not at base (fails); guard deletion of both copies (fails); guard mode change (fails); a PR with an empty head ref (fails); the CLI flags and exit codes; the `ci.yml` step passes the PR context through `env:` and uses `--event pull_request` / `--event push`.
4. **Docs:** `agents.md` "Sync-state check" bullet; `CLAUDE.md` §28.C sentence on the CI check; `changelog.d/5246-guard-sync-provenance.md` (`<!-- changelog: security -->`).

## Files & Modules

- `scripts/claude_twin_sync.py`
- `.github/workflows/ci.yml`
- `tests/test_claude_twin_sync.py`
- `agents.md`, `CLAUDE.md`
- `changelog.d/5246-guard-sync-provenance.md` [new]
- `docs/implement-plan/issue-5246-guard-sync-provenance.md` (log)

## Tests

- **Unit** (`tests/test_claude_twin_sync.py`, throwaway git repos): every case in step 3.
- **Existing:** the whole file stays green; `tests/claude_twin_state.py` users are unaffected (they do not call `check`).
- **Lint:** `ruff check` and `yamllint` as `ci.yml` runs them.

## Risks & Mitigations

- **The GPT review path has no hard gate on failing checks, and `main` has no required checks.** A codex-authored PR editing a guard path now gets a failing `lint` job, which the review feeds to its reviewers and editor, but nothing deterministic stops a clean-review auto-merge there. ACCEPTED (AD-1): Claude-fixer PRs are hard-gated, unattended Claude sessions cannot write `.claude/**`, and making `lint` a required check in the `main` ruleset is an operator step (§23.C), listed under Rollout and in `agents.md`.
- **A deliberate owner change to a hook (a conflict fix, a hook removal) now fails CI.** Intended: guard changes outside a sync PR need the owner, who merges the PR by hand; the push to `main` then reports it once. Documented in `agents.md`.
- **A sync PR built on an older `main` whose twin moved since fails G1.3** until the sync workflow rebuilds it on the next push to `main` that touches the twins (already the rebuild rule). No change needed.
- **Provenance is by branch name and repository, not identity.** Anyone with write access can push a `claude/claude-twin-sync-*` branch, but its guard content must equal the reviewed twin on `main`, and such a PR is skipped by AI review and the sweep and never merged by the workflow, so only the owner merges it (AD-2).

## Rollout

1. The phase PR merges into the #4785 project branch; the rule reaches `main` with #4785's final PR #4804.
2. Operator steps (§23.C, never done by the chain), optional: add `lint` (and `claude-twin-sync/owner-approval`) as required status checks in the `main` ruleset, so GitHub itself enforces the guard rule for every PR.
3. Disable: revert the PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where is the guard policy enforced? — Picked: A — in `claude_twin_sync.py check` (the CI "Claude twin sync state" step in the `lint` job), fail closed for PRs into `main` and pushes to `main`. Alternatives: B — also add a `review_autofix.yml` gate skip for every PR touching a `.claude/` guard path; C — PRs only, pushes unchanged. Why: A fixes the check the finding names and blocks Claude-fixer auto-merge deterministically; B edits a 455 KB reusable workflow that consumers run, with several merge points (§5, §12.C blast radius), and the GPT-path residual is documented with the operator's required-check step. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as "verified bot provenance" when every PR and commit is authored by the owner account? — Picked: A — a same-repository `claude/claude-twin-sync-*` head plus guard content bound to the twin blob and mode on the base commit. Alternatives: B — also require the `claude-twin-sync` committer identity on every head commit; C — refuse every guard change, sync PRs included. Why: B's identity is forgeable with `git config` and adds no security; C breaks the owner-merged sync path #4785 ships. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How does a push to `main` treat guard changes, where no PR context exists? — Picked: A — the new content must equal the twin at `before`, and a deletion fails. Alternatives: B — keep the twin-at-head rule for pushes; C — skip guard paths on pushes. Why: A passes every merged sync PR and reports a combined twin-and-`.claude/` edit that reached `main`; B and C leave the bypass invisible after merge (§1). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Is deleting a guard file together with its twin still allowed as a "synced delete"? — Picked: A — no: a guard deletion is always a violation, so removing a hook or setting needs the owner's hand-merge. Alternatives: B — keep allowing a two-sided guard delete. Why: the sync never copies a deletion, and removing a guard weakens what sessions are limited by (§1); non-guard deletes are unchanged (§5). Applied in: phase 1. Status: pending review

## Notes

- `.claude/scripts/security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5246` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- This project touches no path under `.claude/`.

## References

- Issue #5246; parent project #4785 (`docs/plans/issue-4785-twin-first-claude-sync-plan.md`, final PR #4804)
- CLAUDE.md §28.C; agents.md "Claude twin sync"
