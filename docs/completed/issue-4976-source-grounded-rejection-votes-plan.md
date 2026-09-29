# Rejection votes must carry source-grounded evidence before they demote a finding

Source issue: shubhodeep1/coding-workflows#4976 (https://github.com/shubhodeep1/coding-workflows/issues/4976)
Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
Security pass: skip (ai:security: automation-produced issue)

## Summary

A `REJECTED_FINDING` vote in a pass-2 reviewer's output counts toward demoting a single-reviewer finding only when it carries an `evidence: <file>:<line>` citation plus a `quote:` that the Claude-fixer hand-off gate finds, verbatim, at those lines of the reviewed commit. A vote with only a free-text reason no longer counts, so the finding stays blocking. This closes security finding #4976 (A08, high) in `scripts/review_claude_fixer_nonblocking.py:525`.

## Context

- Issue #4586's project (base branch above) lets the Claude-fixer hand-off gate move a consensus finding raised by one reviewer into a `NON-BLOCKING FINDINGS` block when a strict majority of the other successful reviewers (at least two) reject it. When that was the last finding, the round takes the zero-findings path and `review_autofix.yml` auto-merges the PR.
- #4688 bound votes to run-issued `RF-<16 hex>` IDs, and #4687 bound IDs to pass-1 `consensus_id`s. Both are already on the base branch.
- #4976 (this issue): the pass-2 prompt shows those IDs next to PR-controlled text. If that text induces two reviewers to print `REJECTED_FINDING: <ID> | … | reason: false positive`, `_votes_in` counts the lines (`scripts/review_claude_fixer_nonblocking.py:520-525`) with no evidence that the reviewer checked the code. Demoting the last finding authorizes auto-merge. Recommendation: "Require automatically checked, source-grounded rejection evidence bound to each ID; otherwise keep the finding blocking."
- The hand-off step (`scripts/review_autofix_step_claude_fixer_handoff.sh`, sourced by the `Hand review round to Claude session (Claude-fixer mode)` step of `.github/workflows/review_autofix.yml`) runs in `GITHUB_WORKSPACE`. That is the PR head checkout, and its `HEAD_SHA` is `INITIAL_HEAD_SHA`, the commit the reviewers reviewed (`review_autofix.yml`, "Checkout PR head branch"). So the gate can read the reviewed source with local `git cat-file`, with no API call.

## Goals

- G1: `_votes_in` / `reviewer_votes` count an ID-bound vote only when its `evidence:` and `quote:` verify against the reviewed commit. Otherwise the vote is ignored, and a finding without enough verified votes stays blocking (`too_few_rejecters`).
- G2: Evidence is checked automatically and bound to the voted ID. The cited file must be the manifest entry's file. The cited range must lie within `EVIDENCE_LINE_WINDOW` (10) lines of the entry's range and span at most `EVIDENCE_MAX_LINES` (20) lines inside the file. The whitespace-collapsed quote must be at least `EVIDENCE_MIN_QUOTE_CHARS` (10) non-whitespace characters and must occur in the whitespace-collapsed cited lines of the file at the reviewed commit.
- G3: Without a source (no `--source-root` / `--source-commit`, an invalid commit, or an unreadable file), no vote verifies and nothing is demoted (fail toward blocking, §1).
- G4: The hand-off step passes `--source-root "${GITHUB_WORKSPACE}" --source-commit "${HEAD_SHA}"`. The pass-2 cross-pollination header asks for the new vote shape and states the limits.
- G5: Diagnostics (§8): one `CLAUDE_FIXER_NONBLOCKING_EVIDENCE source=<ok|missing|unavailable> commit=<sha|none> verified=<n> unverified=<n>` line, and one `CLAUDE_FIXER_NONBLOCKING_UNVERIFIED id=<RF-…> reviewer=<slug> reason=<reason>` line per ID-bound vote that failed its evidence check.

## Non-goals

- No change to the ID manifest, `consensus_id` binding, majority rule, `MIN_REJECTERS`, or `LINE_TOLERANCE`.
- No change to the summariser prompt (`scripts/summarize_reviewer_consensus.sh`). It already treats every `REJECTED_FINDING: <ID> | … | reason: …` line as a non-finding, and the new fields follow `reason:`.
- No change to `.claude/**` (the command docs there describe the `NON-BLOCKING FINDINGS` semantics, which are unchanged).
- No change to the GPT editor path. Demotion applies only in Claude-fixer mode.
- No attempt to make LLM reviewers immune to prompt injection. The gate proves each rejecting reviewer quoted the reviewed code at the flagged location; see Risks.

## Constraints

