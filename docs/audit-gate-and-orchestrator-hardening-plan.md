# Plan: Audit Gate Stability, Orchestrator Loop Hardening, and Template Onboarding Stub

This plan captures seven concrete defects observed in production orchestrator
runs (April 2026) and groups them into three independently shippable projects.
None of these items are addressed by tracking issue #1608 ("Finish validation
template rollout with real fixture self-tests and status tracking") because
that project is scoped to nightly self-test wiring, status publishing, and
docs finalization. The defects below survive #1608's merge and are filed here
so the orchestrator can pick them up as the next planning cycle.

The three projects are independent and can run in parallel:

1. **Audit Gate Stability** — fix the `npm audit` allowlist matching key,
   ship a deterministic regen mode, fix the planner so it stops generating
   unsolvable fix-up issues, and fix the templating gap that ships a
   `package.json` script reference without vendoring the script.
2. **Orchestrator Loop Hardening** — add a same-failure fingerprint circuit
   breaker to the judge-fixup loop, and stop `/judge_resume` from silently
   zeroing the validation recovery budget.
3. **Template Onboarding Stub** — close the first-cycle gap where
   template-mode validation aborts on missing `.ai/validate.yml`.

---

## Project A — Audit Gate Stability

### Goals

1. Make the npm audit allowlist stable across `npm audit` runs so allowlisted
   findings stop flapping when transitive resolution shifts.
2. Replace hand-edited allowlist surgery with a deterministic regen mode.
3. Stop the planner from generating fix-up issues whose acceptance criterion
   is unreachable by construction.
4. Make the audit gate template atomic: when a template apply step adds
   `audit:ci` to `package.json`, it must also vendor the referenced script.

### Non-Goals

- Replacing `npm audit` with a different audit backend.
- Auto-rotating `expiresOn` dates.
- Cross-ecosystem (pip, cargo) allowlist unification.

### Sub-Issues

#### A1 — Drop `viaPackages` from the audit-finding identity key

**Problem.** The validator's finding-ID composer (`composeFindingId` in
`scripts/security/check-npm-audit.js`) and the exact-Map-key match in
`evaluatePolicy` include `viaPackages` (the transitive resolution path) in
the matching key. `viaPackages` churns whenever lockfile resolution shifts
(npm version bumps, deduped graphs, hoisting changes), so allowlist entries
silently drift out from under themselves.

**Fix.** The matching key must be `severity + package` (optionally
`+ advisorySources` or the GHSA ID). `viaPackages` becomes informational
metadata on each entry, not part of the identity.

**Acceptance.** A finding allowlisted under one lockfile resolution is still
recognized after `npm install` produces a different `viaPackages` chain for
the same `severity + package`.

#### A2 — Add `audit:ci --write` regen mode

**Problem.** Implementers are told to hand-edit `dependency-audit-allowlist.json`
to satisfy an exact-match validator. This is unreliable and is the proximate
cause of multiple cycles of churn on PR #178.

**Fix.** Add a `--write` flag to the `audit:ci` script that rewrites
`dependency-audit-allowlist.json` from the current `npm audit --json`,
preserving `reason`, `owner`, and `expiresOn` when an existing entry's
matching key (per A1) matches.

**Acceptance.** `npm run audit:ci -- --write` produces a valid allowlist on
a clean checkout, and a follow-up `npm run audit:ci` (no flag) exits 0.
Running `--write` twice is a no-op when the audit output is unchanged.

#### A3 — Stop the planner from creating unsolvable fix-up issues

**Problem.** The planner has been generating fix-up issue bodies whose
acceptance criteria say "make `npm audit` deterministic" or "match the
validator's normalization exactly." Once A1 lands, no implementer can satisfy
this; before A1, no implementer can satisfy it either, because the input
changes between runs.

**Fix.** When the gate's failure reason is identified as `audit:ci`, the
planner must propose A1+A2 work, not "match the validator." If A1+A2 are not
yet landed, the planner must escalate rather than spawn another cycle.

**Acceptance.** No fix-up issue body contains the string "match the
validator's normalization." After A1+A2 land, the planner stops generating
audit-gate fix-ups for upstream-driven `npm audit` churn entirely.

#### A4 — Vendor `check-npm-audit.js` whenever `package.json` references it

