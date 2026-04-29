# E2E Smoke Test Failure — run 25126757724 (2026-04-29)

## TL;DR

The `e2e-smoke-test` job in `test-and-mark-stable.yml` failed at the **"Phase 4b: Verify editor removed bait line"** step (`status=bait_remained`, exit 1).

**Root cause (verified): the review-autofix editor model (`openai/gpt-5.3-codex`, `EDITOR_REASONING_EFFORT=xhigh`) ran for ~6 minutes, made ~10 read-only tool calls, observed the bait line at `tests/e2e_smoke_canary.txt:4`, and exited at 19,272 tokens without writing any edit.** `scripts/review_apply_fixes.sh` detected an empty/failure-marker editor summary, wrote a 600-byte fallback summary, and the deferred autofix push therefore never happened. The bait line stayed on the PR head, so the e2e gate (which is exactly designed to catch this) correctly failed.

The smoke test caught a real regression in the editor pipeline — it is **not** a flake in the e2e gate itself.

---

## Evidence chain (all timestamps UTC)

### 1. e2e job log (run 25126757724, job 73642869884)

- **18:37:14** — bait commit `90fe0da5` injected on `ai/issue-1783` (test-and-mark-stable.yml:740-826).
- **18:37:15** — review-autofix poller started; PR head fixed at `90fe0da`.
- **18:37:34 → 19:04:13** — review-autofix run #25127054791, job `review / codex-agent` (id 73642854993) ran for ~26 min; conclusion `failure`.
- **19:03:30** — poller's `FAILED_STEPS=1` shortcut fired (test-and-mark-stable.yml:935-941): step `Run Codex resolver, validate, stage, commit` had concluded with failure while subsequent cleanup steps were still in_progress. Poller exited `status=completed_with_findings` (treated as success → control falls to verify-bait).
- **19:03:30** — Phase 4b re-fetched canary on `ai/issue-1783`. The 4 lines came back with the bait still present (`# E2E_EDITOR_BAIT_25126757724: this line should be removed by the editor (smoke gate)`). Step exited 1 → job failed.

### 2. review-autofix codex-agent job log (job 73642854993)

Step transitions (per e2e poller probes):

