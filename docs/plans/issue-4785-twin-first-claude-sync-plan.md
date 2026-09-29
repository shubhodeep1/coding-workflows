# Land .claude/ changes without a watched session: twin-first edits plus an Actions sync PR

Source issue: shubhodeep1/coding-workflows#4785 (https://github.com/shubhodeep1/coding-workflows/issues/4785)
Base branch: main
Security pass: run

## Summary

Unattended sessions cannot edit `.claude/**`: Claude Code never auto-approves those edits. So every `/implement-plan-claude` phase that changes a hook, a setting, a command, or a session script stops at `Status: BLOCKED` until an operator-watched session applies it. This plan makes the unprotected twin, `workflow-templates/.claude/**`, the only thing unattended sessions edit. A new workflow, `.github/workflows/claude-twin-sync.yml`, copies each changed twin into `.claude/**` through one sync PR after the change reaches `main`. Sync PRs that touch only commands and scripts merge themselves once CI is green. Sync PRs that touch hooks or settings wait for the repository owner.

## Context

- Issue #4785 records the operator decision (Q37: A, 2026-09-28): twin-first edits, sync after merge, merge rule by path, and no mid-branch hook changes.
- CLAUDE.md §28.C ("Protected-path edits") and `/implement-plan-claude` steps 3–4 stop a phase that must edit `.claude/**`. The Claude-fixer convergence phases 3–4, #4755, the `gh` read-loop guard change, and every `ai:permission-prompt` fix hit this stop.
- Today `.claude/**` and `workflow-templates/.claude/**` are kept byte-identical by hand, and about 15 test files assert that (for example `tests/test_pr_watch_guard.py:245`, `tests/test_implement_plan_claude_command.py:31`, `tests/test_check_in_status_hand_back.py:482`). Behaviour tests load the `.claude/` copy.
- Not every file is a twin. `commands/analyze-log.md`, `commands/deploy-activate.md`, `commands/investigate-issue.md`, `commands/validate-consumer-issue.md`, and `commands/verify-activation.md` are deliberate consumer variants: the `workflow-templates/` copy is the consumer-repo edition, and the two differ by design. `commands/claude-issue-pickup.md` exists only in `.claude/` and is never shipped.
- Consumers receive `workflow-templates/.claude/**` from `.github/workflows/update_workflows.yml` ("Sync .claude/ assets from upstream"). They take it at `@stable` and never read this repo's `.claude/`.
- `review_autofix.yml` runs every PR-backed `claude/*` head in Claude-fixer mode (CLAUDE.md §26.H). A clean review, or the deterministic small-diff skip, enables auto-merge. `scripts/claude_pr_sweep.py` queues a fresh Claude fixer for any `claude/*` PR whose fix is due. Both already skip a PR whose title or body carries `[skip ai]`, and neither merges it.
- Facts about `main` (read 2026-09-28):
  - Repository ruleset 14197800 has only `deletion`, `non_fast_forward`, and `copilot_code_review` rules, and they apply to every branch. There are no required status checks and no required reviews, so `gh pr merge --auto` would merge at once.
  - Every PR in the repo, whether from a session or from a `GH_PAT` workflow, is authored by the owner account `shubhodeep1`. GitHub does not let a PR's author approve it.
  - CI (`.github/workflows/ci.yml`) runs only on PRs into `main` / `stable` and on pushes to them, with a depth-1 checkout.
- Binding rules: §5 (minimal change set), §6 (no renames; new identifiers unique), §9 (tabs; YAML 2-space), §15 (API budget), §18.A/B/F (wired into the scheduler, registry entry), §19, §20 (changelog fragment), §23.C (merges, repo settings, and rulesets are operator actions), §23.E / §24.G (no session env vars in Actions code), §27 (`review_autofix.yml` is 451,634 bytes, under the 480,000 guard; this plan adds under 3 KB).

## Goals

