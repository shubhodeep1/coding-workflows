# CLAUDE.md §21: full reference

This is the full text of CLAUDE.md §21. CLAUDE.md keeps the
rules every session needs and points here for the rest. Both are binding;
where this file adds detail, follow it. It reaches consumer repos with the
rest of `.claude/` through the `@stable` sync.

## §21. Merged-PR Commit Guard (MANDATORY)

**Never commit or push onto a branch whose pull request has already
merged.** A merged PR is finished — it cannot track new work. Commits
stacked on top of already-merged history land on a branch no open PR
carries, so the work is stranded: it never reaches the default branch and
is lost when the branch is deleted.

This is a real failure mode for long-lived interactive sessions. A session
that runs for days or weeks outlives the PR it opened; the PR merges
mid-session and the model, working from context that predates the merge,
keeps committing to the same branch.

### A) The rule

Before every `git commit` and `git push` in an interactive session, confirm
the current branch is not sitting on a merged PR. If it is:

1. Restart the branch from the latest default branch, **keeping the same
   branch name**:
   `git fetch origin <default> && git checkout -B <branch> origin/<default>`
2. Re-apply the pending work and commit it there.
3. Open a **new** pull request. The merged PR is not reusable, and §12.A's
   single-PR rule does not apply across a merge boundary — the merged PR is
   closed history, so the follow-up work is a new PR, not a split of an
   existing one.

If the branch carries unmerged commits beyond the merged history, rebase
them onto the new base rather than discarding them.

### B) Enforcement

The rule is enforced deterministically by `.claude/hooks/pr_merge_status_guard.py`,
wired as a `PreToolUse` hook on `Bash` in `.claude/settings.json`. Prose alone
cannot enforce this — the instruction is furthest from the context window's live
edge exactly when the session has run long enough for the merge to happen.

The guard blocks a command only when all three conditions hold:

1. A **merged** PR exists whose head ref is the current branch, **and**
2. no **open** PR exists for the current branch, **and**
3. that merged PR's head commit is an ancestor of `HEAD`.

Condition 3 is what makes the guard self-clearing. Branch names are reused
after the reset in §21.A, so the merged PR matches `--head <branch>`
forever; ancestry is what separates "stacking on merged history" from
"fresh work that reuses the name". No override flag is needed, and the
guard goes quiet as soon as the branch is reset — before the new PR exists.

Condition 3 is refined for repositories that merge with **merge commits**.
There the merged head *is* an ancestor of the default branch, so after the
reset it is an ancestor of `HEAD` too and plain ancestry would block the
very remediation §21.A prescribes. The guard therefore also looks at where
the branch forks off `origin/<default>`: a fork point on the default
branch's first-parent chain means the branch was rebuilt from the default
branch (allow); a fork point on merged side history means the branch is
still sitting on the merged commits (block). Squash- and rebase-merged PRs
never put the merged head into the default branch, so plain ancestry
already decides for them.

The guard finds `git commit` and `git push` behind the shell forms that still
run them in the same repository, and judges them like a bare command:
- leading `VAR=value` assignments and an `env` wrapper;
- `time`, `command`, `nice`, `ionice`, `exec`, `nohup`, `setsid` and
  `timeout` prefixes, with their options;
- control words, `( ... )` subshells and `bash -c` / `eval` scripts.

It follows a `cd` written behind `VAR=value`, `builtin`, `command [-p]` or
`time [-p]`, all of which still run the builtin in the current shell.

When it cannot tell where the Git write runs, it asks for confirmation
instead of passing silently. That covers:
- another command that runs its words as a command, such as `sudo git push`
  or `xargs git push`;
- a prefix option it does not understand;
- a `cd` whose destination depends on a `CDPATH` set in the same command;
- a `cd` after `cd` is redefined (a function, an alias or `enable -n cd`);
- a Git write after a `pushd`, `popd`, `eval`, `source` or `.`, bare or
  behind the prefixes above.

`command -v git`, `time git status` and `echo "git push"` are not Git writes
and pass without a prompt.

The same hook guards the GitHub MCP push tools — `mcp__github__push_files`
and `mcp__github__create_or_update_file` — through a second `PreToolUse`
matcher in `.claude/settings.json`. Those tools write to a remote branch
without a local `git push`, so the remote branch tip (fetched via
`git ls-remote` + `git fetch`) takes the role `HEAD` plays for a local
commit. When the target repository is not the local checkout, ancestry
cannot be verified and a merged-PR match **asks** for confirmation instead
of blocking, because a block there could never self-clear.

### C) Fail-open contract

An unreachable GitHub API — `gh` missing, token expired or unauthenticated,
network failure, or Claude Code Web's agent proxy answering HTTP 403 as it
does for sessions without the GitHub App connected — does **not** silently
allow the command. The guard falls back to git history alone, fetching
`origin/<default>` through the git remote (which works in exactly the
sessions where the API does not):

1. If the branch sits on merge-commit side history already in
   `origin/<default>` and origin still holds a branch tip fully contained in
   `origin/<default>`, the command is **blocked** with the same remediation as
   the API path.
2. Otherwise git history is **inconclusive** (an absent remote ref could be a
   deleted merged branch or a never-pushed branch, and a rebuilt branch and a
   squash- or rebase-merged one look identical): a `git push` or an MCP push
   is routed through the harness permission prompt (`permissionDecision: ask`)
   so a human confirms the PR is still open, and a bare `git commit` is allowed
   with a warning, since the work only strands once pushed.

