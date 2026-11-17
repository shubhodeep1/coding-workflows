# Orchestrator sync: deterministic contract-list union pre-resolver

## Summary

Teach the orchestrator poller's `main → orchestrator/project-*` sync to resolve
one narrow conflict class on its own: both sides appended entries to a
`read_entrypoints:` / `write_entrypoints:` list in a `db/contracts/*.yml`
file. The poller merges locally, keeps both sides, validates the YAML, pushes
the merge commit, and never dispatches the LLM conflict resolver for it.

## Context

Every consumer contract file carries `read_entrypoints:` and
`write_entrypoints:` lists (CLAUDE.md §10.A). Any two PRs that add an
entrypoint to the same contract append at the same position, and git cannot
distinguish "two adjacent appends" from "two conflicting edits". The
motivating incident on `shubhodeep1/tele-funtoken-msg-scoring`:

- Tracking issue #3862, final PR #3868, integration branch
  `orchestrator/project-3862`.
- Poll run 33581471652 (02:03Z, 2026-09-02): the merges API returned 409;
  the only conflicted path was `db/contracts/fantasy_leaderboards.yml`,
  where `main` (PR #3922) added
  `ft.games/app.py::cosmodea_fantasy (settlement results section, read-only)`
  and the integration branch (PR #3877) added
  `backend/fantasy_pot_settlement.py::_ranking_documents_for_pot` at the same
  line of `read_entrypoints:`.
- The poller dispatched the review/autofix resolver (run 33581805826). That
  run spends the full reviewer + editor pipeline before its resolver step
  runs, so a two-line keep-both resolution costs an hour of wall clock and a
  model invocation, and consumes one tick of the
  `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES=1` budget before the judge.

What already exists, and why it does not cover this case:

- `sync_default_into_integration_branch`
  (`scripts/orchestrate_poll_process.sh:7793`) syncs through
  `POST repos/<slug>/merges` (line 7886). A server-side merge ignores
  `.gitattributes`, so a `merge=union` attribute on contracts would not help
  the poller (the same reason CLAUDE.md §20.C gives for `CHANGELOG.md`), and
  union-merging arbitrary YAML would silently produce duplicate keys when two
  PRs edit the same index option.
- On 409 the function computes the conflicted paths with
  `merge_tree_conflict_paths_json` (line 7918), records `.sync.status =
  "conflict"`, posts the "⚠️ Integration sync conflict" comment, and calls
  `heal_integration_branch_conflict` (line 7419), which dispatches
  `_dispatch_review_for_conflicts`.
- `scripts/review_conflict_prepare.sh:189-290` already union-merges the
  workspace manifest deterministically, but it only runs inside a resolver
  dispatch and explicitly skips `orchestrator/project-*` branches, so it
  neither avoids the dispatch nor applies to integration syncs.
- `_refresh_integration_resolver_tooling` (line 6224) is the existing pattern
  for the poller committing and pushing to the integration branch from a
  detached worktree, with worktree-registry bookkeeping and a three-attempt
  push-race retry. The new path reuses that shape.
- The poll job checks out the consumer repo with `fetch-depth: 0` and
  `secrets.GH_PAT` (`.github/workflows/orchestrate_poll.yml:195-199`), so a
  local merge and push need no new credentials.

Clarification answers that fixed the design (all `A`): poller-only location;
strict eligibility (contracts, entrypoint lists, list items only, YAML and
set validation); integration entries first then main's; env-var kill switch
defaulting to `true`; state field + stable log prefix + tracking comment, no
Telegram; Python helper plus a poller bash function; single phase.

## Goals

- A 409 on the sync whose every conflicted path is a `db/contracts/*.yml` or
  `*.yaml` file with only entrypoint-list append/append hunks results in a
  pushed two-parent merge commit on the integration branch within the same
  poll tick, with zero review/autofix dispatches and zero increments to
  `integration_conflict_unresolved_ticks`, `integration_conflict_dispatch_count`,
  and `integration_conflict_total_dispatches`.
- The merged contract parses as YAML, contains no conflict markers, and each
  affected list equals the union of both sides with no duplicates and with
  integration-branch entries before `main`'s.
- Any other conflict shape, any validation failure, a missing PyYAML, or a
  push that still fails after three attempts falls open to the existing
  conflict path with no behaviour change.
- The outcome is observable: `.sync.last_sync_outcome = "merged-deterministic"`,
  a `SYNC_LIST_UNION_V1:` log line, and one tracking-issue comment naming the
  resolved file(s).
- `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED=false` restores today's behaviour
  exactly.
- The #3862 conflict, reconstructed as a test fixture, is resolved by the new
  path in the poller test sandbox.

## Non-goals

- Resolving any conflict outside entrypoint lists in contract files
  (indexes, invariants, purpose text, other YAML sequences).
- Changing `review_conflict_prepare.sh` or lifting its `orchestrator/project-*`
  skip for the manifest union; the resolver workflow is untouched.
- Sorting entrypoint lists, or enforcing a sorted convention in consumer
  repos.
- Any change to the stall judge's `resolve_merge_conflict` action or to
  `finalize_integration_merge_if_needed`.
- Telegram notifications for the new success path.

## Constraints

- §6: no existing identifier is renamed or removed. `sync_default_into_integration_branch`,
  `heal_integration_branch_conflict`, every `INTEGRATION_CONFLICT_*` /
  `CONFLICT_DISPATCH_COOLDOWN_SECS` env var, and every `.sync.*` state field
  keep their names and meanings. New names must not collide with anything
  in scope: `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED`, `sync_contract_list_union_merge`,
  `scripts/sync_contract_list_union.py`, `.sync.last_list_union_paths`,
  `.sync.last_list_union_at`, `SYNC_LIST_UNION_V1`, `[sync-list-union]`, and
  the `merged-deterministic` outcome value were checked against
  `scripts/`, `tests/`, `README.md`, and `agents.md` and are unused.
- §9: tabs in the Python helper and in the poller's new bash function; the
  workflow YAML stays two-space.
- §10: the helper edits contract files only by inserting list items both
  sides already wrote; it never invents, removes, or reorders index or
  invariant content. Consumer contracts are not modified by this repo.
- §14: the behaviour reaches every repo in `.github/ai/consumer_repos.json`
  on the next `@stable` promotion through the `orchestrate_poll.yml@stable`
  reusable workflow. `vars.*` in a reusable workflow resolve in the caller
  repo, so the kill switch needs no wrapper-template change and no
  `update_workflows.yml` sync.
- §15: the new path issues no GitHub REST or GraphQL calls. It uses
  `git fetch` and `git push` only, on refs the poll job already fetches.
- §18: no manual script. The helper is a library invoked by the poller on
  the existing `*/5` schedule; it is neither single-use nor long-running, so
  `docs/scripts-pending-removal.md` gets no entry.
- §20: the implementation PR ships one `changelog.d/<pr>-sync-contract-list-union.md`
  fragment under `<!-- changelog: changed -->`.
- Security: the helper reads three blobs from the merge index and writes one
  file inside a throwaway worktree; it executes no content from the
  contracts and shells out to nothing.

## Approach

Insert one deterministic attempt between "409 confirmed" and "record
conflict state" in `sync_default_into_integration_branch`. The attempt runs a
real `git merge --no-commit` in a detached worktree at the integration tip,
lets git auto-merge everything it can, and hands each still-unmerged path's
three index stages to a Python helper that either produces a validated
merged file or declares the path ineligible. Eligibility is all-or-nothing
per merge: one ineligible path aborts the merge and the function returns
non-zero, and the existing code continues exactly as today. On success the
poller commits with the same subject the merges API would have used
(`chore: sync <default> into <integration>`), pushes, updates `.sync`, marks
the sync clean, and posts one comment.

Alternatives considered and rejected:

- `merge=union` via `.gitattributes`: ignored by the merges API and unsafe for
  key/value YAML.
- Extending the resolver workflow's manifest union: still costs a dispatch and
  the reviewer pipeline; would also need the fingerprint-expansion skip
  reconsidered.
- Sorted lists in consumer repos: only lowers the collision probability, and
  needs a convention in every consumer.

### Helper contract: `scripts/sync_contract_list_union.py`

Invocation (from the poller only):

```
python3 scripts/sync_contract_list_union.py \
  --path db/contracts/<name>.yml \
  --base <stage-1 file or empty file> \
  --ours <stage-2 file> \
  --theirs <stage-3 file> \
  --out <merged file>
```

Exit codes: `0` merged and validated (`--out` written); `3` ineligible
(nothing written, one `SYNC_LIST_UNION_INELIGIBLE_V1: path=<p> reason=<token>`
line on stderr); `1` unexpected error. Reason tokens are stable strings:
`path_not_contract`, `base_missing`, `pyyaml_missing`, `hunk_outside_entrypoints`,
`hunk_non_list_line`, `hunk_one_sided`, `yaml_parse_failed`,
`list_not_union`, `markers_remain`.

Algorithm:

1. Reject unless `--path` matches `^db/contracts/[^/]+\.ya?ml$` and the
   base file is non-empty (an add/add conflict on a whole contract is not a
   list append).
2. Run `git merge-file -p --marker-size 7 -L ours -L base -L theirs ours base
   theirs` to obtain git's own conflict-marked text; the helper parses only
   that output, so its notion of a hunk is identical to what the resolver
   would have seen.
3. For each `<<<<<<< ours` … `=======` … `>>>>>>> theirs` hunk: both sides
   must be non-empty; every line on both sides must match
   `^([ \t]+)- \S` with one common indentation; scanning upward from the hunk
   in the marked text, the nearest column-0 key must be `read_entrypoints:` or
   `write_entrypoints:` and no other column-0 key may intervene. Otherwise
   exit 3 with the matching reason.
4. Replace the hunk with the ours lines followed by the theirs lines that are
   not byte-identical to an ours line (dedupe exact repeats only).
5. Post-conditions on the result: no marker lines remain; `yaml.safe_load`
   succeeds (PyYAML absent → exit 3 `pyyaml_missing`); for each affected key,
   the merged list equals `ours_list ∪ theirs_list` as a set and has no
   duplicates; every other top-level key's parsed value is identical to the
   auto-merged value git produced outside the hunks. Any failure → exit 3.

### Poller function: `sync_contract_list_union_merge <integration_branch> <default_branch>`

Placed next to `_refresh_integration_resolver_tooling` and modelled on it:

1. Return 1 immediately unless `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED` is
   `true` and the branch matches `orchestrator/project-*`.
2. Pre-filter on the already-computed `conflict_paths_json` from
   `merge_tree_conflict_paths_json`: every path must match
   `^db/contracts/[^/]+\.ya?ml$`, otherwise log
   `SYNC_LIST_UNION_V1: … outcome=ineligible reason=non_contract_path` and
   return 1. This costs nothing and skips the worktree for the common case.
3. Fetch both refs with explicit refspecs, `git worktree add --detach` at
   `refs/remotes/origin/<integration>`, register it with
   `worktree_registry_register`, set `user.name`/`user.email` to the same
   `codex-bot` identity the tooling refresh uses.
4. `git merge --no-ff --no-commit refs/remotes/origin/<default>`; if it
   succeeds cleanly (the API signal was stale), fall through to commit and
   push as a plain sync. Otherwise for each `git diff --name-only --diff-filter=U`
   path: `git show :1:`/`:2:`/`:3:` into a tmpdir (`:1:` may be absent →
   empty base file), run the helper, `cp` the merged file over the path and
   `git add` it. Any non-zero helper exit → `git merge --abort`, return 1.
5. `git commit -m "chore: sync <default> into <integration>" -m "<body>"`
   where the body names the resolved paths and says the resolution was the
   deterministic contract-list union from `sync_contract_list_union_merge`.
   The subject matches the merges-API message so nothing that pattern-matches
   sync commits changes.
6. `git push origin HEAD:refs/heads/<integration>`; on failure refetch and
   retry from step 3 up to three attempts (push race), then return 1.
7. Always deregister and remove the worktree; every failure returns 1 and
   logs `SYNC_LIST_UNION_V1: … outcome=failed reason=<step>` at
   `::warning::` level. Success logs `outcome=merged paths=<csv>` and sets
   `SYNC_LIST_UNION_RESOLVED_PATHS_JSON` for the caller.

### Wiring in `sync_default_into_integration_branch`

After the conflict paths are computed (line 7918) and before the `.sync`
conflict write (line 7922):

```
if sync_contract_list_union_merge "${integration_branch}" "${default_branch}"; then
  jq --argjson paths "${SYNC_LIST_UNION_RESOLVED_PATHS_JSON}" --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '.sync = ((.sync // {}) + {"status":"active","last_sync_outcome":"merged-deterministic",
      "last_conflict_paths":[],"last_conflict_fingerprint":"",
      "last_list_union_paths":$paths,"last_list_union_at":$now})' …
  post_state_comment || true
  mark_integration_sync_clean "${default_branch}"
  post_tracking_comment "## ✅ Integration sync auto-resolved (contract list union) …"
  return 0
fi
```

`mark_integration_sync_clean` already resets `integration_conflict_unresolved_ticks`
and posts "✅ Integration self-healing resolved" only when the previous
status was not `clean`, so a first-time deterministic resolution posts the
new comment and, if a prior episode was in flight, the existing one too;
neither dispatch counter moves.

## Phases & Merge Strategy

Single phase, one PR, per the accepted clarification (`Q7: A`). The helper
without its wiring would be dead code, and the wiring without the helper
would fail open on every tick; neither half is a shippable state on its own.

1. **Phase 1 — helper, poller wiring, flag, tests, docs, changelog.**
   Files: `scripts/sync_contract_list_union.py` [new],
   `scripts/orchestrate_poll_process.sh`, `.github/workflows/orchestrate_poll.yml`,
   `tests/test_sync_contract_list_union.py` [new],
   `tests/test_orchestrate_poll_process.py`, `README.md`, `agents.md`,
   `changelog.d/<pr>-sync-contract-list-union.md` [new].
   Done when: all tests in this plan pass in CI; a poller sandbox run with the
   #3862 fixture pushes a merge commit and records no review dispatch; the
   same run with the flag `false` dispatches exactly as today.
   Rollback: revert the single PR, or set the repo var
   `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED=false` on any consumer to disable
   it without a deploy. Merge commits it already pushed are ordinary sync
   merges and need no cleanup.

## Implementation Steps

Phase 1, in commit order:

1. `scripts/sync_contract_list_union.py` [new]: implement the helper contract
   above. Tab-indented, `argparse` CLI, no third-party imports besides an
   optional `yaml`; `git merge-file` is the only subprocess.
2. `tests/test_sync_contract_list_union.py` [new]: unit tests listed under
   Tests, including the #3862 fixture (`fantasy_leaderboards.yml` base at
   `1ed42a7`, ours at `15c1c44`, theirs at `3ed7ca0`, reduced to the
   `read_entrypoints`/`write_entrypoints` region plus a few surrounding keys).
3. `scripts/orchestrate_poll_process.sh` (env defaults block, near line
   1654): add `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED="${ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED:-true}"`
   with the same `true`/`false` validation-and-warn pattern the neighbouring
   vars use.
4. `scripts/orchestrate_poll_process.sh` (after `_refresh_integration_resolver_tooling`,
   ends near line 6530): add `sync_contract_list_union_merge` with the
   docstring block stating inputs, outputs, exit codes, the zero-API-call
   contract, and fail-open behaviour (§15 "document the batching contract"
   wording applies to the no-call claim).
5. `scripts/orchestrate_poll_process.sh` (`sync_default_into_integration_branch`,
   between lines 7918 and 7922): insert the wiring block above. No other
   line in the function changes.
6. `.github/workflows/orchestrate_poll.yml` (poll job `env:` near line 714):
   add `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED: ${{ vars.ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED || 'true' }}`
   with a two-line comment pointing at the poller function. In the
   `Install OS-level core tools` step (line 232) add a guarded
   `python3 -c 'import yaml' || python3 -m pip install --quiet pyyaml`, so
   PyYAML is present on the runner; the helper still fails open if it is not.
7. `tests/test_orchestrate_poll_process.py`: add the three poller tests under
   Tests, using `env_overrides` for the flag and the bare-`origin` sandbox
   pattern already used near line 15876 so the push is a real push.
8. `README.md`: new row in the env-var table after
   `INTEGRATION_CONFLICT_LIFETIME_MAX` (line 178) for
   `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED`; extend paragraph 12c (line 1421)
   with one sentence describing the deterministic pre-resolver and its
   `merged-deterministic` outcome.
9. `agents.md`: add `SYNC_LIST_UNION_V1` and `SYNC_LIST_UNION_INELIGIBLE_V1`
   to "Stable log prefixes (contractual)" (line 460); add one bullet under
   "Integration-sync verifier + bootstrap contract" (line 830) naming the
   helper, the flag, and the new `.sync` fields.
10. `changelog.d/<pr>-sync-contract-list-union.md` [new]: `changed` entry per
    CLAUDE.md §20.D, leading with the operator-visible effect (no resolver
    dispatch for contract-list appends), the flag name and default, and the
    #3862 numbers.

## Files & Modules

- `scripts/sync_contract_list_union.py` [new]
- `scripts/orchestrate_poll_process.sh`
- `.github/workflows/orchestrate_poll.yml`
- `tests/test_sync_contract_list_union.py` [new]
- `tests/test_orchestrate_poll_process.py`
- `README.md`
- `agents.md`
- `changelog.d/<pr>-sync-contract-list-union.md` [new]

## Data Model / Index Changes

None. No MongoDB collection, index, or contract in this repo changes. The
helper edits consumer contract files only by merging list entries both sides
already wrote, which is the contract-update path CLAUDE.md §10.A requires of
the two originating PRs.

## Tests

Unit (`tests/test_sync_contract_list_union.py`, pytest, no git remote):

- #3862 fixture: one adjacent append/append hunk in `read_entrypoints` →
  exit 0, merged list is `[api_fantasy_leaderboard, _ranking_documents_for_pot,
  cosmodea_fantasy …, _load_group_ranking]`, `write_entrypoints` untouched.
- Two hunks, one in each list → exit 0, both merged.
- Same entry added by both sides → exit 0, appears once.
- Hunk inside `indexes:` → exit 3 `hunk_outside_entrypoints`.
- Hunk containing a non-list line (a comment or a nested key) → exit 3
  `hunk_non_list_line`.
- One-sided hunk (modify/delete shape) → exit 3 `hunk_one_sided`.
- Path `backend/foo.py` or `db/other.yml` → exit 3 `path_not_contract`.
- Empty base → exit 3 `base_missing`.
- PyYAML import patched to fail → exit 3 `pyyaml_missing`, no output file.
- Merged text that fails `yaml.safe_load` (constructed by a hunk whose kept
  lines have mismatched indentation) → exit 3 `yaml_parse_failed`.

Poller integration (`tests/test_orchestrate_poll_process.py`, sandbox with a
bare `origin`, merges API faked with `merge_conflict_on_sync=True`):

- Contract-list conflict, flag default: integration tip is a two-parent
  merge whose contract contains both entries; `review_dispatches == []`;
  state `sync.last_sync_outcome == "merged-deterministic"`,
  `sync.last_list_union_paths == ["db/contracts/x.yml"]`,
  `integration_conflict_unresolved_ticks == 0`; a tracking comment starting
  "## ✅ Integration sync auto-resolved" exists; log contains
  `SYNC_LIST_UNION_V1:` with `outcome=merged`.
- Same conflict plus a conflicting `backend/app.py`: no push, one review
  dispatch, existing "⚠️ Integration sync conflict" comment, log
  `outcome=ineligible reason=non_contract_path`.
- Same conflict with `env_overrides={"ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED": "false"}`:
  identical to today's behaviour (the existing
  `test_integration_sync_conflict_uses_sync_specific_retry_budget_default_one`
  assertions hold).

