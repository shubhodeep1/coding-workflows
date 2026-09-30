# Implement-Plan Log — Guard differential check: accept intended loosening only from a base-branch policy, never from the PR body

- Plan: docs/plans/issue-5326-guard-differential-trusted-loosening-plan.md
- Source issue: shubhodeep1/coding-workflows#5326   Progress comment: 5902380346
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5174-guard-differential-check
- Project branch: claude/implement-plan-issue-5326-guard-differential-trusted-loosening   Final PR: #5360 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance fix PR (see ## Conformance)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: project checker session_01KsqE28FAB72xsTiHLCUEvX (per-wait safety-net and hand-back ids are in the stage report)
- Last updated: 2026-09-30
- Last note: conformance 1/3: CONFORMANT (G1-G6 mapped to merged code, 105 guard differential tests pass); one stale-doc concern fixed in the conformance fix PR (a malformed `--head-sha` always exits 2, even with no hook change; AD-7).

## Phases
1. [x] Phase 1 — base-branch loosening policy (script, policy file, tests, ci.yml step, docs)   — PR #5389 merged 2026-09-30 (250b29f); review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — fix PR (stale `--head-sha` exit-code wording in agents.md and plan G5, plus a pinning test) (pre-security)

## Security pass
- Skipped (ai:security: automation-produced issue), per the plan header.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Where may an intended-loosening exception come from? — Picked: A — a policy file read only from the base ref, each entry scoped to hook, verbatim shape, and PR head commit, with required `approved_by` / `reason` audit fields. Alternatives: B — no exceptions at all; an operator merges past the red check (§23.C); C — keep the PR body but require an OWNER/MEMBER PR author. Why: A is machine-verifiable with no API call and sits in the check's existing base-ref trust boundary; C gates nothing here because every PR is the owner's. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What does the PR body's `Intended loosening:` section do now? — Picked: A — keep parsing it (§6) but as data only: a listed shape without a policy entry still fails, and its regression line carries `pr_body_listed=true`. Alternatives: B — ignore the body, leaving `--pr-body-file` a no-op; C — require both the body listing and the policy entry. Why: keeps the flag and parser meaningful for reviewers without letting the author authorize anything. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Where does the policy live, and in what format? — Picked: A — `.github/guard_differential/intended_loosening.json`, `{"version": 1, "exceptions": [...]}`. Alternatives: B — `tests/guard_corpus/intended_loosening.json`; C — a markdown list. Why: separate from the corpus that PRs routinely edit, never matched by the corpus loader's `*.txt` glob, and strictly parseable. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] How does the check learn the head revision? — Picked: A — a new `--head-sha` flag fed from `github.event.pull_request.head.sha` in CI; otherwise the `--head-ref` commit; otherwise none, and no entry matches. Alternatives: B — the checked-out merge commit, which changes on every base move; C — the head hook tree's object id. Why: the event payload is GitHub's, not the author's, and #5326 asks for head-revision scope. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] New changelog fragment or correct #5174's unreleased one? — Picked: A — correct the one sentence in `changelog.d/5174-guard-differential-check.md`, no new fragment. Alternatives: B — add `changelog.d/5326-….md` as a `security` entry as well. Why: the PR-body exemption never reached a release, so a separate security entry would describe behaviour no release carried and contradict the #5174 entry. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Update #5174's plan goal G5, which specifies the PR-body exemption? — Picked: A — amend G5 to the policy rule with a pointer to #5326, so #5174's later conformance and activation audits grade the fixed behaviour. Alternatives: B — leave it, and let #5174's audits flag the fix as a regression. Why: the goal is superseded by a security finding filed against that project. Applied in: phase 1 PR. Status: pending review
- AD-7 [conformance 1/3, 2026-09-30] agents.md and plan G5 say a malformed `--head-sha` exits 2 only once a hook tree is compared, but the code validates it up front and exits 2 even when no hook changed. Which one moves? — Picked: A — keep the code (a malformed input fails closed) and correct agents.md and G5, with a CLI test pinning it. Alternatives: B — move the `--head-sha` check after the no-hook-change skip to match the old wording. Why: §1 fail-closed on malformed input and §5 smallest change (docs only); CI always passes a valid SHA from the event payload, so no PR changes outcome. Applied in: conformance fix PR. Status: pending review

## Lessons
- [source:intervention] A versioned JSON config must reject a missing, non-integer (including `true`), or unknown `version` instead of reading it under the current rules, and every audit field the text output prints must also be in the `--json` rows. (files: scripts/guard_differential.py)
- [source:conformance] When a CLI validates a flag before an early skip, the docs must say the flag fails even on the skip path; phrase exit-code rules per input, not "X or Y exits 2 once …". (files: agents.md, scripts/guard_differential.py)
- [source:conformance] A new `scripts/*.py` needs its `docs/INVENTORY.md` entry in the same PR, or `tests/inventory_parity.py` (`CI / Inventory parity`) fails on the next PR into `main`. (files: docs/INVENTORY.md, tests/inventory_parity.py)

## Notes
- Conformance 1/3 (2026-09-30), outside this project's scope and not fixed here: (1) `tests/inventory_parity.py` fails on the project branch (and on its base) because `docs/INVENTORY.md` lacks `scripts/guard_differential.py`, a #5174 gap that #5185's CI will report; fixing it in each of #5174's four child projects would conflict. (2) `ci.yml` runs the PR's own copy of `scripts/guard_differential.py`, so a PR can edit the checker itself (visible in the diff, unlike the body); #5327's project (final PR #5364) pins the verifier to the base branch.
- Review round 1 (2026-09-30): the `— resume.` block named `Checker session: session_22a9c863-2258-5384-b27b-7d08164ff3ae`, which is not a session id in `list_sessions`; the real project checker is this stage's parent, session_01KsqE28FAB72xsTiHLCUEvX, and was reused. The checker's `session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}` apparently yields a UUID form in its environment.
- Local verification (phase 1): 39 of 40 related suites pass. `tests/test_orchestrate_poll_process.py` (CI-sharded) hit a local 300 s cap with no failure. `tests/test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean` also fails on the unchanged base because this container has no `gawk`.
- Issue mode (CLAUDE.md §28.A): single-phase plan written by /implement-issue-claude from issue #5326 (security-audit finding on #5174's project branch).
- Base branch is not the default branch: the issue closes by an explicit close + `ai:merged` at final merge, and steps 12–13 do not run (`Activation: n/a`).
- `ci.yml` runs only on PRs into `main` / `stable`, so neither this project's PRs nor its final PR (into #5174's branch) run the guard differential step; verification is local, and the step gates #5185 into `main`.
