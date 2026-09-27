# Review sandbox: snapshot and transfer the per-PR workspace, not the checkout

Source issue: shubhodeep1/coding-workflows#4580 (https://github.com/shubhodeep1/coding-workflows/issues/4580)
Base branch: ai/issue-4568
Security pass: skip (ai:workflow-heal: automation-produced issue)

## Summary

Every review-editor edit made inside the disposable review sandbox is copied back into
`GITHUB_WORKSPACE`, but the editor step, its change check, and the commit step all work in
the per-PR workspace (`WORKSPACE_PATH`). Edits are therefore silently lost on every run; this
plan points the sandbox's snapshot, refresh, and transfer at the workspace the commit step reads.

## Context

- Issue #4580 is a workflow-failure-heal report: `Internal: AI Review & Autofix` failed 5
  consecutive times on PR #4572 (head `ai/issue-4568`, head SHA `110eb165`, support pin
  `fd38e55`). The automated diagnosis was inconclusive.
- Evidence from the five runs (36245301903, 36248822801, 36252266668, 36255888384,
  36259294550) and their `codex-review-autofix-failure-logs-*` artifacts:
  - every editor attempt that claimed edits hit `Editor claimed changes but git shows no
    substantive diff from HEAD` (`scripts/review_apply_fixes.sh`, the EDITOR_CHANGES_LOST
    gate), four runs ended `finalize_reason=editor_changes_lost` and re-dispatched a fresh
    review hour after hour;
  - the next attempt then reported the same findings as "already applied", because the
    sandbox's `/source` still held the previous attempt's edits while the host tree did not;
  - on the fifth run, attempts 2 and 3 failed with OpenRouter `This request requires more
    credits, or fewer max_tokens` (environmental, and made worse by the re-dispatch loop).
- Root cause (reproduced locally against `ai/issue-4568`):
  - `.github/workflows/review_autofix.yml` step `Activate workspace shell context` (present
    since #3048, June 2026) makes every later step `cd "${WORKSPACE_PATH}"` via `BASH_ENV`
    and exports `GIT_DIR=${GITHUB_WORKSPACE}/.git`, `GIT_WORK_TREE=${WORKSPACE_PATH}`.
    `WORKSPACE_PATH` is a materialized copy under `${RUNNER_TEMP}/workspaces/`
    (`scripts/workspace_init.sh`).
  - #4435 (2026-09-25) moved the writer into `scripts/review_untrusted_sandbox.sh`, which
    snapshots, refreshes, and transfers `workspace="${GITHUB_WORKSPACE:-$PWD}"`. The editor
    therefore reads the checkout and writes its validated edits back into the checkout,
    where no later step looks.
  - A local reproduction (fake `docker`, the base branch's own scripts, `WORKSPACE_PATH` and
    `GIT_DIR`/`GIT_WORK_TREE` set as the workflow sets them) shows the edit landing in
    `GITHUB_WORKSPACE` and `git diff HEAD` in the workspace staying empty.
- The defect is not specific to PR #4572: every GPT-reviewed PR since #4435 loses editor
  edits (for example run 36240513501 on PR #4510 lost attempt 1's edits, then passed as
  `clean_review_no_commit`).
- Review support for this repository's own PRs is staged from protected `main`
  (`review_support_sha`), so this fix changes live review behaviour only once it reaches
  `main` (see AD-1 and Rollout).

## Goals

- `review_untrusted_sandbox.sh prepare` selects the directory the editor step's Git work tree
  points at: `WORKSPACE_PATH` when it is set, and it must resolve to a direct child of
  `${RUNNER_TEMP}/workspaces`; otherwise `GITHUB_WORKSPACE` as before.
- `prepare` records that directory inside the sandbox root, and `run` transfers into the
  recorded directory only. `run` never re-derives it from the environment.
- The snapshot lists tracked and untracked files through the checkout's Git database
  (`GITHUB_WORKSPACE/.git`) with the workspace as the work tree, so a workspace without its
  own `.git` snapshots exactly what the commit step will see.
- A regression test reproduces the lost-edit path, with a separate `WORKSPACE_PATH` and the
  workflow's `GIT_DIR`/`GIT_WORK_TREE`. It fails on the current code and passes with the fix.
- Existing sandbox behaviour is unchanged when no workspace is active: the legacy test
  `test_review_host_python_launches_are_isolated_and_broker_keeps_credential` still passes.

## Non-goals

- Staging `review_floor_rules.sh`, `review_consolidate.sh`, `review_parse_consolidator.sh`,
  and `review_issue_ledger.sh` into the runtime bundle. They are not in
  `REQUIRED_BOOTSTRAP_SCRIPTS`, and the `.codex-workflow-src/scripts/` fallback in
  `resolve_support_script` cannot resolve from the workspace cwd, so those stages have been
  skipped since June 2026. This is a separate latent gap that changes review output when
  fixed, not the cause of these failures (AD-3).
- Loosening the editor's `Review file issue audit:` format check (one attempt wrote `total:`
  instead of `total issues listed:`); that is model output drift the retry loop already
  absorbs (AD-5).