Static: `bash -n scripts/orchestrate_poll_process.sh`,
`python3 -m py_compile scripts/sync_contract_list_union.py`, the existing
`tests/test_ci_poll_test_sharding.py` and inventory-parity checks must stay
green (the new test file is added to whatever inventory those enforce).

Manual, post-merge: on the next consumer conflict of this shape, confirm one
poll run log shows `SYNC_LIST_UNION_V1: … outcome=merged` and the tracking
issue shows the new comment with no "🔧 Integration self-healing started"
comment for that episode.

## Risks & Mitigations

- **Keep-both is wrong for a genuinely conflicting list edit** (one side
  renamed an entry the other side also touched). Mitigation: eligibility
  requires both hunk sides to be pure additions relative to base
  (`git merge-file` only emits a two-sided hunk when both sides changed the
  region; the helper additionally rejects any hunk whose lines are not list
  items), and the set-union post-check rejects a result that drops any line
  either side wrote. Renames show up as a removed base line and fail the
  union check.
- **Push race with a sub-issue PR merging into the integration branch in the
  same seconds** (exactly what happened at 02:04Z when #3918 merged during
  the resolver dispatch). Mitigation: three refetch-and-retry attempts, then
  fail open to the existing path.
- **PyYAML missing on the runner.** Mitigation: the workflow step installs it;
  the helper fails open with `pyyaml_missing` if that install was skipped.
