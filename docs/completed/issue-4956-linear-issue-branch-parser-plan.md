# Parse issue branch lines in linear time in issue_body_integration_branch

Source issue: shubhodeep1/coding-workflows#4956 (https://github.com/shubhodeep1/coding-workflows/issues/4956)
Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
Security pass: skip (ai:security: automation-produced issue)

## Summary

`issue_body_integration_branch` in `scripts/gh_helpers.sh` reads an issue's `Integration branch:` / `Target branch:` line with two `re.MULTILINE` regexes whose run time grows faster than linearly with the body. This plan replaces them with a line-by-line parser that runs in linear time and returns the same branch for every body whose label line carries its value on the same line.

## Context

The security audit of project #4813's branch (`security-audit.yml`, tracker #3576) filed #4956: `scripts/gh_helpers.sh:1704`, STRIDE Denial of Service, severity medium, confidence 9/10. `close_merged_issues_sweep` (`scripts/orchestrate_poll_process.sh:3998`) and `issue_pr_status.yml` (lines 226 and 462) call the helper on issue bodies that anyone who can open or edit an issue controls. Both run under job timeouts (the PR-close step has five minutes).

Measured on the base branch (Python 3.11, this session):

| Input | Time |
| --- | --- |
| `x` + 8,000 blank lines + `y` (shell helper) | 1.9 s |
| `x` + 16,000 blank lines + `y` (shell helper) | 7.0 s |
| `Integration branch: a` + 2,000 spaces + `` `b`` (one line, the regex alone) | 29.7 s |

Two causes:

1. `^\s*` under `re.MULTILINE` matches newlines, so the scan restarted at every line start walks over all the blank lines after it. That is quadratic in the number of blank lines, which is the case the issue reports.
2. Inside one line, the lazy value group followed by `` \s*`?\s*$ `` has two whitespace runs the engine can split in any way before failing. That is cubic in the length of a whitespace run. A single line of a 65,536-character body (GitHub's limit) could keep the helper busy for hours. The issue's "bounded lines" alone would not fix this. The value has to be parsed without ambiguous backtracking.

The helper was added by #4813 (phase 1, PR #4842) with the same regexes as `INTEGRATION_BRANCH_LINE_RE` / `TARGET_BRANCH_LINE_RE` in `scripts/orchestrate_lib.py` and the alias pattern in `scripts/resolve_integration_ref.sh`. Those two copies carry the same patterns (see Non-goals and AD-1).

## Goals

- `issue_body_integration_branch` runs in time linear in the body length: at most 5 seconds (in practice about 50 ms, mostly process start) for a 60,000-blank-line body and for a label line carrying 5,000 spaces. The current code takes minutes on both.
- For every body, the helper prints exactly what the original grammar prints when that grammar is applied to one line at a time: the first line whose `Integration branch:` form matches, else the first line whose `Target branch:` form matches, else nothing. `tests/test_gh_helpers_issue_body_integration_branch.py` checks this against the original regexes with a fixed-seed generated corpus and a curated edge-case list.
- The existing parity test against `orchestrate_lib.extract_integration_branch` keeps passing unchanged.
- Both branch patterns (canonical and `Target branch:` alias) use the new parser, as the issue asks.
- A `changelog.d/` fragment (§20, `security`).

## Non-goals

- `scripts/orchestrate_lib.py` (`INTEGRATION_BRANCH_LINE_RE`, `TARGET_BRANCH_LINE_RE`, `TRACKING_ISSUE_LINE_RE`) and `scripts/resolve_integration_ref.sh` keep their regexes. They are on `main` already, outside this project's diff, and route orchestrator planning and implementation. They are listed under Notes for a separate issue (AD-1).
- The helper's interface does not change: same name, same `$1` body argument, same stdout, same exit 0 and silent failure, same `python3 -c` over stdin.
- No caller changes (`scripts/orchestrate_poll_process.sh`, `.github/workflows/issue_pr_status.yml`).

## Constraints

- §1: security first. The fix removes the denial-of-service path without widening what the parser accepts.
- §5: one function, its test, and a changelog fragment.
- §6: `issue_body_integration_branch` keeps its name and contract. The embedded Python uses only local names.
- §9: tabs in the shell function and the embedded Python, matching the surrounding file.
- §14: `scripts/gh_helpers.sh` reaches consumer repos through `@stable` (`issue_pr_status.yml` fetches it from `stable`). The change is self-contained, so no wrapper or template change is needed.
- §15: no GitHub API call is added or removed.
- §19: phase and completion PRs use `Refs #4956`. The final PR merges into the #4813 project branch, not the default branch, so it also uses `Refs #4956`, and the final-merge stage closes #4956 explicitly (Issue Mode).
- §27: no workflow file changes.

## Approach

Replace the two regex searches in the embedded Python with a parser that works line by line:

1. Split the body on `"\n"` only. That is the same line boundary `re.MULTILINE` uses for `^` and `$`.
2. For each line, strip leading whitespace, drop one optional `-` and the whitespace after it, and check for the label with `str.startswith`. A line without the label costs one strip and two prefix checks.
3. Parse the text after the label with string operations that follow the regex grammar exactly:
   - **Integration:** optional opening backtick after whitespace, optional closing backtick before trailing whitespace, and a non-empty value between them with no backtick. The value is printed stripped, as today (a whitespace-only value prints an empty line, as today).
   - **Target:** either `` `value` `` followed by the end of the line or by whitespace and any trailing prose, or one token with no whitespace and no backtick.
4. Try every line for the canonical form first, then every line for the alias. That keeps "canonical line wins" and "first match wins".

"Whitespace" is Python's `str.isspace()` set, which is the set `\s` matches in a `str` pattern. It does not include `"\n"`, because lines are split on it. So `\r` (CRLF bodies), tabs and Unicode spaces behave as they do today (AD-2).

The only inputs whose result changes are bodies where the original regex took a label's value from a later line (for example `Integration branch:` alone on a line, followed by `foo`). The helper now reads each label line on its own. The issue's recommendation ("space-or-tab matching that cannot span newlines") asks for exactly this.

Alternatives considered (AD-3): keeping the regexes but applying them line by line (still cubic within one line: 29.7 s for 2,000 spaces); line-bounded regexes plus a line-length cap (changes results for long lines and still costs a quadratic amount up to the cap); possessive quantifiers (need Python 3.11+, and consumer runners' `python3` may be older).

## Phases & Merge Strategy

This is a single-phase plan. Issue mode (CLAUDE.md §28.A) authorises it: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — linear-time parser for `issue_body_integration_branch`.**
   - Files: `scripts/gh_helpers.sh`, `tests/test_gh_helpers_issue_body_integration_branch.py`, `changelog.d/4956-linear-issue-branch-parser.md` [new]. No `.claude/**` path.
   - Done when: the new and existing tests pass (run directly, as `ci.yml` line 914 does, and under `pytest`); the pathological bodies finish well inside the 5-second bound; the generated corpus shows no difference from the line-by-line original grammar; `bash -n scripts/gh_helpers.sh` is clean.
   - Rollback: revert the phase PR. The helper goes back to the regex version, which is correct but slow on crafted bodies.

## Implementation Steps

Phase 1:

1. `scripts/gh_helpers.sh` `issue_body_integration_branch` (lines ~1680-1712): replace the embedded Python with the line parser from the Approach. Update the header comment to say the grammar is the same as the Python parser's, read one line at a time, in linear time (issue #4956), and why the regexes were not kept.
2. `tests/test_gh_helpers_issue_body_integration_branch.py`:
   - add `test_pathological_bodies_finish_quickly`: a 60,000-blank-line body, a label line with 5,000 spaces for each pattern, and 1,500 label lines with space runs. Each must return the expected value within 5 seconds, and the subprocess timeout stops a regression from hanging CI;
   - add `test_shell_parser_matches_line_bounded_grammar`: a curated edge-case list (CRLF, tabs, NBSP, whitespace-only values, a lone or doubled backtick, `` `a`b ``, `` `a` b ``, bullets, bold labels, both labels) plus a fixed-seed generated corpus. Each is compared with the original regexes from `orchestrate_lib` applied line by line. All bodies go through one `bash` process to keep the test fast;
   - register both in `__main__`, which is how `ci.yml` runs the file.
3. `changelog.d/4956-linear-issue-branch-parser.md` [new]: a `security` fragment per §20.D.

## Files & Modules

- `scripts/gh_helpers.sh`
- `tests/test_gh_helpers_issue_body_integration_branch.py`
- `changelog.d/4956-linear-issue-branch-parser.md` [new]
- `docs/plans/issue-4956-linear-issue-branch-parser-plan.md` [new] (this plan), `docs/implement-plan/issue-4956-linear-issue-branch-parser.md` [new] (progress log)

## Tests

- Unit: the two new tests above, plus the three existing ones.
- Regression: `tests/test_orchestrate_poll_process.py` and `tests/test_issue_pr_status_target_branch_gate.py` run the callers against the real helper, so they must stay green.
- Manual check before the PR: the timing table from Context, re-run against the new helper.

## Risks & Mitigations

- A body that relied on a label's value sitting on the next line would now yield no branch. ACCEPTED — no automation writes that form (`security_audit.sh`, the issue templates and `/implement-issue-claude` all put the value on the label's line), and it is the change the issue asks for. The sweep and `issue_pr_status.yml` then treat the issue as having no integration branch, which fails closed (only default-branch and managed merges count).
- The string parser could drift from the regex grammar. Mitigation: the generated-corpus test compares it with the original regexes on every CI run.
- The Python and `resolve_integration_ref.sh` copies stay slow on crafted bodies. ACCEPTED — out of this issue's scope (AD-1). Recorded under Notes and in the PR body for a follow-up issue.

## Rollout

No flag. The helper ships with the #4813 project's final merge into `main`, then to consumers on the next `@stable` sync. Rollback is a revert of the phase PR.

## References

- #4956 (this issue), security audit tracker #3576
- #4813 and its plan `docs/plans/issue-4813-close-sweep-target-branch-merges-plan.md`, phase PR #4842, final PR #4826
- `scripts/orchestrate_lib.py:46-63`, `scripts/resolve_integration_ref.sh:8-33`

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which copies of the branch-line grammar does this issue fix? — Picked: A — only `issue_body_integration_branch` in `scripts/gh_helpers.sh` (the flagged location, both of its patterns). Alternatives: B — also `scripts/orchestrate_lib.py` and `scripts/resolve_integration_ref.sh`. Why: §5; the other copies are on `main`, outside this project's diff, and route orchestrator planning, so changing them belongs in its own issue. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as whitespace inside a line? — Picked: A — every character `\s` matched before except `"\n"` (tabs, `\r`, Unicode spaces), so single-line results stay identical to the Python parser. Alternatives: B — space and tab only, as the issue words it, which would stop matching values followed by `\r` or other whitespace that parse today. Why: §1 without breaking backward compatibility; the issue's goal (no newline spanning) is met either way. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How to make the parse linear? — Picked: A — a line-by-line string parser with no regex backtracking. Alternatives: B — the same regexes applied per line (still cubic within a line: 29.7 s for 2,000 spaces); C — regexes plus a line-length cap (changes results for long lines, still quadratic up to the cap); D — possessive quantifiers (Python 3.11+ only). Why: the only option that is linear for every input with no change in results for single-line forms. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 4956` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Follow-up candidate (not filed by this chain): `INTEGRATION_BRANCH_LINE_RE`, `TARGET_BRANCH_LINE_RE` and `TRACKING_ISSUE_LINE_RE` in `scripts/orchestrate_lib.py` and the two patterns in `scripts/resolve_integration_ref.sh` share the same super-linear backtracking (the Python parser took 3.3 s on 8,000 whitespace-only lines and 13.8 s on 16,000).
