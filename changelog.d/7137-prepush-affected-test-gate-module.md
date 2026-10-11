<!-- changelog: added -->
- **Interactive sessions now get a pre-push test check: `git push` asks for confirmation unless the affected tests were run for the commit being pushed.** Issue #7137 (plan phase P1 of `docs/plans/prepush-affected-test-gate-plan.md`).

A new gate, `.claude/hooks/prepush_affected_tests.py`, finds the tests a change affects. It selects:
- the changed test files;
- tests named after the changed file (`scripts/foo.sh` → `tests/test_foo.py`);
- test files that mention the changed file's path, name or stem, including through a shared test helper module;
- registry guards (CI test wiring, inventory, live `.claude/` parity, workflow size, `make generate-check`, `agents.d` check);
- `ruff` and ShellCheck on the changed files.

It runs them and re-runs any failure on the merge-base, so failures that already happen on the base are reported as inherited rather than blamed on the change. After committing, run `python3 .claude/hooks/prepush_affected_tests.py stamp`. It writes a stamp for `HEAD` under `${TMPDIR:-/tmp}/claude-prepush-gate/`, outside the repository. A new `PreToolUse` hook then reads that stamp on every `git push`: a passing stamp lets the push through silently; a missing stamp, or one that records failures the change caused, asks first. The hook never runs tests itself and fails open with a warning. `CLAUDE_PREPUSH_TEST_GATE=off` turns it off.

`scripts/prepush_test_gate_run.sh` is the credential-free container wrapper the AI pipelines will use: no network, read-only root, no runner environment, gate module taken from the trusted support checkout. Nothing calls it yet. The implement, review autofix, conflict resolver, heal and integration-branch gates follow in later phases (P2-P5).

| The numbers that matter | Value |
| --- | --- |
| Measured CI misses the selector catches | 7 of 7 (pinned in `tests/test_prepush_affected_tests.py`) |
| GitHub API calls made by the gate | 0 |
| Default time budget / parallel shards | 15 minutes / 4 |

What this means for operators: after the release, consumers' `.claude/settings.json` registers the new hook, and an interactive push without a stamp asks for confirmation. Here, the live `.claude/` copies arrive through the held `ai/sync-claude-live-copies-held` PR, which needs a human merge. Until it merges, `main` CI and the release gates report the live-copy drift.

### For contributors

On pull-request CI only, the live-copy parity tests now tolerate template-ahead drift the PR itself caused, or that its base already has (`tests/claude_template_drift.py`, keyed on `CLAUDE_PARITY_PR_BASE_SHA`). Pushes to `main` and both release gates stay strict.
