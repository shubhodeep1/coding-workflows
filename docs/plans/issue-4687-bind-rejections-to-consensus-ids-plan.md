# Bind reviewer rejections to pass-1 consensus ids so a nearby rejection cannot demote a distinct finding

Source issue: shubhodeep1/coding-workflows#4687 (https://github.com/shubhodeep1/coding-workflows/issues/4687)
Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
Security pass: skip (ai:security: automation-produced issue)

## Summary

`scripts/review_claude_fixer_nonblocking.py` stops matching `REJECTED_FINDING` lines to ledger entries by file and a 3-line window. Every pass-1 consensus finding gets a content-derived `consensus_id`, a rejection counts only when it cites that id, a pass-2 finding is tied to the pass-1 entry only through the same id carried by its own flagger, and any entry that could be confused with a nearby one stays blocking.

## Context

Issue #4687 is a security-audit follow-up (A08, high, `scripts/review_claude_fixer_nonblocking.py:223`) filed against the #4586 project branch. Issue #4586 made a consensus finding non-blocking in Claude-fixer mode when one reviewer raised it and a strict majority of the other successful pass-2 reviewers (at least two) rejected it with `REJECTED_FINDING: <file>:<line> | flagged_by: <slug> | reason: …`. The demoter (line 223) counts a rejection for an entry when the file and flagger match and the line range overlaps or lies within `LINE_TOLERANCE` (3) lines.

That proximity match is the defect. Pass-2 reviewers write `REJECTED_FINDING` lines against the **pass-1** ledger they see in the cross-pollination summary (`build_cross_pollination_summary` in `scripts/review_run_reviewers.sh`), but the demoter applies them to the **pass-2** ledger (`REVIEWER_CONSENSUS_FILE`). When the same reviewer reported a false positive and a distinct real flaw within three lines of each other, rejections of the false positive also match the flaw and demote it; if the checks are green the PR auto-merges with the flaw unhandled. The same happens when the flagger drops the false positive in pass 2 and raises a different defect next to it, or when a per-reviewer bullet near the demoted entry is swept into the non-blocking block. Nothing in the ledgers identifies which finding a rejection was about.

The pass-1 ledger is persisted as `${PREVIOUS_REVIEWS_DIR}/consensus_pass1.txt` (`review_run_reviewers.sh`, two-pass mode), the same directory the demoter already reads the raw `review_<slug>.txt` outputs from, so the demoter can recompute anything derived from it without a new artifact.

## Goals

- A `REJECTED_FINDING` line counts only when it cites the `consensus_id` of exactly one pass-1 `CONSENSUS FINDINGS` entry and its file, overlapping range, and `flagged_by` agree with that entry. An id-less (legacy) line is ignored and counted in a diagnostic line.
- A pass-2 `CONSENSUS FINDINGS` entry is demoted only when it carries exactly one `consensus_id` line naming that pass-1 entry, the flagger's own raw pass-2 output contains that id, the single flaggers agree, the ranges overlap, and the usual threshold holds (at least 2 rejecters and a strict majority of the other successful reviewers).
- Ambiguous matches stay blocking: another pass-1 consensus entry within 3 lines of the bound pass-1 entry, another pass-2 consensus entry within 3 lines of the demoted entry, or the id on more than one pass-2 consensus entry or more than one pass-1 entry.
- Per-reviewer bullets move with a demoted entry only from the flagger's or a rejecter's section, only when their range overlaps the entry's and their text carries the same id.
- Every scenario in the issue (rejections of a false positive next to a distinct flaw from the same reviewer) leaves the flaw blocking, proven by tests.
- The #4575 shape (one flagger, others reject by id) is still demoted, so issue #4586's behaviour survives.

## Non-goals

- Changing the threshold (`MIN_REJECTERS`, strict majority), the hand-off step, the auto-merge conditions, or the `NON-BLOCKING FINDINGS` block format.
- Any change to the GPT editor path, which does not run the demoter.
- Task gaps (still never demoted).
- Writing ids into `consensus_pass1.txt` or any ledger another consumer reads as input; ids appear only in the cross-pollination text reviewers see and, when reviewers cite them, in the pass-2 ledger.
- The workflow YAML (`.github/workflows/review_autofix.yml` is 451,394 bytes; it is not edited, §27).