- OpenRouter credit exhaustion. It is an account balance, not code. The report says so.
- Any change to PR #4572's own content.

## Constraints

- §1: security first. The sandbox's trust boundary is unchanged. The transfer target is chosen
  only by the trusted `prepare` step, validated with `realpath` against
  `${RUNNER_TEMP}/workspaces`, and stored in the 0700 sandbox root. No container path or
  model-controlled value selects it.
- §5: minimal change. Only `scripts/review_untrusted_sandbox.sh`,
  `scripts/review_untrusted_workspace.py`, their tests, and the docs that describe the
  transfer target change.
- §6: no identifier is renamed. `review_untrusted_workspace.py` keeps its three sub-commands
  and 4-argument form; `snapshot` gains an optional fifth argument (the host Git dir).
- §9: tabs in shell and Python, as the files already use.
- §15: no GitHub API calls are added.
- §18: no new script. The change folds into the existing `Install project dependencies
  (best-effort)` / `Apply fixes with editor model` steps of `review_autofix.yml`, which
  already call the sandbox.
- §20: a `changelog.d/` fragment (`fixed`), since the fix changes observable review behaviour.
- §27: `.github/workflows/review_autofix.yml` is not edited.

## Approach

`prepare` resolves the host directory once:

1. If `WORKSPACE_PATH` is non-empty, `realpath -e` it and require its parent to equal
   `realpath -e "${RUNNER_TEMP}/workspaces"` (the only root `workspace_init.sh` creates).
   A set but invalid `WORKSPACE_PATH` fails closed (`::error::Review workspace path rejected`).
   The host Git dir is `${GITHUB_WORKSPACE}/.git`, and it must be a directory.
2. Otherwise the host is `${GITHUB_WORKSPACE:-$PWD}`, with no separate Git dir (legacy
   behaviour, unchanged).

It writes the host path to `${root}/workspace`, passes the Git dir to `snapshot`, and
`refresh`/`run`/`transfer` use the recorded path. `run` refuses a root without the record (the
same "Review sandbox not prepared" guard that already covers `image` and `baseline.json`).

In `review_untrusted_workspace.py`, `snapshot` accepts an optional host Git dir. When given, the
two `git ls-files` listings run as `git --git-dir=<dir> --work-tree=<host>`. The environment
stays the stripped `git_env`, so no runner `GIT_*` variable leaks into the synthetic
repository. `refresh` and `transfer` need no Git and are unchanged.

Alternatives rejected (AD-4): mirroring transferred files from `GITHUB_WORKSPACE` into
`WORKSPACE_PATH` afterwards. That adds a second copy path and leaves the snapshot reading a
different tree than the commit step. Trusting `GIT_WORK_TREE` from the environment directly
lets an environment value pick the write target without validation.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for a standalone
issue, and the fix is one indivisible change: a sandbox that records the workspace without the
snapshot listing it, or the reverse, still loses edits.

1. **Phase 1 — transfer editor edits into the per-PR workspace.**
   - Files: `scripts/review_untrusted_sandbox.sh`, `scripts/review_untrusted_workspace.py`,
     `tests/test_model_provider_broker.py`,
     `README.md`, `agents.md`, `changelog.d/4580-review-sandbox-workspace-transfer.md` [new].
   - Done when: the new regression test passes, fails when the sandbox fix is reverted, and
     the existing sandbox, broker, and pipeline-contract tests pass.
   - Rollback: revert the phase PR. The sandbox returns to the checkout target, which loses
     edits again but is otherwise safe.

## Implementation Steps

Phase 1:

1. `scripts/review_untrusted_sandbox.sh`, `prepare`: resolve and validate the host directory
   and Git dir as described in Approach. Write `${root}/workspace`, and pass the Git dir to
   `snapshot`. `refresh` uses the same host.
2. `scripts/review_untrusted_sandbox.sh`, `run`/`cleanup` preconditions: add
   `[ -f "${root}/workspace" ]` to the prepared-root check, read the host from it, and require
   that it still resolves to an existing directory. `transfer` uses it.
3. `scripts/review_untrusted_workspace.py`: accept `snapshot <host> <workspace> <manifest>
   [<host-git-dir>]`, and pass `--git-dir`/`--work-tree` to both `git ls-files` calls when the
   Git dir is given. The Git dir must be an existing directory. Other sub-commands keep
   exactly four arguments.
4. Tests (see Tests).
5. Docs: `README.md` (review isolation bullet) and `agents.md` (the review runtime bullet) say
   validated edits return to the per-PR workspace the commit step reads (`WORKSPACE_PATH`, or
   the checkout when no workspace is active).
6. `changelog.d/4580-review-sandbox-workspace-transfer.md` with `<!-- changelog: fixed -->`.

## Files & Modules

