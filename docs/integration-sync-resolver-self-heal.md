# Integration-Sync Resolver Self-Heal — Pre/Post-Resolve Delta Verification + Main-Snapshot Bootstrap of Safety Scripts

> Status: **Phase 1 shipped on this branch; later phases remain follow-up work.**
> Owner: orchestrator (implementation will be driven by the AI orchestrator pipeline).
> Scope: targeted "safest, partial" fix for the integration-sync resolver loop wedge. Bigger structural options (adaptive fingerprint quarantine, graduated verification tiers, branch rebuild) are deferred to §10 Future Work.

---

## 1. Background

### 1.1 Symptom

PR #1569 (head `orchestrator/project-1469`) accumulated 18+ identical `**AI review/autofix failed — needs human intervention**` comments in a single ~11 hour window (`19:13Z 2026-04-23` → `06:02Z 2026-04-24`). The fallback comment text is generic; every comment was triggered by the `failure()` post-step at `review_autofix.yml:3392-3433`. Inspecting the underlying job logs (runs `24872524074`, `24869836943`, `24868131744` etc.) all show the same failing step:

> `review / codex-agent` → step **"Run Codex resolver, validate, stage, commit"** →
> `::error::Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent`
> `::error::Refusing to create [ai-merge-resolve] commit.`

Concrete pattern counts across the failed runs:

| Run | `must_contain` satisfied | Notable issue cited |
| --- | --- | --- |
| `24872524074` | 314 / 317 | #1519 (PR #1521): 10 patterns appearing in **both** `must_contain` and `must_not_contain` |
| `24869836943` | 313 / 317 | #1508, #1500, #1475 — patterns missing in `scripts/implement_diagnose_post_codex_failure.sh`, `.github/workflows/validate.yml`, `.github/workflows/ci.yml`, `README.md` |

### 1.2 Why this loops forever

The hard-fail in `verify_integration_fingerprints.py:307-319` is a *whole-tree, absolute* check. It compares the post-resolve tree against the *entire* captured `merged_issue_fingerprints` set:

- If **any** `must_contain` pattern is unsatisfied or **any** `must_not_contain` pattern matches, exit 1.
- The resolver wrapper refuses to stage `[ai-merge-resolve]`, no commit is pushed, no new HEAD lands, and `mergeable_state` stays `dirty`.
- The orchestrator stall-poller re-kicks `review_autofix.yml` via `workflow_dispatch` (the cron bypass at `review_autofix.yml:201-205`), with cadence determined by the poll schedule (`internal-orchestrate-poll.yml`, `cron: */5 * * * *`) and further gated by `CONFLICT_DISPATCH_COOLDOWN_SECS` (default `900`s, see `scripts/orchestrate_poll_process.sh:782`) plus the in-flight check at `scripts/orchestrate_poll_process.sh:2564`.
- Same tree → same verifier outcome → same failure → same generic comment. Loop.

### 1.3 What is already decoupled (and what is not)

The **clean** sync path is *already* independent of the verifier:

- `sync_default_into_integration_branch` in `scripts/orchestrate_poll_process.sh:2631` calls GitHub's server-side merge API directly:
  ```text
  scripts/orchestrate_poll_process.sh:2724-2727
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/merges" \
      -f base="${integration_branch}" \
      -f head="${default_branch}" \
      -f commit_message="chore: sync ${default_branch} into ${integration_branch}"
  ```
  On HTTP 200 the `chore: sync …` commit lands and `mark_integration_sync_clean` is called. The verifier never runs on this path.
- Only the **conflict** path (HTTP 409) sets `integration_sync_status="conflict"` (`scripts/orchestrate_poll_process.sh:2503,2518`), which routes through `heal_integration_branch_conflict` → `review_autofix.yml`'s resolver path → `scripts/review_conflict_resolve.sh` → `verify_integration_fingerprints.py`.

