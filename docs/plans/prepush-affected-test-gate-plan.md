# Pre-push affected-test gate for AI- and Claude-authored changes

## Summary

Add a deterministic pre-push gate that finds the tests and registries a change
affects, runs them in a credential-free container before every AI push (implement,
review autofix, conflict resolver, workflow heal, integration-branch repair) and
before interactive `git push`, gives the editor one repair turn on failures the
change itself caused, and records the result in the PR body. Today nothing runs
tests before an AI push, so half of all red CI runs on AI branches are stale
tests, unwired registries, or broken new tests that a local run would have caught.

## §18 Automation surface (plan output requirements, CLAUDE.md §18.E)

- **Scripts.** Introduces one gate module, `workflow-templates/.claude/hooks/prepush_affected_tests.py`
  [new] (live copy `.claude/hooks/prepush_affected_tests.py` arrives through the
  existing `sync-claude-live-copies.yml`), one shared host-side container wrapper
  `scripts/prepush_test_gate_run.sh` [new], and two step scripts
  (`scripts/implement_step_prepush_test_gate.sh` [new],
  `scripts/review_autofix_step_prepush_test_gate.sh` [new]). Everything else
  extends existing code. No standalone manual scripts (§18.A): every new script
  is executed only by a workflow step, by the poller, or by the interactive
  `PreToolUse` hook flow.
- **Scheduler / trigger entry points.**
  - P1: `.claude/settings.json` `PreToolUse` (`Bash`) for interactive sessions;
    `.github/workflows/ci.yml` steps for the parity tolerance.
  - P2: `.github/workflows/orchestrate.yml` step "Validate decomposition JSON"
    (line 974) and "Create Wave 1 issues only" (line 1225); poller wave 2+
    creation in `scripts/orchestrate_poll_process.sh` (~21951-21977).
  - P3: `.github/workflows/implement.yml` (dispatched by the existing `/approved`
    flow, `internal-implement.yml` / consumer `workflow-templates/ai-implement.yml`).
  - P4: `.github/workflows/review_autofix.yml` (`pull_request` + dispatch via
    `internal-review.yml` / `workflow-templates/ai-review.yml`), and the heal
    route inside `implement.yml`.
  - P5: the poller tick (`.github/workflows/orchestrate_poll.yml` →
    `scripts/orchestrate_poll_process.sh`) dispatching `review_autofix.yml`.
- **Supervisor.** None. Every gate run is a bounded one-shot inside an existing job.
- **DB operations.** None. No MongoDB collections, indexes or contracts are
  touched (§10 not applicable).
- **§18.F registry.** P1 adds an entry for `workflow-templates/.claude/hooks/prepush_affected_tests.py`
  and `scripts/prepush_test_gate_run.sh`; P3 adds `scripts/implement_step_prepush_test_gate.sh`;
  P4 adds `scripts/review_autofix_step_prepush_test_gate.sh`. Type
  `long-running`, removal trigger `permanent — review annually` (same precedent
  as `docs/plans/review-autofix-deterministic-editor-failure-resume-plan.md`).

## Context

### Measurement (2026-10-11)

The 100 most recent failed `ci.yml` runs on `ai/issue-*`, `claude/*` and
`orchestrator/project-*` branches (41 PRs, 2026-10-09T10:00Z to
2026-10-11T02:01Z) were classified from their job logs and from each branch's
diff against `main` as it stood at run time.