| Time | Step | Notes |
|---|---|---|
| 18:38:44 | Install dependencies | |
| 18:39:08 | Setup Serena MCP server | |
| 18:47:30 | Detect smoke test and tune LLM settings | |
| 18:47:42 → 18:53:48 | Run reviewer models | 6 reviewers all flagged the bait at `tests/e2e_smoke_canary.txt:4`, confidence 5 (consensus file in log) |
| 18:53:49 → 18:59:55 | **Apply fixes with editor model** | Editor invoked codex exec with `MODEL_EDITOR=openai/gpt-5.3-codex`, `EDITOR_REASONING_EFFORT=xhigh`. Editor ran read-only commands (`cat`/`nl -ba`/`ls`) and exited at `tokens used 19,272` — **no Edit/Write tool call** |
| 18:59:54 | `::warning::Editor summary contains failure/fallback markers — editor never completed a validated review.` | Set `EDITOR_NOOP_SUSPICIOUS=true` (review_autofix.yml:3104-3157) |
| 18:59:54 | Editor summary comment posted to PR (#1787 comment 4346699142): "Changes made: none (editor failed before producing a validated summary)" | |
| 18:59:56 → 19:00:08 | Detect merge conflicts | Set `MERGE_CONFLICT=true` (review_autofix.yml:3263-3504) |
| 19:00:08 → 19:03:25 | Run Codex resolver, validate, stage, commit | Concluded `failure` (`##[error]Process completed with exit code 1.` at line 16387 of the codex-agent log) |
| 19:04:00 | Cleanup posted: "AI review/autofix encountered a post-editor failure — needs human intervention" + `ai:review-blocked` label | |

### 3. PR `ai/issue-1783` final state

- `additions: 2, deletions: 1, commits: 2` — the only commits are `2d282a78` (implementation) and `90fe0da5` (bait). **No editor or resolver commit was ever pushed.**
- PR was closed at 19:03:51 by the e2e cleanup step.

---

## Root cause analysis

### Primary failure: editor model produced no edit

From the codex-agent log (lines 14000-14177), the editor was given:

1. `pr_diff.patch` showing the +1 bait line at `tests/e2e_smoke_canary.txt:4`.
2. `reviewer_consensus.txt` with 6/6 reviewers flagging the bait at confidence 5 ("file no longer matches the required exact three-line canary content").
3. `linked_issue_context.txt` quoting issue #1783's task verbatim ("Update its content so it is **exactly** these three lines").
4. `nl -ba tests/e2e_smoke_canary.txt` output showing the bait at line 4.

Despite this, the editor (gpt-5.3-codex) ran ~10 read-only `bash -lc` calls, never invoked an editing tool, and exited cleanly at 19,272 tokens. `scripts/review_apply_fixes.sh` detected the empty/failure summary and wrote a fallback ("editor failed after retries before final classification"), so `DID_COMMIT=false` propagated downstream.

There are two contributing signals visible in the input the editor saw:

- **`CLASSIFICATION: unclassified`** on the issue passthrough at line 14148-14156 of the codex-agent log:
  ```
  === ISSUE PASSTHROUGH 001 ===
  FILE: tests/e2e_smoke_canary.txt
  LINES: 4
  CLASSIFICATION: unclassified
  EVIDENCE:
    reviewer_bundle> Line: 4
  NOTES:
    Anchor was not covered by consolidator output; passthrough emitted by parser.
  === END ISSUE PASSTHROUGH 001 ===
  ```
  The consolidator stage (`XPOLL_SUMMARISER_MODEL=openai/gpt-5.4-mini`, reasoning=medium) did **not** emit a structured finding for line 4 — the parser had to fall back to a `passthrough` issue with no classification. This means the editor saw the issue tagged as `unclassified` and `iss_…  NEW 1 tests/e2e_smoke_canary.txt:4 UNKNOWN_LENS none`, despite the matching `FLOOR_HIGH_CONFIDENCE` floor tag.

- **`FLOOR_HIGH_CONFIDENCE`** floor tag was present (line 14159), but evidently was not weighty enough to override `unclassified`/`UNKNOWN_LENS` in the editor's reasoning.

### Secondary failure (cascading): merge-conflict resolver

`Detect merge conflicts` set `MERGE_CONFLICT=true`, even though `main` had not advanced past PR base (PR base.sha `f6b39b43` was committed at 18:30:37; PR opened at 18:36:59; no main pushes since). The most likely path is the defensive fallback at review_autofix.yml:3460-3486 — `git merge` exited non-zero without producing unmerged index entries (e.g. untracked-file collision) — which gets treated as a conflict. The Codex resolver then attempted to resolve a non-conflict, exited 1, and was the step the e2e poller saw as `FAILED_STEPS=1`.

This cascading failure is what made the e2e poller exit early (rather than wait for the full 30-minute review timeout), but it is **not** the proximate cause of `bait_remained`. Even if the resolver had succeeded, there was no editor commit staged for it to push — `DID_COMMIT=false` was already locked in before the resolver ran.

---

## What's NOT broken

- **e2e gate logic.** Phase 4b correctly identified that the editor pipeline produced no fix and failed loudly. This is the test working as designed (test-and-mark-stable.yml:1058-1098).
- **Reviewer models.** All 6 reviewers (deepseek-v4-pro, minimax-m2.5, kimi-k2.5, qwen3.6-plus, grok-4.1-fast, glm-5) correctly flagged the bait at confidence 5.
- **Bait-injection step.** `Phase 3c: inject editor bait line` (test-and-mark-stable.yml:740-826) injected the bait correctly and the PR-head-stability check confirmed the parent SHA before pushing.
- **Reviewer→editor handoff.** `reviewer_consensus.txt` was correctly assembled and passed to the editor prompt.

---

## Proposed remediation

In rough priority order. **No changes have been made yet** — this is a proposal pending your approval.

### A. Fix the consolidator → editor handoff so an `unclassified` finding cannot override unanimous reviewer consensus

The consolidator (`scripts/summarize_reviewer_consensus.sh` driven by `XPOLL_SUMMARISER_MODEL`) failed to emit a structured finding for `tests/e2e_smoke_canary.txt:4` despite 6/6 reviewers flagging it at confidence 5. The fallback parser then emitted a `passthrough` issue with `CLASSIFICATION: unclassified` / `UNKNOWN_LENS` / `disposition=none`, which the editor evidently downweights.

**Fix candidates** (one of, not all):

- **A1.** When the consolidator fails to classify an anchor that the floor-tag step already promoted to `FLOOR_HIGH_CONFIDENCE`, copy the floor tag's classification onto the passthrough record. The parser already has both inputs; the wiring is the gap. Files to inspect: `scripts/summarize_reviewer_consensus.sh`, `scripts/review_apply_fixes.sh` (passthrough emit path), and the editor prompt block that lists `floor_tags.txt`.
- **A2.** Tighten the editor's prompt (`prompts/review_apply_fixes_editor*`) so a `FLOOR_HIGH_CONFIDENCE`-tagged anchor is **mandatory to address** even when the lens classification is `UNKNOWN_LENS` / `unclassified`. The current prompt's "validate before editing" guidance combines with the unclassified label to give the editor an out.
- **A3.** Increase consolidator reasoning effort or switch model. `XPOLL_SUMMARISER_MODEL=openai/gpt-5.4-mini`, `reasoning=medium` looks too weak for the volume of reviewer evidence it processes (each reviewer's full bundle is fed in). For a 6-reviewer-unanimous finding to drop on the floor is a quality signal.

### B. Add a hard guard: empty editor summary + `EDITOR_NOOP_SUSPICIOUS=true` should fail-loud, not silently continue

Currently the `Apply fixes with editor model` step exits 0 when `review_apply_fixes.sh` writes the fallback summary, then `Validate editor no-op disposition` sets `EDITOR_NOOP_SUSPICIOUS=true`, and the workflow continues into `Detect merge conflicts` and `Run Codex resolver` — both of which are wasted work because there's no commit to push.

**Fix:** in `review_autofix.yml`, gate `Detect merge conflicts` and `Run Codex resolver, validate, stage, commit` on `env.EDITOR_NOOP_SUSPICIOUS != 'true'` (in addition to the existing `MERGE_CONFLICT == 'true'` guard for the resolver). Saves ~3.5 min of wasted runner time and removes the cascading `FAILED_STEPS=1` that confused the e2e poller.

### C. Decouple the e2e poller's "FAILED_STEPS=1" shortcut from cleanup-step failures

The poller in `test-and-mark-stable.yml:927-941` treats *any* failed step as "main work has failed, cleanup still running" and exits with `status=completed_with_findings`. In this run the failed step (`Run Codex resolver`) was a cascading consequence of the empty-editor failure that had already been written to the editor summary; the poller exited the wait at 19:03:30 even though the *root* failure was visible 3 minutes earlier (the `EDITOR_NOOP_SUSPICIOUS` warning at 18:59:54).

**Fix:** add a separate, earlier shortcut: if the live job log contains the literal `Editor summary contains failure/fallback markers` annotation, exit the wait immediately with the existing `completed_with_findings` status. Saves ~3.5 min on every empty-editor run and avoids the resolver's confusing cascade.

### D. Don't trigger merge-conflict detection / resolver when `EDITOR_NOOP_SUSPICIOUS=true`

Same as B but stated as a conditional rather than a gate addition. Either form is fine; pick whichever is cleaner against the existing `if:` chains.

### E. (Optional) bump editor reasoning effort or add a one-shot retry on empty summary, model-attributed

`AUTOFIX_INSTEP_RETRY_ENABLED=true` already exists (review_autofix.yml:2750-2782) but is gated on `_prior_autofix_count == 0` AND `[ ! -s "${EDITOR_SUMMARY_FILE:-/dev/null}" ]`. In this run the editor wrote a 600-byte fallback summary, so the file was non-empty and the retry didn't fire. Loosen the retry gate to also trigger on `EDITOR_NOOP_SUSPICIOUS=true`, since the fallback-marker check is a stronger empty-output signal than file size.

---

## Action items

| # | Action | Owner | Cost |
|---|---|---|---|
| 1 | Decide which of A1/A2/A3 to pursue (consolidator passthrough hardening) | maintainer | discussion |
| 2 | Add `env.EDITOR_NOOP_SUSPICIOUS != 'true'` gate to merge-conflict detect & resolver steps in `review_autofix.yml` | maintainer | trivial (~30 min + tests) |
| 3 | Add empty-editor-marker shortcut to e2e poller in `test-and-mark-stable.yml` | maintainer | small (~1 hr + e2e re-run) |
| 4 | Loosen `AUTOFIX_INSTEP_RETRY` gate to also fire on `EDITOR_NOOP_SUSPICIOUS=true` | maintainer | small (~30 min) |
| 5 | Re-run the e2e smoke test with item 2 + 3 + 4 applied to confirm the cascade is cleaned up; the underlying editor-model regression (item 1) will still need separate investigation | — | one e2e run |

Items 2–4 are mechanical and low-risk. Item 1 is the actual root-cause fix and needs a maintainer call on which lever to pull.

---

## References

- e2e job log: `https://github.com/shubhodeep1/coding-workflows/actions/runs/25126757724/job/73642869884`
- review-autofix job log: `https://github.com/shubhodeep1/coding-workflows/actions/runs/25127054791/job/73642854993`
- PR: `https://github.com/shubhodeep1/coding-workflows/pull/1787`
- Issue: `https://github.com/shubhodeep1/coding-workflows/issues/1783`
- Bait commit: `90fe0da593cf5098605e4abf2bed02b27d377da9`
- Editor summary comment (fallback): `https://github.com/shubhodeep1/coding-workflows/pull/1787#issuecomment-4346699142`