## Constraints

- §1 security first: every failure mode (no pass-1 ledger, annotation failure, missing or duplicated ids, a reviewer or summariser that does not follow the id instructions) must leave the finding blocking.
- §5: extend `review_claude_fixer_nonblocking.py` and the two prompts that already carry the `REJECTED_FINDING` instructions; no new script, step, or artifact.
- §6: no existing identifier is renamed, removed, or repurposed. `REJECTED_FINDING_RE`, `LINE_TOLERANCE`, `_near`, `reviewer_rejections`, `demote(ledger_text, reviews_dir)`, the CLI flags, and the `CLAUDE_FIXER_NONBLOCKING demoted=<n> successful_reviewers=<m>` / `CLAUDE_FIXER_NONBLOCKING_ENTRY` log lines keep their meaning. The new ledger field is `consensus_id` (`finding_id` already names security-audit findings, agents.md), and the new names (`consensus_id`, `CONSENSUS_ID_RE`, `REJECTED_FINDING_ID_RE`, `--annotate`, `CLAUDE_FIXER_NONBLOCKING_KEPT`, `CLAUDE_FIXER_NONBLOCKING_LEGACY_REJECTIONS`, `CLAUDE_FIXER_CONSENSUS_IDS`) are unused in the repo today.
- §9: tabs in Python; `review_run_reviewers.sh` keeps its 2-space style and `summarize_reviewer_consensus.sh` its tabs.
- §15: no GitHub API calls are added.
- §20: one `changelog.d/4687-…` fragment in the `security` section.
- `review_claude_fixer_nonblocking.py` is already in `REQUIRED_BOOTSTRAP_SCRIPTS` (`scripts/stage_workflow_support.sh`), so the reviewer runner can call it from `SUPPORT_SCRIPTS_DIR`.

## Approach

1. **Ids.** `consensus_id(entry)` = `p1-` + the first 12 hex digits of the SHA-256 of the entry's lines (right-stripped, joined with `\n`, any existing `consensus_id:` line excluded). It is computed from `consensus_pass1.txt`, which is never rewritten, so the annotation and the demoter always agree and a regenerated ledger invalidates every old id instead of rebinding it.
2. **Annotation.** New `--annotate` mode of `review_claude_fixer_nonblocking.py` (`--annotate --ledger <pass-1 ledger> --output <file>`) writes a copy with `  consensus_id: <id>` inserted after the header line of every `CONSENSUS FINDINGS` entry and prints `CLAUDE_FIXER_CONSENSUS_IDS annotated=<n>`. `build_cross_pollination_summary` shows that copy; if the helper is missing or fails it shows the plain ledger as today, reviewers can then cite no id, and nothing is demoted.
3. **Reviewer instructions** (cross-pollination header): reject with `REJECTED_FINDING: <consensus_id> | <file>:<line or start-end> | flagged_by: <slug> | reason: …`; a line without the id is ignored; when re-reporting the same defect as a `CONSENSUS FINDINGS` entry, add `consensus_id: <id>` to the finding; never put an id on a different defect, even one on a nearby line.
4. **Summariser instructions** (`summarize_reviewer_consensus.sh`): copy a reviewer's `consensus_id:` line verbatim into the matching `CONSENSUS FINDINGS` entry and per-reviewer bullet; never invent one or take one from a `REJECTED_FINDING` line; omit it when merged findings carry different ids. The `REJECTED_FINDING` description gains the id.
5. **Demoter.** Parses `consensus_pass1.txt` from `--reviews-dir` (absent → nothing is demoted; unparseable → error, so the caller keeps the original ledger). Rejections are read with `REJECTED_FINDING_ID_RE` (id prefix), and the remainder is parsed by the existing `REJECTED_FINDING_RE` for file, range, and flagger. The binding and ambiguity rules in Goals decide; each single-flagger entry that stays blocking gets one `CLAUDE_FIXER_NONBLOCKING_KEPT file=<path:span> flagged_by=<slug> reason=<reason>` line (§8), and id-less rejection lines are counted in `CLAUDE_FIXER_NONBLOCKING_LEGACY_REJECTIONS count=<n>`.