**Problem.** A template apply step in this repo injects
`"audit:ci": "node scripts/security/check-npm-audit.js"` into a consumer
`package.json` without dropping the script in `scripts/security/`. PR #168
spent a whole cycle re-introducing the missing file (#177 → #178), proving
the two halves are split across template units.

**Fix.** The same template unit that mutates `package.json` must also copy
`scripts/security/check-npm-audit.js` (and any required helper). Equivalently,
the `package.json` mutation may be made conditional on the script being
present in the source tree.

**Acceptance.** A fresh consumer-repo apply produces a working
`npm run audit:ci` on the first cycle. The contract is exercised by an
existing or new fixture in the nightly self-test matrix.

### Dependencies (within Project A)

- `A1 → A2` (regen merge rule depends on the new identity key).
- `A2 → A3` (planner can only stop proposing the unsolvable goal once a
  solvable alternative exists).
- `A4` is independent and can run in parallel.

---

## Project B — Orchestrator Loop Hardening

### Goals

1. Detect and break loops where the judge fails for the *same reason* across
   consecutive cycles.
2. Preserve the validation-recovery budget across `/judge_resume` so that
   "human required" stays "human required."

### Non-Goals

- Replacing `MAX_VALIDATION_RECOVERY_ATTEMPTS` (additive only).
- Replacing `INTEGRATION_CONFLICT_LIFETIME_MAX` (different loop, different cap).
- Touching the integration-sync resolver loop.

### Sub-Issues

#### B1 — Same-failure fingerprint circuit breaker on the judge-fixup loop

**Problem.** `INTEGRATION_CONFLICT_LIFETIME_MAX`
(`scripts/orchestrate_poll_process.sh:980`, used at `:2920`) caps the
integration-sync resolver loop only. The judge → fixup → implement loop is
governed by `MAX_VALIDATION_RECOVERY_ATTEMPTS` (`:3753`), which counts
attempts but does not notice that the *same* failure is repeating cycle after
cycle. Production has observed the same `npm run audit:ci` justification
across cycles 5–9.

**Fix.** Fingerprint the judge's `Justification` (after stripping cycle
numbers, timestamps, and `file:line` offsets) and, after N consecutive
cycles with the same fingerprint, refuse to spawn another fix-up issue and
escalate to a human comment.

**Design notes.**

- New env knob `JUDGE_REPEAT_FINGERPRINT_MAX` (default `2`), per CLAUDE.md
  §4 (always provide defaults).
- Strip rules need a small test corpus so the hash is stable across cosmetic
  variation but sensitive to genuinely new failure modes.
- Additive — does not replace `MAX_VALIDATION_RECOVERY_ATTEMPTS`. Whichever
  fires first wins.
- Persist the rolling fingerprint window in the orchestrator state file under
  a new field; treat absence as zero (idempotent).

**Acceptance.** A synthetic test that returns the same judge justification
across N+1 cycles escalates to a human comment instead of dispatching another
fix-up issue.

#### B2 — `/judge_resume` must preserve `validation_recovery_count`

**Problem.** Comment dated `2026-04-25T13:22:14Z` on a tracking issue shows
`Recovery count reset: 3 → 0` on resume. Silently zeroing the budget converts
"recovery exhausted, human required" into "infinite retries via human pressing
resume," defeating the purpose of the budget.

**Fix.** Default `/judge_resume` behavior preserves `validation_recovery_count`.
Clearing it requires an explicit `--force` (or `--reset-recovery`) flag.

**Acceptance.** Resuming a project with `validation_recovery_count = 3` and
`MAX_VALIDATION_RECOVERY_ATTEMPTS = 3` does not dispatch a new judge cycle
unless `--force` was passed. The reset path remains available with the flag.

### Dependencies (within Project B)

- B1 and B2 are independent and can run in parallel.

---

## Project C — Template Onboarding Stub

### Goals

Close the first-cycle cliff where template-mode validation aborts on a fresh
adopter because `.ai/validate.yml` is absent.

### Non-Goals

- Generating manifest content from heuristics. The stub is a documented
  do-nothing-but-pass placeholder, not an inferred config.
- Reintroducing the freehand harness path (retired by the #1292 cutover).

### Sub-Issue

#### C1 — Generate a stub `.ai/validate.yml` on first apply

**Problem.** Cycles 1 and 2 of multiple fresh projects fail with:

```
Template mode requires /home/runner/work/.../.ai/validate.yml but it is missing.
```

The runtime validator in `scripts/validate_process.sh` aborts; the
workflow-templates copy step does not generate a stub. Fixture-repo wiring
(#1609) does not address this — fixture repos already commit a manifest.

**Fix preference.** The template apply step should drop a documented stub
`.ai/validate.yml` into adopter repos, with a comment pointing to the harness
docs. A silent fallback to a default in the validator is rejected because it
hides misconfiguration and runs the wrong tests.

**Stub location.** Likely `workflow-templates/_shared/` or a per-stack
template directory under `workflow-templates/validation-harness/`.

**Acceptance.** Running the workflow-templates apply step on an empty
consumer repo produces a working first-cycle `validate_process.sh` run. A new
fixture exercises the empty-repo path in the nightly self-test matrix.

---

## Shipping Order

Recommended order, cheapest first:

1. **C1** (onboarding cliff — single template change, immediate value).
2. **A4** (template atomicity — single template change, blocks A1–A3 from
   being properly testable).
3. **A1** → **A2** → **A3** (validator semantics, then the operational mode,
   then the planner stops proposing the unsolvable goal).
4. **B2** (one-line default change with a flag).
5. **B1** (highest design surface — fingerprint corpus and state-file
   migration).

A, B, and C are independent projects; their sub-issues only have intra-project
dependencies as noted above.

---

## Out of Scope

- Anything inside the open project #1608 (nightly fixture-repo wiring,
  status/streak publisher, plan-doc snapshots) — that work is being completed
  separately under #1626 and does not interact with this plan.
- Auto-merge gating, label routing, and PR cadence (unchanged).
- DB contracts (no `/db/contracts/*.yml` changes are required by this plan).