- §1: security first. Every unverifiable case fails toward blocking.
- §5: minimal change set. Only the vote parser, the demoter's inputs, the hand-off call, the prompt text, tests, and docs change.
- §6: no identifier is renamed or removed. `reviewer_votes`, `demote`, and `demote_with_diagnostics` gain an optional keyword `source`. Existing log lines keep their names and fields. `votes=` on `CLAUDE_FIXER_NONBLOCKING_VOTES` keeps its documented meaning, "counted votes", which now means verified votes. New log keys and constants are new, unique names.
- §7: README.md and agents.md describe the vote shape and must be updated, along with docs/INVENTORY.md's entry for the script.
- §9: tabs in Python; the bash files keep their existing 2-space style.
- §15: no GitHub API calls are added. Evidence comes from local `git cat-file` on the checked-out commit, at most one per distinct file per run (cached).
- §18: no new script. The change extends `scripts/review_claude_fixer_nonblocking.py` and its existing caller; there is nothing to wire and no registry entry.
- §19: phase, completion, and final PRs use `Refs #4976` and `Refs #3576` (the audit tracker); the final PR targets a non-default base, so the issue is closed explicitly at final merge.
- §20: one `changelog.d/4976-source-grounded-rejection-votes.md` fragment (`security`).
- §27: `.github/workflows/review_autofix.yml` is not edited.

### §18 summary

- New script: none. Extends `scripts/review_claude_fixer_nonblocking.py` and `scripts/review_autofix_step_claude_fixer_handoff.sh`.
- Scheduler entry point: unchanged, the `Hand review round to Claude session (Claude-fixer mode)` step of `.github/workflows/review_autofix.yml` (pull_request / workflow_dispatch triggers).
- Long-running supervisor: none.
- DB work: none.
- `docs/scripts-pending-removal.md` entry: none (no new script).

## Approach

Vote shape (the fields after `reason:` are new; `reason:` must come before `evidence:`, and `quote:` is last because code may contain `|`):

```
REJECTED_FINDING: <ID> | <file>:<line> | flagged_by: <slug> | reason: <one sentence> | evidence: <file>:<line or start-end> | quote: <text copied verbatim from those lines>
```

In `scripts/review_claude_fixer_nonblocking.py`:

1. A new `VOTE_EVIDENCE_RE` splits the text after the ID at the first `| evidence:` into the head (where `reason:` is looked up, as today) and the evidence path, range, and quote.
2. A new `git_source(root, commit)` returns a reader `path -> list[str] | None` backed by `git -C <root> cat-file blob <commit>:<path>`. The commit must be 40 or 64 lowercase hex characters. The path must be relative, with no `.`/`..` segments, NUL, or newline. Results are cached per path. There is no network access.
3. A new `_verify_evidence(entry, evidence, source)` returns `None` when verified, or a reason string: `no_evidence`, `wrong_file`, `out_of_range`, `span_too_long`, `quote_too_short`, `quote_mismatch`, or `source_unavailable`. It compares the quote with whitespace runs collapsed to one space, and also tries it with one pair of wrapping backticks removed.
4. `_votes_in(text, manifest, source=None, unverified=None)` adds an ID to its result only when the evidence verifies, and appends `(id, reason)` to `unverified` otherwise. Votes without a valid reason are ignored as before and are not listed as unverified.
5. `demote_with_diagnostics(..., source=None)` / `demote(..., source=None)` pass the reader through. `stats` gains `unverified` (a list of `(slug, id, reason)`).
6. The CLI gains `--source-root DIR` and `--source-commit SHA`. When both are given it builds `git_source` and checks the commit once (`git cat-file -e <sha>^{commit}`) to report `source=ok|unavailable`; otherwise `source=missing`. It prints the EVIDENCE line after the VOTES line, then the UNVERIFIED lines.