Alternatives considered: keeping proximity and only requiring a rejection to match exactly one entry (AD-1 B; leaves the case where the rejected entry is gone from the pass-2 ledger and a new flaw sits next to it), and exact `file:line` equality without ids (AD-1 C; leaves the same-line reframing case and still trusts location alone).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: one issue, one phase, one chain.

1. **Phase 1: bind rejections to consensus ids and keep ambiguous matches blocking.**
   - Files: see [Files & Modules](#files--modules).
   - Done when: the extended `tests/test_review_claude_fixer_nonblocking.py` (id binding, every issue scenario, annotation, CLI) and `tests/test_review_autofix_claude_fixer_mode.py` (hand-off with ids) pass; `tests/test_review_autofix_step_scripts_contract.py`, `tests/test_workflow_file_size_limit.py`, and the reviewer-runner tests that exist for `review_run_reviewers.sh` pass; `bash -n` passes on both shell scripts; docs and the changelog fragment describe the behaviour.
   - Rollback: revert the phase PR. The change only narrows when a finding is demoted, so a revert restores the #4586 behaviour with no state to clean up.

## Implementation Steps

Phase 1:

1. `scripts/review_claude_fixer_nonblocking.py`: add `CONSENSUS_ID_RE`, `REJECTED_FINDING_ID_RE`, `consensus_id()`, `annotate()`, pass-1 ledger loading, id-bound rejection parsing, the binding and ambiguity rules, id-gated bullet moves, the `KEPT` / `LEGACY_REJECTIONS` diagnostics, the `--annotate` CLI mode (`--reviews-dir` required only for demotion), and the docstring.
2. `scripts/review_run_reviewers.sh` `build_cross_pollination_summary`: annotate the pass-1 ledger through the helper (plain ledger on failure) and replace the rejection instructions.
3. `scripts/summarize_reviewer_consensus.sh`: the `consensus_id:` format line and copy rule; the id in the `REJECTED_FINDING` description.
4. `scripts/review_autofix_step_claude_fixer_handoff.sh`: header comment only (rejections are bound by id).
5. Tests: rewrite the fixtures of `tests/test_review_claude_fixer_nonblocking.py` for ids (pass-1 ledger, cited ids, flagger citation) and add the issue scenarios, ambiguity, legacy lines, missing pass-1 ledger, annotation, and CLI tests; update `tests/test_review_autofix_claude_fixer_mode.py` (`_run_handoff` writes `consensus_pass1.txt`; fixtures cite ids).
6. Docs: `README.md` ("Rejected single-reviewer findings are not handed off" and the `CLAUDE_FIXER_ENABLED` row), `agents.md` (Claude-fixer mode), `docs/INVENTORY.md`, and `changelog.d/4687-bind-rejections-to-consensus-ids.md`.

## Files & Modules

- `scripts/review_claude_fixer_nonblocking.py`
- `scripts/review_run_reviewers.sh`
- `scripts/summarize_reviewer_consensus.sh`
- `scripts/review_autofix_step_claude_fixer_handoff.sh` (comment)
- `tests/test_review_claude_fixer_nonblocking.py`, `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`, `agents.md`, `docs/INVENTORY.md`
- `changelog.d/4687-bind-rejections-to-consensus-ids.md` [new]

## Tests

- Unit (demoter): the #4575 shape with ids is demoted; issue scenario A (false positive and distinct flaw by the same reviewer within 3 lines, both in both ledgers) keeps both blocking; scenario B (the false positive dropped in pass 2, a new flaw next to it) keeps the flaw blocking; the summariser attaching the false positive's id to the flaw on another line keeps it blocking; an id the flagger's raw output never cited keeps it blocking; duplicate ids in either ledger keep it blocking; id-less rejections are ignored and counted; a rejection whose id is right but whose file, range, or flagger disagrees is ignored; no pass-1 ledger demotes nothing; bullets move only with the same id; the flagger, failed reviewers, minority and single rejecters, multi-reviewer entries, and task gaps behave as before.
- Unit (annotation): ids are deterministic, inserted only into `CONSENSUS FINDINGS` entries, idempotent, and equal to what the demoter computes; the CLI writes the file and the log line, and fails on a missing ledger.
- Hand-off step: the rejected-singleton, mixed-round, and failing-filter tests run with a pass-1 ledger and cited ids; an id-less rejection hands the round off.
- Regression: the full Claude-fixer, step-script contract, and workflow-size suites; `bash -n` on the edited shell scripts.

## Risks & Mitigations

- Reviewers or the summariser do not propagate ids, so fewer singletons are demoted than under #4586 → ACCEPTED: the issue asks for ambiguous matches to stay blocking; a missed demotion costs one Claude review round, a wrong one can merge a defect. The `KEPT` diagnostic names the reason for each.
- The flagger itself puts the false positive's id on a different defect on the same lines → ACCEPTED (residual): the flagger has declared the two the same finding, the ranges overlap, no other entry is within 3 lines, and the non-blocking block keeps it visible on the PR.
- Annotation fails at runtime → the plain ledger is shown, no id can be cited, nothing is demoted.
- The pass-1 ledger is regenerated between pass 2 and the hand-off → content-derived ids no longer match, nothing is demoted.

## Rollout

Ships with the #4586 project (this issue's base branch) through its final PR, and from there with the next `@stable` release; consumers pick up `scripts/` through the staged support bundle (no `.claude/` change, no registry change, §14). No flag: the demoter runs only in Claude-fixer mode (`CLAUDE_FIXER_ENABLED`, default on) and fails toward blocking. Rollback is a revert.

## References

- Issue #4687 (this follow-up), parent issue #4586 and its project branch `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason` (final PR #4593), security audit tracker #3576.
- #4586 plan AD-7 (the 3-line window this plan supersedes for binding; the window is kept for ambiguity detection).
- CLAUDE.md §1, §5, §6, §8, §20, §26.H, §27, §28.

## Auto-decisions

- AD-1 [plan, 2026-09-28] How is a rejection bound to the finding it is about? — Picked: A — a content-derived `consensus_id` on every pass-1 consensus entry; a rejection counts only when it cites that id (with matching file, overlapping range, and flagger), and a pass-2 entry is tied to the pass-1 entry only by the same id carried by its flagger's own output. Alternatives: B — keep proximity and require each rejection to match exactly one entry; C — exact `file:line` equality without ids. Why: the issue's recommendation, and B and C still demote a new flaw beside a rejected entry the flagger dropped (§1). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-28] What counts as an ambiguous match that must stay blocking? — Picked: A — another pass-1 consensus entry within 3 lines of the bound pass-1 entry, another pass-2 consensus entry within 3 lines of the demoted entry, or the id on more than one entry in either ledger. Alternatives: B — only duplicate ids. Why: a mis-copied id between nearby entries is the likeliest summariser error, and the issue asks for ambiguous matches to stay blocking (§1). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-28] How is the id derived? — Picked: A — `p1-` plus 12 hex digits of the SHA-256 of the pass-1 entry text, recomputed from the never-rewritten `consensus_pass1.txt`. Alternatives: B — ordinal numbers; C — write ids into `consensus_pass1.txt`. Why: a regenerated or reordered ledger invalidates content ids instead of silently rebinding ordinals, and C changes an input other steps read (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-28] What happens to `REJECTED_FINDING` lines without an id? — Picked: A — ignored, and counted in a `CLAUDE_FIXER_NONBLOCKING_LEGACY_REJECTIONS` log line. Alternatives: B — still honoured by proximity. Why: B keeps the vulnerability open (§1); the prompt and the demoter ship in the same support bundle, so no reviewer sees the old instructions with the new demoter. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-28] Which per-reviewer bullets move with a demoted entry? — Picked: A — only bullets in the flagger's or a rejecter's section whose range overlaps the entry and whose text carries the same id. Alternatives: B — every bullet within 3 lines, as today. Why: B can move a distinct flaw's bullet out of the blocking count (§1). Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-28] What is the new ledger field called, and what happens to the identifiers the proximity match used? — Picked: A — `consensus_id`, and `REJECTED_FINDING_RE`, `LINE_TOLERANCE`, `_near`, and `reviewer_rejections` stay with their meaning (location parsing, the ambiguity window, and the legacy-line count). Alternatives: B — `finding_id`, and delete the proximity helpers. Why: `finding_id` already names security-audit findings, and §6 forbids removing identifiers. Applied in: phase 1. Status: pending review