Only the cases where nothing branch-shaped can be checked at all — an
unparseable hook payload, detached HEAD, not a git repo, an undeterminable
`<owner>/<repo>` — still **allow** the command and emit a warning naming the
branch. A guard that hard-blocks every commit whenever the environment is
broken would cost more than the bug it prevents; asking on a push is the
compromise.

The merged-PR check is skipped on the default branch and when
`CLAUDE_PR_MERGE_GUARD=off` is set (default: unset, i.e. enabled). The API-write
confirmation safeguard remains active in both cases.

### D) API-call budget

Per §15, the guard issues **one** API call per guarded command — a single
`state=all` request answers both the merged and the open question — and
caches the result for 300 seconds keyed on `<slug>/<branch>`. Cached data may
satisfy an *allow* for a bare `git commit` only; a *block* is always
re-verified against a live call first, so opening a new PR clears the guard
immediately instead of after the TTL.

A push never reads the cache, because a cached answer may predate a merge and
a push is where the work strands. A `git push` or an MCP push always takes PR
state from a live lookup. When one command guards the same branch more than
once, a push reuses only a snapshot that came from a live lookup in the same
hook run; a snapshot an earlier `git commit` in that command took from the
cache is queried again once. A failed push lookup follows §21.C and never
falls back to cached data.

The git-history fallback in §21.C issues no GitHub API calls at all: one
`git ls-remote` for the branch and one `git fetch` for the default branch
(plus the branch, when origin still has it), both through the git remote.

The call goes over REST (`gh api repos/<slug>/pulls`), **not** `gh pr list`.
Claude Code Web's agent proxy serves only a pinned set of GraphQL operations
and rejects the rest with HTTP 403, and `gh pr list` is GraphQL-backed — using
it as the primary transport would make the guard fail open on every commit in
exactly the long-running web sessions §21 exists to protect. `gh pr list`
remains wired as a transport fallback for environments where REST is gated
instead. The fallback is a retry of the same question on a different
transport, never a second query.

### E) Push Command Shape

Write every `git push` as one plain command with literal arguments:
`git push -u origin <branch>`, or after a §21.A reset
`git push --force-with-lease origin <branch>` (or
`--force-with-lease=<branch>:<sha>` with the SHA typed out). Never put a push
inside a `for`/`while` retry loop: the guard cannot tell which directory the
push runs in, so it asks for confirmation. Keep
`$VAR` and `$(...)` out of the push arguments too, so the allow rules below
can match. Create the branch and commit in their own command before the push:
the guards cannot resolve a branch that the same command creates
(`git checkout -b X && ... && git push -u origin X`). In Auto mode, a push
whose only problem is one of these shapes is refused with instructions
instead of prompting (the merged-PR guard exits 2, the pre-push test gate
denies), so nothing runs and the session pushes again as a plain command.
When a push fails on a network error, retry it as a new call with
the same plain command.

`.claude/settings.json` allows the plain `git push` and
`git push --force-with-lease` forms to `origin claude/*` without a prompt
(§23.C makes force-pushing the session's own working branch routine). Its
`ask` rules cover pushes that delete a branch with `--delete`, `-d` or
`--prune`, or that name `main` or `master` as a destination. The trailing `*`
of a `claude/*` allow rule also matches a second refspec, so the
`pr_merge_status_guard.py` hook (§21.B) checks the parsed refspecs as well. It
asks for every branch deletion (`--delete`, `-d`, a `:ref` refspec, `--prune`),
and for every refspec whose destination is the repository's resolved default
branch, with or without a `+` prefix (`claude/x +main`, `claude/x:main`), so a
consumer repo whose default branch has another name is covered too. When
neither `origin/HEAD` nor `git ls-remote --symref origin HEAD` names the
default branch, it asks for every destination outside `claude/*`. What still
passes without a prompt: `claude/x:release` (a non-default branch, checked
only against its merged PR), a short `heads/<name>` destination such as
`claude/x:heads/main` (the hook compares it unexpanded), a push to a renamed
default branch while the checkout's cached `origin/HEAD` still names the old
one, and
`--force-with-lease=claude/a:<sha> origin claude/b` (which leases a different
branch from the one it overwrites). `CLAUDE_PR_MERGE_GUARD=off` turns these
deletion and default-branch asks off together with the merged-PR check; the
`settings.json` ask rules still apply. So push exactly one plain branch name, give
`--force-with-lease=` the same branch as the destination, and never add a
second refspec, a `+` refspec or a `src:dst` refspec to a push.

The same rule of one plain command applies to cleanup. Delete files with
their absolute path, in a call of their own, never chained after a `cd` (for
example `rm -rf /tmp/.../scratchpad/rel_shards`, not
`cd <dir> && ...; rm -rf ../../rel_shards`). Claude Code's built-in
critical-path check on `rm` appears to resolve a relative target against the
session's working directory rather than the earlier `cd`, so `../../x` from
`/home/user` reads as `/x`, a direct child of `/`. That check prompts in every
permission mode, and no allow rule or hook can approve it. Files under the
session scratchpad need no cleanup at all: the scratchpad goes away with the
container.