So the chicken-and-egg is **not** that "syncing main is gated by the verifier" — it is that **once a conflict exists**, every resolver attempt is gated by a *whole-tree* check, including fingerprints whose paths the resolver never touched and which were already failing on the integration branch *before* the resolver ran (e.g. from a prior capture-side bug like the contradictory `must_contain`/`must_not_contain` patterns in #1519, partially fixed by `32c0ce0` on `main` but unable to reach the stuck branch because… see next paragraph).

### 1.4 Compounding factor — bootstrap order

Per `review_autofix.yml:445-489`, bootstrap walks `REQUIRED_BOOTSTRAP_SCRIPTS` then `OPTIONAL_BOOTSTRAP_SCRIPTS`. For each script the lookup order is:

1. `.codex-workflow-src/scripts/${f}` — the *checked-out branch*'s copy
2. `.codex-workflow-src-main/scripts/${f}` — the `main` snapshot, only consulted on miss

A stuck branch therefore keeps running its **own** (older) `verify_integration_fingerprints.py` and `review_conflict_resolve.sh` even after `main` ships fixes (`32c0ce0`, `aec9b96`, `dca8711` are all on `main` as of `2026-04-24` but cannot reach `orchestrator/project-1469`). The only escape today is operator action.

---

## 2. Non-Negotiable Constraints (from CLAUDE.md — re-read before starting)

1. **Prime Directive / Always-On Ask-First (§0, §2).** Before writing code, batch clarifying Q1/Q2/… using the mandatory format. Do not assume "reasonable defaults" for anything below marked **CONFIRM**.
2. **§6 Naming immutability.** Do not rename or repurpose any existing identifier. New flags on `verify_integration_fingerprints.py` (§5.1) and the new bootstrap list in `review_autofix.yml` (§5.2) must be additive — defaults preserve the current behaviour exactly. Existing callers that pass no new flags get the existing whole-tree absolute check.
3. **§14 Consumer repo registry.** Any new `OPTIONAL_*` / `MAIN_PRIMARY_*` bootstrap entry must be safe to absent on consumer repos pinned to an older `script_ref`. Bootstrap miss must `::warning::` and continue, not `::error::` and exit (§5.2 inherits the existing `OPTIONAL_BOOTSTRAP_SCRIPTS` semantics at `review_autofix.yml:480-489`).
4. **§15 GitHub API hygiene.** Net `gh api` / `gh_retry` / `_safe_gh_jq` calls must **not increase**. Both proposed changes are pure-local: §5.1 is in-process JSON I/O on a tempfile; §5.2 is a file-copy reorder during workflow bootstrap. Zero new API calls.
5. **§7 Output.** Final response (when implementation lands) must list every file changed with line ranges, and update `README.md` / `agents.md` for the new `--baseline-fingerprints-state` / `--compare-against-baseline` verifier modes and the `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` env entry.
6. **§10 (B) Index Registry / runtime safety.** Not directly applicable (no Mongo work) but the pattern is the same: never silently relax a safety check; always emit a structured warning the operator can grep for.

---

## 3. Goals (in priority order)

1. **Stop the loop on conflict-path resolver failures whose root cause is *pre-existing* fingerprint drift on the integration branch.** A fingerprint that was already failing on the branch *before* the resolver ran cannot have been broken by the resolver and must not block the resolver's commit. (§5.1)
2. **Guarantee that fixes shipped to `verify_integration_fingerprints.py`, `review_conflict_resolve.sh`, and `review_conflict_prepare.sh` on `main` reach every running branch on its next bootstrap, regardless of whether the branch's own copy is older.** Eliminates the "stuck branch runs old verifier" feedback loop. (§5.2)
3. **Preserve full pre-resolve safety against *resolver-introduced* regressions.** A fingerprint that was passing in the pre-resolve baseline and fails post-resolve must still hard-fail the run. The verifier's strength is unchanged for the case it was designed to catch; only the irrelevant pre-existing-drift case is loosened. (§5.1, §6 Acceptance)
4. **Surface pre-existing drift loudly so a follow-up audit can address the capture-side root cause.** Every pre-existing-drift fingerprint that is excused from the hard-fail must be emitted as a structured `::warning::` with a stable marker (`PRE_EXISTING_FINGERPRINT_DRIFT_V1`) so a future job (§10) can grep, dedup, and open issues against the capture pipeline. (§5.1)
5. **Net `gh api` calls flat or down.** Both changes are local-only. (§2 #4)
6. **No new operator escape hatches.** Goal is fully unattended self-heal; if §5 is insufficient the system stays in the loop until §10 future work lands. Documented as a known gap (§7). No human-only labels added by this plan.

---

## 4. Scope — Files Touched

| File | Change | Magnitude |
| --- | --- | --- |
| `scripts/verify_integration_fingerprints.py` | Add two CLI flags: `--baseline-fingerprints-state <path>` (capture-mode write) and `--compare-against-baseline <path>` (verify-mode read). When `--compare-against-baseline` is supplied, demote pre-existing failures to `::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1` and only `return 1` for new (post-resolve) failures. Default behaviour (no flags) unchanged. | Medium (adds two modes + JSON I/O; the existing `verify()` function refactored to compute per-fingerprint pass/fail set, with the absolute-vs-delta decision applied at the bottom). |
| `scripts/review_conflict_resolve.sh` | Wrap the existing resolver invocation: (1) before invoking codex, run `verify_integration_fingerprints.py --baseline-fingerprints-state <runtime>/baseline_fp_state.json "${INTEGRATION_FINGERPRINTS_FILE}"` against the *pre-resolve* worktree; (2) after codex writes its tree to the working copy *but before* the `git commit -m "[ai-merge-resolve]…"` step, run `verify_integration_fingerprints.py --compare-against-baseline <runtime>/baseline_fp_state.json "${INTEGRATION_FINGERPRINTS_FILE}"`. The verifier remains a pre-commit gate — a non-zero exit prevents the `[ai-merge-resolve]` commit from being created, so no regression ever lands in branch history. If the baseline-capture step fails for any reason (script missing, IO error, malformed JSON), fall back to the existing absolute check — fail-open on the fail-open path, fail-closed on the safety check. | Small (15-30 lines: two new invocations + one error path). |
| `.github/workflows/review_autofix.yml` | Add a third bootstrap list `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` whose lookup order is `.codex-workflow-src-main/scripts/${f}` first, `.codex-workflow-src/scripts/${f}` fallback. Initial members: `verify_integration_fingerprints.py`, `review_conflict_resolve.sh`, `review_conflict_prepare.sh`. Bootstrap loop emits `::notice::Bootstrapped ${f} from main snapshot (branch copy ignored)` whenever the main snapshot is preferred over a branch copy that exists. | Small (one new array, one new for-loop matching the existing optional-loop shape at L482-489). |
| `README.md` | Document the new `--baseline-fingerprints-state` / `--compare-against-baseline` verifier modes, the `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` rationale, and the `PRE_EXISTING_FINGERPRINT_DRIFT_V1` marker so an operator grepping action logs knows what to look for. | Small (one section addition under the existing "Integration sync resolver" prose). |
| `agents.md` | One-line entry per env/CLI-flag addition under the existing `verify_integration_fingerprints.py` and `review_autofix.yml` anchors. Append-only per CLAUDE.md §6 / the agents.md append-only rule. | Tiny. |
| `tests/` | New `tests/test_verify_integration_fingerprints_baseline.py` covering: (a) fingerprint passing in baseline + failing post-resolve → exit 1, (b) fingerprint failing in baseline + still failing post-resolve → exit 0 with `PRE_EXISTING_FINGERPRINT_DRIFT_V1` marker, (c) fingerprint failing in baseline + fixed post-resolve → exit 0 (no warning, no error), (d) malformed baseline JSON → fall through to absolute check. | Medium (4 cases; pure pytest, no GH calls). |

No changes to `scripts/orchestrate_poll_process.sh` — the clean-sync path (§1.3) is already correctly decoupled and the conflict-path callsite invokes `review_conflict_resolve.sh` unchanged.

---

## 5. Design

### 5.1 Pre/Post-Resolve Delta Verification (`verify_integration_fingerprints.py`)

#### 5.1.1 Baseline-state JSON shape

A new "baseline" file captures the *pre-resolve* satisfaction status of every fingerprint. Shape:

```json
{
  "schema_version": 1,
  "captured_at": "2026-04-24T06:08:34Z",
  "branch": "orchestrator/project-1469",
  "head_sha": "<pre-resolve worktree HEAD>",
  "fingerprints": {
    "<issue_key>": {
      "must_contain": [
        { "fp_key": ["scripts/foo.py", "<regex>"], "file": "scripts/foo.py", "regex": "<regex>", "satisfied": true },
        { "fp_key": ["...", "..."], "file": "...", "regex": "...", "satisfied": false }
      ],
      "must_not_contain": [
        { "fp_key": ["...", "..."], "file": "...", "regex": "...", "satisfied": true }
      ]
    }
  }
}
```

Field-naming notes (kept consistent with the existing fingerprints input schema in `scripts/verify_integration_fingerprints.py:112-113`):

- `file` and `regex` mirror the keys the existing verifier already reads from the input fingerprints JSON. Reusing those names lets `_fp_satisfied` (see below) accept either a baseline-list entry or a raw input fingerprint without a key-rewrite shim.
- `fp_key` is the JSON-array serialisation of the existing `_fp_key(fp)` Python tuple `(file, regex_src)` (declared at `verify_integration_fingerprints.py:109` returning `tuple[str, str] | None`). A JSON array is chosen over a hashed string so the value remains human-readable in the baseline file (operator triage on a stuck branch is faster when `fp_key` is grep-able as the same path/regex pair that appears in the `::warning::` output) and so a future change to the tuple's element ordering produces a visible doc-diff rather than a silently-different hash. No new hashing function is introduced; baseline-write stores `fp_key` as `list(_fp_key(fp))` (plain Python list, **not** `json.dumps(list(...))` — wrapping in `json.dumps` would produce a JSON-string containing the serialised array, which would not round-trip through `tuple(entry["fp_key"])`). Compare-read does `tuple(entry["fp_key"])` to round-trip back to the existing tuple shape that the rest of the verifier already keys off (`verify_integration_fingerprints.py:139,215`). The enclosing baseline document is then serialised normally with a single `json.dump(doc, fh)` at the end. If the helper's tuple shape is ever changed (e.g. extended to 3 elements), bump the baseline `schema_version`.
- `satisfied` is whether the fingerprint **requirement** is met, not the raw boolean outcome of `re.search`. For `must_contain`, `satisfied` means the regex matches at least one line in `file`. For `must_not_contain`, `satisfied` means the regex does **not** match any line in `file` (i.e. the negation of the raw search result). Refactor the inner regex test into a single kind-aware helper `_fp_satisfied(fp, file_cache, kind) -> bool` (where `kind ∈ {"must_contain", "must_not_contain"}`) and use it from both capture-mode and compare-mode — **and** from the existing `verify()` and `list_violated_files()` loops so there is exactly one source of truth for the satisfaction semantics. Without the kind parameter, must_not_contain would be misclassified in every mode.

#### 5.1.2 Capture mode (`--baseline-fingerprints-state <out_path>`)

When this flag is supplied alongside the existing positional `<fingerprints.json>` arg:

1. Walk the same fingerprints structure the existing `verify()` walks.
2. For each `(issue_key, "must_contain"|"must_not_contain", fp)` triple, compute `satisfied` against the *current cwd tree*.
3. Write the JSON above to `<out_path>` (create parent dirs if missing; `chmod 0644`).
4. Exit 0 unconditionally (capture is a snapshot, never a verdict). On any IO/parse error: print `::warning::baseline capture failed: <err>` and exit 0 (fail-open — the caller will fall through to absolute-check).

Important: capture mode runs the *same* deduplication as verify (`_fp_key`-based shared-key removal at `verify_integration_fingerprints.py:215-228`), so a `must_contain` ↔ `must_not_contain` capture-side false positive on issue #1519 is also stripped from the baseline. This means the baseline cannot contain a contradictory pair, and the post-resolve compare cannot be tricked into "regressing" something the verifier itself stripped.

#### 5.1.3 Compare mode (`--compare-against-baseline <baseline_path>`)

When this flag is supplied alongside the existing positional `<fingerprints.json>` arg:

1. Load `<baseline_path>`. Validate `schema_version == 1` only — do **not** require the baseline keyset to be a subset of the current `<fingerprints.json>` keyset. Asymmetric handling per direction:
   - **Baseline has keys the current set does not** (e.g. a sub-issue was reverted or removed from orchestrator state between capture and compare): silently ignore those extra baseline entries; they describe a sub-issue we no longer need to verify.
   - **Current set has keys the baseline does not** (e.g. a sub-issue was merged between baseline-capture and now): treat each such fingerprint as "must hold absolutely" — it had no pre-resolve baseline so it falls back to the existing absolute check, *per fingerprint*, without abandoning the baseline-mode benefit for the keys that do match.
   This avoids the failure mode where a single missing/extra key forces the entire run back to absolute verification.
2. Walk the current cwd tree exactly as `verify()` does today, computing per-`fp_key` `satisfied`.
3. For each fingerprint, classify into one of four buckets:
   - `still_passing` — baseline `satisfied=true`, current `satisfied=true` → silent pass.
   - `newly_fixed` — baseline `satisfied=false`, current `satisfied=true` → emit `::notice::PRE_EXISTING_FINGERPRINT_DRIFT_V1 fixed_by_resolver fp_key=<...> issue=#<N> path=<...>` and pass.
   - `pre_existing_drift` — baseline `satisfied=false`, current `satisfied=false` → emit `::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged fp_key=<...> issue=#<N> path=<...> pattern=<...> kind=<must_contain|must_not_contain>` and pass. **Does not contribute to violations.** Counted in a `pre_existing_drift_count` summary line.
   - `regressed_by_resolver` — baseline `satisfied=true`, current `satisfied=false` → append to `violations` exactly as `verify()` does today.
4. After classification:
   - If `regressed_by_resolver` is non-empty, emit the existing `::error::Integration fingerprint verification FAILED — resolver output regressed merged sub-issue intent` block and `::error::Refusing to create [ai-merge-resolve] commit.`, then `return 1`. Note the wording is unchanged; only the *cause set* is narrower.
   - If `regressed_by_resolver` is empty and `pre_existing_drift` is non-empty, emit a new summary line: `Integration fingerprint verification PASSED with pre-existing drift — resolver did not introduce any new regressions (pre_existing_drift_count=<N>; see PRE_EXISTING_FINGERPRINT_DRIFT_V1 markers above for triage).` and `return 0`.
   - If both empty, emit the existing `Integration fingerprint verification PASSED — all merged sub-issue intent preserved.` and `return 0`.

#### 5.1.4 Default-mode preservation

When neither `--baseline-fingerprints-state` nor `--compare-against-baseline` is supplied, `main()` dispatches to the existing `verify()` unchanged. Every existing caller (production and tests) gets identical behaviour to today. This honours CLAUDE.md §6 naming immutability — the existing verbs/exits/log lines are byte-identical for callers that don't opt in.

#### 5.1.5 Mode-conflict guard

If a caller passes both `--baseline-fingerprints-state` and `--compare-against-baseline` in the same invocation, exit 2 with `::error::verify_integration_fingerprints: --baseline-fingerprints-state and --compare-against-baseline are mutually exclusive`. Prevents footgun where the resolver wrapper accidentally captures and compares in the same call (which would compare against a freshly-captured "current = baseline" snapshot and always pass).

### 5.2 Main-Snapshot Bootstrap of Safety Scripts (`review_autofix.yml`)

#### 5.2.1 New bootstrap list

Add a third array alongside the existing two at `review_autofix.yml:455-475`:

```yaml
# Safety scripts where the main-branch snapshot MUST take precedence
# over the checked-out branch's copy. Inverts the lookup order used
# for REQUIRED_/OPTIONAL_BOOTSTRAP_SCRIPTS so a stuck branch cannot
# wedge itself by shipping its own (older) verifier/resolver.
#
# Add a script here only if (a) it is part of the integration-sync
# safety perimeter AND (b) it is safe to run against an arbitrary
# branch's tree (i.e. its CLI is stable and accepts the inputs the
# branch's review_autofix.yml produces). Anything else belongs in
# REQUIRED_/OPTIONAL_BOOTSTRAP_SCRIPTS.
MAIN_PRIMARY_BOOTSTRAP_SCRIPTS="verify_integration_fingerprints.py review_conflict_resolve.sh review_conflict_prepare.sh"
```

**Invariant: no overlap with `OPTIONAL_BOOTSTRAP_SCRIPTS`.** In the same diff that adds `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS`, **remove** `verify_integration_fingerprints.py` from `OPTIONAL_BOOTSTRAP_SCRIPTS` at `review_autofix.yml:468` so the list becomes either empty or a single space. Rationale: `OPTIONAL_BOOTSTRAP_SCRIPTS` runs *after* `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` (see §5.2.2), and the optional loop's lookup order is branch-first / main-fallback — so a script present in both lists would be installed from `main` by the main-primary loop and then **overwritten** by the branch copy from the optional loop, silently defeating the whole plan. The `MAIN_PRIMARY_*` loop already inherits the "warn and continue on total absence" semantics (§5.2.2 bottom branch), so the fail-open behaviour `OPTIONAL_*` provided for the verifier is preserved. Overlap with `REQUIRED_BOOTSTRAP_SCRIPTS` is acceptable — `review_conflict_prepare.sh` and `review_conflict_resolve.sh` are in both, `MAIN_PRIMARY_*` runs *after* `REQUIRED_*`, so main-primary wins last-writer for those two files (which is the desired outcome).

#### 5.2.2 Bootstrap loop (matches existing optional-loop shape)

```yaml
for f in ${MAIN_PRIMARY_BOOTSTRAP_SCRIPTS}; do
  src=""
  if [ -f ".codex-workflow-src-main/scripts/${f}" ]; then
    src=".codex-workflow-src-main/scripts/${f}"
    if [ -f ".codex-workflow-src/scripts/${f}" ]; then
      echo "::notice::Bootstrapped ${f} from main snapshot (branch copy at .codex-workflow-src/scripts/${f} ignored — main snapshot wins for safety-perimeter scripts)."
    fi
  elif [ -f ".codex-workflow-src/scripts/${f}" ]; then
    src=".codex-workflow-src/scripts/${f}"
    echo "::warning::main snapshot for ${f} unavailable; falling back to branch copy (less safe — see docs/integration-sync-resolver-self-heal.md §5.2)."
  fi
  if [ -z "${src}" ]; then
    echo "::warning::main-primary safety script '${f}' not available in either checkout; downstream features relying on this helper will be unavailable."
    continue
  fi
  install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/${f}"
done
```

This loop must run **after** `REQUIRED_BOOTSTRAP_SCRIPTS` (so any errors in required-bootstrap surface first) and **before** `OPTIONAL_BOOTSTRAP_SCRIPTS` (so an OPTIONAL script that depends on a MAIN_PRIMARY one finds the right version when copied). The resulting overwrite is intentional — three of the named scripts also appear in `REQUIRED_BOOTSTRAP_SCRIPTS` (`review_conflict_resolve.sh`, `review_conflict_prepare.sh`); the main-primary loop installs over the top of the required-bootstrap copy.

#### 5.2.3 Consumer-repo safety

Consumer repos (`.github/ai/consumer_repos.json`) that bootstrap from a stable `script_ref` typically have BOTH `.codex-workflow-src/` and `.codex-workflow-src-main/` populated by the upstream pin/snapshot dance. If a consumer is mis-configured and only `.codex-workflow-src/` exists (no `main` snapshot at all), the loop emits the `::warning::main snapshot for ${f} unavailable` line and falls back to the branch copy — same behaviour they have today. No regression.

If both checkouts are missing the script entirely, the `continue` branch fires the existing-shape `::warning::… not available …` message and the resolver falls open at the call site (existing fail-open at `scripts/review_conflict_prepare.sh:389`). No new failure mode.

#### 5.2.4 Why not invert `REQUIRED_BOOTSTRAP_SCRIPTS` itself

Naïvely flipping the lookup order on the existing `REQUIRED_BOOTSTRAP_SCRIPTS` loop would change behaviour for ~25 scripts at once, including `gh_helpers.sh`, `setup_serena.sh`, `tg_helpers.sh`, `ai_memory.py`, etc. Many of these have legitimate per-branch evolutions (e.g. a branch under active development may have edited `gh_helpers.sh` to add a feature its workflow depends on). Forcing the main copy on those would silently revert in-flight branch work. The MAIN_PRIMARY list is deliberately tiny — the safety perimeter only.

---

## 6. Acceptance Criteria

A change is "complete" only when every box is checked.

### 6.1 Verifier behaviour (`verify_integration_fingerprints.py`)

- [ ] **Default mode unchanged.** Run the existing test suite (whichever tests already cover this script) without modification — all pass byte-identical to pre-change.
- [ ] **Capture mode happy path.** New unit test: `--baseline-fingerprints-state /tmp/baseline.json fingerprints.json` writes a JSON file with `schema_version=1`, the expected `fingerprints` map, and exits 0.
- [ ] **Capture mode error path.** Unwritable output path → `::warning::baseline capture failed: …` → exit 0 (fail-open).
- [ ] **Compare mode — pre-existing drift passes.** Synthesize a baseline where issue #X has `must_contain` `satisfied=false` for `fp_key=K`; current tree still has `K` unsatisfied; exit 0; stdout contains `PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged fp_key=K`; no `::error::` lines.
- [ ] **Compare mode — resolver-introduced regression fails.** Baseline `K` `satisfied=true`; current tree `K` `satisfied=false`; exit 1; stdout contains `Integration fingerprint verification FAILED — resolver output regressed` and `Refusing to create [ai-merge-resolve] commit.` (existing wording preserved); no `PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged` for `K`.
- [ ] **Compare mode — newly fixed.** Baseline `K` `satisfied=false`; current `K` `satisfied=true`; exit 0; stdout contains `PRE_EXISTING_FINGERPRINT_DRIFT_V1 fixed_by_resolver fp_key=K`.
- [ ] **Compare mode — fingerprint absent from baseline.** Current set has issue #Y not in baseline; behaviour falls through to absolute check for #Y (any current failure on #Y → exit 1). Documented in the function docstring.
- [ ] **Mode-conflict guard.** Both flags supplied → exit 2 with the exact error message in §5.1.5.
- [ ] **Malformed baseline JSON.** Compare mode loads a file that fails JSON parse or `schema_version != 1` → fall through to absolute check, emit `::warning::baseline malformed (…); using absolute verification.` Test the fall-through path returns the same verdict the absolute check would.

### 6.2 Resolver wrapper (`scripts/review_conflict_resolve.sh`)

- [ ] Pre-resolve baseline capture call exists, runs against the un-resolved worktree, and writes to a path inside `${RUNTIME_DIR}` (so it is purged with the rest of the run's runtime artefacts).
- [ ] If baseline capture fails (script missing, IO error, exit code != 0), the resolver continues but uses `verify_integration_fingerprints.py` *without* `--compare-against-baseline` (i.e. the existing absolute check). Logged as `::warning::baseline capture unavailable; falling back to absolute fingerprint verification.`
- [ ] Post-resolve verifier call uses `--compare-against-baseline` *only* when the baseline file exists and was non-empty.
- [ ] No new `gh api` / `gh_retry` / `_safe_gh_jq` calls. Verified by `git diff scripts/review_conflict_resolve.sh | grep -E 'gh (api|_retry)|_safe_gh_jq'` returning no additions.

### 6.3 Bootstrap (`review_autofix.yml`)

- [ ] `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` array exists, contains exactly the three names in §5.2.1, and the for-loop matches the shape in §5.2.2.
- [ ] Run order in the bootstrap step is: `REQUIRED_…` → `MAIN_PRIMARY_…` → `OPTIONAL_…`.
- [ ] When both `.codex-workflow-src/` and `.codex-workflow-src-main/` have a copy, the main-snapshot copy is the one installed (verified by SHA comparison in a workflow-test).
- [ ] When only `.codex-workflow-src/` has a copy, the branch copy is installed and the `::warning::main snapshot for ${f} unavailable` line is emitted.
- [ ] When neither has a copy, the `continue` branch fires and the existing-shape `::warning::… not available …` line is emitted (no `::error::`, no early exit).

### 6.4 End-to-end behaviour on PR #1569 (regression test for the actual incident)

- [ ] Reproduce the failing tree from run `24872524074` (314/317 satisfied; the 3 missing must_contain on issue #1519 were already absent on the integration branch *before* the resolver ran).
- [ ] With Part A active, the resolver runs the codex pass, captures baseline (3 already-failing fp_keys), invokes verifier with `--compare-against-baseline`, classifies the 3 as `pre_existing_drift`, and the run produces an `[ai-merge-resolve]` commit.
- [ ] The PR's `mergeable_state` transitions from `dirty` → mergeable on the next sync tick. PR #1569 unblocks fully unattended within 1 stall-poller cycle of the change landing on `main`.

### 6.5 Net impact

- [ ] `gh api` call count per resolver run unchanged or reduced. Compare via the workflow log's `_GH_API_CALL_COUNT` if exposed; otherwise grep counts of `gh api` / `gh_retry` invocations in a run log before/after.
- [ ] No new `ai:*` labels.
- [ ] No new repository variables.
- [ ] `README.md` and `agents.md` updated per §4.

---

## 7. Known Gaps — what this plan deliberately does NOT fix

This is the "safest, partial" fix per the Q1=A decision (`docs/integration-sync-resolver-self-heal.md` chat history, 2026-04-24). It addresses one specific failure mode (pre-existing fingerprint drift wedging the conflict-path resolver loop) and one structural feedback (stuck branches running stale safety scripts). It explicitly does **not** address:

1. **Resolver-introduced regressions still hard-block forever.** If the resolver actually produces a tree that breaks a previously-passing fingerprint and *keeps* breaking it on every retry, the loop reproduces. Mitigations belong in §10 #1 (consecutive-failure escape) and §10 #3 (graduated tiers).
2. **Capture-side false positives (the #1519 contradictory-pattern class) are *masked*, not fixed.** Part A's dedup at `verify_integration_fingerprints.py:215-228` is already in place and silently strips contradictory fp_keys; this plan inherits the existing dedup. Whether the *underlying* capture bug that emitted contradictory patterns has been fully fixed by `32c0ce0` is out of scope here. Follow-up: §10 #4 (capture invariant tests).
3. **No structural recovery if `main` itself breaks the verifier.** Part B inverts the bootstrap so the *main* copy of the verifier always wins. If `main` ships a broken `verify_integration_fingerprints.py`, every running branch breaks simultaneously. Mitigation: comprehensive unit tests on the verifier (already partially present; expand per §6.1) plus the standard CI gate on `main` PRs touching this file.
4. **No retry-budget cap on the conflict-path resolver itself.** Even with Part A passing, an underlying codex failure (timeout, empty output, bad JSON) still produces a `failure()` and a retry next stall-cron tick, indefinitely. Existing `MAX_CODEX_ATTEMPTS` per `docs/resilient-codex-failure-plan.md` §4 governs in-workflow retries; **this plan does not introduce a *cross-run* attempt budget** and does not surface the run-by-run failure count anywhere structured. Follow-up: §10 #2 (consecutive-failure structured state).
5. **No cleanup of the 18 (and counting) stale `**AI review/autofix failed**` comments on PR #1569 once it does unblock.** The duplicate-suppression already in `review_autofix.yml` (search for `tg_cleanup` markers in the PR body) addresses Telegram, not GitHub PR comments. Operator-visible noise persists. Cosmetic; out of scope.
6. **No metric / dashboard for `pre_existing_drift_count`.** Part A emits a structured marker; nothing yet aggregates it across runs. Until §10 #5 (audit job) lands, drift accumulates silently in workflow logs. The `::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1` lines are still grep-able by an operator who knows to look.
7. **PR #1569 specifically may need a one-time manual nudge to land Part B's bootstrap inversion.** Until the changes from §5 reach `main`, the stuck branch keeps using its older bootstrap that doesn't know about `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS`. Either (a) operator merges `main` into `orchestrator/project-1469` once after §5 ships, or (b) the next clean-sync tick (which doesn't go through the verifier — see §1.3) will eventually carry the bootstrap change to the branch *if and only if* a clean-sync window opens. Worst case: one operator nudge to bridge the gap. Documented as a one-time migration cost, not an ongoing failure mode.

---

## 8. Operational Telemetry

Until the future-work audit job (§10 #5) lands, the only signals operators can use to detect this plan's failure modes are workflow-log greps:

| Signal | Grep | Meaning |
| --- | --- | --- |
| Plan working as designed | `PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged` | A pre-existing drift was excused. Triage the underlying capture for that issue if the count climbs. |
| Plan working — resolver helped | `PRE_EXISTING_FINGERPRINT_DRIFT_V1 fixed_by_resolver` | The resolver coincidentally fixed a pre-existing drift. Bonus signal; no action needed. |
| Plan correctly hard-failing | `Integration fingerprint verification FAILED — resolver output regressed` | Resolver actually broke something. This is the safety check doing its job. If it loops, escalate to §10 #1. |
| Bootstrap working | `Bootstrapped <script> from main snapshot (branch copy … ignored)` | Main-primary bootstrap fired as intended. |
| Bootstrap fallback | `main snapshot for <script> unavailable; falling back to branch copy` | The main snapshot wasn't available; running on the branch copy. Investigate why the snapshot is missing (consumer-repo misconfig?). |
| Mode-conflict footgun | `--baseline-fingerprints-state and --compare-against-baseline are mutually exclusive` | A caller misused the verifier. Should never appear in production; treat as a code bug in the resolver wrapper. |

Add a `tg_notify` (`scripts/tg_helpers.sh`) call **only** if `pre_existing_drift_count > 0` AND the resolver's commit subsequently lands AND a *new* drift fingerprint appears that wasn't in the prior run's baseline — i.e. only notify on *novel* drift, never on stable drift. Threshold prevents alert fatigue from the steady-state #1519-class noise. (Detailed call-site lives in §10 #5; this plan ships without the notify, accepting log-only telemetry as the v1 surface.)

---

## 9. Appendix A — Code Anchors

For implementation reference, the exact lines this plan touches or reads:

| File:Line | What it is | Plan reference |
| --- | --- | --- |
| `scripts/verify_integration_fingerprints.py:139` | `_fp_key` first usage in `list_violated_files` walk | §5.1.1 (reuse in baseline JSON) |
| `scripts/verify_integration_fingerprints.py:188-325` | `verify()` — the function being refactored | §5.1.2, §5.1.3 |
| `scripts/verify_integration_fingerprints.py:215-228` | Existing capture-side false-positive dedup | §5.1.2 (capture mode inherits this) |
| `scripts/verify_integration_fingerprints.py:294-305` | Silent-regression detector + ratio log | §5.1.3 (kept; supplemented by per-fingerprint classification) |
| `scripts/verify_integration_fingerprints.py:307-319` | The hard-fail block whose *cause set* this plan narrows | §5.1.3 (`regressed_by_resolver` only) |
| `scripts/verify_integration_fingerprints.py:328-358` | `main()` arg parsing — extension point for new flags | §5.1.2, §5.1.3, §5.1.5 |
| `scripts/orchestrate_poll_process.sh:2631,2724-2727` | Clean-sync API call in `sync_default_into_integration_branch` (already verifier-decoupled) | §1.3 (no change needed) |
| `scripts/orchestrate_poll_process.sh:2503,2518` | `integration_sync_status="conflict"` setters | §1.3 (signal that triggers the resolver path; no change) |
| `scripts/review_conflict_prepare.sh:265-389` | Integration-sync detection + fingerprints-file staging | §5.1 (baseline path lives in same `RUNTIME_DIR`) |
| `scripts/review_conflict_resolve.sh` | Resolver wrapper that calls the verifier | §4, §5.1 (callsite addition) |
| `.github/workflows/review_autofix.yml:455-475` | `REQUIRED_BOOTSTRAP_SCRIPTS` array | §5.2.1 (immediate predecessor of new array) |
| `.github/workflows/review_autofix.yml:482-489` | Optional-bootstrap loop shape to mirror | §5.2.2 |
| `.github/workflows/internal-orchestrate-poll.yml:3-6` | Poll schedule — source of truth for retry cadence (`cron: */5 * * * *`) | §1.2 (background only) |
| `.github/workflows/review_autofix.yml:201-205` | Stall-cron `workflow_dispatch` bypass comment. **Note:** the inline prose in this comment block still says "every 30 min" and "~30 min worst-case window", which is stale — the actual cron is `*/5` per the row above. Treat this citation as implementation detail only; the cadence source of truth is `internal-orchestrate-poll.yml`. A follow-up housekeeping commit should update the stale comment (not in scope for this plan). | §1.2 (background only) |
| `.github/workflows/review_autofix.yml:3392-3433` | Generic `failure()` fallback comment (the 18-comments source) | §1.1 (background only) |

---

## 10. Future Work (deferred, intentionally out of scope here)

Ranked by leverage. None of these are required for the v1 fix to ship; each is a follow-up issue.

1. **Consecutive-identical-failure escape valve.** After N runs with the same failure signature on the same head SHA, automatically escalate: graduated tier downgrade (see #3), branch rebuild (see #6), or controlled fingerprint quarantine (see #4). Without this, §7 #1 remains a forever-loop risk.
2. **Structured per-PR consecutive-failure state comment.** A single `<!-- AUTOFIX_RESOLVER_RETRY_STATE_V1 …` comment that tracks: head SHA, consecutive failure count, last failure signature hash, last `pre_existing_drift_count`, last `regressed_by_resolver` set. Updated in place, not appended. Lets the stall poller decide whether to escalate without re-parsing 18 generic comments. Replaces §7 #5's noise problem.
3. **Graduated verification tiers.** `strict` (today) → `ratio` (must_contain satisfaction ≥ 95%) → `count_only` → `warn_only`, each unlocked after N consecutive failures of the previous tier. Pairs with #1.
4. **Adaptive fingerprint quarantine.** A fingerprint that has been classified `pre_existing_drift unchanged` for ≥M consecutive runs is moved to a quarantine list and skipped (with a one-time `::warning::FINGERPRINT_QUARANTINED_V1` marker). Long-horizon audit job (#5) periodically re-evaluates quarantined fingerprints against the sub-issue's actual PR diff.
5. **Drift audit job.** A scheduled workflow that scans `PRE_EXISTING_FINGERPRINT_DRIFT_V1` and `FINGERPRINT_QUARANTINED_V1` markers across recent runs, dedupes by fp_key, opens a tracker issue per persistent drift cluster with the suspected capture-side root cause. Closes the loop on §7 #2 / §7 #6.
6. **Last-resort branch rebuild.** After M hours of solid failure on the same head SHA, the orchestrator deletes the stuck PR branch and recreates it from current `main`, replaying merged sub-PRs in order. Per-branch rebuild-rate cap to prevent rebuild-loops. The "nothing else worked" hammer.
7. **Replace fingerprints with sub-issue test runs.** Bigger project: replace regex-based intent capture with "did the sub-issue PR's added tests still pass on the integration tree?" as the gate. Direct evidence of intent preservation rather than regex proxy. Removes the entire capture-quality dependency. Unrelated rewrite; mentioned for completeness.

---

## Appendix B — Decision Log

- **2026-04-24** — Q1=A (split sync from resolve / verifier-fix-propagation, refined to "delta verification + main-snapshot bootstrap" after confirming clean-sync is already decoupled at `scripts/orchestrate_poll_process.sh:2724-2727`).
- **2026-04-24** — Q2=A (full plan-of-record matching `docs/resilient-codex-failure-plan.md` shape).
- **2026-04-24** — Filename `docs/integration-sync-resolver-self-heal.md` chosen.
- **Open** — N (consecutive-failure threshold for §10 #1), M (drift-quarantine threshold for §10 #4), M-hours (rebuild threshold for §10 #6) all deferred to the respective follow-up issues. **CONFIRM** before implementing any of those.