- G1: `CLAUDE.md` §28.C, `/implement-plan-claude`, `/implement-issue-claude`, and `/fix-claude-pr` tell unattended sessions to edit only `workflow-templates/.claude/**` in coding-workflows and never `.claude/**`. A phase that changes a twinned `.claude/` file no longer stops `BLOCKED` for that reason. The protected-path stop remains only for the non-twin files (G2's exclusion list), and in repos without `workflow-templates/.claude/` (consumers), where `.claude/**` is upstream-owned.
- G2: `scripts/claude_twin_sync.py` defines the twin set: every regular file under `workflow-templates/.claude/`, minus a fixed exclusion list (`UPSTREAM_ONLY_PATHS`: the five consumer variants and `commands/claude-issue-pickup.md`).
  - It refuses symlinks, `..` segments, absolute paths, control characters, and any path that resolves outside the twin root or `.claude/`.
- G3: `.github/workflows/claude-twin-sync.yml` runs on:
  - a push to `main` that touched `workflow-templates/.claude/**`;
  - an hourly schedule (catch-up);
  - `workflow_dispatch`;
  - completion of the `CI` workflow on a sync branch;
  - a PR review submitted or dismissed on a sync branch.
- G4: Each run syncs idempotently.
  - A file whose `.claude/` copy is missing, or equals an earlier version of its twin, is copied.
  - A file whose `.claude/` copy matches neither the current twin nor any earlier twin version, or is a symlink, is a **conflict**. It is listed and never overwritten.
  - With nothing to copy and no conflict, the run opens nothing and closes a stale open sync PR with a comment.
  - Otherwise the run creates `claude/claude-twin-sync-<main sha>` with one PR. If a sync PR is already open, it updates that PR's branch with a forward-only commit instead of opening a second one.
- G5: Merge rule by path.
  - A sync PR whose diff is only non-guard `.claude/` files is merged by the workflow (`--squash --match-head-commit`) when all of these hold:
    - every check run on its head has completed with `success`, `neutral`, or `skipped`;
    - CI's `lint` run is among them;
    - every changed path is a non-guard, non-excluded `.claude/` file whose head content equals the twin on `main`.
  - A sync PR that touches `.claude/hooks/**`, `.claude/settings.json`, or `.claude/settings.local.json`, or that lists a conflict, is labelled `ai:claude-sync-approval` and triggers one Telegram alert. The workflow never approves, merges, or enables auto-merge on it.
  - The workflow posts a commit status `claude-twin-sync/owner-approval` on every sync head. For a guard or conflict sync PR it reads `success` only when the owner's latest review on the current head is `APPROVED`, and `pending` otherwise. A non-guard sync PR gets `success`, so the status can be made a required check without blocking it.
- G6: Automation cannot merge a sync PR by AI review alone. Sync PRs carry `[skip ai]`, and in addition `review_autofix.yml`'s gate and `scripts/claude_pr_sweep.py` skip any head starting `claude/claude-twin-sync-`, with no auto-merge.
- G7: A new CI step runs `claude_twin_sync.py check`, so `.claude/` is never ahead of its twin. It fails when a PR, or a push to `main`/`stable`, changes a non-excluded `.claude/<file>` to anything other than the twin's content at the same commit. The twin may be ahead of `.claude/`.
- G8: Tests follow the twin.
  - Behaviour and text tests load `workflow-templates/.claude/**`, so a twin-only PR is exercised.
  - Byte-parity asserts become `tests/claude_twin_state.py::assert_claude_not_ahead`:
    - it passes when the copies match, or when the `.claude/` copy equals an earlier twin version;
    - it fails when the `.claude/` copy never matched;
    - it skips in a shallow clone, where G7's step enforces the rule.
- G9: Security.
  - `GH_PAT` is used only for the push, the PR (open, update, label, close, merge), and the commit status. Everything else uses `github.token`.
  - Every `actions/checkout` is pinned to a full SHA with `persist-credentials: false`.
  - The workflow reads no session env var.
  - The job runs only in `shubhodeep1/coding-workflows`.
- G10: Docs:
  - `agents.md` gains a "Claude twin sync" section, including consumer ordering;
  - `README.md` lists the workflow;
  - `docs/scripts-pending-removal.md` gets an entry (§18.F, `permanent — review annually`);
  - `changelog.d/4785-twin-first-claude-sync.md` is added (§20);
  - `.github/ai/label_contract.v1.json` gains the new label.

## Non-goals

- Editing `.claude/**` in this project. The command-file edits reach `.claude/commands/**` through the first sync PR after the final PR merges (the issue's bootstrap order).
- Changing `update_workflows.yml` or what consumers receive. Consumers keep taking the twin at `@stable` and never depend on a sync PR.
- Teaching the Claude issue pickup (`claude-issue-pickup.md`, upstream-only, "never reads … pull requests") to watch for the label. The Telegram alert replaces it (AD-6).
- Changing the repository ruleset. Making `claude-twin-sync/owner-approval` a required check is an operator step (§23.C), listed under Rollout.
- Reconciling the five consumer-variant commands.

## Constraints

- §6: no identifier is renamed. New names (`claude_twin_sync.py`, `claude-twin-sync.yml`, `UPSTREAM_ONLY_PATHS`, `GUARD_PATH_PREFIXES`, `GUARD_PATH_FILES`, `ai:claude-sync-approval`, `claude-twin-sync/owner-approval`, `tests/claude_twin_state.py`, `assert_claude_not_ahead`) were checked against the repo and are unused.
- §15: each workflow run makes a bounded number of REST calls:
  - one call to list open PRs;
  - one call to list the sync PR's files;
  - one call to page its check runs;
  - one call to read its reviews;
  - writes only when state changes.

  No GraphQL.
- §18.A/B: no manual script. Everything runs from the workflow's triggers.
- §27: the `review_autofix.yml` change is a few gate lines. Size is re-checked with `wc -c`.
- §19: every PR in this project uses `Refs #4785`. Only the final PR into `main` carries `Fixes #4785`.

## Approach

One Python module holds all decisions and is unit-tested against temporary git repos. The workflow is thin shell around it.

- `plan --repo-root . --ref <main sha>`: for each twin file at `<ref>`, compares `.claude/<rel>` at `<ref>`.
  - Missing → `copy`.
  - Equal → nothing.
  - Blob equals any blob the twin path had in `git log <ref> -- <twin path>` (up to 500 revisions) → `copy`.
  - Otherwise → `conflict`.
  - A symlink on either side → `conflict` with a reason.

  It prints JSON listing `copies`, `conflicts`, and `guard` (whether any copy or conflict is a guard path).
- The copies are written, with the twin's file mode, onto a temporary git index (`build_tree`), never into a work tree. (AD-11: the plan first named this an `apply` subcommand.)
- `check --base <sha> --head <sha>` implements G7 on `git diff --name-status`.
- `merge-check --pr-files <json> --head <sha> --ref <main sha>` implements G5's content test.

The workflow checks out `main` with `fetch-depth: 0` and runs `plan`. It then finds the open sync PR, if any: an open PR whose head branch is in this repo and starts `claude/claude-twin-sync-`. (AD-11: the plan first also required the `GH_PAT` account as author; every PR here is authored by the owner account, and a foreign sync-prefixed PR is rebuilt from `main` plus the copies, so the filter would add nothing.) What follows:

1. **Nothing to do** → close any stale sync PR with a comment and exit.
2. **Build the desired tree**: main's tree plus the copies. If only conflicts exist, the tree equals main's.
3. **Commit it.**
   - No open PR: create a commit on `claude/claude-twin-sync-<short sha>` from `main`. When there are no copies, this is an empty marker commit.
   - Open PR: create a two-parent commit with `git commit-tree <tree> -p <sync head> -p <main>`. The branch only moves forward, as the ruleset requires, and the PR diff is exactly the copies. Skip this when the sync head's `.claude/` tree already equals the desired tree's and `git diff --name-only $(git merge-base main <sync head>) <sync head>` lists no path outside `.claude/`: a `main` commit elsewhere changes neither the PR's diff nor what it merges, and a new head would restart CI and void an owner approval, while a foreign commit on the sync branch is rebuilt away. (AD-13: the plan first rebuilt whenever the tree or parents were not current. AD-14: the foreign-commit rebuild.)
4. **Push and PR.** Push with `GH_PAT`. Open or edit the PR, with `[skip ai]` and the copy and conflict lists in the body. Sync PRs name no issue, so they never cross-link #4785 or trip §19.
5. **Guard or conflict PR** → set the label, the status, and one Telegram alert per new head.
6. **Non-guard PR with no conflict** → try the merge rule (G5). The `workflow_run` and hourly triggers retry it.

Every trigger runs the same idempotent pass: for a PR review that refreshes the status and, on a non-guard PR, retries the merge rule (AD-11).

The rejected alternatives are recorded as auto-decisions below.

## Phases & Merge Strategy

A single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase for one issue. Everything below lands in one PR against the project branch, and the project's final PR into `main` carries it all.

1. **Phase 1 — twin-first docs, sync workflow, sync-state checks.**
   - **Scope:** the Implementation Steps below.
   - **Done when:**
     - `tests/test_claude_twin_sync.py` and every converted test file pass locally;
     - `python3 scripts/claude_twin_sync.py check` passes on the phase branch against its base;
     - the workflow YAML parses and passes `yamllint` and the repo's workflow contract tests;
     - `wc -c .github/workflows/review_autofix.yml` is under 480,000;
     - no path under `.claude/` differs from the base branch (`git diff --name-only origin/<base>... -- .claude` is empty).
   - **Rollback:** revert the PR. With the workflow gone, twins simply stop syncing, and a pending sync PR can be closed by hand.

## Implementation Steps

Phase 1:

1. **`scripts/claude_twin_sync.py` [new]** (tabs, `#!/usr/bin/env python3`).
   - Constants `TWIN_ROOT = "workflow-templates/.claude"`, `CLAUDE_ROOT = ".claude"`, `UPSTREAM_ONLY_PATHS` (6 entries), `GUARD_PATH_PREFIXES` (`hooks/`) and `GUARD_PATH_FILES` (`settings.json`, `settings.local.json`), `SYNC_BRANCH_PREFIX = "claude/claude-twin-sync-"`, `HISTORY_LIMIT = 500`.
   - Subcommands `plan`, `check`, `merge-check`, and `run` (the workflow driver). The copies are written by `build_tree` on a temporary index, and the owner approval is decided in-process by `owner_approved` from the reviews list and the head sha (AD-11: the plan first named these `apply` and `approval` subcommands). Git access goes through `subprocess` with argument lists only.
   - Docstring per §15: input and output shape, API calls (none — git only), and fail-closed behaviour: any error exits 2, and the workflow treats that as "do not merge".
2. **`.github/workflows/claude-twin-sync.yml` [new]**, 2-space YAML.
   - Triggers: see G3.
   - `permissions: contents: read`; `concurrency` group per repo with no cancel-in-progress; `if: github.repository == 'shubhodeep1/coding-workflows'`.
   - Every checkout uses `actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.1.0` with `persist-credentials: false`.
   - Steps: plan → build → push and PR → label, status, and alert → merge attempt.
   - `GH_PAT` goes only into the env of the push, PR, and status steps. The push uses `git -c http.extraheader` scoped to that one command. Reads use `github.token`.
3. **`review_autofix.yml` gate:** after the `[skip ai]` check (around line 542), add a head-ref case. A `claude/claude-twin-sync-*` head sets `SHOULD_RUN=false`, `SKIP_REASON=claude_twin_sync`, and never reaches the deterministic-skip auto-merge job. It also logs `AUTOFIX_GATE_SKIP reason=claude_twin_sync`, the gate's existing skip-log convention (AD-11). `pr_head_ref` must already be resolved at that point; if it is not, move the check to just after it is resolved.
4. **`scripts/claude_pr_sweep.py` `list_candidates`:** skip heads starting with the sync prefix and log `reason=claude_twin_sync`. The prefix is a local constant; `check_in_status.py` lives under `.claude/` and is not edited.
5. **`ci.yml`:**
   - Add a step "Claude twin sync state (CLAUDE.md §28.C)" after the checkout. It fetches the event's base sha (`pull_request.base.sha`, or `push.before`) at depth 1 and runs `claude_twin_sync.py check`. When there is no base (a new branch, or an all-zero `before`), it only reports.
   - Add a pytest step for `tests/test_claude_twin_sync.py`.
6. **`tests/claude_twin_state.py` [new]**: `assert_claude_not_ahead(rel)` per G8.
   - Convert every byte-parity assert to it, in: `test_pr_watch_guard`, `test_gh_api_write_guard`, `test_pr_check_in_reminder`, `test_pr_merge_status_guard` (not the `CLAUDE.md` symlink assert), `test_permission_prompts`, `test_check_in_status`, `test_check_in_status_hand_back`, `test_dispatch_workflow`, `test_edit_comment`, `test_security_pass_skip`, `test_implement_plan_claude_command`, `test_implement_issue_claude_command`, `test_audit_plans_command`, `test_ingest_implement_plan_lessons`.
   - Switch behaviour and text loads to the twin in those files and in `test_stale_routines`, `test_session_start_extract_repo_slug`, `test_claude_issue_route`, `test_update_workflows_guardrails`, and `test_orchestrate_poll_promote_cycle`, where they read a twinned `.claude/` file. Reads of non-twin files stay on `.claude/`.
7. **`tests/test_claude_twin_sync.py` [new]**:
   - `plan`: copy, equal, conflict, new file, symlink, `..`, excluded, and guard classification;
   - the `check` rule;
   - `merge-check`, including a twin edit smuggled onto the sync branch and a guard path;
   - `owner_approved`, including a stale head, a later `CHANGES_REQUESTED`, and a non-owner reviewer;
   - workflow contract: triggers, SHA pins, `persist-credentials: false`, and `GH_PAT` only in the allowed steps; no `GH_TOKEN`/`FUNTOKEN_IO_CF`/`FT_GAMES_CF`/`DIGITALOCEAN_ACCESS_TOKEN` session reads; `[skip ai]` in the PR body; no `--auto`; no approve call.
8. **`review_autofix` and sweep tests:** add a case to `tests/test_review_autofix_claude_fixer_mode.py` (or the gate contract test that owns the `[skip ai]` case) and to `tests/test_claude_pr_sweep.py`.
9. **Twin command edits** (twins only):
   - `workflow-templates/.claude/commands/implement-plan-claude.md`: step 3 "Protected paths" and step 4 stop now cover only non-twin `.claude/` paths, and consumer repos; add a Rule "Edit the twin, never `.claude/**`".
   - `implement-issue-claude.md` and `fix-claude-pr.md`: one Rule line each.
10. **`CLAUDE.md`:**
    - §28.C "Protected-path edits" rewritten for twin-first, sync after merge, and the merge rule by path;
    - one sentence in §26.H naming the sync-branch exception;
    - §23.I's protected-path sentence adjusted to point at the twin.

    No section is added or renumbered (§6).
11. **Docs:**
    - `agents.md`: new "Claude twin sync" section, and the line at around line 1105 updated;
    - `README.md`: the workflow list and the line at around line 1080;
    - `.github/ai/label_contract.v1.json`: `ai:claude-sync-approval`;
    - `docs/scripts-pending-removal.md` entry;
    - `changelog.d/4785-twin-first-claude-sync.md`;
    - progress log.

## Files & Modules

- `scripts/claude_twin_sync.py` [new]
- `.github/workflows/claude-twin-sync.yml` [new]
- `.github/workflows/review_autofix.yml`
- `.github/workflows/ci.yml`
- `scripts/claude_pr_sweep.py`
- `tests/claude_twin_state.py` [new]
- `tests/test_claude_twin_sync.py` [new]
- `tests/test_*.py` listed in step 6, plus `tests/test_review_autofix_claude_fixer_mode.py` and `tests/test_claude_pr_sweep.py`
- `workflow-templates/.claude/commands/implement-plan-claude.md`, `implement-issue-claude.md`, `fix-claude-pr.md`
- `CLAUDE.md` (`workflow-templates/CLAUDE.md` is a symlink to it)
- `agents.md`, `README.md`, `.github/ai/label_contract.v1.json`, `docs/scripts-pending-removal.md`
- `changelog.d/4785-twin-first-claude-sync.md` [new]
- `docs/implement-plan/issue-4785-twin-first-claude-sync.md` (log)

## Tests

- **Unit** (`tests/test_claude_twin_sync.py`): every classification and rule above, built on throwaway git repos.
- **Contract:** the workflow YAML is loaded and asserted on (triggers, pins, credentials, token scoping, no merge or approve on guard PRs, `[skip ai]`).
- **Existing suites:** every converted file runs green locally (`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider <files>`). They are also run with a deliberately twin-only edit applied in a scratch branch, to show they pass while `.claude/` lags. `claude_twin_sync.py check` then reports that nothing is ahead.
- **Lint:** `yamllint`, `ruff` (as CI runs them), and `bash -n` / `shellcheck` on any shell in the workflow.
- **End-to-end:** after the final PR merges, the push to `main` runs the workflow, which opens the first sync PR carrying the three command edits. The runtime validation stage and verify-activation read that run.

## Risks & Mitigations

- **Every AI actor acts as the owner account on GitHub.** Sessions go through the App proxy as `shubhodeep1`, and `GH_PAT` is `shubhodeep1`. A session could therefore submit an "owner" approval, or merge a guard sync PR itself. ACCEPTED: the workflow cannot tell a human owner from an AI acting as the owner. The boundary is policy: sessions never approve, and merges are §23.C ask-first. The residual risk is documented in `agents.md`, and a dedicated sync identity is suggested as an operator follow-up.
- **The owner cannot approve a sync PR its own account opened.** Because `GH_PAT` is the owner, `claude-twin-sync/owner-approval` stays `pending` in this repo, and the owner reviews and merges the PR by hand. ACCEPTED (AD-2); documented.
- **Twin edits reach consumers at `@stable` with AI review only.** This is pre-existing and unchanged. ACCEPTED, noted in `agents.md`.
- **Someone pushes non-twin content to a sync branch.** `merge-check` compares every changed file to `main`'s twin and refuses any path outside `.claude/`, so such a PR is never merged automatically.
- **The history scan is slow on a long history.** `HISTORY_LIMIT` is 500 revisions per file, and blob hashes are compared rather than contents.
- **A conflict persists after its PR is merged or closed without being resolved.** The next run opens a new one. That is intended: the conflict stays visible until the owner edits both copies to match.

## Rollout

1. The final PR merges into `main`. Its push touches `workflow-templates/.claude/**`, which runs `claude-twin-sync.yml`.
2. The workflow opens the first sync PR, carrying the three command edits. It is non-guard, so it merges once CI is green (the bootstrap order from the issue).
3. Operator steps (§23.C, never done by the chain):
   - optionally add `claude-twin-sync/owner-approval` as a required status check in the `main` ruleset, so GitHub itself enforces the owner gate;
   - optionally give the workflow a dedicated bot identity, so owner approval becomes possible and distinguishable.
4. Disable: turn off the workflow in the Actions UI, or revert. Twins then stop syncing, and nothing else changes.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Which files does the sync treat as twins, given five intentional consumer-variant commands and one upstream-only command? — Picked: A — an explicit `UPSTREAM_ONLY_PATHS` list in `scripts/claude_twin_sync.py`. Those files are never synced and are exempt from the sync-state check, and editing their `.claude/` copy keeps the protected-path stop. Alternatives: B — sync every differing file, which overwrites the upstream variants with consumer editions; C — make the variants identical, which changes consumer behaviour. Why: the only option that keeps both editions intact (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-28] How is "merges only after an owner approving review" enforced, when `GH_PAT` authors PRs as the owner (so self-approval is impossible) and `main` has no required reviews? — Picked: A — the workflow never approves, merges, or enables auto-merge on a guard or conflict sync PR. It posts the `claude-twin-sync/owner-approval` status from a verified owner `APPROVED` review on the current head, and the owner merges. Alternatives: B — enable auto-merge once an approval is verified; C — open guard PRs with `github.token` so the owner can approve. Why: the literal reading of "never approves or merges it itself", with the fewest privileges (§1); C contradicts "GH_PAT … for the PR". Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-28] How do sync PRs stay out of Claude-fixer review, auto-merge, and the §26.H sweep? — Picked: A — the existing `[skip ai]` marker in the body, plus a head-ref skip (`claude/claude-twin-sync-*`) in the `review_autofix.yml` gate and in `claude_pr_sweep.py`, so removing the marker cannot route a guard PR into AI auto-merge. Alternatives: B — the marker only; C — the head ref only. Why: the marker extends an existing mechanism, and the head ref cannot be edited on an open PR. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-28] What does "auto-merges once required checks pass" mean with no required checks on `main`? — Picked: A — the workflow merges a non-guard sync PR itself (`--squash --match-head-commit`) only when every head check run has completed green, including CI's `lint`, and `merge-check` passes. It retries on CI completion and hourly. Alternatives: B — `gh pr merge --auto`, which merges immediately before CI. Why: B would merge untested. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-28] When every differing file is a conflict there is nothing to copy, yet the issue says the sync PR "opens with the conflict listed". — Picked: A — open or update the sync PR with an empty marker commit, the conflict list, and the approval label. Alternatives: B — only alert, with no PR. Why: it follows the issue literally and gives the conflict one labelled home. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-28] Who alerts the operator for `ai:claude-sync-approval`? The Claude issue pickup never reads PRs and has no twin. — Picked: A — the workflow sends one Telegram alert per new head through `scripts/tg_helpers.sh`, and adds the label. The pickup is unchanged. Alternatives: B — edit `claude-issue-pickup.md`, which is upstream-only and would stop the chain for a watched session. Why: it reaches the operator without a protected-path stop. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-28] How do suites cover the twin on a twin-only PR? — Picked: A — behaviour and text tests load the twin, and parity asserts become `assert_claude_not_ahead` (equal, or equal to an earlier twin version; skipped in shallow clones, where the CI `check` step enforces the rule). Alternatives: B — parametrise over both copies, which fails on every twin-only PR. Why: the twin is the source of truth, and each `.claude/` state was the tested twin when it was current. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-28] How is an open sync PR updated when the ruleset forbids non-fast-forward pushes on every branch? — Picked: A — a forward-only two-parent commit (`git commit-tree`, with parents the sync head and `main`) whose tree is `main` plus the copies. Alternatives: B — close it and open a new PR every run. Why: B duplicates PRs, which the issue forbids. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-09-28] What is the "old twin" in the conflict rule for a catch-up run that has no single push to compare against? — Picked: A — any version the twin path had in `git log` (up to 500 revisions): a `.claude/` copy equal to one of them is behind; otherwise it is a conflict. Alternatives: B — only the twin at the push's `before` sha, which leaves scheduled runs undefined. Why: it gives the same answer for every trigger. Applied in: phase 1. Status: pending review
- AD-10 [plan, 2026-09-28] What happens to an open sync PR when the twins already match? — Picked: A — close it with a comment, since the workflow owns it. Alternatives: B — leave it open. Why: stale PRs would otherwise accumulate. Applied in: phase 1. Status: pending review
- AD-11 [conformance 1/3, 2026-09-29] The shipped phase differs from the plan text in five equivalent-or-stronger ways (no `apply` / `approval` subcommands, `GUARD_PATH_PREFIXES` / `GUARD_PATH_FILES` instead of `GUARD_PATHS`, the open sync PR matched by a same-repository head instead of the `GH_PAT` author, the gate log key `AUTOFIX_GATE_SKIP reason=claude_twin_sync`, and a review running the full idempotent pass). How are they reconciled? — Picked: A — correct the plan text to the shipped behaviour. Alternatives: B — change the code to the plan text (add the two CLI subcommands, an author filter, a new log key, a status-only review path); C — leave both and record the divergence only. Why: the shipped design is equivalent or stronger (no work-tree writes; the author filter distinguishes nothing when every PR is authored by the owner account, and a foreign sync-prefixed PR is rebuilt from `main` plus the copies and refused by `merge-check`; the log key follows the gate's convention and is tested), and B adds unused surface (§5). Applied in: conformance fix PR (plan text only). Status: pending review
- AD-12 [conformance 2/3, 2026-09-29] #4948's sunset test asserts its interim twin-first default is gone from both `.claude/commands/implement-plan-claude.md` and its twin, but this project never edits `.claude/**`, so the `.claude/` copy keeps the interim paragraph until the first sync PR after the final merge. How is the sunset guard reconciled? — Picked: A — check the twin, `CLAUDE.md`, `agents.md`, and `docs/operations/master-session.md` only; `.claude/` is covered by `test_template_parity` (`assert_claude_not_ahead`) and catches up through the sync PR. Alternatives: B — keep the `.claude/` asserts, which fail on the project branch and on `main` until the sync PR merges; C — edit `.claude/` in this PR, which the non-goals and CLAUDE.md §28.C forbid. Why: G8 / AD-7 (text tests load the twin), §5. Applied in: conformance fix PR 2. Status: pending review
- AD-13 [conformance 3/3, 2026-09-29] An open sync PR is rebuilt on every run in which `main` moved at all: a new head, a new CI run of about 45 minutes, and a voided owner approval. With `main` taking 1 to 4 commits an hour, a non-guard sync PR merges only after `main` stays still for a whole CI run. When should an open sync PR be rebuilt? — Picked: A — only when its head's `.claude/` tree differs from `main`'s plus the copies. Alternatives: B — keep rebuilding on every `main` commit (step 3 as first written) and document the delay; C — keep rebuilding, but let the merge accept a head whose checks passed on an older `main`. Why: the PR's diff and what it merges depend only on `.claude/`, so A removes the churn with one condition; C would still restart CI and void owner approvals (§5). Applied in: conformance fix PR 3. Status: pending review
- AD-14 [conformance 3/3 — fix PR 4, 2026-09-29] The fix check on conformance fix PR #5171 was FIX-DEFECTIVE: AD-13's condition never rebuilds a sync head that carries a commit outside `.claude/` (a push onto the sync branch that edits `README.md` or a twin), so `merge-check` refuses it on every run with no alert. How does the project continue? (Q2 on #4785, answered A by the master session under the operator's standing delegation.) — Picked: A — one more fix PR outside the conformance cap: also rebuild when `git diff --name-only $(git merge-base main head) head` lists a path outside `.claude/`, with the reproduction as a test, then one more fix check and the security pass. Alternatives: B — accept it as a known limitation and document it; C — rebuild whenever the whole tree differs (AD-13 option B), which restarts CI on every `main` commit. Why: keeps AD-13's no-churn rule and restores "the PR diff is exactly the copies" (§5). Applied in: conformance fix PR 4. Status: pending review

## Notes

- `.claude/scripts/security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 4785` printed `{"skip": false, "label": null, "reason": "no skip label"}`, so the security pass runs.
- This project touches no path under `.claude/`. Its command-file edits ship in the twins and reach `.claude/commands/**` through the first sync PR after the final merge.

## References

- Issue #4785; `docs/plans/claude-fixer-unattended-convergence-plan.md`
- CLAUDE.md §26.H, §27, §28.C; `.github/workflows/update_workflows.yml` ("Sync .claude/ assets from upstream")
- `scripts/claude_issue_queue_watchdog.sh` (Telegram pattern), `scripts/claude_pr_sweep.py`
