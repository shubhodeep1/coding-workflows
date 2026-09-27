# Rejected single-reviewer findings stop blocking Claude-fixer PRs, and hold comments name their real reason

Source issue: shubhodeep1/coding-workflows#4586 (https://github.com/shubhodeep1/coding-workflows/issues/4586)
Base branch: main
Security pass: run

## Summary

A consensus finding raised by exactly one reviewer and explicitly rejected by a majority of the other reviewers no longer becomes a blocking Claude-fixer hand-off: it moves to a visible `NON-BLOCKING FINDINGS` ledger block. Separately, `claude_fix_claim.py` hold comments state the actual reason for the hold and cite the hand-back cap only when the cap was reached.

## Context

On conformance-fix PR #4575 (project for #4550), review run 36246765869 reported one consensus finding (`README.md:1261`, flagged only by `google_gemini-3_1-flash-lite`) that the other five pass-2 reviewers called a false positive. `scripts/summarize_reviewer_consensus.sh` keeps every singleton ("Do not suppress singletons"), and `scripts/review_autofix_step_claude_fixer_handoff.sh` counts every `- ` bullet in every ledger block as blocking, so the round was handed to Claude. The fixer judged it invalid, had no dedicated verdict bot, and followed `.claude/commands/fix-claude-pr.md` step 5 to post a hold. The #4550 chain sat for about 8.5 hours.

The hold comment itself (`.claude/scripts/claude_fix_claim.py` `claim_body()`) always says the PR "reached the cap of {cap} Claude hand-backs", although `check_in_status.py` counts only `conflict` / `ci` / `blocked` claims (`FIX_CLAIM_COUNTED_KINDS`) and #4575 had two `review` claims (`hand_backs` = 0).

Today no reviewer has a structured way to reject a pass-1 finding: pass-2 reviewers see the pass-1 ledger through `build_cross_pollination_summary` in `scripts/review_run_reviewers.sh` and can only disagree in prose.

## Goals

- In Claude-fixer mode, a round whose only consensus finding is raised by one reviewer and explicitly rejected by a majority of the other successful pass-2 reviewers posts no `ai:claude-fixer-handoff` marker and takes the existing zero-findings auto-merge path (fresh ready same-head check snapshot still required).
- The demoted finding stays visible: the posted ledger carries it in a `=== NON-BLOCKING FINDINGS ===` block with the rejecting reviewers named, in both the hand-off case and the clean case.
- A singleton finding the other reviewers did not address, or rejected by fewer than a majority, stays blocking exactly as today.
- Hold comments name the real reason; the cap sentence appears only for `--reason cap`. The `<!-- ai:claude-fix-claim:v1 head=… kind=hold by=… -->` marker is byte-for-byte unchanged (§6).
- Tests cover both; `README.md`, `agents.md`, `.claude/commands/fix-claude-pr.md` (and its template copy), and a `changelog.d/` fragment describe the behaviour.

## Non-goals

- A verdict bot for the fixer (explicitly out of scope in the issue).
- Changing the GPT editor path, the memory-record step, or the ledger any non-Claude-fixer consumer reads.
- Demoting task gaps (`CONSENSUS TASK GAPS`); only `CONSENSUS FINDINGS` entries are eligible (AD-4).
- Single-pass review mode: reviewers there never see each other, so no rejection exists and nothing changes.

## Constraints

- §1: the rule must fail toward blocking. A missing or failing filter, an unparseable ledger entry, or a rejection that cannot be matched leaves the finding blocking.
- §5: extend the existing hand-off step and claim writer; no new workflow step (the workflow file is 451,394 bytes, under the §27 480,000-byte guard; it is not edited).
- §6: no identifier renamed. `claim_body(head, kind, by)` keeps its positional signature (a new keyword-only `reason` parameter is added), the claim marker format is unchanged, and hand-off markers are unchanged.
- §9: tabs in Python and in the scripts that already use tabs; the hand-off script keeps its existing 2-space style.
- §15: no new GitHub API calls. In the clean-with-demotions case the ledger is posted through the existing `post_review_comment.sh` (the same chunked comments a hand-off round already posts).
- §20: one `changelog.d/4586-…` fragment.
- §27: the new Python helper lives in `scripts/` and is staged through `REQUIRED_BOOTSTRAP_SCRIPTS`.
- `.claude/` template copies under `workflow-templates/.claude/` stay byte-identical (tests enforce it).

