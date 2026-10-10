# Triage: PR #6611 CI "shard 1" failure

Findings note for the re-issued triage of PR #6611 (re-issued from #6932).
This note records what a local reproduction could and could not show. It
changes no code, tests or workflows.

## Target

- PR: #6611 (head branch `ai/issue-6607`, merged into `main` on
  2026-10-09). Per the approved clarification (Q1=A), any fix targets `main`.
- Failing run: 37922279826, job 113792812346 (an `orchestrate-poll` matrix
  job of `.github/workflows/ci.yml`). The job log needs sign-in and could not
  be read from the implement sandbox, which has no network and no GitHub
  credential.
- Failing PR head `bef70791b7c6`: **not resolvable** in the checkout
  (`git cat-file -t bef70791b7c6` returns `Not a valid object name`). The
  sandbox's git history is a single synthetic commit.
- Tree tested: the implement sandbox snapshot of `main` (synthetic commit
  `cbfc3fcc7268`; the plan recorded `main` at `4fca6c5ddd5d` when it was
  written).

## Assumptions

- "Shard 1" means local shard 1 (of `CI_POLL_TEST_SHARDS=4`) inside one
  `orchestrate-poll (g)` matrix job. The job id does not say which group, so
  all four groups were run, with all four local shards each.

## Commands

The two step bodies were extracted verbatim from `.github/workflows/ci.yml`
(job `orchestrate-poll`): "Derive orchestrate poll test subsets" and
"Orchestrate poll process unit tests". The environment matched CI:

```text
PYTHONDONTWRITEBYTECODE=1
CI_POLL_TEST_SHARDS=4
CI_POLL_TEST_GROUP_COUNT=4
CI_POLL_TEST_GROUP_INDEX=0, 1, 2, 3   (one run per group)
nproc=4, Python 3.11.2                (CI: ubuntu-latest, Python 3.12)
```

The fast-fail subset ran once, as group 0 does in CI:
`python3 tests/test_orchestrate_poll_process.py <10 test_implementation_failed_* names>`.

One local-only change was made to the /tmp copy of the step body, not to
the repository: its `python3 tests/test_ci_poll_test_sharding.py` line was
skipped. That test imports PyYAML, which is not installed in the offline
sandbox (CI installs it), so every group otherwise exited at once with
`ModuleNotFoundError: No module named 'yaml'`.

## Results

Subset derivation: 10 fast-fail tests and 540 remaining tests.

Fast-fail subset: 0 passed, 0 failed, 10 skipped.

| Group | Shard 0 (pass/fail/skip) | Shard 1 | Shard 2 | Shard 3 | Step result |
|---|---|---|---|---|---|
| 0 | 6/0/27 | 7/0/27 | 5/0/29 | 7/0/27 | exit 1 (see below) |
| 1 | 8/0/25 | 5/0/29 | 6/0/28 | 5/0/29 | exit 0 |
| 2 | 8/0/25 | 8/0/26 | 3/0/31 | 6/0/28 | exit 0 |
| 3 | 6/0/27 | 7/0/27 | 6/0/28 | 8/0/26 | exit 0 |

Totals over the 540 sharded tests: 101 passed, 0 failed, 439 skipped. No
shard logged a `FAIL` line or a process error. Every skip has the same
reason:

```text
SKIP  <test>: jq binary not available in test environment
```

The implement sandbox has no `jq` binary and cannot install one offline.

Group 0's non-zero exit came from the single-file module the step runs after
the shards, `tests/test_orchestrate_poll_noop_suspicious_recovery.py`
(28 passed, 13 failed). Its tests have no `jq` skip guard. They run the
fingerprint-cap block extracted from `scripts/orchestrate_poll_process.sh`,
which pipes the comment and commit JSON through `jq`, so with no `jq` the
block fails open and dispatches. One failure message shows the cause
directly:

```text
FAIL  test_standalone_batch_accepts_matching_sha_and_falls_back_on_uncertainty: bash: line 10: jq: command not found
```

These failures come from the sandbox environment, not from the code. They
are not a "shard 1" failure, because this module runs after the shards and
is not sharded.

## Conclusion

**Inconclusive. Nothing reproduced, but most of the shard was never
exercised.** No failure appeared in any shard of any group. However, 439 of
the 540 sharded tests (81%) skipped for lack of `jq`, so a failure in a
`jq`-dependent test would not have shown up here. The failing head
`bef70791b7c6` was not available either, so a failure that existed only at
that head could not be checked.

Following the approved plan, there are no code, test or workflow changes.
Nothing was demonstrated, so any fix would be speculative. The `lint`
aggregate, the shard-failure tally and the isolation safeguards are
untouched.

To reproduce the original failure, the run needs a host or runner with `jq`
and PyYAML, either reading the job 113792812346 log (sign-in required) or
running the same two step bodies with `CI_POLL_TEST_GROUP_INDEX=0..3`.

## CI confirmation

This PR's own CI run is the required rerun, and every `orchestrate-poll`
group there runs with `jq` installed. Status: pending this PR's CI. The
required `lint` aggregate has not been observed to pass.
