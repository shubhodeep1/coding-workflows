# Let non-conflicting approved PRs merge past the merge lease

## Summary
The per-base merge lease (#9660, merged 2026-11-17 in aafc04a2, shipped to
consumers in v1.47.4) now serializes almost every approved PR on `main` behind
one holder. Over 41 hours, 69% of lease time went to holders whose lease
expired without a merge. This plan makes five independently mergeable changes
so that PRs which do not really conflict merge in parallel, without bringing
back the update-and-re-review race the lease was built to stop:
1. lease metrics;
2. revoking idle or regressed grants;
3. priority for forward-merge PRs;
4. a "soft" path class that overlaps only on a real textual conflict;
5. multiple holders whose paths do not overlap.

## Automation (§18.E)
- **Scripts:** no new script. Every phase only modifies existing code:
  - `scripts/pr_checks_lib.sh`: `_pr_base_freshness` (1333-1483), `_merge_lease_gate` (2294-2422), `_merge_lease_paths_intersect` (2217-2224), `_merge_lease_queue_first` (2248-2281), `_merge_lease_read` / `_merge_lease_write` (2091-2201);
  - `scripts/review_merge_train.sh`: `_mt_merge_lease_tick` (1600-1675), `_mt_list_open_prs` (337-346), `_mt_conflict_probe` (745 onward).
- **Scheduler / entry points:** the existing callers, with no new trigger, workflow or job:
  - the merge paths that call `_pr_base_fresh_for_merge`, in the `review_autofix.yml` jobs, `scripts/review_rb_judge.sh`, `scripts/review_enable_auto_merge.sh` and `scripts/orchestrate_poll_process.sh`;
  - the lease tick, run from `_mt_release` (`scripts/review_merge_train.sh` 1957). That runs on every poller tick (`.github/workflows/orchestrate_poll.yml`, schedule) and on every PR close (`.github/workflows/cancel_on_pr_close.yml`, `pull_request: closed`).
- **Repo-variable wiring:** new `MERGE_LEASE_*` variables are added beside the existing ones in the four workflows that pass them today:
  - `.github/workflows/review_autofix.yml` 1211-1213;
  - `.github/workflows/review_autofix_sweep.yml` 233-234;
  - `.github/workflows/orchestrate_poll.yml` 294-296;
  - `.github/workflows/cancel_on_pr_close.yml` 422-424.
- **Long-running supervisor:** none.
- **DB work:** none. No MongoDB collection, index or contract is touched (§10 N/A).
- **`docs/scripts-pending-removal.md`:** no entry. No single-use or long-running script or supervisor is introduced.

## Decisions

The user answered every clarification question with A on 2026-11-19 (relayed by the parent session as Q56-Q63).

### D1 — Soft path class
- **Chosen:** `README.md`, `agents.md`, `AGENTS.md`, `docs/INVENTORY.md`, `CHANGELOG.md`, and the prefixes `agents.d/` and `changelog.d/`. A shared soft path counts as overlap only when `git merge-tree --write-tree` reports a conflict on it.
- **Alternatives considered:**
  - fragments only (`agents.d/`, `changelog.d/`), which removed 0 of 67 overlapping pairs in today's queue;
  - all `*.md`, which also covers `.claude/commands/*.md`, `workflow-templates/**/*.md` and `CLAUDE.md`, files that ship to consumers or are read into model context.
- **Why:** this is the smallest list that removes the README-only serialization (67/78 overlapping pairs become 13/78). Fragments still need the conflict check, because their names are not unique in practice: `agents.d/pr-body-lint-job-rename.md` appears in #9693, #9698 and #9709.

### D2 — Where the soft rule applies
- **Chosen:** in both the freshness "needs update" decision (`_pr_base_freshness`) and the lease overlap (`_merge_lease_paths_intersect` in merge mode and in holder-slot checks).
- **Alternatives considered:** the lease overlap only.
- **Why:** a one-sided rule recreates the loop. If a README-only merger is let past a holder while the holder's freshness still counts README, the holder re-updates and re-runs final CI.

### D3 — Revoke idle or regressed grants
- **Chosen:** the tick releases a held lease when the holder:
  - is draft;
  - has `[skip ai]` in its title;
  - has no pipeline `ai-review/head-gate` success on its current head;
  - has a final CI run that concluded with failure;
  - has neither renewed nor had a final CI run queued or in progress for `MERGE_LEASE_IDLE_MINUTES` (default 60).

  The same eligibility check runs at grant time. Ineligible waiters are skipped and unlabelled.
