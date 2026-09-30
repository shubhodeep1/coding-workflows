<!-- changelog: security -->
- **A `[skip ai]` example quoted inside a nested code fence no longer skips review.** The skip-AI marker rule now closes a code fence only on a matching fence, so a three-backtick line inside a four-backtick block keeps the block open.

The review gate in `.github/workflows/review_autofix.yml`, the stale-review sweep in `.github/workflows/review_autofix_sweep.yml`, and the §26.H catch-all (`scripts/claude_pr_sweep.py` through `has_skip_ai_marker` in `.claude/scripts/check_in_status.py`) toggled their "inside a fence" state on every line starting with ```` ``` ```` or `~~~`. A PR description that quoted a Markdown sample in a ```` ```` ```` fence, with a ```` ``` ```` line and then a `[skip ai]` line inside it, counted as an intentional opt-out, and the PR was not reviewed (security audit finding `nested-fence-skips-review`, #5377). Now a fence opens on any line starting with three or more backticks or tildes and closes only on a line of up to 3 spaces, then a run of the same character at least as long as the opener, then only blanks. A shorter fence, the other fence character, an indented fence, or a fence with an info string inside the block is content, and an unclosed fence runs to the end of the description. The title rule and the body-line rule are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Copies of the rule changed | 3 (gate awk, sweep awk, `has_skip_ai_marker`) |
| New cases in `tests/test_skip_ai_marker_rule.py` | 14 |
| New API calls | 0 |

What this means for operators: a PR is skipped only when its description has a real `[skip ai]` line outside every code block. When a fence cannot be closed cleanly, the PR is reviewed. To opt out anyway, put `[skip ai]` in the PR title.

### For contributors

The awk program stays a single POSIX line (no `{n,}` interval expressions), so it runs the same under gawk on the runners and under mawk. `SKIP_AI_FENCE_RE` keeps its match set and now captures the whole fence run; the closer is the new `SKIP_AI_FENCE_CLOSE_RE`.