| Primary cause | Runs | Notes |
| --- | --- | --- |
| Inherited from red `main` | 40 | Three episodes on 2026-10-09: #6741 merged 26 h after its last green CI and broke `test_security_pass_cap_converts_low_keep_fixing_to_advisory`; #6938 merged with its final CI run cancelled and broke `tests/test_workflow_failure_heal.py::test_review_autofix_passes_support_sha_to_every_marker_site` plus the `review_autofix.yml` size guard. `main` has been green since 18:39Z that day. |
| Stale existing test | 19 | Code changed, an existing test still asserted the old behaviour. 12 of 19 PRs had edited *some* tests and missed one (#6971, for issue #6967, updated ~20 tests and missed `tests/test_unblock_scan.py:562`). |
| Registry / parity not updated | 15 | `docs/INVENTORY.md` rows, `ci.yml` test wiring, workflow allowlists, and a `workflow-templates/.claude/` edit whose live copy the pipeline cannot write (#6674: 6 pushes, can never go green). |
| Test the PR added was broken | 8 | e.g. a pytest fixture argument in the custom-runner `tests/test_orchestrate_poll_process.py`, a test defined after `if __name__ == "__main__"`. |
| Merge skew on integration branches | 8 | A main sync or a sibling merge broke tests that passed on each branch alone (`orchestrator/project-6664`, `-6902`). |
| ShellCheck killed the runner | 7 | All on #6682; its additions to the 1.46 MB `scripts/orchestrate_poll_process.sh` take the runner down in the `ShellCheck static analysis` step. |
| Real bug caught by a test | 2 | |
| Flake | 1 | `BrokenPipeError` in `tests/test_dependency_registry_proxy.py`. |

Of the 52 runs classified as caused by the PR (stale test, registry, broken
new test, merge skew, real bug), a local run of the affected tests before
push would have shown the failure in 48, and a plain grep finder (test files
whose text mentions a changed path or basename) selects the failing test file
in 47.

### Why it happens today

- **No pre-push test gate anywhere.** `implement.yml` goes editor → `Validate
  syntax of changed files` (line 4005, `scripts/validate_changed_files_syntax.sh`:
  `py_compile`, `node --check`, `bash -n`, YAML/JSON parse) → `Commit changes`
  (4631) → `Push branch` (4944). Running tests is only a prompt instruction
  (`prompts/mode-implement.txt:37-47`; the Claude-engine prompt at
  `implement.yml:2507-2514` allows "mark it UNVERIFIED"). The review editor
  path has no validation before push at all; the conflict resolver runs only
  `scripts/check_resolver_diff.sh` (syntax); heal runs only the syntax validator
  (`scripts/heal_isolated_implement.sh:150-196`).
- **Review autofix reacts after CI is red.** `CHECK_RUNS_AUTOFIX_ENABLED`
  (`review_autofix.yml:245-263`) feeds failed check-runs to the editor, which is
  exactly the extra loop this plan removes.
- **The implementer cannot see its own scope.** Orchestrator child bodies are
  the LLM-written `issues[].body` plus an "Orchestrator metadata" footer
  (`orchestrate.yml:1268-1283`, `scripts/orchestrate_poll_process.sh:21952-21963`);
  `files_touched` lives only in the decomposition JSON and state. Bodies tell the
  implementer to "stay inside the files_touched list" it never sees, so it
  leaves tests alone. (With no `files_touched:` block in the body the scope guard
  skips with exit 10, so it does not block test edits for these children; it
  does for judge reissues, unblock-judge edits, heal scopes and human-written
  blocks.)
- **Text-contract tests.** 51 test files read `implement.yml` as text and 53 read
  `scripts/orchestrate_poll_process.sh`. Import-based affected-test tools miss
  these; a path/basename grep finds them.
- **Weakened tests are a known failure mode.** Heal PRs #6907 and #6916
  rewrote a test to match buggy code (flipping the security-pass cap rule that
  #6906 restored). A gate that tells an editor "make the tests pass" must not
  reward that.

### Existing mechanisms this plan extends (§5, "extend, never compete")

- `scripts/workflow_failure_heal.py:2137-2250`: `heal_scope_test_subjects` maps
  `tests/test_<stem>.py` → `scripts/<stem>.{sh,py}` / `.github/workflows/<stem>.yml`,
  and `HEAL_SCOPE_GUARD_TEST_SUBJECTS` maps guard tests to the files their fix
  edits. The finder applies the same conventions in reverse.
- The heal intake's `heal-scope failing-tests` log parser (agents.md "Isolated
  Codex agents", "Heal scope from failing tests") already recognises the
  failure shapes the gate must parse: `TEST_CASE_EVENT ... "status":"fail"`,
  `FAIL  test_x`, pytest `FAILED tests/x.py::test_y`, script tracebacks.
- The implement post-Codex syntax repair (`implement.yml:4017-4537`): capture
  file + marker (`=== CAPTURED SYNTAX DIAGNOSTICS (FULL) ===`) + allow-list +
  stray-edit rollback + re-validate, re-invoking the editor via
  `codex_thread_reuse.sh direct-run`. The test-repair turn copies it.
- Heal's credential-free validator container (`heal_isolated_implement.sh:120-196`:
  `docker run --network none --read-only --cap-drop ALL ...` against the editor
  snapshot, "never run a checkout script on the host after editor transfer").
  Tests execute PR code, so the gate runs the same way.
- `scripts/codex_isolated_exec.sh prepare --deps` (implement.yml:2910, 4048)
  already installs Python/Node dependencies into prep roots (`/opt/codex-venv`,
  `node_modules`) through the allow-listed `scripts/dependency_registry_proxy.py`,
  so tests can run under `--network none`.
- `.claude/hooks/pr_merge_status_guard.py` already parses `git push` out of
  arbitrary Bash (`_guarded_git_invocations`, line ~792); the interactive hook
  reuses it instead of writing a second shell parser.

### Decisions recorded in the clarification rounds

| Q | Decision |
| --- | --- |
| Q3 | After one repair turn, push anyway and list remaining failures in the PR body. Never block the push. |
| Q4 / Q10 / Q18 | Affected tests and companion files (`docs/INVENTORY.md` for new scripts, `.github/workflows/ci.yml` for new test files) are appended to the decomposition JSON `files_touched` (so sibling issues serialize on them) and rendered into the child body as an advisory "Expected files" section the scope guard does not parse. Standalone issues get the same advisory list in the implement prompt. Enforcement is unchanged. |
| Q5 | Gate in implement, review autofix, conflict resolver and workflow heal. |
| Q6 / Q21 | Interactive sessions: the gate module ships under `.claude/hooks/`; Claude runs it before pushing and it writes a pass stamp; a fast `PreToolUse` hook on `git push` asks for confirmation when the stamp is missing or stale, and fails open with a warning. Plus a new CLAUDE.md section. |
| Q7 | Failing tests are re-run on the merge-base; failures also present there are "inherited": reported, never handed to repair. |
| Q8 | The repair turn may change an existing test's expected value only when the issue or plan names that behaviour change; every changed pre-existing assertion is listed in the PR body. |
| Q9 | Affected tests + always-cheap registry/parity guards + ruff and ShellCheck on changed files. |
| Q11 / Q25 | The parity tests tolerate template-ahead drift on PR CI when the PR changed that template or the drift already exists on the PR's base; main pushes and the release gates stay strict. |
| Q12 | 15-minute cap (repo var), UNVERIFIED on timeout. |
| Q13 | This repo and every consumer in `.github/ai/consumer_repos.json`. |
| Q14 | `repair` mode everywhere on merge (repo var kept as a kill switch). |
| Q15 / Q19 | Integration-branch skew: after a clean main sync or a sibling merge, the poller dispatches `review_autofix` on the integration PR (once per head SHA); failures `main` does not share go to the resolver sandbox in a new test-repair prompt mode; the existing push step pushes. |
| Q20 | Consumer test commands resolved deterministically per ecosystem, with an optional `prepush_test_command` override in `.ai/validate.yml`. |
| Q22 | Files with more than 50 tests: select only test functions that mention a changed function or file name, sharded 4 ways; whole file sharded if nothing matches. |
| Q16 / Q23 / Q24 | 5 independently mergeable phases; the gate module, the interactive hook and the parity tolerance ship together in P1; P2-P5 skip their step when the module is absent. |
| Q17 | This plan is committed on the session branch `claude/brave-newton-d72s6d`. |

## Goals

- G1. Every AI push path (implement, implement heal route, review autofix
  editor, conflict resolver, integration test repair) runs the gate before push
  when `PREPUSH_TEST_GATE_MODE` is `report` or `repair`; the PR body carries a
  `## Pre-push test gate` section stating pass / fail / UNVERIFIED, tests run,
  PR-caused failures, inherited failures, and changed pre-existing assertions.
- G2. A PR-caused failure triggers exactly one repair turn per gate run in
  `repair` mode; inherited failures never do.
- G3. The finder selects every test file that mentions a changed path, basename
  or (for stems of 4+ characters) basename stem, plus naming-convention
  matches and the applicable registry guards. Each measured case listed in
  P1 step 9 selects its failing test file, by the rule named there.
- G4. Orchestrator child issues carry an "Expected files" section listing the
  augmented `files_touched`, and siblings that share an added test or companion
  file are serialized by the existing partition guard.
- G5. Interactive `git push` without a fresh stamp for `HEAD` triggers an `ask`
  in this repo and in every consumer after the next `@stable` sync.
- G6. A PR that changes a `workflow-templates/.claude/` file without its live
  copy is green on PR CI, and so is every PR opened while that drift waits on
  the held sync PR; `main` pushes and the release gates still fail on the drift.
- G7. A clean integration sync or sibling merge that leaves the integration
  head with a failure `main` does not share is repaired by the resolver sandbox
  without a new `ai/issue` loop, bounded by the existing resolver caps.

## Non-goals

- Fixing the red-`main` merges themselves (#6741 stale-green merge, #6938
  cancelled-CI merge). The merge-base freshness gate from #6938/#7108 owns that.
- Fixing #6682's ShellCheck runner crash. The gate will surface it, not fix it.
- Changing the scope guard's enforcement (Q18): no parseable `files_touched:`
  block is ever added to orchestrator child bodies by this plan.
- Running the full CI suite before push. Only affected tests and the cheap
  guards run.
- Retrying flaky tests. A failure is a failure; the baseline re-run (Q7) only
  classifies.
- Auto-approving the held `ai/sync-claude-live-copies-held` PR. P1's live copy
  still needs a human merge (see Rollout).

## Constraints

- **§6 naming immutability.** No existing identifier is renamed or removed.
  New identifiers (all verified unused on `fc91d577`): `prepush_affected_tests`,
  `PREPUSH_TEST_GATE` (log prefix), `PREPUSH_TEST_GATE_MODE`,
  `PREPUSH_TEST_GATE_TIMEOUT_MINUTES`, `PREPUSH_TEST_GATE_LARGE_FILE_TESTS`,
  `PREPUSH_TEST_GATE_SHARDS`, `CLAUDE_PREPUSH_TEST_GATE`,
  `CLAUDE_PARITY_PR_BASE_SHA`, `augment-files-touched`, `post_sync_test_repair`,
  `INTEGRATION_TEST_REPAIR_ACTIVE`, `prepush_test_command`,
  `prompts/mode-implement-repair-tests.txt`, `prompts/integration-sync-test-repair.txt`.
  New identifiers must not shadow anything in scope (§6 "New Identifier
  Uniqueness"); implementers re-check with `grep -rIF` before adding any other name.
- **§4 env defaults.** Every new repo var has a default in the workflow
  expression *and* in the script that reads it (unattended §8 note: a helper
  run from `@stable` may see an old workflow).
- **§9 style.** Tabs in Python/shell; 2-space YAML; braces on a new line where
  the language has them.
- **§13 repo hygiene.** The gate never writes into the repository or `.git/`.
  Stamps and baseline trees live under `${TMPDIR:-/tmp}`; containers get a
  disposable copy.
- **§14 consumers.** P1's `.claude/` files, the CLAUDE.md section and P4/P5's
  wrapper input reach all 13 repos in `.github/ai/consumer_repos.json` through
  `update_workflows.yml` on the next `@stable`; pipeline behaviour reaches them
  through the `@stable` reusable workflows and the runtime support checkout
  (`implement.yml:1012-1063`). No consumer-side PR is needed.
- **§15 GitHub API.** The gate itself issues **zero** GitHub API calls (git
  only). P5 adds at most one `gh workflow run` per new integration head SHA,
  deduped in tracking state, reusing `_dispatch_review_for_conflicts`'s
  dispatch loop; no new reads.
- **§19.** Gate summaries contain test names and paths only; the PR-body
  composition still runs `scripts/lint_pr_body_auto_close.py`.
- **§20 / §30.** One `changelog.d/` fragment and, where behaviour is
  documented, one `agents.d/` fragment per phase PR; never edit `CHANGELOG.md`
  or `agents.md` directly.
- **§27.** `review_autofix.yml` is 444,296 bytes on `fc91d577` (35,704 under
  the 480,000 guard, already above `CI_BUDGET_WORKFLOW_WARN_BYTES`=440,000).
  P4 and P5 together may add at most 3,000 bytes to it: new logic goes in
  `scripts/review_autofix_step_*.sh` (registered in `scripts/stage_workflow_support.sh`
  `REQUIRED_BOOTSTRAP_SCRIPTS`, `docs/INVENTORY.md` and the step-script test
  registry per agents.md "Workflow file size limit"). `implement.yml` (351,922)
  gains at most 4,000 bytes.
- **Security.**
  - Tests are PR code. They run only inside a container with `--network none
    --read-only --cap-drop ALL --security-opt no-new-privileges`, no environment
    from the runner, no `.git`, no credential paths (the `codex_isolated_workspace.py`
    snapshot filter), never on the host and never in the interactive hook.
  - The pipelines run the gate module from the trusted support checkout
    (`.codex-workflow-src/workflow-templates/.claude/hooks/prepush_affected_tests.py`),
    never the copy in the PR checkout, so a PR cannot change what gates it.
  - Gate output fed to a repair prompt is wrapped as untrusted
    (`=== CAPTURED TEST FAILURES (UNTRUSTED) ===`), capped at 16 KiB, with
    `::` workflow-command prefixes neutralised as heal does.
  - The interactive hook only reads a stamp; it never executes tests or repo code.

## Approach

### The gate module (one file, stdlib only)

`workflow-templates/.claude/hooks/prepush_affected_tests.py`, Python 3.10+,
no third-party imports (optional `yaml` import for `.ai/validate.yml`, with a
single-key line parser fallback). Subcommands:

| Subcommand | Input | Output |
| --- | --- | --- |
| `select` | `--repo-root`, one of `--changed-files-file` / `--paths-file` (planned paths, may not exist yet) / `--base <ref>` (git diff `<ref>...HEAD` plus untracked) | JSON: `changed_files`, `affected_tests[] {path, reasons[], functions[]?}`, `companion_files[]`, `registry_checks[]`, `static_checks[]` |
| `run` | `select` inputs + `--baseline-tree <dir>` (optional) + `--timeout-minutes` + `--shards` + `--out <json>` + `--summary <md>` | Result JSON `{status: pass|fail|unverified|skipped, ran[], failures[] {test, file, tail}, inherited[], unverified[] {item, reason}, changed_assertions[]}` and a Markdown summary |
| `assertion-diff` | `--baseline-tree`, `--changed-files-file` | Removed or modified assertion lines in test files that exist in the baseline tree (Q8) |
| `stamp` | interactive; no args | Refuses with a message when tracked changes are uncommitted; otherwise runs `select --base <merge-base with origin/<default>>` + `run` in the working tree and writes `${TMPDIR:-/tmp}/claude-prepush-gate/<sha256(toplevel)[:16]>/<HEAD>.json` |
| `hook` | PreToolUse payload on stdin | `permissionDecision: ask` when the Bash command contains a `git push` and no stamp exists for `HEAD`; allow otherwise; allow + `systemMessage` warning on any internal error |

Selection rules, in order (each match records its reason):

1. Changed test files themselves (test file = `tests/test_*.{py,sh}`,
   `**/test_*.py`, `**/*_test.py`, `**/*.test.{js,jsx,ts,tsx,mjs,cjs}`,
   `**/*.spec.{js,jsx,ts,tsx,mjs,cjs}`, `**/__tests__/**`).
2. Naming convention, the reverse of `heal_scope_test_subjects`:
   `scripts/<stem>.{sh,py}` and `.github/workflows/<stem>.yml` (`-` → `_`) →
   `tests/test_<stem>.{py,sh}`; generic `<dir>/<name>.<ext>` → sibling or
   `tests/` files named `test_<name>.*`, `<name>_test.*`, `<name>.test.*`,
   `<name>.spec.*`.
3. Text reference: a test file whose text contains a changed file's repo path,
   its basename, or its basename stem as a whole word when the stem is at least
   4 characters and not in a stop list (`index`, `main`, `utils`, `util`,
   `common`, `config`, `setup`, `__init__`, `conftest`, `test`, `tests`).
   One level of helper indirection: when a non-`test_` module in the same
   test directory (for example `tests/review_autofix_step_scripts.py`, which
   26 test files import for `expanded_review_autofix_text()`, or
   `tests/agents_doc.py`) matches, every test file that imports it
   (`import <module>` / `from <module> import`) is selected.
4. Registry guards, active only when the guard exists in the repo:

   | Guard | Runs when |
   | --- | --- |
   | `tests/test_ci_wires_every_test_file.py` | a `tests/test_*` file is added or renamed, or a workflow changes |
   | `tests/inventory_parity.py` | a file under `scripts/` or `.github/workflows/` is added, renamed or deleted, or `docs/INVENTORY.md` changes |
   | `tests/test_claude_template_live_parity.py` | anything under `.claude/` or `workflow-templates/.claude/` changes |
   | `tests/test_workflow_file_size_limit.py` | a `.github/workflows/*.yml` changes |
   | `make generate-check` | a path is added, renamed or deleted |
   | `python3 scripts/assemble_agents.py check --repo-root .` | `agents.md` or `agents.d/*` changes |

   Plus `HEAL_SCOPE_GUARD_TEST_SUBJECTS`'s inverse: a change to
   `.github/workflows/ci.yml` selects `tests/test_ci_job_split_contract.py`,
   `tests/test_ci_inventory_parity_order_contract.py`,
   `tests/test_ci_shared_shell_block_guard.py` and `tests/test_ci_poll_test_sharding.py`.
5. Static checks on changed files only: `ruff check --select E,F --ignore E501`
   on `*.py`, `shellcheck --severity=error` on `*.sh` (the same flags as
   `ci.yml:318-322` and `:378-383`). A missing tool yields
   `unverified {item, reason: tool_unavailable}`, never a failure.
6. Large files (Q22): for a selected file with more than
   `PREPUSH_TEST_GATE_LARGE_FILE_TESTS` (default 50) test functions (AST for
   Python), keep only the test functions whose source mentions a changed
   function name (from diff hunk headers and added/removed `def`/`name()`
   definitions) or a changed file's basename; none match → whole file.

Companion files (`select --paths-file`, used by P2): a planned path under
`scripts/` that does not exist yet adds `docs/INVENTORY.md` (when
`tests/inventory_parity.py` exists); a planned `tests/test_*` file that does not
exist yet adds `.github/workflows/ci.yml` (when
`tests/test_ci_wires_every_test_file.py` exists). In consumer repos neither
guard exists, so no companions are added.

Test command resolution (Q20), first match wins:

1. `.ai/validate.yml` key `prepush_test_command` (string; `{files}` is replaced
   by the space-separated selected files, shell-quoted; without `{files}` the
   command runs once, whole).
2. A Python test file named in a `.github/workflows/*.yml` line as
   `python3 tests/<file>` (script style) is run the same way; the custom
   runner gets the selected test names as argv
   (`python3 tests/test_orchestrate_poll_process.py name...`, as `ci.yml:1715-1750`
   does). Named as `python3 -m pytest ... <file>` → pytest.
3. Python with pytest configured (`pytest.ini`, `conftest.py`,
   `tests/conftest.py`, `[tool.pytest.ini_options]`; the same detection as
   `codex_isolated_workspace.py:743-746`): `python3 -m pytest -q -p no:cacheprovider <file>[::name]`.
   Otherwise `python3 <file>`.
4. `*.sh` test files: `bash <file>`.
5. Node: when `package.json` `scripts.test` invokes `jest`, `vitest` or
   `mocha`, run that runner with the selected file paths; otherwise `npm test`
   once, whole.
6. A `Makefile` with a `test:` target and no other match: `make test` once, whole.
7. Nothing resolves: `unverified {reason: no_test_command}`.

Every test command runs with `PYTHONDONTWRITEBYTECODE=1` (§13). Selected units
are split into `PREPUSH_TEST_GATE_SHARDS` (default 4) parallel groups; the
module stops starting new units when the time budget is spent and reports the
rest as `unverified {reason: timeout}`. Failure tails are parsed with the
heal intake's shapes.

Baseline (Q7): the caller provides `--baseline-tree`, an export of the
merge-base (`git archive <merge-base> | tar -x`, made on the host by the
caller; the module itself never needs `.git`). Only the failing units are
re-run there, with the same dependency roots mounted; a unit that also fails
on the baseline moves to `inherited`. A unit whose test file does not exist on
the baseline is never inherited.

### The container wrapper

`scripts/prepush_test_gate_run.sh` (host side, used by P3-P5). Steps:
compute changed files with git; export the merge-base tree; snapshot the
worktree with `codex_isolated_workspace.py`'s credential filter into a temp
dir; `docker run --rm --init --network none --read-only --cap-drop ALL
--security-opt no-new-privileges --pids-limit 512 --memory 4g --tmpfs
/tmp:rw,nosuid,nodev,size=512m` with the snapshot, the baseline tree
(read-only), the prep roots from `codex_isolated_exec.sh prepare --deps`
(read-only) and the module from the trusted support checkout (read-only);
`timeout --kill-after=30s <cap>`; copy back only the result JSON and summary.
Arguments name the image (`codex_isolated_exec` image for implement/heal, the
`scripts/review_sandbox/Dockerfile` image for review/resolver). Exit 0 always;
outcomes are in the JSON. Log prefix `PREPUSH_TEST_GATE` with
`phase=` / `outcome=` / `reason=` keys.

### The repair turn (Q3, Q8)

One turn, reusing each pipeline's existing editor invocation. Prompt
`prompts/mode-implement-repair-tests.txt` (implement, heal, review editor) or
`prompts/integration-sync-test-repair.txt` (integration repair) contains:

- the failures, wrapped as untrusted, capped at 16 KiB;
- the rule from Q8 verbatim: change an existing test's expected value only when
  the issue or approved plan names that behaviour change; otherwise fix the code;
  add missing registry entries (INVENTORY rows, `ci.yml` wiring) rather than
  weakening guards; never delete, skip or `xfail` a test;
- the allow-list: the change's files ∪ affected tests ∪ companion files, still
  subject to every existing scope / automation-path guard.

After the turn: the existing syntax validator, then the gate once more (no
second repair). Whatever the result, the push proceeds (Q3), and
`assertion-diff` output lands in the PR body section.

### Mode switch

`PREPUSH_TEST_GATE_MODE` repo var: `off` (skip), `report` (run, write the PR
body section, no repair), `repair` (default, Q14). Unknown values behave as
`repair` and log `PREPUSH_TEST_GATE outcome=mode_invalid`. Interactive
sessions use `CLAUDE_PREPUSH_TEST_GATE=off` (default unset = on) as their
off switch, mirroring `CLAUDE_PR_MERGE_GUARD`.

## Decisions

### D1 — Select tests by path text and naming, not by import graph

- **Chosen:** path / basename / stem text matches, one level of test-helper
  indirection, the reverse of the heal naming convention, and a fixed table of
  registry guards.
- **Alternatives considered:** import-graph tools (pytest-testmon, jest
  `--findRelatedTests`); running the whole suite.
- **Why:** most tests here read workflows and scripts as text, which import
  graphs never see; the text rule found the failing file in 47 of 52 measured
  PR-caused failures. The whole suite takes ~35 minutes for the poller file
  alone.

### D2 — Run tests only in a credential-free container, with the module from the trusted support checkout

- **Chosen:** the heal `validate_snapshot` container shape
  (`--network none --read-only --cap-drop ALL`), a credential-filtered
  snapshot, prep-root dependencies, and the module mounted from
  `.codex-workflow-src`.
- **Alternatives considered:** running tests on the host runner; running the
  module from the PR checkout.
- **Why:** tests execute PR code, and the host holds `GH_PAT`; a PR must not be
  able to change the code that gates it.

### D3 — Push anyway after one repair turn

- **Chosen:** one repair turn on PR-caused failures, then push with a PR-body
  section listing what still fails (Q3), inherited failures excluded (Q7).
- **Alternatives considered:** blocking the push and labelling the issue;
  unlimited repair turns.
- **Why:** the repair runs while the editor's context is warm, which is
  cheaper than a review-autofix loop; blocking would strand work on
  environment-only failures.

### D4 — One gate module under `workflow-templates/.claude/hooks/`

- **Chosen:** a single stdlib-only file used by the interactive hook and by
  every pipeline, shipped with the parity tolerance in one phase (Q24, Q23).
- **Alternatives considered:** a canonical copy in `scripts/` plus a template
  copy kept equal by a test; interactive gate for this repo only.
- **Why:** consumers receive `.claude/` but not `scripts/`; one copy cannot
  drift; pipeline editors cannot write `.claude/**`, so the template-only
  change must carry its own parity tolerance.

### D5 — Advisory "Expected files", unchanged enforcement

- **Chosen:** augment the decomposition `files_touched` (for sibling
  serialization) and render an advisory body section the scope guard does not
  parse (Q18).
- **Alternatives considered:** a parseable `files_touched:` block; JSON only.
- **Why:** a parseable block would newly scope-block every orchestrator child;
  JSON alone leaves the implementer blind to the tests it must update.

### D6 — Integration skew repaired by a dedicated review_autofix dispatch

- **Chosen:** after a clean sync or sibling merge, one dispatch per head SHA
  with `post_sync_test_repair=true`, gate + resolver sandbox in a test-repair
  mode (Q19).
- **Alternatives considered:** a fix-up sub-issue per failure; main syncs only.
- **Why:** clean syncs are server-side merges with no checkout and the
  resolver only starts on conflict markers; a fix-up issue costs a whole
  implement loop for what is usually a one-line test update.

## Phases & Merge Strategy

Five phases, one PR each. P2-P5 locate the gate module and
`scripts/prepush_test_gate_run.sh` in the trusted support checkout and skip
with `PREPUSH_TEST_GATE phase=<p> outcome=skipped reason=module_absent` when
either is not there, so any merge order is safe. Each
phase is production-safe on merge and reverts cleanly on its own.

| Phase | Scope | Done when | Rollback |
| --- | --- | --- | --- |
| P1 | Gate module, interactive hook + settings entry, parity tolerance for PR CI, CLAUDE.md §31 | Module unit tests and the measured cases pass; a PR that adds the template files is green on PR CI; `main` CI is strict | Revert the PR; close the held sync PR if still open, otherwise revert it too |
| P2 | `orchestrate_lib.py augment-files-touched`, wiring in `orchestrate.yml`, "Expected files" section at both child-creation sites | A decomposition replay shows added tests/companions in JSON and body, partition guard serializes sharing siblings, scope guard still exits 10 on the rendered body | Revert; module absence path already no-ops |
| P3 | Implement gate step + test-repair turn + PR body section + standalone advisory list + tools in the isolated image | A fixture implement run with a stale test gets one repair turn and a PR body section; mode `off` is a no-op | Set `PREPUSH_TEST_GATE_MODE=off` immediately; revert the PR |
| P4 | Gates in review autofix editor path, conflict resolver and heal route | Fixture runs for each path show gate + one repair + push; `review_autofix.yml` grows ≤ 2,000 bytes | `PREPUSH_TEST_GATE_MODE=off`; revert |
| P5 | Integration-branch post-sync test repair (poller dispatch, review_autofix input, resolver test-repair mode) | A fixture integration branch with a sync-induced failure is repaired once and pushed; second dispatch for the same head SHA is deduped | `PREPUSH_TEST_GATE_MODE=off` stops dispatch and repair; revert |

## Implementation Steps

### P1 — Gate module, interactive hook, parity tolerance

1. Add `workflow-templates/.claude/hooks/prepush_affected_tests.py` [new] with
   the five subcommands, selection rules, command resolution, sharded runner,
   baseline classification, `assertion-diff`, stamp writer and hook described
   in Approach. The `hook` subcommand loads `pr_merge_status_guard.py` from its
   own directory with `importlib.util.spec_from_file_location` and calls
   `_guarded_git_invocations`; if loading fails it falls back to a conservative
   check (any token sequence `git ... push` in the command) and warns.
2. Add `scripts/prepush_test_gate_run.sh` [new], the host-side container
   wrapper (it ships with P1 so P3-P5 need only call it; it is unused until
   then).
3. Register the hook in `workflow-templates/.claude/settings.json` as a **new,
   separate** `PreToolUse` entry with matcher `Bash` and command
   `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/prepush_affected_tests.py hook`,
   timeout 30. Do not append to the existing `Bash` entries:
   `tests/test_gh_api_write_guard.py:1047-1053` asserts its entry has exactly
   one hook.
4. Add `tests/claude_template_drift.py` [new] (helper, not a test file), with
   `pending_template_paths(repo_root) -> set[str]`: empty unless
   `CLAUDE_PARITY_PR_BASE_SHA` is a 40-hex SHA; otherwise the template paths
   under `workflow-templates/.claude/` that (a) differ from their live copy at
   `HEAD`, and (b) were changed by `git diff --name-only <base>...HEAD` or
   already differ from their live copy at `<base>` (Q25). Any git error returns
   the empty set (strict).
5. Make the parity assertions use it: `tests/test_claude_template_live_parity.py`
   skips pending paths (printing them as `pending sync`), and the settings
   byte-equality assertions at `tests/test_pr_merge_status_guard.py:2309`,
   `tests/test_gh_api_write_guard.py:1082` and `tests/test_pr_watch_guard.py:246`
   skip only when `settings.json` is pending. The guard-file equality
   assertions (`:2308`, `:1081`, `:245`) follow the same rule for their own file.
6. `.github/workflows/ci.yml`: add
   `CLAUDE_PARITY_PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}` to the
   `env:` of the steps that run those four test files (parity step ~486-489,
   merge guard ~482-484, watch guard ~491-494, gh api guard ~526-529). Push
   events leave it empty, so `main` stays strict. Do **not** add it to
   `mark-stable.yml:506` or `test-and-mark-stable.yml:4972` (release gates stay strict).
7. Add `## §31. Pre-Push Affected Tests (MANDATORY)` to `CLAUDE.md` **and**
   `workflow-templates/CLAUDE.md` (byte-identical; `tests/test_pr_merge_status_guard.py:2312`):
   before any `git push` in an interactive session, commit, then run
   `python3 .claude/hooks/prepush_affected_tests.py stamp`; fix PR-caused
   failures; do not weaken assertions unless the task names the behaviour
   change; inherited failures are reported, not fixed; the hook asks when the
   stamp is missing; `CLAUDE_PREPUSH_TEST_GATE=off` disables it.
8. Tests [new] `tests/test_prepush_affected_tests.py` (pytest): selection rules
   1-6 on a scratch repo; command resolution 1-7; sharding and time budget;
   baseline classification; `assertion-diff`; stamp path is under `TMPDIR` and
   never under the repo or `.git`; `stamp` refuses on uncommitted tracked
   changes; hook decisions (push + no stamp → ask; push + stamp → allow;
   non-push → no decision; malformed payload → allow with warning;
   `CLAUDE_PREPUSH_TEST_GATE=off` → allow); settings wiring in the template;
   no network or GitHub calls (patch `subprocess` and assert no `gh`/`curl`).
9. Measured cases, as test cases in `tests/test_prepush_affected_tests.py`
   run against the real repository tree (`select --changed-files-file` with
   the listed changed path; assert the failing test file is selected with the
   named reason). Each is a real miss from the measurement:

   | PR | Changed path | Failing test file that must be selected | Rule |
   | --- | --- | --- | --- |
   | #6971 | `.github/workflows/review_autofix.yml` | `tests/test_unblock_scan.py` | 3, via `tests/review_autofix_step_scripts.py` |
   | #6655 | `.github/workflows/clarify.yml` | `tests/test_workflow_failure_heal.py` | 3, path text |
   | #6654 | `scripts/gh_api_retry.py` | `tests/test_gh_api_retry.py` | 2, naming |
   | #6674 | `workflow-templates/.claude/hooks/gh_api_write_guard.py` | `tests/test_claude_template_live_parity.py` | 4, registry |
   | #7012 | `.github/workflows/ci.yml` (dropped the wiring of `tests/test_validate_driver_credential_scrub.py`) | `tests/test_ci_wires_every_test_file.py` | 4, registry |
   | #6951 | `scripts/claude_pool_health_alert.sh` (added) | `tests/inventory_parity.py` | 4, registry |
   | #7012 | `.github/workflows/ci.yml` (added the budget-watch job) | `tests/test_ci_job_split_contract.py` | 4, guard inverse |

   Every listed path exists on `fc91d577`. If one is renamed or removed before
   P1 is implemented, that case uses a scratch repository with a stub file of
   the same name and a test that references it, so the rule is still
   exercised. The full measurement (52 PR-caused runs; the grep rule selected the
   failing file in 47) stays in this plan's Context; it is not committed as a
   fixture because its 1,136 changed paths need git history CI does not have.
10. Tests: extend `tests/test_claude_template_live_parity.py` with the
    tolerance cases (PR-own drift, inherited drift, no env → strict, bad SHA →
    strict).
11. Wire the new test files in `.github/workflows/ci.yml` (one step, pytest
    style) so `tests/test_ci_wires_every_test_file.py` passes.
12. Registries: `docs/INVENTORY.md` rows for the module and the wrapper;
    `docs/scripts-pending-removal.md` entries (§18.F);
    `changelog.d/<pr>-prepush-affected-test-gate-module.md`;
    `agents.d/<pr>-prepush-affected-test-gate.md` with a new section
    `Pre-push affected-test gate` (` new`) and the `PREPUSH_TEST_GATE` log
    prefix block for `Stable log prefixes (contractual)`.

### P2 — Orchestrator scoping

1. `scripts/orchestrate_lib.py`: add `cmd_augment_files_touched` and the
   `augment-files-touched --decomposition <file> --repo-root <dir> --gate-module <path> --write-back`
   subcommand. For each issue it runs the module's `select --paths-file` on the
   issue's `files_touched`, appends `affected_tests[].path` and
   `companion_files` not already present, stops at the existing 50-entry cap
   (`validate_decomposition`, lines 128-168) and logs
   `PREPUSH_TEST_GATE phase=augment issue=<id> added=<n> truncated=<n>`.
   Module missing or failing: no-op, `outcome=skipped`.
2. `.github/workflows/orchestrate.yml` "Validate decomposition JSON" (line 974):
   run the subcommand after `orchestrate_lib.py validate` (~984) and before
   `check-partition` (~1023), so the partition guard, `compute-waves
   --write-back` and `pending_issue_defs` all see the added paths. Gate on
   `${{ vars.PREPUSH_TEST_GATE_MODE || 'repair' }}` != `off`.
3. Render the advisory section at both creation sites,
   `orchestrate.yml` "Create Wave 1 issues only" (`FULL_BODY`, ~1272-1283) and
   `scripts/orchestrate_poll_process.sh` wave 2+ (`FULL_BODY`, ~21955-21963),
   before the metadata footer:

   ```
   **Expected files** (advisory, from the decomposition; includes the tests and
   registries this change affects. Update them in the same change.)
   - `path/one`
   ```

   The heading must not contain `files_touched` and entries are backticked, so
   `scripts/files_touched_scope_guard.py` `extract_files_touched` (111-141)
   still finds no block.
4. Tests: extend `tests/test_orchestrate_lib.py` for augmentation, cap, idempotence, and the module-absent no-op;
   a test that renders a body and asserts `extract_files_touched` returns no
   allowlist; a partition test where two siblings gain the same test file and
   `auto_serialize_file_overlaps` adds a dependency edge.
5. `changelog.d` fragment; `agents.d` fragment appending to the existing
   `Workflow architecture` section. Only P1 creates the new
   `Pre-push affected-test gate` section, so P2-P5 fragments never depend on
   P1 having merged.

### P3 — Implement gate and repair turn

1. `scripts/codex_isolated_exec.sh` Dockerfile (150-164): add `shellcheck`
   (apt) and a pinned `ruff` (pip, same major as CI) so static checks run in
   the container. Update the image-input list in
   `.github/workflows/publish-sandbox-images.yml` only if the Dockerfile text
   is not already an input.
2. `scripts/implement_step_prepush_test_gate.sh` [new]: reads the mode; locates
   the module in `.codex-workflow-src` (absent → skip); calls
   `prepush_test_gate_run.sh` with the isolated-exec image and prep roots; on
   PR-caused failures in `repair` mode builds the repair prompt from
   `prompts/mode-implement-repair-tests.txt` exactly like the syntax repair
   (`implement.yml:4403-4435`: template, untrusted failure block, changed files,
   allow-list, current diff), invokes the editor once with
   `codex_thread_reuse.sh direct-run` and the same isolation env as
   `implement.yml:4036-4054` / `4449-4465`, rolls back stray edits outside the
   allow-list (as `4466-4520`), re-runs `validate_changed_files_syntax.sh` and
   the gate once; writes `${RUNTIME_DIR}/prepush_test_gate_summary.md`. Always
   exits 0 except on an internal bootstrap error, which is logged and also
   exits 0 (fail open).
3. `.github/workflows/implement.yml`: new step "Pre-push affected-test gate"
   between "Enforce syntax validation outcome" (4622) and "Commit changes"
   (4631), `if: env.SKIP_IMPLEMENT != 'true' && env.HEAL_ROUTE != 'true'`, env
   `PREPUSH_TEST_GATE_MODE: ${{ vars.PREPUSH_TEST_GATE_MODE || 'repair' }}`,
   `PREPUSH_TEST_GATE_TIMEOUT_MINUTES: ${{ vars.PREPUSH_TEST_GATE_TIMEOUT_MINUTES || '15' }}`,
   `PREPUSH_TEST_GATE_LARGE_FILE_TESTS: ${{ vars.PREPUSH_TEST_GATE_LARGE_FILE_TESTS || '50' }}`,
   `PREPUSH_TEST_GATE_SHARDS: ${{ vars.PREPUSH_TEST_GATE_SHARDS || '4' }}`,
   `continue-on-error: true`, body sourcing the step script with the
   staged-support → `.codex-workflow-src` fallback the other step scripts use.
4. "Create Pull Request" (5240): append the summary file, when present, as
   `## Pre-push test gate` before the §19 pre-flight lint runs.
5. "Build implementation context" (2171): for standalone issues (no
   orchestrator metadata footer), extract repo-relative paths named in the
   approved plan that exist in the tree, run `select --paths-file`, and append
   an "Expected files (advisory)" section to the implementation context.
6. Prompts: `prompts/mode-implement.txt` "Test verification" gains "Update the
   tests and registries listed under Expected files together with the code; a
   pre-push gate re-runs them"; the inline Claude-engine prompt
   (`implement.yml:2507-2514`) gets the same sentence. Add
   `prompts/mode-implement-repair-tests.txt` [new]. Check
   `tests/prompt_size_budget.py` and the prompt validation steps still pass.
7. Stage the new script in `implement.yml`'s "Stage workflow support files"
   copy list (~1098) and `scripts/stage_workflow_support.sh`.
8. Tests [new] `tests/test_implement_prepush_test_gate.py`: the step script
   with a fake `docker` and fake editor: pass → no repair; PR-caused failure →
   exactly one editor call with the untrusted block and Q8 rule in the prompt;
   inherited-only → no repair; `off` → nothing runs; module absent → skip log;
   summary written; workflow step order and `if:` pinned by parsing
   `implement.yml`. Wire in `ci.yml`.
9. README: rows for the four repo vars in the repo-variable table next to
   `MAX_POST_CODEX_REPAIR_ATTEMPTS` (~line 274) and a step `10b` after the
   "Post-Codex syntax repair (in-job)" item (~2792). `docs/INVENTORY.md`,
   `docs/scripts-pending-removal.md`, `changelog.d`, and an `agents.d`
   fragment appending to `Workflow architecture` (as in P2).

### P4 — Review autofix, conflict resolver, heal

1. `scripts/review_sandbox/Dockerfile`: add `shellcheck` and pinned `ruff`.
2. `scripts/review_autofix_step_prepush_test_gate.sh` [new]: same contract as
   the implement step script, using `review_untrusted_sandbox.sh`'s image and
   dependency venv; the repair turn re-runs the editor through
   `scripts/review_apply_fixes.sh`'s sandbox invocation with the repair prompt
   as its findings input.
3. `.github/workflows/review_autofix.yml`: one new step "Pre-push affected-test
   gate" between "Apply fixes with editor model" (4748) and "Commit changes"
   (4838), with the same `if:` as "Commit changes" plus the mode check; body
   sources the step script with the staged-support fallback. Register it in
   `REQUIRED_BOOTSTRAP_SCRIPTS` (`scripts/stage_workflow_support.sh`), the
   step-script test registry and `docs/INVENTORY.md`. Measure `wc -c`: growth
   ≤ 2,000 bytes.
4. Conflict resolver, `scripts/review_conflict_resolve.sh`: after
   `check_resolver_diff.sh` passes and before the
   `[ai-merge-resolve] resolve merge conflicts` commit, run the gate; on
   PR-caused failures run one extra `_resolver_sandbox_attempt` with
   `prompts/mode-implement-repair-tests.txt` appended; extend
   `scripts/check_resolver_diff.sh` with an optional `--extra-allowed-file`
   (affected tests ∪ companions) so a test fix outside the conflicted set
   passes its subset check. Commit regardless (Q3).
5. Heal, `scripts/heal_isolated_implement.sh`: after the `validate_snapshot`
   loop succeeds (196) and before `transfer`, run the gate against
   `${root}/source` with the same image and limits as `validate_snapshot`;
   PR-caused failures → one `run_editor` turn with the repair prompt, then
   `validate_snapshot` once more; transfer as today (heal scope already
   includes `tests/**`).
6. Tests [new] `tests/test_review_prepush_test_gate.py` (step script, fake
   docker/editor), extend `tests/test_review_conflict_resolve_sandbox_only.py`
   (gate call order, extra allow-list), extend
   `tests/test_heal_isolated_editor.py` and
   `tests/test_heal_isolated_claude_editor.py` for the extra turn. Wire any new
   file in `ci.yml`.
7. Registries, `changelog.d`, and an `agents.d` fragment appending to
   `Workflow architecture` (as in P2).

### P5 — Integration-branch post-sync test repair

1. Inputs: add `post_sync_test_repair` (boolean, default `false`) to
   `review_autofix.yml`'s `workflow_dispatch` and `workflow_call` inputs, and
   pass-through in `internal-review.yml` and `workflow-templates/ai-review.yml`.
2. Poller, `scripts/orchestrate_poll_process.sh`: new
   `_dispatch_integration_test_repair <tracking_issue> <integration_branch> <final_pr>`
   called after `mark_integration_sync_clean` (11535; callers ~11657 and the
   merge-tree clean exit in `heal_integration_branch_conflict` ~11191-11205)
   and after a successful sibling squash-merge in the ready-to-merge loop
   (~22505/22508). It reads the integration head SHA, skips when tracking state
   field `integration_test_repair_sha` already equals it, skips when the mode
   is `off`, skips (log `reason=wrapper_input_absent`) when the default-branch
   copy of the wrapper it would dispatch does not declare
   `post_sync_test_repair`, and otherwise runs the same three-candidate
   `gh workflow run` loop as `_dispatch_review_for_conflicts` (19866) with
   `-f post_sync_test_repair=true`; records the SHA on success. One API call
   per new head SHA (§15).
3. `scripts/review_autofix_step_prepush_test_gate.sh` gains an integration
   mode, used by a new step "Integration post-sync test repair" placed right
   after "Pre-review deterministic merge-topology gate" (4430): runs only when
   the input is `true` and the head ref matches `orchestrator/project-*`; sets
   `INTEGRATION_TEST_REPAIR_ACTIVE=true`; runs the gate on the PR head against
   the merge-base with the base branch; on PR-caused failures invokes
   `scripts/review_conflict_resolve.sh --test-repair <failures-file>` [new
   flag], which reuses the resolver sandbox with
   `prompts/integration-sync-test-repair.txt` [new] and the existing
   integration placeholders (`{{INTEGRATION_BRANCH}}`, `{{INTENT_FINGERPRINTS_JSON}}`,
   `{{MERGED_SUB_ISSUES_LIST}}`, tracking issue fields), runs
   `check_resolver_diff.sh --extra-allowed-file` and the fingerprint hard
   gate, commits `[ai-merge-resolve] repair post-sync test failures`, and
   counts against `INTEGRATION_CONFLICT_LIFETIME_MAX`.
4. `review_autofix.yml`: add `&& env.INTEGRATION_TEST_REPAIR_ACTIVE != 'true'`
   to every `if:` that contains `env.AUTOFIX_PRE_REVIEW_RESOLVE != 'true'`
   (16 `if:` lines on `fc91d577`), so the reviewer and editor steps are skipped
   for this dispatch; extend "Push all pending commits" (6076) to also run when
   `INTEGRATION_TEST_REPAIRED=true` is exported by the resolver. Growth ≤
   1,000 bytes.
5. Tests: extend `tests/test_orchestrate_poll_process.py` (custom runner, no
   pytest fixtures, as `test_custom_runner_tests_need_no_pytest_arguments`
   (`tests/test_orchestrate_poll_process.py:24094`) enforces) for dispatch, SHA dedupe, mode off, wrapper-input-absent skip;
   extend `tests/test_review_autofix_review_pipeline_contract.py` for the
   added `if:` clause count and push condition; extend the resolver tests for
   `--test-repair`. Wire any new file in `ci.yml`.
6. Registries, `changelog.d`, and an `agents.d` fragment appending to
   `Workflow architecture` (new log keys under
   `PREPUSH_TEST_GATE phase=integration`).

## Files & Modules

- `workflow-templates/.claude/hooks/prepush_affected_tests.py` [new] (P1)
- `.claude/hooks/prepush_affected_tests.py` [new, via `sync-claude-live-copies.yml`, not edited by the phase]
- `workflow-templates/.claude/settings.json` (P1); `.claude/settings.json` [via sync]
- `scripts/prepush_test_gate_run.sh` [new] (P1)
- `tests/claude_template_drift.py` [new] (P1)
- `tests/test_prepush_affected_tests.py` [new] (P1)
- `tests/test_claude_template_live_parity.py`, `tests/test_pr_merge_status_guard.py`, `tests/test_gh_api_write_guard.py`, `tests/test_pr_watch_guard.py` (P1)
- `CLAUDE.md`, `workflow-templates/CLAUDE.md` (P1)
- `.github/workflows/ci.yml` (P1, plus wiring in P3-P5)
- `scripts/orchestrate_lib.py`, `.github/workflows/orchestrate.yml`, `scripts/orchestrate_poll_process.sh` (P2; poller again in P5)
- `tests/test_orchestrate_lib.py` (P2)
- `scripts/codex_isolated_exec.sh` (P3), `scripts/review_sandbox/Dockerfile` (P4)
- `scripts/implement_step_prepush_test_gate.sh` [new], `.github/workflows/implement.yml`, `prompts/mode-implement.txt`, `prompts/mode-implement-repair-tests.txt` [new], `tests/test_implement_prepush_test_gate.py` [new] (P3)
- `scripts/review_autofix_step_prepush_test_gate.sh` [new], `.github/workflows/review_autofix.yml`, `scripts/stage_workflow_support.sh`, `scripts/review_conflict_resolve.sh`, `scripts/check_resolver_diff.sh`, `scripts/heal_isolated_implement.sh`, `tests/test_review_prepush_test_gate.py` [new] (P4)
- `.github/workflows/internal-review.yml`, `workflow-templates/ai-review.yml`, `prompts/integration-sync-test-repair.txt` [new], `tests/test_orchestrate_poll_process.py`, `tests/test_review_autofix_review_pipeline_contract.py` (P5)
- `README.md`, `docs/INVENTORY.md`, `docs/scripts-pending-removal.md`, one `changelog.d/*.md` and one `agents.d/*.md` per phase

## Tests

- **Unit (P1).** Selection, command resolution, sharding, budget, baseline,
  `assertion-diff`, stamp location, hook decisions, settings wiring, parity
  tolerance. No network, no GitHub calls (asserted).
- **Measured cases (P1).** The seven real misses in P1 step 9 are selected
  by the named rules (G3).
- **Contract (P2-P5).** Step order and `if:` conditions pinned by parsing the
  workflow YAML; `review_autofix.yml` byte growth asserted against the
  `fc91d577` baseline + 3,000; scope guard still finds no block in rendered
  bodies; poller dedupe by SHA.
- **Integration (P3-P5).** Each step script runs end to end with a fake
  `docker` (executes the container command locally in a temp dir) and a fake
  editor, covering pass, PR-caused fail → one repair, inherited-only, timeout →
  UNVERIFIED, mode `off`, module absent.
- **Existing suites.** `tests/test_ci_wires_every_test_file.py`,
  `tests/inventory_parity.py`, `tests/test_workflow_file_size_limit.py`,
  `make generate-check`, `scripts/assemble_agents.py check`,
  `tests/prompt_size_budget.py` pass on every phase PR.
- **Success criterion after rollout.** Over the first 100 red `ci.yml` runs
  on AI branches after P3 and P4 merge, runs whose primary cause is a stale
  test, a missed registry or a broken new test fall from 42 (measured here) to
  below 10. Every gate run emits `PREPUSH_TEST_GATE` lines and a PR-body
  section, so the classification can be repeated from logs alone.

## Risks & Mitigations

- **Gate time on hot files.** Changes to `scripts/orchestrate_poll_process.sh`
  select the 307-test poller file. Mitigation: Q22 function-level selection +
  4 shards; worst case UNVERIFIED after 15 minutes, never a blocked push.
- **Over-selection by the text-reference rule.** Short or common basenames
  match many files. Mitigation: 4-character minimum, stop list, whole-word
  match; the measured cases pin recall; precision is logged
  (`selected=<n>`) for tuning.
- **Test weakening by the repair turn.** Mitigation: Q8 prompt rule, one turn
  only, `assertion-diff` listing in the PR body for reviewers, and the
  existing review pipeline still runs.
- **Running PR code.** Mitigation: container-only execution with no network,
  no credentials, no `.git`, read-only mounts, resource limits; module taken
  from the trusted support checkout.
- **Serialization from companion files.** `docs/INVENTORY.md` and `ci.yml` on
  many siblings serialize waves. ACCEPTED — Q10 A.
- **Held live-copy sync.** P1's live `.claude/` copy needs a human to merge the
  held sync PR; until then PR CI tolerates the drift (Q25) and the release
  gates block `@stable`. ACCEPTED — that review is the security checkpoint
  for hook changes.
- **`repair` on consumers immediately (Q14).** A consumer whose tests need
  services or network will show UNVERIFIED or inherited failures. Mitigation:
  push is never blocked; `PREPUSH_TEST_GATE_MODE=off` per repo.
- **Old consumer wrappers in P5.** Mitigation: the poller checks the wrapper
  declares `post_sync_test_repair` before dispatching.
- **`review_autofix.yml` size.** Mitigation: ≤ 3,000 bytes total across P4-P5,
  asserted in tests; logic in step scripts.
- **Interactive hook friction.** A push without a stamp asks for
  confirmation. Mitigation: CLAUDE.md §31 tells Claude to stamp first;
  `CLAUDE_PREPUSH_TEST_GATE=off` exists.

## Rollout

1. P1 merges → `sync-claude-live-copies.yml` opens the held sync PR for the
   live `.claude/` copies; a human reviews and merges it. Until then PR CI is
   green (tolerance) and the release gates block `@stable`.
2. P2-P5 merge in any order; each runs in `repair` mode on merge (Q14).
3. The next `@stable` release (`mark-stable.yml` / `test-and-mark-stable.yml`)
   carries the pipeline phases to the 13 consumers through the reusable
   workflows and support checkout, and `update_workflows.yml` copies the
   `.claude/` files, the CLAUDE.md section and the P5 wrapper input.
4. Kill switches: `PREPUSH_TEST_GATE_MODE=off` (per repo, pipelines),
   `CLAUDE_PREPUSH_TEST_GATE=off` (interactive). Rollback of any phase is a
   revert of its PR.

## References

- CI runs analysed: 100 failed `ci.yml` runs, 2026-10-09T10:00Z to 2026-10-11T02:01Z,
  41 PRs including #6674, #6682, #6741, #6906, #6935, #6938, #6940, #6947,
  #6971, #6999, #7012, #7026, #7058, #7065, #7096.
- Red-`main` causes: #6741 (stale green), #6938 (cancelled CI); `main` was repaired by #6941 and #6969.
- Test weakening by heal: #6907, #6916 vs #6906.
- `docs/plans/review-autofix-deterministic-editor-failure-resume-plan.md` (§18.F precedent).
- agents.md: "Isolated Codex agents", "Workflow file size limit",
  "Implement scope-lock label", "Test-suite git environment isolation",
  "Integration-sync verifier + bootstrap contract", "Live .claude/ copies".