- **Alternatives considered:** a 120-minute idle limit; no revocation.
- **Why:** 28h19 of 41h10 went to holds that expired unmerged (#9428, #9367 ×3, #9368, #9359).

### D4 — Order after an unproductive hold
- **Chosen:** a PR whose hold ended without a merge (expired or revoked) is skipped by the grant until its head SHA changes. Otherwise the order stays priority first, then lowest PR number.
- **Alternatives considered:** FIFO by wait-since; today's order unchanged.
- **Why:** the smallest change to the documented merge-train ordering (README 4a) that still stops one failing low-numbered PR from winning repeatedly. #9367 won three times, for 12h11 in total.

### D5 — Multiple holders
- **Chosen:** build up to `MERGE_LEASE_MAX_HOLDERS` holders whose effective (D1/D2) path sets are pairwise disjoint. Ship with a default of 1 (today's behaviour). Raise it to 2 by repo variable after one week of Phase 1 metrics. Keep the record readable by v1 readers.
- **Alternatives considered:** ship with 3; do not build it.
- **Why:** after D1, today's 12 waiters fall into 4 disjoint waves. Each holder still runs one full final CI (about 30 jobs) on a pool of about 20 concurrent jobs, so concurrency is raised only once metrics show slack (§33).

### D6 — Forward-merge priority
- **Chosen:** `MERGE_LEASE_PRIORITY_HEAD_PREFIXES`, default `auto/forward-merge-stable-`. A PR whose head ref starts with a listed prefix orders with the `MERGE_TRAIN_PRIORITY_LABELS` group.
- **Alternatives considered:** no head-ref priority.
- **Why:** #9735 and #9755 carry `stable` fixes back to `main` and wait last only because their PR numbers are high.

### D7 — Auto-merge disarm on acquire
- **Chosen:** deferred. Not part of this plan (see Non-goals).
- **Alternatives considered:** include it now.
- **Why:** one observed slip, with its cause unconfirmed (see Context).

### D8 — Delivery
- **Chosen:** this multi-PR plan, one PR per phase, handed to the orchestrator after the user's go.
- **Alternatives considered:** one single-PR issue.
- **Why:** each phase has its own kill switch and rollback and can be measured on its own.

### D9 — Conflict probe reuse
- **Chosen:** Phase 4 lifts the body of `_mt_conflict_probe` and its helpers (`_mt_git`, `_mt_git_fetch`, `_mt_probe_repo_init`, `_mt_probe_repo_cleanup`, `review_merge_train.sh` 658-809) into `scripts/pr_checks_lib.sh` as `_pr_merge_tree_probe`. The `_mt_*` names stay as thin wrappers (§6).
- **Alternatives considered:** a second, independent probe in `pr_checks_lib.sh`.
- **Why:** §5, "extend existing mechanisms, never compete". The merge train's probe already handles the no-checkout callers with a private bare repo and token hygiene.

## Context

### How the lease works today (`scripts/pr_checks_lib.sh` at 4200e09)
- **Entry point:** `_pr_base_fresh_for_merge` (1822-1913) is the one call every merge path makes.
- **Needs update:** `_pr_base_freshness` (1333-1483) intersects the base-side files of `compare/{head}...{base}` with the PR's own `pulls/{n}/files` (1440-1450). Any shared path is `overlap`. There is no textual check. The PR's paths are cached in `PR_BASE_FRESHNESS_PR_PATHS` (1451-1457).
- **Update mode:** on overlap the gate is called in update mode (1837). Update mode compares no paths (2340-2345): any other live holder means `wait reason=held`. A free lease goes to `_merge_lease_queue_first` (2248-2281): priority label first, then the lowest number, among non-draft PRs labelled `ai:merge-lease-wait`.
- **Merge mode:** `fresh` and `clean` PRs call the gate in merge mode (1877). It waits only when `_merge_lease_paths_intersect` (an exact `grep -Fxq`) finds a path the PR shares with the holder's recorded `paths:`, which is the holder's full file list. Every holder lists `README.md`.
- **Record and TTL:**
  - The record is `refs/merge-lease/<base>`, a commit whose message is `merge-lease v1` plus `state= base= pr= head= expires= acquired= updated= paths:` (2161-2170).
  - Writes use a fast-forward-only PATCH as the compare-and-swap.
  - The TTL is 240 minutes, renewed only by the holder's own update.
- **Hand-off:** `_mt_merge_lease_tick` frees a lease only when its holder is closed or past expiry (1627-1633), then grants it and re-dispatches the waiter's review.

### Measured baseline ("before")
Method:
- Walked the 26-record commit chain of `refs/merge-lease/main` (each write is one commit).
- Cross-checked it against `MERGE_LEASE` and `MERGE_BASE_FRESHNESS` lines from 39 poller runs and 95 review runs since 2026-11-17 15:00Z, plus PR timelines.
- The window is 2026-11-17 15:35Z to 2026-11-19 08:45Z.

| Holder | Held | End |
| --- | --- | --- |
| #9428 | 4h16 | expired, not merged |
| #9367 | 4h03 | expired, not merged |
| #9368 | 6h00 (renewed once) | expired, not merged |
| #9367 | 4h05 | expired, not merged |
| #9368 | 4h00 | merged after 3h24; freed at expiry, 36 min later |
| #9359 | 5h52 (renewed once) | expired, not merged |
| #9367 | 4h03 | expired, not merged |
| #9506 | 3h07 | merged |
| #9567 | 1h41 | merged |
| #9666 | 2h26 (updated twice) | merged |
| #9677 | 1h37+ | in progress at measurement time |

| The numbers that matter | Value |
| --- | --- |
| Hold time that expired without a merge, 41 h | 28h19 (69%) |
| Same, last 24 h | 11h09 (46%) |
| Merges under the lease, 41 h | 4 |
| Productive hold, median | about 2h30 (one final CI of 70-90 min plus dispatch and merge lag) |
| Non-draft waiters on 2026-11-19 08:45Z | 12 (#9683, #9685, #9693, #9698, #9699, #9705, #9709, #9711, #9723, #9725, #9735, #9755) |
| Their waits so far | 22h, 19h37, 19h13, 18h41, 17h15, 11h32, 11h, 8h39, 7h49, 6h44, 3h11, 0h25 |
| Waiters that overlap holder #9677 only on README.md | 11 of 12 (#9711 also shares auto-release files; #9735 shares nothing) |
| Path-overlapping pairs among the 13 PRs, today's rule | 67 of 78 |
| Same, after dropping only `agents.d/` and `changelog.d/` | 67 of 78 |
| Same, with the D1 soft class | 13 of 78 |
| Pairs that really conflict textually (`git merge-tree`) | 4 of 78 |
| Serial slots to drain the queue, today's rule | 12 (about 30 h at 2.5 h each) |
| Disjoint waves with D1 | 4: [9677, 9683, 9685, 9693, 9705, 9725, 9735] [9698, 9699, 9755] [9709, 9723] [9711] |
| Waiters needing no update or lease with D1/D2 | 6 of 12 (#9705, #9709, #9725 and #9755 overlap the base only on soft files and merge cleanly; #9685 and #9735 are already up to date) |

Other observations:
- **Race slip:** #9727 shares `README.md` with #9666. It merged 84 seconds after #9666 acquired the lease and updated, which forced #9666 into a second update (+47 min). The merge came from the repository account outside any pipeline run found. Cause not confirmed.
- **Stale wait labels:** 23 `[skip ai]` draft PRs still carry `ai:merge-lease-wait`. Drafts are skipped today, but any one made ready again would jump the queue by PR number.

## Goals
- G1: Every lease release logs how long it was held and whether the holder merged. Every grant logs the queue length.
- G2: No lease stays with a holder that is draft, `[skip ai]`, lost its head-gate approval, failed final CI, or made no progress for `MERGE_LEASE_IDLE_MINUTES`. Ineligible PRs are never granted.
- G3: A PR whose hold ended unmerged is not granted again until its head changes.
- G4: PRs on `auto/forward-merge-stable-*` heads order before non-priority waiters.
- G5: A shared soft path forces neither a branch update nor a lease wait unless `git merge-tree` reports a conflict on it. Any probe failure falls back to today's path rule.
- G6: With `MERGE_LEASE_MAX_HOLDERS=N`, up to N PRs with pairwise disjoint effective paths can hold the lease at once. With 1, behaviour is byte-for-byte today's.
- G7: Each phase has a kill switch (§4) that restores today's behaviour without a code change.

## Non-goals
- **D7, auto-merge disarm on acquire.** Deferred. Revisit when Phase 1 metrics show at least two holder re-updates in a week caused by a PR merging past the lease with GitHub auto-merge armed.
- **Code paths on textual conflict.** No real-conflict rule for code paths; they keep path overlap, because a clean textual merge can still break semantically.
- **Speculative or batched CI.** No speculative or batched train CI (holder ⊕ waiters).
- **GitHub merge queue.** Not available for user-owned repositories, and enabling it would be a §23.C ruleset change in every consumer.
- **Final-CI cost.** No change to the final CI's job count or the runner pool.
- **Cleaning up current PRs.** No removal of stale labels and no closing of #9735. Those are one-off actions for the user, not code.

## Constraints
- **§4 env vars:** every new variable has a default and a kill switch. Existing `MERGE_LEASE_*` names are untouched.
- **§6 naming and formats:**
  - The `MERGE_LEASE` log line is a contractual prefix (`agents.d/merge-lease-final-ci-resume.md`): new fields only append after `blocker=`. New prefixes go into the agents registry via `agents.d/` fragments (§30).
  - The lease record format is an identifier. The `merge-lease v1` header, the `state= base= pr= head= expires= acquired= updated=` lines and the trailing `paths:` block keep their meaning. New lines go before `paths:` (v1 readers parse `paths:` to end of message and ignore unknown keys).
  - `_mt_*` function names stay (D9).
- **§15 API budget:** each phase states its calls. No phase adds a per-item call inside a loop beyond the eligibility read (Phase 2), which runs only for holders and for grant candidates until the first eligible one.
- **§27 workflow size:** the four workflows each gain at most 5 env lines. `review_autofix.yml` is 411,241 bytes, well under 480,000.
- **§33 runner concurrency:** no job, workflow or matrix leg is added. Phase 5 raises concurrent final-CI demand only when the repo variable is raised (D5).
- **§19:** plan and implementation PRs use `Refs` only.
- **§14 consumers:** every repo in `.github/ai/consumer_repos.json` that has synced v1.47.4 or later runs the lease. Each phase reaches them on the next `@stable` sync with the same defaults. Only Phase 4 changes default behaviour for them (soft class on by default). Consumers override any variable per repo.

## Approach
All five phases change functions that already run in the existing jobs.
- **Phase 1** only adds log fields and one per-tick summary line.
- **Phase 2** extends the tick's release step with more release reasons, adds an eligibility filter to `_merge_lease_queue_first`, and records a skip entry in the release write the tick already does.
- **Phase 3** adds a head-ref test to `_merge_lease_order_key`.
- **Phase 4** partitions shared paths into hard and soft. Soft shared paths are checked with the lifted merge-tree probe; the PR is clean when every soft path merges cleanly and no hard path is shared. Freshness uses (PR head, base tip seen by the compare). Merge mode uses (PR head, holder head from the record). Missing SHAs, probe errors and truncated compares fall back to hard overlap.
- **Phase 5** keeps one ref per base and adds holder lines to the same record, so one fast-forward PATCH still decides every acquisition atomically.

Each phase reads and writes the record additively, so any merge order works and each rebases on the others.

## Phases & Merge Strategy
Every phase is independently mergeable: no phase calls code another phase adds, and each defaults to a safe state. The recommended order is 1 → 2 → 3 → 4 → 5, so that Phase 1's metrics measure the later ones. That order is advisory, not required.

1. **Phase 1, lease metrics (I6).**
   - Scope: append `held_s=` and `merged=` to tick release lines and `queue_len=` to grant lines, and emit one `MERGE_LEASE_QUEUE` line per base per tick.
   - Files: `scripts/review_merge_train.sh`, `scripts/pr_checks_lib.sh`.
   - Done when the new fields appear in tests and every existing `MERGE_LEASE` assertion still passes.
   - Rollback: revert the PR (log-only).
2. **Phase 2, revocation, grant-time eligibility and skip-until-head-changes (I2 + I3).**
   - Scope: D3 and D4.
   - Files: `scripts/review_merge_train.sh`, `scripts/pr_checks_lib.sh`, the four workflows.
   - Done when the tick revokes in each listed case, skips ineligible and skipped waiters, and behaves as today with `MERGE_LEASE_REVOKE_ENABLED=false`.
   - Rollback: set the variable to `false`, or revert.
3. **Phase 3, forward-merge priority (I5).**
   - Scope: D6.
   - Files: `scripts/pr_checks_lib.sh`, `scripts/review_merge_train.sh` (pass the head ref through), the four workflows.
   - Done when a forward-merge head orders before a lower-numbered non-priority waiter, and `none` restores today's order.
   - Rollback: `MERGE_LEASE_PRIORITY_HEAD_PREFIXES=none`, or revert.
4. **Phase 4, soft path class in freshness and lease (I1).**
   - Scope: D1, D2 and D9.
   - Files: `scripts/pr_checks_lib.sh`, `scripts/review_merge_train.sh`, the four workflows.
   - Done when a soft-only clean overlap is `clean` in freshness and `no_overlap` in merge mode, a soft conflict stays `overlap`/`wait`, a probe failure stays `overlap`/`wait`, and `MERGE_LEASE_SOFT_PATHS=none` restores today.
   - Rollback: `MERGE_LEASE_SOFT_PATHS=none`, or revert.
5. **Phase 5, multiple disjoint holders (I4).**
   - Scope: D5.
   - Files: `scripts/pr_checks_lib.sh`, `scripts/review_merge_train.sh`, the four workflows.
   - Done when, with `MERGE_LEASE_MAX_HOLDERS=1`, every existing lease test passes unchanged; with 2, two disjoint PRs both acquire and an overlapping third waits; and a v1 reader (today's `_merge_lease_read`) parses a two-holder record as the first holder with the union of paths.
   - Rollback: `MERGE_LEASE_MAX_HOLDERS=1`, or revert. A multi-holder record left behind is still read correctly by v1 code (see step 5.1).

## Implementation Steps

### Phase 1 — lease metrics
1.1 `scripts/review_merge_train.sh` `_mt_merge_lease_tick` (1600-1675):
- In the existing `pulls/<n>` read for a holder missing from the open list (1632), change `--jq` to return `.state` and `.merged_at` together. No new call.
- Append ` held_s=<now - MERGE_LEASE_ACQUIRED> merged=true|false` to the `outcome=released` line. `merged=false` for `expired`.
- Append ` queue_len=<eligible waiters>` to the `outcome=granted` line.

1.2 `scripts/pr_checks_lib.sh` `_merge_lease_queue_first` (2248-2281): also set `MERGE_LEASE_QUEUE_LEN` (the count of waiters considered) and `MERGE_LEASE_QUEUE_PRS` (comma list in order). The printed output is unchanged.

1.3 In the tick, once per lease ref after the hold or grant decision, emit `MERGE_LEASE_QUEUE base=<ref> holder=<n|-> held_s=<s|-> waiters=<count> order=<comma list|->`. No API calls; it reuses the record and listing already read.

1.4 Add an `agents.d/` fragment registering the `MERGE_LEASE_QUEUE` prefix in `## Stable log prefixes (contractual)` (bullet plus `LOG_PREFIX.name=MERGE_LEASE_QUEUE`), and documenting the appended `MERGE_LEASE` fields.

### Phase 2 — revocation, eligibility, skip-until-head-changes
2.1 `scripts/review_merge_train.sh` `_mt_list_open_prs` (337-346): add `title: .title` to the emitted object. Additive; existing readers ignore it.

2.2 `scripts/pr_checks_lib.sh`: add `_merge_lease_pr_eligible <pr> <head_sha> <title> <draft>`. It returns 1 with `MERGE_LEASE_INELIGIBLE_REASON` set to one of `draft|skip_ai|no_head_gate|final_ci_failed`.
- It reads the head's statuses and check runs once (`_pr_wait_for_required_checks` with a 0-minute budget, as `_mt_final_ci_merge_resume` does).
- It trusts only a pipeline-posted `ai-review/head-gate` success.
- It returns 0 on read failure (fail-open: keep today's behaviour).

2.3 `_mt_merge_lease_tick`, for a held, unexpired lease whose holder is open: if `MERGE_LEASE_REVOKE_ENABLED` is true, release (`_merge_lease_write … released`) in either case:
- the holder is ineligible (`reason=revoked_<why>`);
- `now - MERGE_LEASE_UPDATED >= MERGE_LEASE_IDLE_MINUTES*60` with no final CI run queued or in progress on its head, from the same read (`reason=revoked_idle`). `MERGE_LEASE_IDLE_MINUTES=0` disables only this idle test.

  `_merge_lease_read` parses `updated=` into a new `MERGE_LEASE_UPDATED` (the line is already written).

2.4 Skip entries: every release with `merged=false` (expired or revoked) writes `skip=<pr>:<head>` lines before `paths:`, carrying forward existing skip lines.
- Prune the lines whose PR is no longer open or whose head differs from the open PR's head.
- Keep at most 50; drop the oldest first.
- `_merge_lease_read` collects them into `MERGE_LEASE_SKIPS`.

2.5 `_merge_lease_queue_first` gains an optional skip list and eligibility callback. In the tick:
- Candidates are examined in order. A candidate in the skip list with the same head, or ineligible, is skipped.
- An ineligible candidate is also unlabelled (`_merge_lease_unlabel_wait`) and logged `MERGE_LEASE pr=<n> base=<ref> mode=tick outcome=skipped reason=<why> …`.
- The update-mode path in `_merge_lease_gate` applies the same skip list, so a PR blocked by a skip entry cannot jump ahead by calling the gate itself. Eligibility is not re-checked there; the caller is already an approved merge path.

2.6 Env (defaults in `pr_checks_lib.sh`, wired as `${{ vars.X || 'default' }}` in the four workflows):
- `MERGE_LEASE_REVOKE_ENABLED` (default `true`; `true|1|yes|on`). Covers 2.3-2.5.
- `MERGE_LEASE_IDLE_MINUTES` (default `60`, `0` = idle test off, clamped 0-1440).

### Phase 3 — forward-merge priority
3.1 `_merge_lease_order_key` (2228-2246) takes the head ref as an optional third argument. It returns priority `0` when the ref starts with any entry of `MERGE_LEASE_PRIORITY_HEAD_PREFIXES` (default `auto/forward-merge-stable-`; empty, `none` or `off` disables; comma-separated; prefix match, no globs).

3.2 `_merge_lease_queue_first`'s jq projection adds `.head.ref // .head // ""`, which covers both the REST `pulls` shape and the merge-train shape. It passes the ref to the order key.

3.3 Wire the variable in the four workflows.

### Phase 4 — soft path class
4.1 Lift the probe (D9):
- Move the bodies of `_mt_git`, `_mt_git_fetch`, `_mt_probe_repo_init`, `_mt_probe_repo_cleanup` and `_mt_conflict_probe` (`scripts/review_merge_train.sh` 658-809) into `scripts/pr_checks_lib.sh` as `_pr_mt_git`, `_pr_mt_git_fetch`, `_pr_probe_repo_init`, `_pr_probe_repo_cleanup` and `_pr_merge_tree_probe <sha_a> <sha_b>`. They set `_PR_PROBE_RESULT` (`none|conflict|unknown`) and `_PR_PROBE_PATHS`.
- Keep the `_mt_*` functions as wrappers that call them and map the results to `_MT_PROBE_*`, including `ignored` via `MERGE_TRAIN_IGNORE_PATHS`.
- Behaviour and logs (`MERGE_TRAIN_PROBE_REPO`) are unchanged.
- Check every new name for collisions first (§6).

4.2 Add `_merge_lease_soft_path <path>`: 0 when the path equals an exact entry of `MERGE_LEASE_SOFT_PATHS`, or starts with an entry ending in `/`.
- Default: `README.md,agents.md,AGENTS.md,docs/INVENTORY.md,CHANGELOG.md,agents.d/,changelog.d/`.
- `none` or `off` disables it. Entries containing `*`, `?` or `[` are rejected with a warning, as `MERGE_TRAIN_IGNORE_PATHS` does.

4.3 `_pr_base_freshness`, after the overlap list is computed (1440-1450):
- Split it into hard and soft. Hard non-empty means `overlap`, as today.
- Soft-only overlap with `PR_BASE_FRESHNESS_BASE_SHA` set: probe (`head_sha`, `PR_BASE_FRESHNESS_BASE_SHA`).
  - `none` sets outcome `clean`, `reason=soft_paths_merge_clean`, and stores the soft list in a new `PR_BASE_FRESHNESS_SOFT_OVERLAP`.
  - `conflict` or `unknown` gives `overlap`, as today, with reason `soft_paths_conflict` or `soft_paths_probe_unknown`.
- `base_diff_truncated` and `pr_files_truncated` stay `overlap`.

4.4 `_merge_lease_paths_intersect` gains optional `<own_head> <holder_head>` arguments.
- When soft entries are enabled, intersect only hard paths.
- If only soft paths are shared and both heads are 40-hex, probe them: `none` returns 1 (no overlap); anything else returns 0.
- `*` still overlaps everything. Merge mode (2310-2322) passes `lease_head` and `MERGE_LEASE_HEAD`.

4.5 `_pr_base_fresh_for_merge`'s `MERGE_BASE_FRESHNESS` line keeps its fields and appends ` soft_overlap=<paths|->`.

4.6 Wire `MERGE_LEASE_SOFT_PATHS` in the four workflows.

### Phase 5 — multiple disjoint holders
5.1 Record format, additive:
- The primary holder stays in `pr= head= expires= acquired=`.
- Each further holder adds one line before `paths:`: `holder=<pr> head=<sha> expires=<epoch> acquired=<epoch> updated=<epoch>`.
- Each holder's own paths go in `holder_path=<pr> <path>` lines. The primary's own paths are also written this way when more than one holder exists.
- `paths:` holds the union of all holders' paths (`*` if any holder is `*`). A v1 reader therefore sees one holder that overlaps every path any holder touches, which is stricter than today and never fails open.
- When the primary is released while others remain, the oldest-acquired remaining holder is promoted into the `pr=` fields.

5.2 `_merge_lease_read` parses the holder lines into arrays (`MERGE_LEASE_HOLDERS`, per-holder head, expiry and paths).
- It drops expired holders from its view.
- `_merge_lease_is_held` means any live holder.

5.3 `_merge_lease_gate`:
- **Update mode:**
  - It proceeds (`acquired`) when the caller is already a holder (renew its own line).
  - It also proceeds when live holders < `MERGE_LEASE_MAX_HOLDERS`, the caller is first in queue order among waiters disjoint from all holders (effective paths per Phase 4 when present), and its effective paths are disjoint from every live holder's.
  - Otherwise it waits with `reason=held`, or `reason=holder_shares_paths` for a slot refused by overlap.
- **Merge mode** checks against every live holder. `MERGE_LEASE_BLOCKER` is the first overlapping one.

5.4 `_mt_merge_lease_tick`:
- It runs the release checks for each holder (closed, expired, and Phase 2 revocation when present).
- It then grants free slots in queue order, skipping waiters that overlap a live holder.
- Each grant and release is its own CAS write (contention retry as today).

5.5 Env: `MERGE_LEASE_MAX_HOLDERS` (default `1`, clamped 1-5; non-numbers use 1), wired in the four workflows. With 1, the gate takes today's code path exactly.

## Files & Modules
- `scripts/pr_checks_lib.sh` (Phases 1-5)
- `scripts/review_merge_train.sh` (Phases 1-5)
- `.github/workflows/review_autofix.yml`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/cancel_on_pr_close.yml` (env lines only; Phases 2-5)
- `tests/test_merge_lease.py`, `tests/merge_lease_fake_gh.py`, `tests/test_pr_base_freshness_gate.py`, `tests/test_review_merge_train.py` (Phases 1-5)
- `README.md`: rows next to the `MERGE_LEASE_*` rows at 151-155 (Phases 2-5). A direct README edit is allowed; README has no fragment mechanism.
- `changelog.d/<pr>-merge-lease-<slug>.md` [new], one per phase (§20)
- `agents.d/<pr>-merge-lease-<slug>.md` [new], one per phase (§30). Each targets the existing merge-lease section and, for new log fields or prefixes, `## Stable log prefixes (contractual)`.

## Tests
All of these are unit tests through the shipped scripts with the stateful fake `gh` (`tests/merge_lease_fake_gh.py`). Probe tests in Phase 4 use real `git` in a temporary repository, as `tests/test_review_merge_train.py` already does.
- **Phase 1:**
  - The release line carries `held_s=` and `merged=false` on expiry.
  - The release line carries `merged=true` when the fake PR has `merged_at`.
  - The grant line carries `queue_len=`.
  - One `MERGE_LEASE_QUEUE` line appears per lease ref.
  - Existing `MERGE_LEASE` regex assertions still match (the prefix is unchanged).
- **Phase 2:**
  - Revoke on draft, on `[skip ai]`, on a missing or foreign head-gate status, on final-CI failure, and on idle (stale `updated=`, no CI in progress).
  - No revoke when idle but CI is in progress, and none with `MERGE_LEASE_IDLE_MINUTES=0`.
  - The grant skips an ineligible lowest-number waiter and unlabels it.
  - A skip entry blocks a re-grant until the head changes, in both the tick and the update-mode gate.
  - Skip lines are pruned and capped at 50.
  - `MERGE_LEASE_REVOKE_ENABLED=false` gives today's behaviour.
  - An eligibility read failure fails open.
  - A v1 reader parses a record that has `skip=` lines.
- **Phase 3:** a forward-merge head outranks a lower number; a priority label and a forward-merge prefix tie on PR number; `none` disables it; both pull-listing shapes work.
- **Phase 4:**
  - Freshness: soft-only and clean gives `clean reason=soft_paths_merge_clean`. A soft conflict, a probe unknown, or a missing base SHA gives `overlap`. Hard plus soft gives `overlap`.
  - Merge mode: soft-only shared with a clean merge-tree gives `no_overlap`; with a conflict it gives `wait holder_shares_paths`.
  - `MERGE_LEASE_SOFT_PATHS=none` gives today's results for every case.
  - Prefix entries match `agents.d/x.md` but not `agents.dx`.
  - Glob entries are rejected.
  - Every existing `tests/test_review_merge_train.py` probe test passes through the `_mt_*` wrappers.
- **Phase 5:**
  - `MAX_HOLDERS=1` makes every existing lease test pass unchanged.
  - With 2, disjoint PRs both acquire; an overlapping PR waits `holder_shares_paths`; and a third disjoint PR waits `held` at the cap.
  - Merge mode checks against both holders.
  - Releasing the primary promotes the other holder.
  - A two-holder record read by the unchanged v1 parser yields the primary plus the union of paths.
  - The CAS lost-race retry works with two concurrent acquirers.
- **Whole suite:** `python3 -m pytest tests/test_merge_lease.py tests/test_pr_base_freshness_gate.py tests/test_review_merge_train.py tests/test_workflow_file_size_limit.py`, plus `python3 scripts/assemble_agents.py check --repo-root .` for each phase.

## Risks & Mitigations
- **A combined soft-file tree lands on `main` without final CI on that exact combination.** 57 test files read `README.md` or `agents.md`, so a semantic doc clash (a duplicate row, a count line) can break `main` CI after merge. Mitigations:
  - textual conflicts still wait;
  - `main` CI and the heal pipeline catch the rest;
  - the promote gate still treats README and agents changes as code, so the promote cycle's smoke test still covers them before `stable`;
  - `MERGE_LEASE_SOFT_PATHS=none` turns it off.

  ACCEPTED (D1).
- **Phase 4 probe cost in the poller and close callers** (fetch by SHA at depth 200 into a private bare repository): bounded to one fetch per pair, cached per run as the merge train does. Unknown results keep today's rule.
- **Revocation evicts a holder mid-autofix.** At worst it re-queues once. Eligibility reads fail open, and `MERGE_LEASE_REVOKE_ENABLED=false` restores today's behaviour.
- **Mixed script versions during an `@stable` rollout:** every record change is additive, and a v1 reader sees a stricter lease (Phase 5 union paths), never a weaker one. The header stays `merge-lease v1`, so old readers never hit `read_failed` and fail open.
- **Phase 5 raises concurrent final-CI demand on a pool of about 20 jobs.** The default is 1. Raise to 2 only after one week of Phase 1 metrics shows the median productive hold has not grown under the current load.
- **The auto-merge loophole (D7) stays open.** ACCEPTED: deferred with the trigger condition stated in Non-goals.

## Rollout
- Each phase merges with defaults that are safe on its own: Phase 1 is log-only; Phases 2 and 3 default on; Phase 4 defaults on; Phase 5 defaults to 1 holder.
- **After Phase 1**, record the baseline KPIs for one week from `MERGE_LEASE` release and grant lines, `MERGE_LEASE_QUEUE` lines, and the `refs/merge-lease/<base>` record chain:
  - K1: wasted-hold share (released with `merged=false` / total held), baseline 69% over 41 h.
  - K2: p50 and p90 waiter wait (first to last appearance in `MERGE_LEASE_QUEUE order=`), baseline waits of 0h25 to 22h for the 12 waiters on 2026-11-19.
  - K3: merges per day under the lease, baseline about 2.3 (4 in 41 h).
  - K4: branch updates per merged PR (`MERGE_BASE_SYNC … outcome=accepted` per merged PR), baseline 2 for #9666.
- **Acceptance targets:**
  - Phase 2: K1 ≤ 15%.
  - Phase 3: forward-merge PRs wait behind at most the current holder(s).
  - Phase 4: on a queue like today's, at least half of the waiters merge without a lease grant, and K4 does not rise.
  - Phase 5 at 2 holders: K3 rises with no rise in K4 and no rise in the median final-CI duration.
- **Raise `MERGE_LEASE_MAX_HOLDERS` to 2** with the repo variable only after the Phase 5 merge and one week of the K1-K4 readings.
- **Rollback:** each phase's kill switch is listed in Phases & Merge Strategy; reverting any one PR does not disturb the others.
- **Consumers:** every phase reaches each repo in `.github/ai/consumer_repos.json` running v1.47.4 or later on its next `@stable` sync, with the same defaults. Consumers can opt out per repo with the same variables.

## References
- #9660 (merge lease, merged in aafc04a2; project issue #9650) and its predecessor #9642.
- `scripts/pr_checks_lib.sh` "Merge lease" header (1985-2042) and "Final-CI merge resume".
- `scripts/review_merge_train.sh` `_mt_merge_lease_tick`, `_mt_conflict_probe` (issue #6570).
- README rows `MERGE_LEASE_*` (151-155), `MERGE_TRAIN_IGNORE_PATHS` and `MERGE_TRAIN_CONFLICT_CHECK_ENABLED` (218-220), "Promote cycle callbacks" (12f).
- `agents.d/merge-lease-final-ci-resume.md` (the `MERGE_LEASE` log prefix contract).
- PRs in the measured queue: #9677, #9683, #9685, #9693, #9698, #9699, #9705, #9709, #9711, #9723, #9725, #9735, #9755; the race slip #9727 and #9666.
