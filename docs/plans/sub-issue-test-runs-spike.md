# Sub-Issue Test Runs — Discovery Spike

> Status: discovery only.
> Source question: Phase 6 / G6.1 in `docs/completed/integration-sync-resolver-self-heal-plan.md`.
> Decision: **NO-GO** on replacing fingerprints with sub-issue test runs as the sole gate.
> Non-goal: this document does **not** implement G6.2; any G6.2 planning remains future work.

---

## Direct answers to the four G6.1 questions

1. **What fraction of merged sub-issues have a PR that adds runnable tests?**
   In the bounded sample below, **12 / 14 PRs (85.7%)** changed at least one
   standalone-runnable `tests/*.py` file.
2. **What is the wall-clock cost of running those tests on the integration tree?**
   Measured on the current checkout with the repo's prevailing file-level
   invocation shape (`PYTHONDONTWRITEBYTECODE=1 python3 tests/<file>.py`), the
   sample's **13 unique runnable test files took 21.671s once each**, while the
   **per-PR summed cost was 48.294s** because several sub-issues touched the same
   shared test files.
3. **What is the false-positive / false-negative comparison vs fingerprint verification?**
   As a **sole** gate, test runs have an immediate **2 / 14 definite blind spot**
   in this sample because two PRs had no runnable tests. The current fingerprint
   path had **0 / 14 complete misses** in the sample but **2 / 14 partial gaps**
   because the capture allowlist excludes `CHANGELOG.md` and `.gitignore`. Outside
   the sample, fingerprints also have a documented operational false-positive mode
   (the PR #1569 / issue #1519 wedge described in
   `docs/integration-sync-resolver-self-heal.md`).
4. **What is the recommendation?**
   **No-go on replacement.** If this idea is revived later, it should be
   re-scoped as an **additive, opt-in hybrid** layered beside fingerprints, not a
   replacement for them.

---

## Summary

This spike compared the current fingerprint gate with a bounded sample of merged
orchestrator sub-issue PRs from `orchestrator/project-2597`,
`orchestrator/project-2617`, and `orchestrator/project-2627`:
**#2600, #2604, #2620, #2621, #2623, #2625, #2630, #2635, #2658, #2666,
#2667, #2699, #2705, #2720**.

The sample is encouraging on **cost** but not on **replacement safety**:

- **12 / 14 PRs** had at least one runnable Python test target.
- **10 / 14 PRs** already align with explicit `python3 tests/...` entries in
  `.github/workflows/ci.yml`; **2 more** were runnable locally but would need CI
  wiring or manifest generation.
- **12 / 14 PRs** touched only files inside the current fingerprint-capture
  allowlist; **2 / 14** had at least one file outside it.
- **2 / 14 PRs** had **no runnable tests at all**, so a tests-only replacement
  would have no signal for them.
- The heaviest sampled per-PR test cost was still only **10.359s**, so test runs
  are operationally cheap enough for an additive path.

That combination leads to a clear conclusion: test runs are promising as a
**secondary behavioural signal**, but replacing fingerprints with them would
reduce total coverage on this repo's current mix of prompt/script/docs/workflow
sub-issues.

---

## Scope and non-goals

- This document answers the four Phase 6 discovery questions only.
- It does **not** propose workflow, script, or prompt changes.
- It does **not** write a G6.2 implementation plan.
- If the repo owner later wants a hybrid follow-up, that planning belongs in a
  separate `docs/plans/sub-issue-test-runs-implementation-plan.md`, not here.

---

## Current baseline

### Current capture path

`scripts/orchestrate_poll_process.sh::capture_intent_fingerprints_for_merged_subissue`
fetches the merged sub-issue PR diff, keeps only allowlisted paths for the
line-based regex capture, converts net added lines into `must_contain` regexes,
converts net removed lines into `must_not_contain` regexes, records outright file
deletions under `must_not_exist`, and stores the result under
`merged_issue_fingerprints[<issue_num>]` with these fields:

- `issue`
- `pr`
- `captured_at`
- `must_contain`
- `must_not_contain`
- `must_not_exist`

The current allowlist is:

- `.github/`
- `scripts/`
- `prompts/`
- `ai-memory/`
- `tests/`
- `workflow-templates/`
- `docs/`
- `db/contracts/`
- exact files `agents.md`, `README.md`, `CLAUDE.md`

This allowlist matters for Phase 6's **line-based** capture: files outside it do
not contribute `must_contain` / `must_not_contain` regexes, although outright
deletions are still recorded path-agnostically under `must_not_exist`.

### Current verification path

`scripts/verify_integration_fingerprints.py` loads the stored JSON and verifies
the current tree by checking that:

- every `must_contain` regex still matches, and
- every `must_not_contain` regex still does **not** match, and
- every `must_not_exist` path still does **not** exist.

Important properties for the comparison:

- it is **exact on captured lines**, not behavioural;
- it is **fail-open** on missing or unparseable fingerprint input;
- an empty fingerprint object passes;
- it can catch regressions in prompts, docs, README/agents text, and other
  allowlisted non-test files that have no executable test target.

---

## Methodology

### Sample selection

I used a bounded sample of **14 merged AI implementation PRs** whose base ref was
`orchestrator/project-*`, merged from **2026-05-15 through 2026-05-18**, with:

1. changed-file metadata retrievable from GitHub PR data, and
2. any changed `tests/*.py` files present on the current checkout so they could
   be timed locally.

Sample PRs:

- #2600, #2604
- #2620, #2621, #2623, #2625, #2630, #2635
- #2658, #2666, #2667
- #2699, #2705, #2720

Why this sample shape:

- It stays inside the actual Phase 6 target population: merged sub-issues on
  orchestrator integration branches.
- It includes both **test-heavy** and **non-test** sub-issues.
- It is recent enough that the repo still contains most changed test files on
  `main`, which made local runtime checks possible.

### Classification rules

- **Runnable test** = a changed `tests/*.py` file that executed successfully as
  `PYTHONDONTWRITEBYTECODE=1 python3 <file>` on the current checkout.
- **Not runnable** = no changed `tests/*.py` file, or only non-executable test
  assets such as text fixtures.
- **Existing invocation** = `.github/workflows/ci.yml` already contains an
  explicit `python3 tests/<file>.py` line for that file.
- **Standalone only** = the file runs locally but is not yet explicitly listed in
  `.github/workflows/ci.yml`.
- **Full fingerprint coverage** = every changed file sits inside the current
  capture allowlist.
- **Partial fingerprint coverage** = at least one changed file sits outside the
  allowlist.

### Limits of the comparison

This is a **structural lower-bound comparison**, not a replay harness. I did not
rewrite historical merges and run both gates against reverted trees. Instead, I
compared:

- changed-file coverage,
- local executability and file-level runtime,
- existing CI entrypoints, and
- already-documented fingerprint incidents.

So the false-positive / false-negative section below is intentionally framed as
"what this sample proves would be missed or only partially covered," not as a
claim of statistically complete incident rates.

---

## Findings

### 1. Fraction of merged sub-issues with runnable tests

The sample came out more test-rich than expected, but not rich enough to justify
replacement:

- **12 / 14 PRs (85.7%)** had at least one runnable Python test target.
- **10 / 14 PRs (71.4%)** already match explicit file-level test commands in
  `.github/workflows/ci.yml`.
