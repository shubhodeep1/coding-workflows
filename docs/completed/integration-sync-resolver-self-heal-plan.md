# Integration-Sync Resolver Self-Heal — Implementation Plan

> Status: archived completed-plan copy.
> This is the canonical completed-plan source for the full closeout: Phases 1-5 shipped; Phase 6 landed as a discovery-only NO-GO (see [`sub-issue-test-runs-spike.md`](sub-issue-test-runs-spike.md)).

> Source design: [`integration-sync-resolver-self-heal.md`](integration-sync-resolver-self-heal.md).
> This archived plan originally converted that draft proposal into an executable,
> multi-phase rollout covering everything in §5 (v1) and all seven items in §10.
> The codebase now reflects Phases 1-5 across follow-up PRs; Phase 6 closed with
> the discovery-only NO-GO report linked above.

---

## Closeout summary

- Phase 1A shipped the baseline/delta verifier flow.
- Phase 1B shipped `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` for the resolver safety perimeter.
- Phases 2-5 later shipped the resolver escape valve, tier ladder, adaptive quarantine + drift audit, and last-resort branch rebuild.
- Phase 6 ended as discovery-only / NO-GO; fingerprints were **not** replaced with sub-issue test runs.
- `docs/completed/integration-sync-resolver-self-heal-plan.md` is the canonical full archive; `docs/plans/integration-sync-resolver-self-heal-plan.md` is retained only as a short pointer for link stability.
- The remaining sections below preserve the original rollout plan text for archival context; use the shipped code plus `README.md` / `agents.md` for exact final semantics where implementation details diverged.

---

## Original rollout summary

Ship pre/post-resolve delta verification + main-snapshot bootstrap of safety
scripts (v1) to unwedge integration-sync resolver loops where the root cause is
pre-existing fingerprint drift, then layer the consecutive-failure escape valve,
graduated verification tiers, adaptive fingerprint quarantine, drift audit job,
last-resort branch rebuild, and (as a separate large effort) sub-issue test-run
gating on top so every documented failure mode in §10 of the source doc has a
landed implementation path.

---

## Context

### What is wedged today

PR #1569 (head `orchestrator/project-1469`) accumulated 18+ identical
`**AI review/autofix failed — needs human intervention**` comments in ~11h
because `verify_integration_fingerprints.py` does a *whole-tree, absolute* check
post-resolve. Any pre-existing fingerprint that was already failing on the
integration branch *before* the resolver ran (e.g. the #1519-class contradictory
`must_contain`/`must_not_contain` regression on issue #1519's capture, partially
fixed by `32c0ce0` on `main` but unable to reach the stuck branch) hard-fails
every resolver attempt forever. The orchestrator stall poller
(`internal-orchestrate-poll.yml`, `*/5 * * * *`) re-kicks the resolver via
`workflow_dispatch`; same tree → same failure → same generic comment.

The clean-sync path is **already** verifier-decoupled
(`scripts/orchestrate_poll_process.sh:2724-2727` calls GitHub's
`repos/{repo}/merges` API directly, no verifier in the loop). Only the
*conflict* path (HTTP 409 from the merges API) gates on the verifier.

A compounding issue: `review_autofix.yml`'s bootstrap walks
`.codex-workflow-src/scripts/${f}` first and falls back to
`.codex-workflow-src-main/scripts/${f}` only on miss. A stuck branch keeps
running its own (older) `verify_integration_fingerprints.py` and
`review_conflict_resolve.sh` even after `main` ships fixes — so even when the
underlying capture bug gets patched on `main`, the patch cannot reach the stuck
branch unless an operator merges `main` in.

### Stale line numbers in the source doc

The source doc cites line numbers from an older revision of the repo. Current
anchors at HEAD (verified during plan drafting):

| Item | Doc citation | Current location |
| --- | --- | --- |
| `verify()` definition | `verify_integration_fingerprints.py:188-325` | `scripts/verify_integration_fingerprints.py:382` (function header) |
| `main()` arg parsing | `verify_integration_fingerprints.py:328-358` | `scripts/verify_integration_fingerprints.py:529` |
| `REQUIRED_BOOTSTRAP_SCRIPTS` | `review_autofix.yml:455-475` | `.github/workflows/review_autofix.yml:945` |
| `OPTIONAL_BOOTSTRAP_SCRIPTS` first declaration | `review_autofix.yml:468` | `.github/workflows/review_autofix.yml:959` |
| `_verify_fingerprints_soft` in resolver wrapper | (not cited) | `scripts/review_conflict_resolve.sh:612-660` |
| Second verifier callsite in resolver wrapper | (not cited) | `scripts/review_conflict_resolve.sh:1156-1170` |
| `sync_default_into_integration_branch` clean-sync API call | `orchestrate_poll_process.sh:2631,2724-2727` | `scripts/orchestrate_poll_process.sh:2574-2592` region (function is still resident — verified by `grep`) |
| `heal_integration_branch_conflict` | `orchestrate_poll_process.sh` (not cited) | `scripts/orchestrate_poll_process.sh:3373` |

Each implementation step below uses the **current** line numbers. Implementers
should re-verify with `grep -n` before editing because the repo moves quickly.

### Why "phase the whole doc" rather than just ship v1