## Approach

**Defect 1: rejected singletons.**

1. **Structured rejection.** The cross-pollination header pass-2 reviewers see (`build_cross_pollination_summary`) asks them to report each pass-1 ledger entry they verified to be wrong with one line:
   `REJECTED_FINDING: <file>:<line or start-end> | flagged_by: <slug from the ledger entry> | reason: <one sentence>`.
2. **Summariser.** `summarize_reviewer_consensus.sh` is told that `REJECTED_FINDING:` lines are verdicts, never findings (never emitted as bullets), and adds an informational `rejected_by: [...]` line to the matching consensus entry. The gate does not trust this field.
3. **Deterministic filter.** A new `scripts/review_claude_fixer_nonblocking.py` reads the consensus ledger plus the raw pass-2 outputs `${PREVIOUS_REVIEWS_DIR}/review_<slug>.txt` of every reviewer whose `status_review_<slug>.txt` reads `success`. A `CONSENSUS FINDINGS` entry is demoted when all of these hold:
   - its header parses as `- <file>:<start>[-<end>] |` and its `flagged_by` lists exactly one slug `F`;
   - at least two successful reviewers other than `F` (AD-3) each have a `REJECTED_FINDING:` line for the same file, whose line range overlaps the entry's range or lies within 3 lines of it (AD-7), and which names `flagged_by: F`;
   - those rejecting reviewers are a strict majority of the successful reviewers other than `F`.
   The filter writes a copy of the ledger where each demoted entry moves into a `=== NON-BLOCKING FINDINGS ===` block, inserted after `CONSENSUS TASK GAPS`, with a `rejected_by: [...]` line and the sentence "not handed to Claude: raised by one reviewer, rejected by N of M others". Matching per-reviewer bullets move there as well: same file and range, in the flagger's section or a rejecter's section. A section left empty gets `(No findings reported.)` / `(No task gaps reported.)` back, so the existing clean-ledger check still recognises it. It prints one `CLAUDE_FIXER_NONBLOCKING demoted=<n> …` line.
4. **Hand-off step.** `review_autofix_step_claude_fixer_handoff.sh` runs the filter only when the ledger passed its existing well-formedness check, uses the filtered copy for counting, the clean check, the ledger digest, and posting, and falls back to the original ledger on any filter failure. When the filtered ledger is clean, checks are ready, and entries were demoted, it posts the ledger (best-effort, warning on failure) before exporting `CLAUDE_FIXER_ZERO_FINDINGS=true`. When a hand-off still happens, its comment says how many entries are non-blocking.

**Defect 2: hold reason.** `claude_fix_claim.py` gains `--reason` for `kind=hold`, with values `cap`, `review-no-verdict-bot`, `conflict-decision`, `ci-outside-pr`, `workflow-failure`, `needs-human` (AD-6). Each maps to one sentence; only `cap` prints the cap text. A hold without `--reason` gets a neutral sentence ("stopped at a decision it cannot make alone") and no cap claim. `--reason` with a non-hold kind is refused. `fix-claude-pr.md` names the reason at each hold site.

Alternatives considered: trusting a summariser-written `rejected_by` field (rejected; a model error could unblock a real defect), and rewriting the shared ledger for every consumer (rejected; §5, it would change the GPT editor and memory inputs).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: one issue, one phase, one chain.