- **2 / 14 PRs (14.3%)** were runnable only via standalone direct execution on
  the current checkout (`tests/test_review_pipeline_integration.py` in PR #2720
  and `tests/test_cost_audit_serena_metrics.py` in PR #2604).
- **2 / 14 PRs (14.3%)** had no runnable test target at all:
  - **PR #2625** — docs / changelog / README / agents updates only.
  - **PR #2600** — prompt + script update only.

Interpretation: a test-run gate would cover **most** recent sub-issues in this
repo, but not **all** of them, and its misses are concentrated exactly where the
current fingerprint mechanism still provides value: text/config/prompt surfaces.

### 2. Wall-clock cost on the integration tree

I timed the sample's runnable test files on the current checkout using the same
file-level execution pattern the repo already uses in `.github/workflows/ci.yml`.

Key numbers:

- **13 unique runnable test files** across the sample.
- **21.671s total** if each unique file is run once.
- **48.294s total** at per-PR granularity (counting repeated files again when a
  later PR changes the same test file).
- **Median test-bearing PR cost: 1.927s.**
- **Worst case in the sample: 10.359s** (PRs #2635 and #2658).

Heaviest individual files in the sample:

- `tests/test_review_synthesise_smoke.py` — **6.453s**
- `tests/test_implement_post_codex_recovery.py` — **5.698s**
- `tests/test_review_rb_judge_label_propagation.py` — **2.334s**
- `tests/test_review_rb_judge_reissue_baseline.py` — **2.294s**
- `tests/test_review_reject_verify.py` — **2.018s**

This cost profile is low enough that an additive future path would be
operationally reasonable. The cost argument is **not** the blocker.

One important caveat: the repo's current CI convention is **whole-file** test
execution (`python3 tests/<file>.py`), not per-test-case selection. A future
Phase 6 gate would therefore rerun an entire test file even if a sub-issue added
only one new case inside it.

### 3. False-positive / false-negative comparison vs fingerprints

#### 3.1 Lower-bound false-negative exposure in the sample

As a **sole** gate, sub-issue test runs have an immediate blind spot:

- **2 / 14 definite misses**: PR #2625 and PR #2600 had no runnable test files.

The current fingerprint system did better on breadth in this sample, but not on
perfection:

- **0 / 14 complete misses** in the sample.
- **2 / 14 partial gaps** because at least one changed file was outside the
  allowlist:
  - **PR #2630** changed `.gitignore`, which fingerprint capture would ignore.
  - **PR #2625** changed `CHANGELOG.md`, which fingerprint capture would ignore.

The asymmetry matters:

- **PR #2600** is fully fingerprint-coverable today but would be invisible to a
  tests-only replacement.
- **PR #2630** benefits from tests for the behavioural script change, but the
  fingerprint gate still cannot see the `.gitignore` delta.
- **PR #2625** shows that neither method is universal by itself: tests give no
  signal at all, while fingerprints cover README / agents / docs but not
  `CHANGELOG.md`.

So a replacement would trade one set of gaps for a larger and more obvious one.

#### 3.2 False-positive evidence

This spike did **not** find a sample-local false-positive case for direct test
execution: all 12 runnable targets passed on the current checkout.

However, the repo already has a documented fingerprint false-positive class: the
PR #1569 / issue #1519 wedge described in
`docs/integration-sync-resolver-self-heal.md`, where contradictory capture and
whole-tree absolute verification created a resolver loop.

That does **not** make replacement attractive on its own, because the test-run
path has a different weakness: when a sub-issue adds no runnable tests, the gate
disappears entirely.

There is also a likely future false-positive mode for a tests-only gate: several
sampled targets are broad shared harness suites rather than issue-specific smoke
tests. For example:

- `tests/test_implement_post_codex_recovery.py` ran **63 cases** in this spike.
- `tests/test_review_reject_verify.py` ran **20 cases**.
- `tests/test_review_synthesise_smoke.py` ran **16 cases**.

That breadth is good for regression detection, but it also means a later,
unrelated subsystem change could fail an inherited sub-issue test target even
when the original sub-issue intent is still preserved.

#### 3.3 Net comparison

- **Fingerprints are stronger on generic breadth** across allowlisted text,
  prompt, docs, workflow, and script changes.
- **Test runs are stronger on behavioural proof** when a PR adds a focused,
  executable check.
- The two mechanisms have **different blind spots**, so replacing one with the
  other reduces coverage. A hybrid would be the only direction that could improve
  total confidence.

---

## Sample evidence table

Measured seconds below are the sum of the changed runnable test files for that
PR, using current-checkout direct execution.

| PR | Runnable test targets | Existing invocation | Fingerprint coverage | Measured seconds |
| --- | --- | --- | --- | ---: |
| #2720 | `test_review_pipeline_integration.py` | standalone only | full | 0.610 |
| #2705 | `test_review_synthesise_smoke.py`; `test_validate_driver_synthesised_filter.py` | explicit CI | full | 6.504 |
| #2699 | `test_review_reject_verify.py`; `test_review_synthesise_smoke.py` | explicit CI | full | 8.471 |
| #2667 | `test_review_synthesise_smoke.py` | explicit CI | full | 6.453 |
| #2666 | `test_review_autofix_review_pipeline_contract.py`; `test_review_reject_verify.py` | explicit CI | full | 2.688 |
| #2658 | `test_implement_post_codex_recovery.py`; `test_review_rb_judge_label_propagation.py`; `test_review_rb_judge_reissue_baseline.py`; `test_workflow_checkout_integration_ref_audit.py` | mostly explicit CI | full | 10.359 |
| #2635 | same four files as PR #2658 | mostly explicit CI | full | 10.359 |
| #2630 | `test_review_judge_interim_round_trip.py` | explicit CI | partial (`.gitignore` not allowlisted) | 0.905 |
| #2623 | `test_review_autofix_review_pipeline_contract.py` | explicit CI | full | 0.670 |
| #2621 | `test_detect_editor_changes_lost.py`; `test_review_autofix_review_pipeline_contract.py` | explicit CI | full | 1.165 |
| #2620 | `test_review_semble_contract.py` | explicit CI | full | 0.065 |
| #2604 | `test_cost_audit_serena_metrics.py` | standalone only | full | 0.045 |
| #2625 | none | n/a | partial (`CHANGELOG.md` not allowlisted) | 0.000 |
| #2600 | none | n/a | full | 0.000 |

---

## Recommendation

### Decision

**No-go on G6.2 as currently phrased** if "G6.2" means **replacing** the
fingerprint contract with sub-issue test runs.

### Why

1. **Coverage would go down immediately.** This sample already contains two
   merged sub-issues with no runnable tests.
2. **The remaining test-bearing PRs still do not prove everything.** Tests are
   behavioural, but many sampled PRs also carry prompt/docs/README/agents/text
   changes that the test file does not directly assert.
3. **The runtime cost is low enough to justify addition, not replacement.**
   Phase 6's blocker is coverage shape, not wall-clock.

### If the idea is revived later

Only revisit it as a **hybrid**:

- keep fingerprints as the generic floor,
- add per-sub-issue test targets only when a PR actually changes runnable test
  files,
- treat no-test PRs as fingerprint-only,
- and leave full implementation planning to a separate
  `docs/plans/sub-issue-test-runs-implementation-plan.md`.

That hybrid follow-up is **future work**. This spike does not plan or implement
it.