- `scripts/review_untrusted_sandbox.sh`
- `scripts/review_untrusted_workspace.py`
- `tests/test_model_provider_broker.py`
- `README.md`
- `agents.md`
- `changelog.d/4580-review-sandbox-workspace-transfer.md` [new]

## Tests

- New (integration, fake `docker`, as the existing broker test):
  `test_review_sandbox_transfers_into_active_workspace` in `tests/test_model_provider_broker.py`.
  It sets up a checkout at `GITHUB_WORKSPACE`, a materialized copy at
  `${RUNNER_TEMP}/workspaces/<id>`, and `WORKSPACE_PATH`/`GIT_DIR`/`GIT_WORK_TREE` exported
  as the workflow exports them. It runs `prepare`, edits the sandbox source, then runs `run`.
  It asserts that the edit is in `WORKSPACE_PATH`, that `git diff --name-only HEAD` (workflow
  env) lists it, and that `GITHUB_WORKSPACE` is untouched. It also covers an untracked,
  non-ignored workspace file being snapshotted.
- New (unit): a `WORKSPACE_PATH` outside `${RUNNER_TEMP}/workspaces` makes `prepare` fail
  closed, and `run` against a root without the `workspace` record fails.
- New (unit): `review_untrusted_workspace.py snapshot` with the optional Git dir lists files of
  a work tree that has no `.git`, and rejects a Git dir that is not a directory.
- Existing, must pass: `tests/test_model_provider_broker.py`,
  `tests/test_review_autofix_review_pipeline_contract.py -k "review_isolation or sandbox"`,
  `tests/test_detect_editor_changes_lost.py`, `tests/test_editor_capacity_fallback_contract.py`,
  `tests/test_git_auth_hygiene.py`, plus `bash -n` on the changed shell script.

## Risks & Mitigations

- A host-side step writes a baselined workspace file between `prepare` and `transfer`, so the
  transfer is rejected as "host baseline changed" and fails the editor step instead of losing
  edits silently. Mitigation: that is the existing fail-closed contract. The steps between
  them write only runtime files outside the workspace or gitignored files such as
  `pre_assembled_static.txt`.
- A consumer repo's run has no `WORKSPACE_PATH`. Mitigation: the legacy `GITHUB_WORKSPACE`
  target is kept for that case.
- The fix does not heal PR #4572's reviews until it reaches `main`. ACCEPTED — AD-1: the
  issue names `ai/issue-4568` as its target. The report and progress comment state the path
  to `main`.

## Rollout

No flag. The change takes effect for a repository once the review support commit carries it:
here, when it reaches `main` through PR #4572 → `orchestrator/project-3965` → `main`; for
consumers, on the next `@stable` sync. Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-27] Which branch should the fix be built on? — Picked: A — the issue's `Target branch: ai/issue-4568`. Alternatives: B — the default branch `main`, where review support is staged from. Why: `/implement-issue-claude` builds on the branch the issue names, and that branch already carries PR #4572's edits to the same sandbox script, so the fix merges without conflict. The fix reaches live reviews when #4572 and project-3965 merge. Applied in: plan header. Status: pending review
- AD-2 [plan, 2026-09-27] Is the failure a code defect or transient? — Picked: A — a code defect in the review sandbox transfer target, fixed in code; the final run's OpenRouter credit error is reported as environmental. Alternatives: B — report the whole failure as transient and change nothing. Why: the lost-edit path reproduces locally and in all five runs. Applied in: plan. Status: pending review
- AD-3 [plan, 2026-09-27] Should this issue also stage the consolidator/floor/parser/ledger scripts that every run skips? — Picked: A — no; record it as a separate latent gap. Alternatives: B — add them to `REQUIRED_BOOTSTRAP_SCRIPTS` here. Why: §5 minimal change; those stages have been dark since June 2026, and turning them on changes review output well beyond this failure. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-27] How does the sandbox pick its host directory? — Picked: A — `prepare` validates `WORKSPACE_PATH` under `${RUNNER_TEMP}/workspaces`, records it in the sandbox root, and lists files through the checkout's Git database. Alternatives: B — keep the checkout target and mirror transferred files into `WORKSPACE_PATH`; C — trust `GIT_WORK_TREE` from the environment. Why: A keeps the snapshot, the transfer, and the commit step on one tree, and only the trusted prepare step chooses the write target (§1). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-27] Should the editor's `Review file issue audit:` check accept the short `total:` wording one attempt used? — Picked: A — no, out of scope. Alternatives: B — relax the regex here. Why: §5; it is model output drift that the retry absorbs, not the failure's cause. Applied in: no code change. Status: pending review

## References

- Issue #4580; source PR #4572; runs 36245301903, 36248822801, 36252266668, 36255888384, 36259294550.
- #4435 (review sandbox, e8ee6bed); #3048 (workspace shell context).
- `scripts/review_untrusted_sandbox.sh`, `scripts/review_untrusted_workspace.py`,
  `scripts/workspace_init.sh`, `.github/workflows/review_autofix.yml` (`Activate workspace
  shell context`, `Install project dependencies (best-effort)`).