1. **Phase 1: non-blocking rejected singletons, and truthful hold reasons.**
   - Files: see [Files & Modules](#files--modules).
   - Done when: the new filter tests, the extended hand-off tests, and the extended claim tests pass; `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_review_autofix_step_scripts_contract.py`, and `tests/test_workflow_file_size_limit.py` pass; the template copies are byte-identical; docs and the changelog fragment describe the behaviour.
   - Rollback: revert the phase PR. Every change is additive and the filter fails toward blocking, so a revert restores today's behaviour with no data or state to clean up.

## Implementation Steps

Phase 1:

1. `scripts/review_run_reviewers.sh` `build_cross_pollination_summary`: add the `REJECTED_FINDING:` instruction lines to the header.
2. `scripts/summarize_reviewer_consensus.sh`: add the `REJECTED_FINDING` handling rule and the optional `rejected_by:` line to the prompt's CONSENSUS FINDINGS format.
3. `scripts/review_claude_fixer_nonblocking.py` [new]: the deterministic filter (CLI: `--ledger`, `--reviews-dir`, `--output`; exit 0 with the output written, non-zero on any error).
4. `scripts/stage_workflow_support.sh`: append `review_claude_fixer_nonblocking.py` to `REQUIRED_BOOTSTRAP_SCRIPTS`.
5. `scripts/review_autofix_step_claude_fixer_handoff.sh`: run the filter (support dir, then `.codex-workflow-src/scripts` fallback), switch the counted/digested/posted ledger to the filtered copy, post it in the clean-with-demotions case, and report the non-blocking count in the hand-off comment and the `CLAUDE_FIXER_HANDOFF` log line.
6. `.claude/scripts/claude_fix_claim.py` and its `workflow-templates/.claude/scripts/` copy: `--reason` for holds, reason-specific text, docstring update.
7. `.claude/commands/fix-claude-pr.md` and its template copy: pass `--reason` at every hold site; say that `NON-BLOCKING FINDINGS` entries need no fix or verdict.
8. `.claude/commands/implement-plan-claude.md` and its template copy, step 7a: one sentence that `NON-BLOCKING FINDINGS` entries are not findings to judge.
9. Tests: `tests/test_review_claude_fixer_nonblocking.py` [new], extended `tests/test_review_autofix_claude_fixer_mode.py` and `tests/test_check_in_status_hand_back.py`; wire the new test file into the existing Claude-fixer step in `.github/workflows/ci.yml`.
10. Docs: `README.md` ("Claude fixes every claude/* PR" and the `CLAUDE_FIXER_ENABLED` row), `agents.md` (Claude-fixer mode and the claims bullet), and `changelog.d/4586-rejected-singleton-findings-hold-reason.md`.

## Files & Modules

- `scripts/review_claude_fixer_nonblocking.py` [new]
- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `scripts/review_run_reviewers.sh`
- `scripts/summarize_reviewer_consensus.sh`
- `scripts/stage_workflow_support.sh`
- `.claude/scripts/claude_fix_claim.py`, `workflow-templates/.claude/scripts/claude_fix_claim.py`
- `.claude/commands/fix-claude-pr.md`, `workflow-templates/.claude/commands/fix-claude-pr.md`
- `.claude/commands/implement-plan-claude.md`, `workflow-templates/.claude/commands/implement-plan-claude.md`
- `tests/test_review_claude_fixer_nonblocking.py` [new], `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_check_in_status_hand_back.py`
- `.github/workflows/ci.yml`
- `README.md`, `agents.md`
- `changelog.d/4586-rejected-singleton-findings-hold-reason.md` [new]

## Tests

- Unit (new filter): demotes the #4575 shape (one flagger, 5 of 5 others reject); keeps a singleton nobody addressed; keeps one rejected by a minority; keeps one rejected by a single reviewer out of one other (floor of 2); ignores rejections from failed or missing reviewers and from the flagger; ignores rejections naming another flagger or a distant line; never demotes multi-reviewer entries or task gaps; moves matching per-reviewer bullets and restores empty-section placeholders; leaves an unparseable entry blocking; writes an unchanged copy when nothing is demoted.
- Hand-off step (extended): the #4575 shape exports `CLAUDE_FIXER_ZERO_FINDINGS=true`, posts no hand-off marker, and posts the ledger once; a mixed round hands off with the non-blocking count and the filtered-ledger digest; a filter failure (script missing) falls back to today's blocking behaviour.
- Claim writer (extended): cap text only for `--reason cap`; each reason's sentence; neutral text with no reason; `--reason` refused for non-hold kinds; marker unchanged and still parsed by `check_in_status.FIX_CLAIM_RE`; template copies identical.
- Regression: the full existing Claude-fixer, hand-back, step-script contract, and workflow-size suites.

## Risks & Mitigations

- A real defect raised by one reviewer is wrongly rejected by most others → ACCEPTED: this is the issue's chosen trade-off. The floor of two rejecters, the flagger-name match, and the visible NON-BLOCKING block (plus the whole-project final review) bound it.
- Reviewers mangle the `REJECTED_FINDING` line or the flagger slug → the entry stays blocking (today's behaviour).
- The summariser turns a rejection into a bullet → the filter moves same-location bullets in rejecters' sections; any other bullet still blocks.
- A stale raw output from an earlier run → only reviewers whose current `status_review_<slug>.txt` reads `success` count, the same rule `reviewer_count_success_statuses` uses.

## Rollout

Ships with the next `@stable` release; consumer repos pick up `scripts/` through the staged support bundle and `.claude/` through the existing sync (§14, no registry change). No flag: the behaviour applies only in Claude-fixer mode (`CLAUDE_FIXER_ENABLED`, default on) and fails toward blocking. Rollback is a revert.

## References

- Issue #4586; incident PR #4575 (project for #4550); review runs 36246765869 and 36246738117.
- CLAUDE.md §26.H (claims and holds), §27 (workflow size), §28 (auto-decisions).

## Auto-decisions

- AD-1 [plan, 2026-09-27] Where does the "explicitly rejected" signal come from? — Picked: A — a structured `REJECTED_FINDING:` line each pass-2 reviewer emits, read deterministically from its raw output. Alternatives: B — a `rejected_by:` field the summariser model writes; C — infer it from WHY prose. Why: a summariser error cannot unblock a real defect (§1). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-27] Where does the demotion apply? — Picked: A — only in the Claude-fixer hand-off step, on a filtered copy of the ledger. Alternatives: B — rewrite the shared ledger for every consumer. Why: the GPT editor judges findings itself and the memory step keeps its input (§5). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-27] How many rejecters are needed? — Picked: A — a strict majority of the other successful reviewers and at least two rejecters. Alternatives: B — a strict majority only. Why: with a two-reviewer panel, one dissent alone must not clear a finding (§1). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-27] Do task gaps get the same rule? — Picked: A — no, only CONSENSUS FINDINGS entries. Alternatives: B — task gaps too. Why: the issue scopes the change to findings (§5). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-27] How does a demoted finding stay visible when nothing else blocks? — Picked: A — post the filtered ledger (with its NON-BLOCKING block) before auto-merge, best-effort. Alternatives: B — only a workflow log line. Why: the issue requires it to stay visible on the PR. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-27] How does the hold comment learn its reason? — Picked: A — a `--reason` enum on `claude_fix_claim.py` (`cap`, `review-no-verdict-bot`, `conflict-decision`, `ci-outside-pr`, `workflow-failure`, `needs-human`), with neutral text when omitted. Alternatives: B — free-text `--reason`; C — `post` reads the claims to compute `cap_reached` itself. Why: fixed sentences are testable and safe in a comment, and C adds API reads (§15). Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-27] How close must a rejection's line be to the finding? — Picked: A — same file, range overlapping or within 3 lines, and naming the same flagger. Alternatives: B — the summariser's 5-line dedupe window without the flagger match; C — exact line only. Why: #4575's rejections cited line 1262 for a 1261 finding; the flagger match keeps a nearby different finding from being swept up. Applied in: phase 1. Status: pending review
