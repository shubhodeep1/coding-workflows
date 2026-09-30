# gh api guard: unquoted expansions and fake heredoc markers can hide a flag or a call

Source issue: shubhodeep1/coding-workflows#5558 (https://github.com/shubhodeep1/coding-workflows/issues/5558)
Base branch: main
Security pass: run

## Summary

`.claude/hooks/gh_api_write_guard.py` (CLAUDE.md §23.H) reads a Bash command with `shlex` and classifies each `gh api` call. Two gaps let Bash run something other than what the guard reads, and in both the guard makes no decision, so the `Bash(gh api repos/*)` allow rule can approve the command:

1. An **unquoted expansion** in a `gh api` word (`repos/<local>/issues/1/comments$X` with `X=' -Fbody=@/etc/passwd'`) is one word to the guard, but Bash splits it into extra words, such as a new `-F` or `-X DELETE` flag.
2. `strip_heredoc_bodies` treats any `<<WORD` as a heredoc operator, even inside quotes, so `echo '<<EOF'` ⏎ `gh api -X DELETE repos/a/b` ⏎ `EOF` hides the `gh api` line from the guard while Bash runs it.

This plan implements the issue's option A: ask when any word of a direct `gh api` call holds an **unquoted** expansion that can split into several words, keep double-quoted expansions (`"repos/a/b/pulls/$n"`) as they are today, and make the heredoc scanner recognise `<<` only where Bash does.

## Context

- Found in review round 1 of PR #4641 (issue #4619) and left out of it on purpose (AD-9 there). PR #4641 is still open. Its current head only adds the file-backed `-F`/`--input` rule in `classify()`; the `-F`-word expansion checks the issue mentions are the in-flight review-round fix on that PR, not on `main`.
- `shell_segments()` (`gh_api_write_guard.py:307-342`) strips quotes, so by the time `gh_api_invocations()` returns the words, the guard can no longer tell `"$n"` from `$n`. `classify()` only looks at `$` in the path of a write (`gh_api_write_guard.py:774`), and a read with `$` is never approvable (`_has_unsafe_shell_syntax`), so it gets no decision.
- An unquoted `$(...)` is split into its own segment by the tokenizer (`(` is punctuation), so `gh api repos/a/b/x/$(printf ' -XDELETE')` is read as a GET of `repos/a/b/x/$` while Bash runs a DELETE.
- Brace expansion splits one word into several with no environment at all: `gh api repos/<local>/git/refs/heads/x --jq {.a,-XDELETE}` becomes `--jq .a -XDELETE`.
- `strip_heredoc_bodies()` (`gh_api_write_guard.py:274-304`) runs the regex `<<(-?)\s*(['"]?)(WORD)\2` on every line. Besides quotes, it also fires on a here-string `<<<WORD`, an escaped `\<<`, a `#` comment, arithmetic `$((1<<X))`, and `${x#<<X}`, none of which start a heredoc in Bash.
- A command where the guard fails to strip a real heredoc is safe: the body lines are parsed as commands, a `gh api` line there is classified (a write asks), and `_is_approvable_command` never allows a command with `<` or a newline. So when in doubt, the scanner does not strip.
- #4786 (approve read-only `for` loops over literal IDs) and #4909 (approve `echo "$(gh api …)"`) are open and not implemented. #4786's proposed allowed shape puts an unquoted `$VAR` in the endpoint.
- No command file, CLAUDE.md, or `agents.md` example puts an unquoted `$` in a `gh api` word (grep over `.claude/commands`, `CLAUDE.md`, `agents.md`).

## Goals

