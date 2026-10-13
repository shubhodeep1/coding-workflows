<!-- agents: section="Pre-push affected-test gate" new -->
Issue #7137, plan phase P1 of `docs/plans/prepush-affected-test-gate-plan.md`.
P2 (orchestrator `files_touched` augmentation), P3 (implement gate), P4
(review autofix, conflict resolver and heal gates) and P5 (integration-branch
post-sync repair) are follow-ups; until they land no pipeline runs the gate.

- **Module.** `workflow-templates/.claude/hooks/prepush_affected_tests.py`
  (live copy `.claude/hooks/prepush_affected_tests.py` via
  `sync-claude-live-copies.yml`), stdlib only. Subcommands `select`, `run`,
  `assertion-diff`, `stamp` and `hook`. Selection, in order: changed test files
  (`changed_test`); the reverse of the heal naming convention (`naming`); test
  files whose text contains a changed path, basename, or basename stem of 4+
  characters outside a stop list as a whole word (`path_text`,
  `basename_text`, `stem_text`), plus importers of a non-`test_` helper module
  in the same test directory that matches (`helper:<module>`); registry guards
  that exist in the repo (`registry:ci_test_wiring`, `registry:inventory_parity`,
  `registry:claude_live_parity`, `registry:workflow_file_size`, and the
  `make generate-check` / `scripts/assemble_agents.py check` commands); the
  inverse of `HEAL_SCOPE_GUARD_TEST_SUBJECTS` for `ci.yml` (`guard_inverse`,
  copied in `CI_GUARD_TESTS` and pinned by a test); `ruff` / ShellCheck on
  changed files (missing tool → `unverified tool_unavailable`). A Python test
  file run under pytest (configured, or `test_` functions with no
  `__main__` runner) is `unverified pytest_unavailable` when pytest is not
  installed. A baseline re-run gets at
  least 90 seconds even after the main budget is spent. A Python test
  file with more than `PREPUSH_TEST_GATE_LARGE_FILE_TESTS` (50) tests keeps only
  the test functions that mention a changed function or file name. Commands:
  `.ai/validate.yml` `prepush_test_command` (`{files}` = quoted selected
  files), then a workflow's `python3 tests/<f>` / `pytest` usage, pytest
  detection, `bash`, the `package.json` jest/vitest/mocha runner or
  `npm test`, `make test`, else `unverified no_test_command`. Units run in
  `PREPUSH_TEST_GATE_SHARDS` (4) threads within
  `PREPUSH_TEST_GATE_TIMEOUT_MINUTES` (15); unfinished units are
  `unverified timeout`. Failures re-run on `--baseline-tree`; those that also
  fail there are `inherited`. Result schema `prepush_test_gate.v1`
  (`status` pass | fail | unverified | skipped). The Markdown summary carries
  test names and paths only, never output tails, and neutralises `::` lines.
  The module makes no GitHub API call.
- **Interactive flow.** After committing, run
  `python3 .claude/hooks/prepush_affected_tests.py stamp`: it refuses
  uncommitted tracked changes, diffs `HEAD` against its merge-base with
  `origin/HEAD` (else `origin/main`, `origin/master`; none →
  `unverified no_merge_base`), runs the gate on the host (the operator's own
  session, like running pytest by hand) with the merge-base exported by
  `git archive` as the baseline, and writes
  `${TMPDIR:-/tmp}/claude-prepush-gate/<sha256(toplevel)[:16]>/<HEAD>.json`
  (0600, directory 0700, never under the repository or its git dir). The
  `PreToolUse` `Bash` hook (its own settings entry, timeout 30) parses pushes
  with `pr_merge_status_guard._guarded_git_invocations` (conservative regex
  plus a warning if that module cannot load; a `git push` that module does
  not parse, such as `command git push`, is checked as a push of `HEAD`;
  quoted words are ignored for that check). It
  resolves each pushed source ref (the guard's `_push_targets` tip, `HEAD`
  for a bare push) to a commit and asks when that commit's stamp is missing,
  the ref does not resolve, the guard cannot resolve the push options or
  destination (an abbreviated or unknown option such as `--follow-tag`), or
  the stamp records failures this change caused. A bulk push (`--all`, `--mirror`, `--branches`, a pattern) and a
  push that includes tags or other non-branch refs (`--tags`, a
  `refs/tags/` refspec or `--follow-tags`, also next to a branch) always ask. A `pass`,
  `unverified` or `skipped` stamp, a non-push command and
  `CLAUDE_PREPUSH_TEST_GATE=off` give no decision. Internal errors and
  malformed payloads fail open with a `systemMessage`. The hook never runs
  tests or repository code. CLAUDE.md §31 (the instruction to stamp before
  pushing) is an operator step for an interactive session, which must add it
  to both CLAUDE.md copies together.
- **Container wrapper.** `scripts/prepush_test_gate_run.sh --worktree --module
  --image --base --out-dir [--timeout-minutes] [--deps-root]...` refuses a
  module inside the worktree (`reason=module_untrusted`), snapshots the
  worktree through `review_untrusted_workspace.py`, exports the merge-base,
  runs `docker run --network none --read-only --cap-drop ALL --security-opt
  no-new-privileges` with no runner environment (module, baseline and
  dependency roots read-only), and copies back only `result.json` and
  `summary.md` (regular files, 1 MiB). The changed-file list is the worktree
  against the merge-base (uncommitted tracked edits included); `Makefile` and
  `.ai/validate.yml`, which the snapshot filter leaves out, are copied into the
  snapshot; a non-zero container exit discards the result
  (`container_rc_<n>`), since the gate itself exits 0 for every outcome. It always exits 0; wrapper failures
  write a synthetic `unverified` result (`docker_unavailable`,
  `snapshot_failed`, `container_survived`, ...). Unused until P3.
- **PR-only parity tolerance.** `tests/claude_template_drift.py`
  `pending_template_paths()` returns the drifted template paths the PR changed
  or that already drift at `CLAUDE_PARITY_PR_BASE_SHA`; it is empty unless that
  variable is a 40-hex SHA, and on any git error. `ci.yml` sets it from
  `github.event.pull_request.base.sha` on the steps that run the live-copy
  parity, merged-PR guard, PR-watch guard, `gh api` guard and SessionStart
  tests, so push CI is strict; the release gates never set it. The
  `CLAUDE.md` byte-equality check stays strict.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `PREPUSH_TEST_GATE` (`workflow-templates/.claude/hooks/prepush_affected_tests.py`: `phase=run outcome=pass|fail|unverified|skipped selected= ran= failures= inherited= unverified=`, `phase=config outcome=config_invalid name= fallback=`; `scripts/prepush_test_gate_run.sh`: `phase=wrapper outcome=pass|fail|unverified|skipped|error|config_invalid|warning reason=`)
LOG_PREFIX.name=PREPUSH_TEST_GATE