Alternatives considered: requiring each rejecter to cite an evidence line that differs from the flagged line (rejected, because the #4575 false positive is refuted by quoting the flagged line itself); never auto-merging when a demotion happened (rejected, because it reverts #4586's purpose, AD-1).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the whole change is one PR into the project branch.

1. **Phase 1 — source-grounded rejection votes.** Files: `scripts/review_claude_fixer_nonblocking.py`, `scripts/review_autofix_step_claude_fixer_handoff.sh`, `scripts/review_run_reviewers.sh`, `tests/test_review_claude_fixer_nonblocking.py`, `tests/test_review_autofix_claude_fixer_mode.py`, `README.md`, `agents.md`, `docs/INVENTORY.md`, `changelog.d/4976-source-grounded-rejection-votes.md` [new]. Done when every goal G1–G5 holds, the two test files pass, and `tests/inventory_parity.py` passes. Rollback: revert the PR. The previous behaviour (free-text votes count) returns, which is less safe but functional.

## Implementation Steps

Phase 1:
1. `scripts/review_claude_fixer_nonblocking.py`: add the constants (`EVIDENCE_LINE_WINDOW = 10`, `EVIDENCE_MAX_LINES = 20`, `EVIDENCE_MIN_QUOTE_CHARS = 10`), `VOTE_EVIDENCE_RE`, `git_source`, `_verify_evidence`, the `source` / `unverified` plumbing through `_votes_in`, `reviewer_votes`, `demote_with_diagnostics`, and `demote`, and the CLI options and log lines. Update the module docstring (vote shape, evidence rules, usage, log lines, "no network access and no GitHub API calls; reads the reviewed commit with local git").
2. `scripts/review_autofix_step_claude_fixer_handoff.sh`: pass `--source-root "${GITHUB_WORKSPACE:-${PWD}}" --source-commit "${HEAD_SHA:-}"` to the demoter, and extend the header comment and the "Inputs" list.
3. `scripts/review_run_reviewers.sh` (`build_cross_pollination_summary` and its comment): new vote template line and the evidence rules sentence, with the numbers matching the Python constants.
4. Tests (below).
5. Docs: `README.md` (the "Rejected single-reviewer findings are not handed off" section), `agents.md` (the same paragraph), `docs/INVENTORY.md` (script entry), and the changelog fragment.

## Files & Modules

- `scripts/review_claude_fixer_nonblocking.py`
- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `scripts/review_run_reviewers.sh`
- `tests/test_review_claude_fixer_nonblocking.py`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`, `agents.md`, `docs/INVENTORY.md`
- `changelog.d/4976-source-grounded-rejection-votes.md` [new]

## Tests

- Unit (`tests/test_review_claude_fixer_nonblocking.py`): update the vote helpers to emit evidence and pass a fake source. New cases:
  - the #4976 exploit: reason-only ID-bound votes from every other reviewer demote nothing, and each is reported `no_evidence`;
  - evidence in another file, outside the window, spanning too many lines, or past EOF;
  - a short quote; a quote present elsewhere in the file but not at the cited lines;
  - no source; an unreadable file;
  - accepted variants: whitespace differences and wrapping backticks;
  - `git_source` refusing a bad commit or a path with `..`, `/`, or NUL, and reading a real commit;
  - CLI end to end against a real temporary git repository, with `source=ok|missing|unavailable` lines;
  - the prompt template and limits in `review_run_reviewers.sh` match the Python constants.
- Integration (`tests/test_review_autofix_claude_fixer_mode.py`): the rejected-singleton round auto-merges only when the workspace is a git repo whose `HEAD_SHA` commit carries the quoted lines. The same votes without evidence, or with a workspace that is not a repository, hand the round off.
- Repo checks: `python3 -m pytest` on both files (the `ci.yml` step), `python3 tests/inventory_parity.py`, `bash -n` on the edited scripts, and `shellcheck` where installed.

## Risks & Mitigations

- A prompt-injected reviewer can still quote the real flagged line. ACCEPTED — the issue's recommendation is source-grounded, automatically checked evidence bound to each ID. The injection now has to get a majority of at least two independent reviewers to quote the reviewed code at the flagged location, and each demoted entry stays visible in the posted `NON-BLOCKING FINDINGS` block.
- Honest reviewers who omit the new fields keep a false-positive finding blocking, so more rounds go to the Claude fixer. ACCEPTED — that fails toward blocking (§1). The prompt spells out the shape, and the UNVERIFIED log lines show why a vote did not count.
- `git` is missing or the commit is not in the checkout: `source=unavailable`, nothing is demoted. Mitigated by the fail-closed rule and the EVIDENCE log line.
- A large file read through `git cat-file`: bounded by one read per distinct manifest file per run, with a 30-second subprocess timeout that counts as unavailable.

## Rollout

Ships with the base project (#4586) when its final PR #4593 merges into `main`, then reaches consumer repos with the next `@stable` release through the existing `.codex-workflow-src` support checkout. There is no flag. Rollback is a revert.

## Auto-decisions

- AD-1 [plan, 2026-09-29] What must a rejection vote carry to count? — Picked: A — an `evidence: <file>:<range>` citation plus a verbatim `quote:` that the gate checks against the reviewed commit. Alternatives: B — never let a demotion authorize auto-merge (hand the round to the fixer instead); C — drop demotion entirely. Why: A is the issue's recommendation and keeps #4586's purpose; B and C revert it (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Where may the evidence point? — Picked: A — the voted entry's own file, within 10 lines of its range, at most 20 lines long. Alternatives: B — any file in the repository; C — only the flagged lines themselves. Why: binds the evidence to the finding (§1) while still allowing a nearby guard to be cited. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is the quote matched? — Picked: A — at least 10 non-whitespace characters, compared with whitespace runs collapsed, also accepting one pair of wrapping backticks removed. Alternatives: B — an exact byte match; C — any length. Why: tolerates reviewer formatting without letting a trivial quote such as `}` through. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Where does the gate read the source? — Picked: A — `git cat-file blob <HEAD_SHA>:<path>` in `GITHUB_WORKSPACE`, the reviewed commit. Alternatives: B — the working tree files; C — the GitHub contents API. Why: binds the check to the reviewed commit, with no API calls (§15). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Does the summariser prompt change? — Picked: A — no. Alternatives: B — document the new fields there too. Why: it already drops every `REJECTED_FINDING … | reason: …` line, and the new fields follow `reason:` (§5). Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- #4976 (this finding), #3576 (security audit tracker), #4586 / final PR #4593 (base project), #4687, #4688 (earlier hardening of the same gate), #4575 (the false positive that motivated #4586).