The user explicitly requested ("dont defer anything") that the executable plan
cover every §10 item. Each phase below is independently shippable and reviewable;
Phase 1 carries the on-call value (unwedges PR #1569 and any future PR with the
same failure pattern); Phases 2-5 progressively raise the system's ceiling so a
*resolver-introduced* loop or capture-side regression cannot reproduce the same
"forever-loop with no escape" pattern; Phase 6 (§10 #7) is a much larger
architectural rewrite that displaces fingerprint-based gating with sub-issue test
runs and explicitly requires its own discovery/spike before implementation steps
can be made concrete.

---

## Goals

### Phase 1 — v1 (ships in this PR's implementation)

- **G1.1** A fingerprint that was already failing on the integration branch
  *before* the resolver ran no longer blocks the `[ai-merge-resolve]` commit.
  Resolver-introduced regressions (baseline-passing → post-resolve-failing) still
  hard-fail with byte-identical error wording. Verified by a new pytest module
  with four classification cases.
- **G1.2** Fixes shipped to `verify_integration_fingerprints.py`,
  `review_conflict_resolve.sh`, and `review_conflict_prepare.sh` on `main` reach
  every running branch on its next bootstrap regardless of the branch's own
  copy. Verified by a workflow-test that asserts SHA equality between the
  installed copy and the `main`-snapshot copy when both checkouts have the
  script.
- **G1.3** Every pre-existing-drift fingerprint that is excused from the
  hard-fail surfaces as a structured `::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1`
  log line so a later audit job (Phase 4) can consume it. Verified by grepping
  the verifier's stdout in the new pytest module.
- **G1.4** Net `gh api` / `gh_retry` / `_safe_gh_jq` calls **unchanged or
  reduced** per resolver run (CLAUDE.md §15). Verified by `git diff` grep for
  added call sites.
- **G1.5** Default-mode behaviour of `verify_integration_fingerprints.py` is
  byte-identical for any caller that does not pass the new flags (CLAUDE.md §6
  naming immutability). Verified by re-running the existing test suite without
  modification.
- **G1.6** PR #1569's failing tree from run `24872524074` (314/317 satisfied,
  3 pre-existing must_contain failures on issue #1519) produces an
  `[ai-merge-resolve]` commit under the new verifier and the PR's
  `mergeable_state` transitions `dirty → mergeable` on the next sync tick.
  Verified by reproducing the failing tree in a pytest fixture.

### Phase 2 — Escape valve + state comment (§10 #1 + #2)

- **G2.1** After **N = 5** consecutive identical-signature failures on the same
  head SHA, the orchestrator stops re-kicking the resolver for that PR, applies
  `ai:resolver-escalated`, posts one summary comment, fires one `tg_notify`
  alert, and writes a structured `<!-- AUTOFIX_RESOLVER_RETRY_STATE_V1 ... -->`
  comment that captures the failure signature, drift counts, and regressed set.
- **G2.2** A single in-place `AUTOFIX_RESOLVER_RETRY_STATE_V1` comment per PR
  is created/updated on every resolver run (not appended), replacing the 18-
  identical-comments noise pattern from PR #1569.

### Phase 3 — Graduated verification tiers (§10 #3)

- **G3.1** Verifier supports tier sequence `strict` (current) → `ratio`
  (must_contain satisfaction ≥ 95%) → `count_only` → `warn_only`, with each
  tier downgrade unlocked after N consecutive failures of the previous tier.
- **G3.2** Tier downgrades emit a structured
  `FINGERPRINT_TIER_DOWNGRADED_V1 from=<prev> to=<next> reason=<...>` marker
  consumed by the drift audit job (Phase 4) and the per-PR state comment
  (Phase 2).

### Phase 4 — Adaptive quarantine + drift audit job (§10 #4 + #5)

- **G4.1** A fingerprint classified `PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged`
  for ≥ **M = 3** consecutive runs is added to a per-issue quarantine list and
  skipped by the verifier; emits a one-time `FINGERPRINT_QUARANTINED_V1` marker.
- **G4.2** A scheduled workflow (`workflow-templates/drift-audit/`) scans recent
  workflow logs for `PRE_EXISTING_FINGERPRINT_DRIFT_V1` and
  `FINGERPRINT_QUARANTINED_V1` markers, deduplicates by `fp_key`, and opens a
  tracker issue per persistent drift cluster with the suspected capture-side
  root cause.
- **G4.3** Quarantine list is persisted on the `ai-memory` git branch under a
  new `fingerprint_quarantine.v1.json` schema; the audit job periodically
  re-evaluates entries against the sub-issue's actual PR diff.

### Phase 5 — Last-resort branch rebuild (§10 #6)

- **G5.1** After **M-hours = 24h** of continuous failure on the same head SHA
  *and* Phase 2's escape valve has fired, the orchestrator deletes the stuck
  PR branch and recreates it from current `main`, replaying merged sub-PRs in
  order.
- **G5.2** Per-branch rebuild-rate cap (max 1 rebuild per branch per 48h)
  prevents rebuild-loops. Audit log entry written on every rebuild.

### Phase 6 — Sub-issue test runs (§10 #7)

- **G6.1** A discovery/spike lands first to assess feasibility — the source
  doc itself flags this as "Unrelated rewrite; mentioned for completeness." Goal
  6.1 is a written discovery report (`docs/plans/sub-issue-test-runs-spike.md`)
  that decides whether to commit to G6.2+ or close it as not-worth-it.
- **G6.2** (conditional on G6.1) Replace regex-based intent capture with "did
  the sub-issue PR's added tests still pass on the integration tree?" as the
  gate. Per-tier rollout: opt-in per sub-issue → opt-in per repo → default-on.

---

## Non-goals

- **PR #1569 one-time migration nudge** (source doc §7 #7). Explicitly out of
  scope per the clarification round. If PR #1569 has not unblocked by the time
  Phase 1 lands on `main`, the operator handles it manually.
- **Cleanup of the 18 stale "AI review/autofix failed" comments on PR #1569**
  (source doc §7 #5). Cosmetic; out of scope.
- **Replacing or rewriting any existing fingerprint-capture path beyond Phase
  6's opt-in path.** Phase 1-5 preserve the current capture flow.
- **Renaming or removing any existing identifier.** CLAUDE.md §6 bans this;
  all additions are alongside existing names.
- **MongoDB or DB contract changes.** No `/db/contracts/*` are touched. The
  only new persistent store is on the `ai-memory` git branch.

---

## Constraints

- **§0 Prime Directive / §2 Always-On Ask-First.** Two clarification rounds
  resolved the open questions before drafting. The remaining `CONFIRM` items
  (M for §10 #4, M-hours for §10 #6) are surfaced in [Open Questions](#open-questions)
  and must be confirmed before the relevant phase begins.
- **§4 Env-var defaults.** All new env vars ship with defaults: `N=5`,
  `M=3`, `M-hours=24`, `REBUILD_COOLDOWN_HOURS=48`. Operators override
  per-consumer via repo variables.
- **§5 Minimal change set.** Default verifier mode is byte-identical for
  callers that don't opt in to new flags. Existing test suite passes
  unmodified.
- **§6 Naming immutability.** New labels (`ai:resolver-escalated`,
  `ai:fingerprint-quarantined`), env vars (`RESOLVER_ESCAPE_THRESHOLD_N`,
  `FINGERPRINT_QUARANTINE_RUNS_M`, `BRANCH_REBUILD_THRESHOLD_HOURS`,
  `BRANCH_REBUILD_COOLDOWN_HOURS`), state keys
  (`AUTOFIX_RESOLVER_RETRY_STATE_V1`, `FINGERPRINT_QUARANTINED_V1`,
  `FINGERPRINT_TIER_DOWNGRADED_V1`, `BRANCH_REBUILD_AUDIT_V1`), and CLI flags
  (`--baseline-fingerprints-state`, `--compare-against-baseline`,
  `--verification-tier`) are all additive. Nothing renamed.
- **§7 Output.** Every phase's PR description lists files changed with line
  ranges and updates `README.md` / `agents.md` for new env vars and modes.
- **§9 Code style.** Tabs for Python and shell; 2-space for YAML; opening
  braces on a new line.
- **§10 (MongoDB).** N/A — repo has no `/db/contracts/*` and no collections
  are touched. The pattern's spirit (never silently relax a safety check,
  always emit a structured warning) is honored throughout.
- **§14 Consumer repo registry.** Phases 1, 2, 4, and 5 touch
  `.github/workflows/review_autofix.yml` and `scripts/*` that propagate to
  consumer repos via the existing `mark-stable.sh` + `repository_dispatch`
  flow. No consumer wrapper YAML changes; existing
  `.github/ai/consumer_repos.json` covers propagation.
- **§15 GitHub API hygiene.** Phase 1 is **zero new `gh api` calls** (pure
  local file I/O). Phase 2's state comment uses one *upsert* per resolver run
  (replaces 18-comments noise pattern — net reduction). Phases 3-5 reuse the
  orchestrator's existing per-PR `gh pr view` / `gh run list` calls; any new
  call site must justify the call surface and document the batching contract
  per §15.

---

## Approach

### Phase 1 — v1

Two **additive, narrowly-scoped** changes:

1. **`scripts/verify_integration_fingerprints.py`** — add `--baseline-fingerprints-state <out>` (capture-mode write) and `--compare-against-baseline <in>` (verify-mode read). The resolver wrapper captures fingerprint pass/fail state *before* it runs codex; after codex writes its tree, the verifier compares against the baseline and only hard-fails on `regressed_by_resolver` (baseline-passing → current-failing). Pre-existing drift becomes a structured `::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1` and does not block the commit. Default mode (no flags) is byte-identical.
2. **`.github/workflows/review_autofix.yml`** — add a third bootstrap list `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` whose lookup order is `.codex-workflow-src-main/scripts/${f}` first, branch copy fallback. Initial members: `verify_integration_fingerprints.py`, `review_conflict_resolve.sh`, `review_conflict_prepare.sh`. The same diff **removes** `verify_integration_fingerprints.py` from `OPTIONAL_BOOTSTRAP_SCRIPTS` (currently at `.github/workflows/review_autofix.yml:959`) to avoid last-writer-wins overwriting the main-snapshot copy.

### Phase 2 — Escape valve + state comment

- Add a `<!-- AUTOFIX_RESOLVER_RETRY_STATE_V1 ... -->` HTML-comment-delimited block to the PR body (or a dedicated sticky comment) written by `review_autofix.yml` after each resolver run. Schema captures `head_sha`, `consecutive_failure_count`, `last_failure_signature` (sha256 of the verifier's sorted regressed set + drift set), `last_pre_existing_drift_count`, `last_regressed_by_resolver` (truncated list). Update in place via existing `post_review_comment.sh` helper pattern with a sticky-marker grep — no new helper.
- When the comment's `consecutive_failure_count` reaches `RESOLVER_ESCAPE_THRESHOLD_N` (default `5`), `review_autofix.yml`'s failure step applies `ai:resolver-escalated`, posts one human-readable summary, fires `tg_notify`, and writes an `integration_sync_status="escalated"` value to the orchestrator state file so `scripts/orchestrate_poll_process.sh:3373`'s `heal_integration_branch_conflict` stops re-dispatching.

### Phase 3 — Graduated verification tiers

- Add `--verification-tier <strict|ratio|count_only|warn_only>` to `verify_integration_fingerprints.py`. Default `strict` (current behaviour).
- Add a tier-selection helper in `scripts/review_conflict_resolve.sh` that reads the per-PR state comment's `consecutive_failure_count` and selects the next tier per `tier_ladder = [strict, ratio, count_only, warn_only]` with each tier unlocking after `RESOLVER_ESCAPE_THRESHOLD_N` consecutive failures of the previous tier. The escape valve from Phase 2 fires only after `warn_only` itself fails.
- Tier downgrades emit `FINGERPRINT_TIER_DOWNGRADED_V1` markers and are recorded in the per-PR state comment.

### Phase 4 — Adaptive quarantine + drift audit job

- Persist a per-issue quarantine list on the `ai-memory` git branch under `fingerprint_quarantine.v1.json` (schema versioned per CLAUDE.md §6 / existing AI-memory schema conventions). Each entry: `fp_key`, `issue_key`, `first_seen_run_id`, `last_seen_run_id`, `consecutive_unchanged_runs`.
- `verify_integration_fingerprints.py` reads the quarantine list at startup, skips quarantined `fp_key`s in both capture and compare modes, and emits a one-time `FINGERPRINT_QUARANTINED_V1 ...` marker on first skip.
- New scheduled workflow `.github/workflows/drift-audit.yml` (cron: daily at 03:00 UTC) scans recent run logs via `gh run list` + `gh run view --log`, aggregates `PRE_EXISTING_FINGERPRINT_DRIFT_V1` markers by `fp_key`, opens or updates a tracker issue per persistent cluster, and re-evaluates quarantined entries against the sub-issue's actual PR diff (uses GraphQL `closingIssuesReferences` per CLAUDE.md §15).

### Phase 5 — Last-resort branch rebuild

- Extend `scripts/orchestrate_poll_process.sh:3373`'s `heal_integration_branch_conflict` with a `_check_branch_rebuild_threshold` helper that fires when:
  1. `integration_sync_status="escalated"` (Phase 2's escape valve has fired), AND
  2. `now - escalated_at > BRANCH_REBUILD_THRESHOLD_HOURS` (default `24h`), AND
  3. `now - last_rebuild_at > BRANCH_REBUILD_COOLDOWN_HOURS` (default `48h`).
- Rebuild path: snapshot branch state into `branch_rebuild_audit.v1.json` on `ai-memory`, delete remote ref, recreate from `main`, replay merged sub-PR commits in the order recorded in the orchestrator state file. Use `gh api` `repos/{repo}/git/refs/heads/{branch}` for delete (no force-push to a protected branch). Fail-open: if any replay step fails, leave the branch in `branch_rebuild_failed` state and Telegram-alert.

### Phase 6 — Sub-issue test runs (discovery first)

- Discovery report deliverable: `docs/plans/sub-issue-test-runs-spike.md` answering:
  1. What fraction of merged sub-issues have a PR that adds runnable tests?
  2. What is the wall-clock cost of running those tests on the integration tree?
  3. What is the false-positive / false-negative rate vs the current
     fingerprint check on a sample of historical merges?
  4. Recommended go/no-go.
- If go: incremental rollout per `g6.2` (opt-in per sub-issue → opt-in per repo
  → default-on). Implementation steps for G6.2 will be planned in a follow-up
  plan after the spike lands.

---

## Implementation Steps

### Phase 1 — v1 (single PR, five commits)

#### Commit 1: `verify_integration_fingerprints.py` — add baseline capture + delta verification

- **File:** `scripts/verify_integration_fingerprints.py` (current size: 579 lines)
- **Changes:**
  1. Refactor inner regex test into a single kind-aware helper
     `_fp_satisfied(fp, file_cache, kind) -> bool` (where `kind ∈ {"must_contain", "must_not_contain"}`). Replace inline regex calls in `verify()` (`scripts/verify_integration_fingerprints.py:382` onwards), `list_violated_files()`, and any other satisfaction check so there is exactly one source of truth.
  2. Add `--baseline-fingerprints-state <out_path>` capture mode (writes the JSON schema documented in source doc §5.1.1; exit 0 unconditionally; fail-open on IO error with `::warning::baseline capture failed: <err>`).
  3. Add `--compare-against-baseline <baseline_path>` compare mode. Asymmetric key handling per source doc §5.1.3 step 1: extra baseline keys ignored, extra current keys fall back to absolute check per fingerprint. Classify each fingerprint into `still_passing` / `newly_fixed` / `pre_existing_drift` / `regressed_by_resolver`. Hard-fail only on `regressed_by_resolver`. Emit `::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged ...` for `pre_existing_drift` and `::notice::PRE_EXISTING_FINGERPRINT_DRIFT_V1 fixed_by_resolver ...` for `newly_fixed`.
  4. Add mutual-exclusion guard at `main()` (`scripts/verify_integration_fingerprints.py:529`) — both flags supplied → exit 2 with explicit error message.
  5. Add malformed-baseline fall-through: compare mode that fails JSON parse or `schema_version != 1` emits `::warning::baseline malformed (...); using absolute verification.` and dispatches to existing `verify()`.
- **Preconditions:** None.
- **No new tests in this commit** — they land in Commit 4 alongside the fixture data.

#### Commit 2: `scripts/review_conflict_resolve.sh` — wire pre/post-resolve verifier calls

- **File:** `scripts/review_conflict_resolve.sh` (current size: 1481 lines)
- **Changes:**
  1. Pre-resolve: before the existing codex invocation, add a baseline-capture call:
     ```bash
     baseline_state="${RUNTIME_DIR}/baseline_fp_state.json"
     if ! python3 "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" \
         --baseline-fingerprints-state "${baseline_state}" \
         "${INTEGRATION_FINGERPRINTS_FILE}"; then
       echo "::warning::baseline capture unavailable; falling back to absolute fingerprint verification."
       baseline_state=""
     fi
     ```
  2. Post-resolve (replacing the existing absolute verifier call at `scripts/review_conflict_resolve.sh:638-660` and the second callsite at `:1156-1170`):
     ```bash
     if [ -n "${baseline_state}" ] && [ -s "${baseline_state}" ]; then
       python3 "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" \
         --compare-against-baseline "${baseline_state}" \
         "${INTEGRATION_FINGERPRINTS_FILE}"
     else
       python3 "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" \
         "${INTEGRATION_FINGERPRINTS_FILE}"
     fi
     ```
  3. Fail-closed on the safety check itself (a non-zero exit prevents the `[ai-merge-resolve]` commit) — fail-open *only* on the baseline-capture path.
- **Preconditions:** Commit 1 must land first.
- **Verification:** `git diff scripts/review_conflict_resolve.sh | grep -E 'gh (api|_retry)|_safe_gh_jq'` returns no additions (CLAUDE.md §15).

#### Commit 3: `.github/workflows/review_autofix.yml` — add `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS`

- **File:** `.github/workflows/review_autofix.yml` (current size: 5961 lines)
- **Changes:**
  1. Add `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS="verify_integration_fingerprints.py review_conflict_resolve.sh review_conflict_prepare.sh"` immediately after the `REQUIRED_BOOTSTRAP_SCRIPTS` declaration at `.github/workflows/review_autofix.yml:945`.
  2. Add the main-primary bootstrap loop matching the shape in source doc §5.2.2, *after* the existing required-bootstrap loop and *before* the optional-bootstrap loop.
  3. **Remove** `verify_integration_fingerprints.py` from `OPTIONAL_BOOTSTRAP_SCRIPTS` at `.github/workflows/review_autofix.yml:959` (this is the §5.2.1 invariant — overlap would silently overwrite the main-snapshot copy). `OPTIONAL_BOOTSTRAP_SCRIPTS` becomes `"install_semble.sh build_semble_wrapper.sh semble_helpers.sh"` on the same line that previously declared the verifier.
  4. Bootstrap loop emits `::notice::Bootstrapped ${f} from main snapshot (branch copy ... ignored)` when main snapshot wins over a present branch copy, and `::warning::main snapshot for ${f} unavailable; falling back to branch copy` on main-snapshot miss.
- **Preconditions:** Commits 1 + 2 land first so the main-snapshot copy of the verifier already supports the new modes by the time the bootstrap inversion ships.

#### Commit 4: `tests/test_verify_integration_fingerprints_baseline.py` — new pytest coverage

- **File:** `tests/test_verify_integration_fingerprints_baseline.py` `[new]`
- **Test cases** (matching source doc §6.1):
  1. **Capture mode happy path** — `--baseline-fingerprints-state /tmp/baseline.json fingerprints.json` writes JSON with `schema_version=1`, expected fingerprints map; exit 0.
  2. **Capture mode error path** — unwritable output path → `::warning::baseline capture failed: ...` → exit 0.
  3. **Compare — pre-existing drift passes** — baseline `K satisfied=false`, current `K satisfied=false` → exit 0, stdout contains `PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged fp_key=K`.
  4. **Compare — resolver-introduced regression fails** — baseline `K satisfied=true`, current `K satisfied=false` → exit 1, stdout contains the existing `Integration fingerprint verification FAILED — resolver output regressed` and `Refusing to create [ai-merge-resolve] commit.` wording.
  5. **Compare — newly fixed** — baseline `K satisfied=false`, current `K satisfied=true` → exit 0, stdout contains `PRE_EXISTING_FINGERPRINT_DRIFT_V1 fixed_by_resolver`.
  6. **Compare — fingerprint absent from baseline** — current set has issue not in baseline → falls through to absolute check for that fingerprint.
  7. **Mode-conflict guard** — both flags supplied → exit 2 with exact error.
  8. **Malformed baseline JSON** — file fails JSON parse → `::warning::baseline malformed`, falls through to absolute check.
  9. **PR #1569 regression** — fixture with the failing tree from run `24872524074` (314/317 satisfied, 3 pre-existing failures on #1519) → exit 0 under compare mode; exit 1 under default mode (proves the v1 fix unwedges the actual incident).
- **Fixtures:** add `tests/fixtures/integration_fingerprints/pr1569_run_24872524074.json` capturing the relevant subset of the failing fingerprints input.
- **Preconditions:** Commits 1-3 land first.

#### Commit 5: docs — `README.md`, `agents.md`, source doc cross-link

- **Files:**
  - `README.md` — add a section under the existing "Integration sync resolver" prose documenting `--baseline-fingerprints-state` / `--compare-against-baseline` flags, `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` rationale, and the `PRE_EXISTING_FINGERPRINT_DRIFT_V1` marker.
  - `agents.md` — append-only entries per CLAUDE.md §6: one-liner under the existing `verify_integration_fingerprints.py` anchor for the new flags, one-liner under the `review_autofix.yml` anchor for the new bootstrap list.
  - `docs/integration-sync-resolver-self-heal.md` — add a header note: "Status: Phase 1 SHIPPED in PR #<this-PR>. Phase 2-6 tracked in `docs/plans/integration-sync-resolver-self-heal-plan.md`."
- **Preconditions:** Commits 1-4 land first so the README's example commands are valid against the shipped code.

### Phase 2 — Escape valve + state comment (follow-up PR)

1. Add `RESOLVER_ESCAPE_THRESHOLD_N=5` env var (with default) read by `review_autofix.yml`.
2. Add a `_write_resolver_retry_state` shell function inside `.github/workflows/review_autofix.yml`'s `Run Codex resolver, validate, stage, commit` step that:
   - Computes `failure_signature_sha256` over the sorted union of `regressed_by_resolver` `fp_key`s + `pre_existing_drift` `fp_key`s.
   - Reads the existing `AUTOFIX_RESOLVER_RETRY_STATE_V1` block (if any) from the PR body via one existing `gh api` call (no new call site).
   - Increments `consecutive_failure_count` if `failure_signature_sha256` matches the previous run on the same `head_sha`; otherwise resets to 1.
   - Writes the updated block back via the existing `gh pr edit --body` path used by other in-band PR-body updates.
3. When `consecutive_failure_count >= RESOLVER_ESCAPE_THRESHOLD_N`:
   - Apply `ai:resolver-escalated` (new label; pre-create per `tests/test_ai_label_precreation_contract.py` conventions).
   - Post one summary comment using the existing `post_review_comment.sh` helper.
   - Fire `tg_notify` via `scripts/tg_helpers.sh`.
   - Set `integration_sync_status="escalated"` in the orchestrator state file so `scripts/orchestrate_poll_process.sh:3373` stops re-dispatching.
4. Add unit test `tests/test_resolver_retry_state.py` covering signature stability, increment, reset on signature change, and escape-threshold trigger.

### Phase 3 — Graduated verification tiers (follow-up PR)

1. Add `--verification-tier <strict|ratio|count_only|warn_only>` flag to `verify_integration_fingerprints.py`. Default `strict`.
2. Implement tier semantics:
   - `strict` — current behaviour (delta or absolute as already decided in Phase 1).
   - `ratio` — pass when `len(passing_must_contain) / total_must_contain >= 0.95` per issue.
   - `count_only` — pass when at least one `must_contain` passes per issue.
   - `warn_only` — always pass; emit `FINGERPRINT_TIER_WARN_ONLY_V1` marker.
3. Add a tier-selection helper in `scripts/review_conflict_resolve.sh` that reads the Phase 2 state comment's `consecutive_failure_count` and selects the next tier per the `tier_ladder` mapping `0 → strict`, `N → ratio`, `2N → count_only`, `3N → warn_only`.
4. Each tier downgrade emits `FINGERPRINT_TIER_DOWNGRADED_V1 from=<prev> to=<next> reason=<...>` marker.
5. Phase 2's escape valve threshold is moved to "after `warn_only` itself fails N consecutive times" (so the escape valve becomes the ladder's terminus, not a parallel path).
6. New tests: `tests/test_verify_integration_fingerprints_tiers.py` covering each tier's pass/fail semantics and downgrade marker emission.

### Phase 4 — Adaptive quarantine + drift audit job (follow-up PR)

1. Define `fingerprint_quarantine.v1.json` schema on the `ai-memory` branch (single document per repo): `{schema_version: 1, entries: [{fp_key, issue_key, first_seen_run_id, last_seen_run_id, consecutive_unchanged_runs}]}`.
2. Add a `_load_quarantine_list` / `_persist_quarantine_list` pair in `scripts/ai_memory.py` (extends existing AI-memory CLI; no new tool).
3. Extend `verify_integration_fingerprints.py` to:
   - Load quarantine list at startup (fail-open if AI-memory branch unreachable).
   - Skip quarantined `fp_key`s in both capture and compare modes.
   - Emit `FINGERPRINT_QUARANTINED_V1 fp_key=<...> issue=#<N>` on first skip per run.
4. Extend Phase 1's compare-mode classifier to increment `consecutive_unchanged_runs` on `pre_existing_drift` entries via the existing `_persist_quarantine_list` path; promote to quarantine when `consecutive_unchanged_runs >= FINGERPRINT_QUARANTINE_RUNS_M` (default `M=3`).
5. New workflow `.github/workflows/drift-audit.yml` (cron: `0 3 * * *`):
   - Scan last 24h of workflow runs via `gh run list --workflow review_autofix.yml --json databaseId,conclusion,createdAt` (batched per CLAUDE.md §15).
   - Fetch logs via `gh run view --log` (paginated; cache by run-id on `ai-memory` so a second audit job run skips already-processed runs — CLAUDE.md §15 mandates this batching pattern).
   - Aggregate `PRE_EXISTING_FINGERPRINT_DRIFT_V1` and `FINGERPRINT_QUARANTINED_V1` markers by `fp_key`.
   - Open or update one tracker issue per persistent cluster via `mcp__github__issue_write` (sticky-marker comment pattern).
6. New tests: `tests/test_fingerprint_quarantine.py` (quarantine schema round-trip), `tests/test_drift_audit_job.py` (marker aggregation, dedup, issue-upsert mocking).

### Phase 5 — Last-resort branch rebuild (follow-up PR)

1. Add env vars: `BRANCH_REBUILD_THRESHOLD_HOURS=24`, `BRANCH_REBUILD_COOLDOWN_HOURS=48`.
2. Add `_check_branch_rebuild_threshold` helper inside `scripts/orchestrate_poll_process.sh` (near `heal_integration_branch_conflict` at `scripts/orchestrate_poll_process.sh:3373`).
3. Define `branch_rebuild_audit.v1.json` schema on the `ai-memory` branch capturing pre-rebuild branch state: head SHA, list of replayed sub-PR commit SHAs in order, rebuild trigger reason, success/failure outcome.
4. Rebuild flow:
   - Snapshot branch state into `branch_rebuild_audit.v1.json`.
   - Delete remote ref via `gh api -X DELETE repos/{repo}/git/refs/heads/{branch}` (refuse if branch is protected — fail-open with Telegram alert).
   - Recreate from `main` via `gh api -X POST repos/{repo}/git/refs` with `ref=refs/heads/{branch}`, `sha=<main-head>`.
   - Replay merged sub-PR commits in order recorded in the orchestrator state file (`merged_pr_commits` array).
   - On any replay failure: set `integration_sync_status="branch_rebuild_failed"`, Telegram-alert, do not retry.
5. Rebuild rate cap enforced by reading `last_rebuild_at` from the audit document; refuse if `now - last_rebuild_at < BRANCH_REBUILD_COOLDOWN_HOURS`.
6. New tests: `tests/test_branch_rebuild.py` covering threshold trip, cooldown enforcement, audit document round-trip, replay failure path.

### Phase 6 — Sub-issue test runs (discovery first; implementation in follow-up plan)

1. Land discovery report `docs/plans/sub-issue-test-runs-spike.md` answering the four feasibility questions in G6.1.
2. Decision gate: go/no-go review by repo owner.
3. **If go:** plan G6.2 in a separate `docs/plans/sub-issue-test-runs-implementation-plan.md` (out of scope for this plan).
4. **If no-go:** close out this phase with a "decided not to pursue" entry in the discovery report.

---

## Files & Modules

### Phase 1

- `scripts/verify_integration_fingerprints.py` — edit (refactor `_fp_satisfied`, add capture + compare modes, mode-conflict guard, malformed-baseline fall-through)
- `scripts/review_conflict_resolve.sh` — edit (pre-resolve baseline capture call + post-resolve compare-mode call replacing the two existing absolute callsites at `:638-660` and `:1156-1170`)
- `.github/workflows/review_autofix.yml` — edit (add `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` + loop near `:945`; remove `verify_integration_fingerprints.py` from `OPTIONAL_BOOTSTRAP_SCRIPTS` at `:959`)
- `tests/test_verify_integration_fingerprints_baseline.py` `[new]`
- `tests/fixtures/integration_fingerprints/pr1569_run_24872524074.json` `[new]`
- `README.md` — edit (new "Integration sync resolver delta verification" subsection)
- `agents.md` — edit (append-only entries under existing anchors)
- `docs/integration-sync-resolver-self-heal.md` — edit (status header pointing at the shipped Phase 1 PR)

### Phase 2

- `.github/workflows/review_autofix.yml` — edit (`_write_resolver_retry_state` function + escape-threshold trigger)
- `scripts/orchestrate_poll_process.sh` — edit (consume `integration_sync_status="escalated"` near `:3373`)
- `tests/test_resolver_retry_state.py` `[new]`
- New label `ai:resolver-escalated` pre-created via the existing label-precreation contract; update `tests/test_ai_label_precreation_contract.py` and `tests/test_ai_labels.py` fixtures.
- `README.md` — edit (env var table)

### Phase 3

- `scripts/verify_integration_fingerprints.py` — edit (`--verification-tier` flag + per-tier semantics)
- `scripts/review_conflict_resolve.sh` — edit (tier-selection helper)
- `tests/test_verify_integration_fingerprints_tiers.py` `[new]`
- `README.md` — edit (env var table + tier semantics docs)
- `agents.md` — edit (append-only)

### Phase 4

- `scripts/ai_memory.py` — edit (`_load_quarantine_list` / `_persist_quarantine_list` extensions)
- `scripts/ai_memory_lib.py` — edit (schema constants)
- `scripts/verify_integration_fingerprints.py` — edit (quarantine load + skip)
- `.github/workflows/drift-audit.yml` `[new]`
- `scripts/drift_audit.sh` `[new]` (workflow's main runner)
- `tests/test_fingerprint_quarantine.py` `[new]`
- `tests/test_drift_audit_job.py` `[new]`
- `README.md` — edit (drift audit job documentation)
- `agents.md` — edit (drift-audit anchor)

### Phase 5

- `scripts/orchestrate_poll_process.sh` — edit (`_check_branch_rebuild_threshold` + rebuild flow near `:3373`)
- `scripts/ai_memory.py` — edit (`branch_rebuild_audit.v1.json` schema)
- `tests/test_branch_rebuild.py` `[new]`
- `README.md` — edit (env var table + rebuild docs)
- `agents.md` — edit (rebuild anchor)

### Phase 6

- `docs/plans/sub-issue-test-runs-spike.md` `[new]` (discovery report)
- (Follow-up implementation plan opens its own file if go)

---

## Data Model / Index Changes

No MongoDB collections or indexes are touched. Per CLAUDE.md §10 the rule does
not directly apply.

The two new persistent stores both live on the `ai-memory` git branch (existing
infrastructure, managed by `scripts/ai_memory.py`):

- `fingerprint_quarantine.v1.json` (Phase 4)
- `branch_rebuild_audit.v1.json` (Phase 5)

Both ship with `schema_version: 1` and follow the same append-only-via-update
pattern as existing AI-memory record types.

---

## Tests

### Unit tests (per-phase pytest)

| Phase | Test module | Coverage |
| --- | --- | --- |
| 1 | `tests/test_verify_integration_fingerprints_baseline.py` | 9 cases (G1.1, G1.3, G1.5, G1.6) |
| 2 | `tests/test_resolver_retry_state.py` | Signature stability, increment, reset, escape threshold |
| 3 | `tests/test_verify_integration_fingerprints_tiers.py` | Each tier's pass/fail semantics; downgrade markers |
| 4 | `tests/test_fingerprint_quarantine.py`, `tests/test_drift_audit_job.py` | Schema round-trip; marker aggregation; issue upsert mocking |
| 5 | `tests/test_branch_rebuild.py` | Threshold trip; cooldown; audit round-trip; replay failure |

### Integration / end-to-end tests

- Phase 1: workflow-test in CI (existing `.github/workflows/` test wiring) that asserts SHA equality between the installed `verify_integration_fingerprints.py` and the `main`-snapshot copy when both checkouts are present (validates G1.2).
- Phase 2-5: each phase's PR runs the existing review_autofix smoke harness end-to-end against a synthetic stuck-PR fixture.

### Manual verification

- **Phase 1 G1.6:** after the PR lands on `main`, an operator confirms PR #1569's `mergeable_state` transitions `dirty → mergeable` on the next sync tick. (Note: per Non-goals, the operator nudge to propagate the bootstrap change to the stuck branch is out of scope; if needed it is documented in the source doc §7 #7 as a one-time manual step.)
- **Phase 4 G4.2:** scheduled drift-audit job's first run is reviewed manually for false-positive issue creation before letting it run unattended.
- **Phase 5 G5.1:** rebuild path is dry-run validated against a sacrificial test branch before enabling on production integration branches.

---

## Risks & Mitigations

### Phase 1

- **Risk:** Baseline capture failure leaves resolver running absolute check; if the baseline-capture script itself is the bug, the v1 fix is silently disabled. — *Mitigation:* explicit `::warning::baseline capture unavailable; falling back to absolute fingerprint verification.` log line; surfaced in operational telemetry (source doc §8).
- **Risk:** `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` overlap with `OPTIONAL_BOOTSTRAP_SCRIPTS` would silently overwrite the main-snapshot copy. — *Mitigation:* Commit 3 explicitly removes `verify_integration_fingerprints.py` from the optional list in the same diff; covered by a new workflow-test asserting SHA equality.
- **Risk:** A broken `verify_integration_fingerprints.py` shipped to `main` breaks every running branch simultaneously (inverted-bootstrap single point of failure). — *Mitigation:* new pytest module in Commit 4 + existing CI gate on `main` PRs touching this file. *ACCEPTED — same risk pattern as any other bootstrap-script change; mitigated by test coverage, not by architecture.*
- **Risk:** Mode-conflict footgun (both flags supplied silently always passes). — *Mitigation:* explicit exit-2 guard in `main()` (per source doc §5.1.5); covered by pytest case 7.
- **Risk:** Stale line numbers in the source doc → implementer edits the wrong location. — *Mitigation:* this plan's [Context](#context) table lists current line numbers verified at HEAD; each implementation step re-cites current lines.

### Phase 2

- **Risk:** Stale `AUTOFIX_RESOLVER_RETRY_STATE_V1` blocks (e.g. orphaned after force-push) inflate `consecutive_failure_count` falsely. — *Mitigation:* signature comparison is keyed on `head_sha`; a new SHA resets the counter.
- **Risk:** Escape valve fires on transient codex flakes rather than a real wedge. — *Mitigation:* signature-based dedup ensures only *identical-signature* failures count; different signatures reset the counter; N=5 chosen specifically to filter flakes (per clarification round).

### Phase 3

- **Risk:** Tier downgrade to `warn_only` lets a real resolver-introduced regression through. — *Mitigation:* `warn_only` only reached after 3N (default 15) consecutive failures of stricter tiers — at that point the resolver is unambiguously stuck and the alternative is loop-forever; tier downgrade is logged with `FINGERPRINT_TIER_DOWNGRADED_V1` and surfaced in the Phase 2 state comment.

### Phase 4

- **Risk:** Drift audit job opens duplicate tracker issues. — *Mitigation:* sticky-marker comment pattern (existing convention from `post_review_comment.sh`) ensures one issue per `fp_key` cluster; updates rather than recreates.
- **Risk:** Quarantine list grows unbounded. — *Mitigation:* audit job re-evaluates quarantined entries against the sub-issue's actual PR diff and removes resolved entries.
- **Risk:** AI-memory branch becomes the contention point if both review_autofix and the drift audit job write concurrently. — *Mitigation:* `scripts/ai_memory.py` already implements distributed-lock semantics for the AI-memory branch (per CLAUDE.md §10 Operational Safety pattern).

### Phase 5

- **Risk:** Branch rebuild silently loses operator work pushed to the integration branch outside the orchestrator. — *Mitigation:* threshold gated on `integration_sync_status="escalated"` (Phase 2's escape valve has already fired, meaning the resolver has been unambiguously wedged); audit document captures the pre-rebuild HEAD SHA; rebuild rate cap prevents loops. *PARTIALLY ACCEPTED — operator-pushed work on a wedged integration branch is already at risk; rebuild does not make it materially worse but operator should be aware.*
- **Risk:** Rebuild creates a force-push-like effect on consumer-facing branches. — *Mitigation:* never used on protected branches (explicit check); only used on `orchestrator/project-N` integration branches which are orchestrator-owned by convention.

### Phase 6

- **Risk:** Spike concludes "not feasible" and the entire phase is wasted effort. — *ACCEPTED — spike is the cheapest possible discovery vehicle; far less risky than committing to G6.2 directly.*

---

## Rollout

### Phase 1 — single PR, five commits, merged into `main`

- No feature flag; v1 is unconditionally on for any caller using the new flags (and unconditionally off for callers that don't, preserving G1.5).
- Consumer-repo propagation: lands automatically via the existing `mark-stable.sh` + `repository_dispatch` flow after merge. No consumer wrapper YAML changes per CLAUDE.md §14.
- **Rollback:** revert the PR. Default-mode behaviour is preserved because the new flags are additive, so a revert restores the prior behaviour byte-identically for any caller that has not yet adopted the flags. The resolver wrapper change (Commit 2) is the only callsite touched in this PR; reverting it returns the wrapper to the existing absolute check.

### Phase 2

- Behind `RESOLVER_ESCAPE_THRESHOLD_N` env var (default `5`). Set to a very large integer (e.g. `999999`) to disable per-consumer.
- Rollout order: ship to `main`; let it run on `coding-workflows` self-tests for 48h; then propagate to consumer repos.

### Phase 3

- Behind `--verification-tier` flag (default `strict`). Tier ladder is opt-in per resolver invocation; existing callers stay on `strict` until the tier-selection helper from §3.3 is wired up in a follow-up commit within the same PR.

### Phase 4

- Drift audit workflow is opt-in per repo via a `DRIFT_AUDIT_ENABLED` repo variable (default `false`). Enable on `coding-workflows` first; expand after manual review of the first audit cycle (per [Tests](#tests) manual verification).
- Quarantine list is unconditionally read by the verifier; an empty list (the default state) is a no-op.

### Phase 5

- Behind `BRANCH_REBUILD_ENABLED` repo variable (default `false`). Enable on `coding-workflows` first against a sacrificial test branch; expand after dry-run validation.
- Rebuild rate cap (`BRANCH_REBUILD_COOLDOWN_HOURS=48`) is a safety belt.

### Phase 6

- Discovery only; no production rollout.

---

## Open Questions

These survived the clarification rounds and must be answered before the relevant
phase begins. Each is in the same Q-ID format as CLAUDE.md §2.

- **Q-OQ1 (Phase 4):** What value should `FINGERPRINT_QUARANTINE_RUNS_M` default
  to? This plan proposes `M=3` (a fingerprint that has been classified
  `pre_existing_drift unchanged` for 3 consecutive resolver runs is quarantined).
  Confirm before Phase 4 implementation.

- **Q-OQ2 (Phase 5):** What value should `BRANCH_REBUILD_THRESHOLD_HOURS` default
  to? This plan proposes `24h` (after 24h of continuous failure with the escape
  valve fired). Confirm before Phase 5 implementation. Also: should `48h`
  cooldown be tightened or relaxed?

- **Q-OQ3 (Phase 6):** Is the spike timeline acceptable (suggest 1 calendar week
  from when Phase 6 starts), or should the discovery be structured as a
  Time-boxed POC instead?

- **Q-OQ4 (Phase 5):** Should the rebuild path require operator confirmation
  (Telegram approval reply) before deleting the remote ref, or is the
  threshold + cooldown sufficient? This plan defaults to "no operator
  confirmation" matching the doc's "fully unattended self-heal" goal in source
  doc §3 #6, but the operator may prefer a confirmation step.

---

## References

- Source design: [`integration-sync-resolver-self-heal.md`](integration-sync-resolver-self-heal.md)
- Related plan: [`docs/plans/orchestrator-validation-resilience-plan.md`](../plans/orchestrator-validation-resilience-plan.md) — overlapping orchestrator-state and tracking-issue patterns.
- Related plan: [`docs/completed/judge-loop-and-reissue-plan.md`](./judge-loop-and-reissue-plan.md) — judge-loop terminology referenced in Phase 2's escape valve.
- Existing related script: [`scripts/orchestrate_poll_process.sh:3373`](../../scripts/orchestrate_poll_process.sh) (`heal_integration_branch_conflict`).
- Existing related script: [`scripts/verify_integration_fingerprints.py:382`](../../scripts/verify_integration_fingerprints.py) (`verify()`).
- Existing related workflow: [`.github/workflows/review_autofix.yml:945`](../../.github/workflows/review_autofix.yml) (`REQUIRED_BOOTSTRAP_SCRIPTS`).
- CLAUDE.md sections binding this plan: §0, §2, §4, §5, §6, §9, §10 (spirit only), §14, §15.
- Project AI-memory infrastructure: `scripts/ai_memory.py`, `scripts/ai_memory_lib.py`, `scripts/memory_helpers.sh`.
