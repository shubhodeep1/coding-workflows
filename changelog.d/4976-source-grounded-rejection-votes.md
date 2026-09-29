<!-- changelog: security -->
- **A free-text `reason: false positive` can no longer clear a genuine review finding on a `claude/*` PR.** A rejection vote now counts only when it quotes the reviewed code at the flagged location, and the Claude-fixer hand-off gate checks that quote against the commit that was reviewed.

Security audit finding #4976 showed that the gate in `scripts/review_claude_fixer_nonblocking.py` counted any `REJECTED_FINDING: <ID> | … | reason: <text>` line as a vote. The pass-2 prompt shows the run's finding IDs next to PR-controlled text. If that text talked two reviewers into printing an ID with `reason: false positive`, the last single-reviewer finding moved to the non-blocking block and the PR auto-merged, with no proof that either reviewer had checked the code. A vote now needs `| evidence: <file>:<line or start-end> | quote: <text>` after its reason. The Claude-fixer hand-off step (`scripts/review_autofix_step_claude_fixer_handoff.sh`) reads the reviewed commit `HEAD_SHA` from the PR checkout with local `git cat-file` and counts the vote only when the quote is found at the cited lines. The pass-2 cross-pollination header in `scripts/review_run_reviewers.sh` asks for the new shape.

| The numbers that matter | Value |
| --- | --- |
| Vote shape | `REJECTED_FINDING: <ID> \| <file>:<line> \| flagged_by: <slug> \| reason: <one sentence> \| evidence: <file>:<line or start-end> \| quote: <text>` |
| Evidence file | the voted entry's own file (from the run's `rejection_ids_pass1.json`) |
| Evidence range | at most 20 lines, within 10 lines of the entry's range |
| Quote | at least 10 non-space characters, found in the cited lines with whitespace runs collapsed; one pair of wrapping backticks may be dropped |
| Source | `git cat-file blob <HEAD_SHA>:<path>` in `GITHUB_WORKSPACE`, one read per file, no GitHub API calls |
| New log lines | `CLAUDE_FIXER_NONBLOCKING_EVIDENCE source=<ok\|missing\|unavailable> commit=<sha\|none> verified=<n> unverified=<n>`, `CLAUDE_FIXER_NONBLOCKING_UNVERIFIED id=<ID> reviewer=<slug> reason=<why>` |

What this means for operators and Claude sessions: a single-reviewer finding is demoted only when a majority of the other reviewers (at least two) rejected it through this run's IDs and quoted the reviewed code where the finding points. A vote without evidence, with a quote that is not at the cited lines, or with evidence in another file does not count, so the finding goes to the Claude fixer as before #4586. When the reviewed commit cannot be read, nothing is demoted. The `UNVERIFIED` lines in the run log name the reason for every vote that did not count.

### For contributors

`reviewer_votes`, `demote`, and `demote_with_diagnostics` take an optional `source_reader` (`git_source(root, commit)` in production); without one no vote verifies. The CLI takes `--source-root` and `--source-commit`. The commit must be a full 40- or 64-character hex object name, and evidence paths must be relative with no `.` or `..` segment and no `:`. The unverified reasons are `no_evidence`, `wrong_file`, `span_too_long`, `out_of_range`, `quote_too_short`, `source_unavailable`, and `quote_mismatch`. `votes=` on the `CLAUDE_FIXER_NONBLOCKING_VOTES` line keeps counting the votes that count, which now means the verified ones. Residual risk: a prompt injection that gets a majority of reviewers to quote the flagged line verbatim. The quote is still bound to the finding's own lines in the reviewed commit, and every demoted entry stays visible in the posted `NON-BLOCKING FINDINGS` block.