- `X=' -Fbody=@/etc/passwd'; gh api repos/<local>/issues/1/comments$X` asks.
- `echo '<<EOF'` ⏎ `gh api -X DELETE repos/a/b` ⏎ `EOF` asks.
- Any direct `gh api` call with an unquoted `$` (parameter, `${…}`, `$(…)`, `$((…))`, `$'…'`, `$"…"`), an unquoted backtick, or an unquoted brace expansion in any word after `gh api` asks, with a reason that says to double-quote the word.
- `gh api "repos/a/b/pulls/$n" --jq .title` keeps its current outcome (no decision).
- `strip_heredoc_bodies` recognises `<<` only outside quotes, comments, arithmetic, and `${…}`, never as part of `<<<` or after a backslash, and still finds heredocs inside `"$(…)"` (the `git commit -m "$(cat <<'EOF' …)"` pattern).
- Tests cover both, the twin stays byte-identical, and CLAUDE.md §23.H, `agents.md`, and a `changelog.d/` fragment describe the change.

## Non-goals

- The `-F`-word checks (quoted expansions, ANSI-C quoting, `~`, globs in `-F` values) that PR #4641's review round adds. This plan does not touch field values beyond the word-splitting rule.
- Pathname globs (`*`, `?`, `[`) in `gh api` words (AD-2).
- Approving loops or substitutions (#4786, #4909).
- Any change to `settings.json`, the routine endpoint list, or the safe-helper list.

## Constraints

- §1: security first. The change only adds asks and never allows more.
- §6: no identifier is renamed or removed. `strip_heredoc_bodies` keeps its signature and return shape; `evaluate`, `classify`, `shell_segments`, `gh_api_invocations`, `has_hidden_gh_api`, `substitution_bodies` keep theirs. New names are private and checked for clashes in the module.
- §9: tabs in Python, matching the file.
- §14 / §28.C: `.claude/hooks/**` is a protected path. Twin-first (interim default until #4785): edit only `workflow-templates/.claude/hooks/gh_api_write_guard.py`; the `[claude-twin-sync]` copy into `.claude/` is done by the supervising session. `workflow-templates/CLAUDE.md` is a symlink to `../CLAUDE.md`.
- §15: no GitHub API calls in the hook.
- §20: a `changelog.d/` fragment; never `CHANGELOG.md`.
- §23.H: the guard still fails closed.

## Approach

1. **Word-splitting check.** Add `_mark_unquoted_expansions(command)`, a quote-aware scanner that inserts a private-use sentinel character before every unquoted, unescaped `$`, every unquoted backtick, and every unquoted `{` that opens a brace expansion (an unquoted `,` or `..` before the matching `}` and before any unquoted whitespace). Single-quoted text, double-quoted text, and the body of a double-quoted `$(…)` are copied unmarked. `evaluate()` tokenises the marked copy of the heredoc-stripped command with the existing `shell_segments()` and `gh_api_invocations()`, so the sentinel lands in exactly the token that holds the expansion; if any argument word of a direct `gh api` invocation holds the sentinel, the whole call asks. The check runs right after the hidden-call check and before per-call classification, so it also comes before the malformed-jq deny (a word the guard cannot read may not be the jq value it looks like).
2. **Heredoc scanner.** Rewrite the body of `strip_heredoc_bodies()` as a character scanner with a context stack: plain command, single quote, double quote, `$(…)`/backtick command substitution, arithmetic (`$((…))`, `((…))`), and `${…}`. A `<<` operator is recognised only in a command context (top level, `$(…)`, backticks), not preceded by a backslash or `<` and not followed by `<`, and not in a `#` comment. At a newline in a command context, pending heredocs consume their body lines as today (tab stripping for `<<-`, a missing delimiter runs to the end). A heredoc whose operator line ends inside an open quote is dropped, not stripped. The return value is unchanged: the command without bodies, and `(prefix, quoted, body)` per heredoc.

Alternatives: option B (ask for any `$` or backtick, quoted or not) prompts on every quoted loop read today's sessions use; option C leaves (1) to the allow list. Both rejected by the issue (AD-1).

## Phases & Merge Strategy

A single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for a standalone issue.

1. **Phase 1: unquoted expansions and fake heredoc markers ask** — protected paths: `.claude/hooks/gh_api_write_guard.py` (twin-first).
   - Files: `workflow-templates/.claude/hooks/gh_api_write_guard.py` (the `.claude/hooks/` copy follows in the `[claude-twin-sync]` commit), `tests/test_gh_api_write_guard.py`, `CLAUDE.md` (§23.D item 4, §23.H), `agents.md`, `changelog.d/5558-gh-api-guard-word-splitting-heredoc.md`.
   - Done when:
     - both issue commands ask, and every new case in Tests has its expected outcome;
     - `gh api "repos/a/b/pulls/$n" --jq .title` gets no decision;
     - every existing case keeps its outcome, except the three observed commands moved per AD-5 and `gh api repos/a/b/pulls/$n --jq .title`, which now asks;
     - with the twin placed at `.claude/hooks/` in a scratch copy, `tests/test_gh_api_write_guard.py` and the neighbouring hook tests pass;
     - template parity holds after the twin sync.
   - Rollback: revert the PR.

## Implementation Steps

1. Twin hook: add `_mark_unquoted_expansions` and `_gh_api_word_can_split` (new private names), the ask in `evaluate()`, and the scanner body of `strip_heredoc_bodies`. Update the module docstring's `write` bullet and the `strip_heredoc_bodies` docstring.
2. `tests/test_gh_api_write_guard.py`:
   - an ask expectation for unquoted expansions: both issue commands' shapes, `$X`, `${X}`, `$(…)`, `$((…))`, backticks, `$'…'`, brace expansion in a flag value, in the endpoint, in `-F`/`-f` values, in a loop;
   - no-decision cases for the quoted forms (`"repos/a/b/pulls/$n"`, `repos/a/b/pulls/"$n"`, `\$n`, `{owner}/{repo}`, `-f q='a,b'`);
   - an ask expectation for fake heredoc markers: quoted `'<<EOF'` and `"<<EOF"`, `<<<EOF`, `\<<EOF`, `# <<EOF`, `$((1<<X))`, `${x#<<X}`;
   - `strip_heredoc_bodies` unit cases, including a heredoc inside `"$(…)"` and one after a quoted `<<` on the same line;
   - move the observed commands with unquoted `$n`/`$F`/`$S` in `gh api` words to the ask expectation, with their quoted equivalents as no-decision cases (AD-5).
3. CLAUDE.md §23.H table and paragraph, §23.D item 4, and `agents.md`'s guard paragraph: an unquoted expansion in a `gh api` word asks; quote the word.
4. `changelog.d/5558-gh-api-guard-word-splitting-heredoc.md` (`<!-- changelog: security -->`).
5. After the phase PR opens: one coordination comment on #4786 and on #4909 (AD-6).

## Files & Modules

- `workflow-templates/.claude/hooks/gh_api_write_guard.py` (and `.claude/hooks/gh_api_write_guard.py` via the twin sync)
- `tests/test_gh_api_write_guard.py`
- `CLAUDE.md` (and so `workflow-templates/CLAUDE.md`)
- `agents.md`
- `changelog.d/5558-gh-api-guard-word-splitting-heredoc.md` [new]

## Tests

- Unit (`tests/test_gh_api_write_guard.py`, its own `ci.yml` step): the cases in step 2, and every existing case.
- Hook process: an `ask` JSON on stdout for the issue's first command.
- Twin verification before the sync: a scratch copy of the repo with the twin placed at `.claude/hooks/`, running `tests/test_gh_api_write_guard.py`, `tests/test_pr_watch_guard.py`, `tests/test_pr_check_in_reminder.py`, and `tests/test_update_workflows_guardrails.py`.

## Risks & Mitigations

- Sessions that write `gh api repos/o/r/pulls/$n` in a loop now get a prompt instead of no decision, and an unattended session can stop at it. Mitigation: the ask reason and §23.D item 4 say to double-quote the word (`"repos/o/r/pulls/$n"`), which keeps today's outcome. ACCEPTED: the issue chose this cost (option A).
- The heredoc scanner is new code in a fail-closed hook. Mitigation: a scanner miss only stops stripping, which is safe; existing heredoc tests plus new ones cover the commit-message and python-heredoc shapes stage sessions use.
- Merge overlap with PR #4641 in the hook, tests, CLAUDE.md §23.H, and `agents.md`. Mitigation: the changes touch different functions; whichever PR merges second resolves the conflict keeping both rules.

## Rollout

Ships to this repo on merge, and to the consumers in `.github/ai/consumer_repos.json` on the next `@stable` sync of `.claude/`. No flag, no migration. Rollback is a revert.

## References

- Issue #5558; issue #4619 and PR #4641; #4786; #4909.
- CLAUDE.md §23.D, §23.H, §28.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which option closes (1) and (2)? — Picked: A — quote-aware per-word check: ask only when an expansion in a `gh api` word is unquoted, and make `strip_heredoc_bodies` ignore `<<` inside quotes. Alternatives: B — ask for any `$`/backtick in any `gh api` word, quoted or not; C — leave (1) to the allow list and fix only (2). Why: the issue marks A RECOMMENDED; it closes both holes and keeps double-quoted loop reads unchanged. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which unquoted expansions in a `gh api` word ask? — Picked: A — an unquoted, unescaped `$` in any form (`$X`, `${…}`, `$(…)`, `$((…))`, `$'…'`, `$"…"`), an unquoted backtick, and an unquoted brace expansion (`{a,b}`, `{1..3}`). Alternatives: B — `$` and backticks only; C — also pathname globs (`*`, `?`, `[`). Why: brace expansion splits one word into several with no environment (`--jq {.a,-XDELETE}`); globs are left out because `?` is unquoted in most query-string endpoints, so C would prompt on common reads, and a glob keeps its literal prefix. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should an unquoted expansion ask or deny? — Picked: A — ask, as the issue's acceptance says. Alternatives: B — deny with a reason that says to quote the word (like the #4891 malformed-jq deny), so an unattended session retries instead of waiting. Why: the command can be legitimate (a loop over IDs), a human may want to approve it, and the issue specifies ask. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Beyond quotes, which fake heredoc markers does the scanner reject? — Picked: A — also a here-string `<<<`, an escaped `\<<`, `<<` in a `#` comment, in arithmetic (`$((…))`, `((…))`), and in `${…}`; a heredoc whose operator line ends inside an open quote is not stripped. Alternatives: B — quotes only. Why: each is the same misread (the guard drops lines Bash runs) in the same function, and not stripping only adds asks (CLAUDE.md §12.B, latent bug in adjacent code). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] What happens to the observed stage-session commands with an unquoted `$n`, `$F`, or `$S` in a `gh api` word, which the tests record as not forced? — Picked: A — keep them verbatim, move them to the unquoted-expansion ask expectation, and add their double-quoted equivalents as not-forced cases. Alternatives: B — delete them. Why: they are real commands and the best regression cases; the quoted equivalents show the fix a session should make. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] How does this coordinate with #4786 and #4909, whose loop approval puts an unquoted `$VAR` in an endpoint? — Picked: A — §23.H states that a `gh api` word must double-quote an expansion, and the phase posts one comment on each issue saying a loop approval must keep this check (quote the loop variable, or exempt only a variable bound to literal tokens). Alternatives: B — exempt loop variables bound to literal tokens now. Why: neither issue is implemented, and a loop parser belongs to #4786 (§5). Applied in: phase 1 PR (docs) and comments on #4786, #4909. Status: pending review
- AD-7 [plan, 2026-09-30] Base branch while PR #4641 (same file) is still open? — Picked: A — `main`, as the issue names no integration branch; whichever of the two PRs merges second resolves the overlap. Alternatives: B — build on #4641's project branch. Why: the command builds on the branch the issue names, and this change does not depend on #4641's code. Applied in: no code change. Status: pending review