- **The fingerprint verifier and the deterministic merge.** Keep-both never
  removes a line, so `must_contain` fingerprints from merged sub-issue PRs
  cannot be violated; `must_not_contain` lines come from the resolver's own
  removals and are not involved. ACCEPTED — no verifier run in this path.
- **A consumer contract that is not valid YAML before the merge.** The
  post-check fails, the path is ineligible, and the resolver handles it as
  today. ACCEPTED — no behaviour change.
- **Log-analysis tooling that keys on the "⚠️ Integration sync conflict"
  comment count could under-report conflicts.** Mitigation: the new
  `SYNC_LIST_UNION_V1:` prefix and the `.sync.last_list_union_*` fields give
  the analysis a replacement signal; documented in agents.md.
- **Unattended implementer widens scope into `review_conflict_prepare.sh`.**
  Mitigation: Non-goals above and the file list are explicit; the PR
  reviewer should reject changes outside the listed files.

## Rollout

- Ships on `main` in one PR; reaches consumers on the next `main → stable`
  promotion. Default-on with the `ORCH_SYNC_CONTRACT_LIST_UNION_ENABLED`
  repo-var kill switch, so any consumer can disable it without waiting for a
  release.
- No migration, no state schema bump: the two new `.sync` fields are
  additive and absent on older state; readers must treat absence as "never
  resolved deterministically".
- Rollback: revert the PR or set the repo var to `false`. Merge commits
  already pushed are valid sync merges and stay.
- First live verification target: `shubhodeep1/tele-funtoken-msg-scoring`,
  whose fantasy contracts produce this conflict shape most often.

## References

- Incident: https://github.com/shubhodeep1/tele-funtoken-msg-scoring/issues/3862,
  PR #3868, poll run 33581471652, resolver run 33581805826, PR #3922 (main
  side), PR #3877 (integration side).
- `scripts/orchestrate_poll_process.sh`: `sync_default_into_integration_branch`
  (7793), `heal_integration_branch_conflict` (7419),
  `_refresh_integration_resolver_tooling` (6224), `merge_tree_conflict_paths_json`
  (4833), env defaults (1654-1700).
- `scripts/review_conflict_prepare.sh:189-290` — manifest union-merge
  precedent and its `orchestrator/project-*` skip.
- `README.md` env table rows 175-178 and paragraph 12c (line 1421);
  `agents.md` "Stable log prefixes (contractual)" (460) and
  "Integration-sync verifier + bootstrap contract" (830).
- CLAUDE.md §6, §9, §10, §14, §15, §18, §20.
