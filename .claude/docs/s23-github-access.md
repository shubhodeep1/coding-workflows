# CLAUDE.md §23: full reference

This is the full text of CLAUDE.md §23. CLAUDE.md keeps the
rules every session needs and points here for the rest. Both are binding;
where this file adds detail, follow it. It reaches consumer repos with the
rest of `.claude/` through the `@stable` sync.

## §23. GitHub Access (MANDATORY)

The session environment provides a `GH_TOKEN` env var — a GitHub Personal
Access Token whose reach is wider than the session's built-in GitHub tooling
(other repositories, Actions logs, endpoints the MCP surface does not expose).
This section applies in this repo and in every consumer repo that receives
this file via the `@stable` sync. It splits GitHub work into three postures:
**reads are self-serve** (act, do not ask), **routine repository writes are
self-serve**, **destructive and administrative writes are ask-first**.

`GH_TOKEN` is the interactive counterpart of `DIGITALOCEAN_ACCESS_TOKEN`
(§22): a standing credential the session uses itself, rather than a reason to
hand the user commands to run.

### A) Read Operations — Act, Do Not Ask

When a task needs data from GitHub that the session's default tooling cannot
reach, pull it yourself with the token instead of asking the user to fetch it
or to run `gh` on your behalf. This is an explicit carve-out from §2 for
**read-only** GitHub calls — needing GitHub-hosted data is not, by itself, a
reason to stop and ask. Self-serve reads include (non-exhaustively):

- pull requests, issues, review threads, review comments, timeline events,
  labels, and linked-issue relationships;
- Actions workflow runs, jobs, check runs, annotations, and **run/job logs**
  (`gh run view --log <run-id> -R <owner>/<repo>`,
  `gh run view --log --job <job-id> -R <owner>/<repo>`,
  `gh run view --log-failed <run-id> -R <owner>/<repo>`,
  `gh api repos/<owner>/<repo>/actions/runs/<id>/logs`);
- commits, diffs, blame, branches, tags, releases, and file contents at any
  ref, including in a consumer repo whose wrapper or workflow is implicated;
- repo and org metadata a task depends on: default branch, protection rules
  as reported by the API, repo variables (`gh variable list`), workflow
  definitions, and `gh api rate_limit`.

**Repo scope is not widened by the token.** A PAT that *can* read a
repository is not permission to browse one the task does not involve. Stay
within the repos the work actually touches: this repo, the consumers listed
in `.github/ai/consumer_repos.json`, and any repo the user names. When the
host session exposes a repo-attachment mechanism, prefer attaching the repo
over reaching around the session's declared scope.

**Web sessions: the PAT never reaches GitHub.** In Claude Code on the web
(claude.ai/code and the mobile/desktop apps backed by it) every request to
`api.github.com` — `gh`, `curl`, anything — passes through the session's
agent proxy, which strips the `Authorization` header and signs the request
with its own short-lived GitHub App token. `GH_TOKEN` is read by `gh` but
discarded in transit, so a valid, invalid, or absent token all behave the
same. Consequences, verified against the proxy (2026-09-20):

- **Identity and reach are the proxy's.** Calls authenticate as the user
  who connected the GitHub App, and only for repositories attached to the
  session. A consumer repo that is not attached answers 403 `GitHub access
  to this repository is not enabled for this session. Use add_repo to
  request access.` — attach it (with `access: "push"` when writes or the
  API are needed) rather than treating the 403 as a token problem. Making a
  repo reachable in *every* session is done in the Claude GitHub App
  installation and claude.ai's repository access settings, not with any
  env var.
- **Some endpoints are refused outright**, whatever the token: GraphQL
  (§23.D), and Actions paths such as repo variables (`Access to this GitHub
  Actions path is not permitted through this proxy.`). Run/job logs and
  runs remain readable.
- **`gh auth status` is not a token test here.** It verifies over GraphQL,
  so it reports `The token in GH_TOKEN is invalid` for a good PAT. The
  SessionStart hook therefore probes over REST (`gh api user`) and, when an
  unauthenticated `GET /user` also returns 200, reports that the proxy is
  substituting its credential.
- **The reads and writes above that need the PAT itself** — other repos
  without attaching them, GraphQL, repo variables — are available only in
  a local Claude Code session (CLI, desktop app, IDE extension), where no
  proxy sits in front of `api.github.com` and `gh` uses `GH_TOKEN`
  as-is. Do not try to route around the proxy (unsetting `HTTPS_PROXY`,
  alternate hosts, credential helpers): direct egress is blocked as well.

### B) Routine Repository Writes — Act, Do Not Ask

These writes are ordinary session work, already implied by the task the user
gave you. Perform them with the token (or the MCP equivalent, per §23.D)
without a separate approval round:

- pushing commits to the session's own designated working branch;
- opening a pull request, updating its title/body, marking it ready for
  review, and pushing follow-up commits to it;
- pull request and issue comments, review-thread replies, and resolving
  review threads you have addressed;
- applying or removing `ai:*` and other workflow labels the pipelines expect,
  and unsubscribing from PR activity. Subscribing to PR activity is **not**
  a routine write: §25 forbids it outright, and a `PreToolUse` hook blocks
  the call.

Three constraints ride along and are **not** relaxed by this subsection:
§19 (no auto-close keywords against `ai:orchestrator-tracking` issues) governs
every body you post, §25 (PR watching is disabled) governs what happens after
a push, and §21 (merged-PR commit guard) governs every commit
and push.

### C) Destructive & Administrative Writes — ALWAYS Ask First

**Never perform these without asking first** in the §2 Q/A format — even when
the need seems obvious, and even under §12's proactive PR-review scope (this
subsection is NOT superseded by §12):

- merging a pull request, enabling auto-merge, or overriding a failing
  required check;
- force-pushing or otherwise rewriting history on any branch other than the
  session's own working branch, and any push to a default or protected
  branch;
- deleting or renaming branches, tags, releases, issues, or repositories, and
  closing a PR or issue the session did not itself open;
- repository or organization administration: settings, secrets, variables,
  rulesets, webhooks, collaborators, visibility, transfer, archive;
- dispatching workflow runs (`gh workflow run`, `repository_dispatch`,
  re-running a workflow) that start a billed or unattended pipeline;
- any write to a repository outside the scope the session was given.

The question must name the exact repository, the exact object (PR number,
branch, setting), and what the operation changes, so the user approves a
concrete action rather than an intention. After approval, perform the
operation yourself with the token — do not hand the user a command to run
(§18).

**Command-invoked dispatches are already approved.** When the user invokes a
command whose documented job is to dispatch a workflow — `/implement-plan-ai`,
`/apply-analysis`, and any successor that says so in its own file — that
invocation *is* the approval for the dispatch the command describes. Do not
re-ask; the ask-first rule above covers dispatches you would be initiating on
your own judgement. Likewise, the user's go on a §31 plan
approves opening the issues (or the orchestrator dispatch) that plan names.

### D) Transport & Tool Precedence

1. **Prefer the `mcp__github__*` tools** for anything they cover. They are
   scope-checked by the host session and keep the audit trail consistent.
2. **Reach for `GH_TOKEN` when MCP is not enough** — the capability is not
   exposed, the call is scope-gated (403), the response is truncated, or the
   repository is not attached to the session.
3. **Prefer REST over GraphQL** on the `gh` path. Claude Code Web's agent
   proxy serves only a pinned set of GraphQL operations and rejects the rest
   with HTTP 403, so GraphQL-backed commands (`gh pr list`, `gh issue list`,
   `gh search`) can fail in exactly the environments this section is written
   for. Use `gh api repos/<owner>/<repo>/...` as the primary transport and
   treat the GraphQL-backed command as a fallback, never the reverse — the
   same reasoning §21.D applies to the merged-PR guard.

4. **Shape `gh api` calls so the §23.H guard can approve them.**
   **Type IDs literally into the endpoint path** —
   `gh api repos/o/r/actions/runs/123/jobs`, not `.../runs/$RUN_ID/jobs` or
   `.../commits/$(git rev-parse HEAD)/check-runs`, and do not wrap `gh api`
   in `$(...)`. An unquoted expansion in a `gh api` argument prompts in every
   permission mode, and so does a `gh api` call inside a `$(...)` that sits
   in a loop or in a larger word (in Auto mode the guard leaves both to the
   Auto-mode classifier, or denies them with rewrite steps when an allow rule
   such as `Bash(gh api repos/*)` or `Bash(test *)` could match the command,
   §23.H). Look a value up with one
   call, then issue the next call with the value typed out. Put GET
   parameters in the URL (`gh api 'search/issues?q=...&per_page=50'`) or
   pass them with `-X GET -f ...`, never as bare `-f` fields (that makes `gh`
   send a POST). Except for the literal-ID read loops in §23.H, keep `gh api`
   calls out of loops, `$(...)`, `$VAR` paths, file redirects, and heredoc
   scripts, and edit a PR's title or body with
   `mcp__github__update_pull_request`. Post or edit an issue or PR comment
   with `mcp__github__add_issue_comment` / `mcp__github__update_issue_comment`,
   never with `gh api … --input <file>`, `-F body=@<file>`, or a heredoc that
   builds the JSON body: file-backed fields and `--input` always prompt
   under §23.H, on every method and endpoint (where no MCP tool is
   available, pass the text inline with `-f body=...`), a
   heredoc holding `{"` trips Claude Code's own shell check, and an
   unattended session then stalls at a prompt nobody answers. Reads and
   §23.B routine writes are approved without a prompt when the command
   holds only them plus the safe
   helpers §23.H lists (`cd`, `sleep`, `echo`, `2>&1`, pipes into `head`,
   `tail`, `wc -l`, `sort`); beside anything else the guard leaves the
   decision to the allow list or the Auto-mode classifier.

Transport, in order of preference:

```
gh api repos/<owner>/<repo>/<endpoint>          # gh reads GH_TOKEN natively
curl -sS -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/<owner>/<repo>/<endpoint>"
```

Two environment facts that make `gh` calls fail in confusing ways:

- **Pass `-R <owner>/<repo>` on every `gh` call that needs repo context.** In
  Claude Code Web the only git remote points at a local proxy, so bare calls
  fail with `failed to determine base repo` — which is not an auth problem.
  The SessionStart hook prints the resolved slug.
- **Check auth over REST, nounset-safe**, rather than inferring it from the
  SessionStart log:
  `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh api user --jq .login`.
  Do not use `gh auth status` for this in a web session: it verifies over
  GraphQL, which the proxy answers with 403, so it reports the token as
  invalid whether or not it is (§23.A, "Web sessions"). The hook's secondary
  probe only tests `actions:read` for one repo and can emit a
  `NOTE`/`WARNING` while `gh` works fine for everything else.

Useful token scopes: classic PATs need `repo` plus `workflow` (and
`read:org` for org metadata); fine-grained PATs need contents, pull requests,
issues, and **actions: read** for run/job logs. A token without `actions:read`
still serves every non-Actions read in §23.A.

### E) Token Hygiene and Degradation (hard rules)

- Never echo, log, or print the token value; reference it only via env
  expansion (`$GH_TOKEN`).
- Never write the token into committed files, PR bodies, issue comments, commit
  messages, or diagnostic output. Redact it if a tool response contains it.
- If the token is missing, the REST identity probe fails, or an API call
  returns 401/403, **say so once**, fall back to the `mcp__github__*` tools for
  whatever they can still reach, and continue with the rest of the task — do
  not retry-loop, and do not ask the user to run the calls manually (§18).
  Diagnose authentication with `gh api user --jq .login`, not the GraphQL-
  backed `gh auth status`. In a web session, a successful identity probe may
  be the proxy's identity and a 403 may be a proxy scope restriction (§23.A);
  only a local session's rejected REST probe supports diagnosing the PAT as
  invalid, expired, or saved incorrectly.
- Never commit a workflow, script, or hook that reads `GH_TOKEN` from the
  session environment. This section governs interactive sessions only;
  Actions-side code authenticates via `secrets.GH_PAT` (§23.G).

### F) API-Call Budget

§15 (GitHub API Call Hygiene) applies to interactive `gh` and `curl` calls,
not just to workflow code. Before adding a call, check whether an existing one
in the same task can be extended; batch per-item lookups; cache results you
will need again in the same session instead of re-fetching per loop iteration.

### G) Relationship to `GH_PAT` and `GITHUB_TOKEN` (§6)

`GH_TOKEN` already exists as an identifier in this repo's Actions workflows
and scripts, where it is exported from the `GH_PAT` secret. That name is
unchanged and must not be repurposed — the two readings are the same variable
name in two different environments:

| Environment | Value | Governed by |
|---|---|---|
| Actions workflows / unattended pipelines | `${{ secrets.GH_PAT }}` exported as `GH_TOKEN` | workflow YAML, `unattended_system_instructions.md` |
| Interactive Claude Code session | PAT set in the Claude Code cloud session environment | this section |

Consequences:

- Do not "unify" the two by rewriting workflow YAML to read a session env var,
  and do not add `GH_PAT` handling to interactive tooling. Either direction is
  a §6 breaking change and requires the §2 ask flow.
- `GITHUB_TOKEN` remains the accepted fallback name in session tooling that
  already checks both (`.claude/hooks/session-start.sh`, the `.claude/commands/*`
  Tool Access blocks). Keep accepting both; prefer `GH_TOKEN` when both are set.
- The unattended pipelines read `unattended_system_instructions.md` and never
  see this file, so §23 grants no new access to any codex-driven phase.

### H) Permission Guard for `gh api`

Whether a `gh api` call prompts is decided by
`.claude/hooks/gh_api_write_guard.py`, a `PreToolUse` hook on `Bash` in
`.claude/settings.json`, not by `permissions.ask` rules. The seven former
ask rules (`gh api * -X *`, `--method`, `-f`, `-F`, `--field`,
`--raw-field`, `--input`) could not tell a read from a write: a search that
passes query parameters with `-X GET -f q=...` prompted like a
`-X DELETE`, and an ask rule prompts even in Auto mode and even when a hook
allows, so unattended stage sessions stopped on reads. Do not add
`gh api` ask rules back; they would override the hook.

For each `gh api` call the hook works out the method the way `gh` does
(`-X`/`--method`, else POST when fields or `--input` are present, else GET)
and classifies it:

| Class | What | Outcome |
|---|---|---|
| read | GET/HEAD to any REST endpoint; a GraphQL query that is not a mutation, is not read from a file, and has no shell expansion; in both cases with no file-backed `-F` value and no `--input`. A complete `for VAR in TOKEN…; do BODY; done` over literal IDs is also approved when every item is a vetted `gh api` read, `gh run view/list` or `gh pr view` read, or literal/`$VAR` echo (§23.H below). A bare `gh api --help` or `gh api -h` with no other argument is a read too (it prints usage and sends no API request); `--help` / `-h` next to an endpoint, any other flag or `--` stays unreadable and prompts (#6668). | not prompted by the hook |
| routine | a §23.B write to the local checkout's repository (or `{owner}/{repo}`): create a PR; edit a PR's or issue's `title`/`body`; add or edit an issue or PR comment; reply to a review thread; add or remove one label; request reviewers; dispatch (`ref`, `inputs` only) one of the workflows `.claude/settings.json` already allows as `gh workflow run <file> *` (§23.C command-invoked carve-out) | not prompted by the hook |
| write | everything else: any other endpoint or field (`state`, `base`, merges, dispatches, deletions, settings), another repository, `--input` or a file-backed `-F`/`--field` value (`@<file>`, or `@-` for stdin, which `gh` reads and sends) on any method, endpoint, or repository, GraphQL included (issue #4619; a `-f`/`--raw-field` value is sent literally and reads no file), an `-F` word the shell could rewrite into one (`$`, a backtick, `~`, or a glob character in it), a header other than `Accept`/`X-GitHub-Api-Version`, an unreadable call, or `gh api` that could run hidden (in a backtick or double-quoted `$(...)` substitution Bash would run — single-quoted text is data — handed to `bash -c`, `sudo`, `xargs`, `python3` and similar, or in a heredoc fed to one) | prompt, in every permission mode (in Auto mode a hidden call gets no decision instead, or a deny with rewrite steps where an allow rule could match; see "Auto mode" below) |
| malformed jq | a `-q`/`--jq` value that is one of jq's own command-line options (matches `^--?[A-Za-z]`: `--arg`, `-r`, `--raw-output`, `-c`); `gh api` has no such flags, so the call could never work (#4891) | denied with a reason that says how to fix the command; nothing runs and no human is needed |

The hook decides once for the whole Bash call: **deny** when any call
passes a jq command-line option to `--jq` (checked after the
unparseable-command and hidden-call asks, and winning over ask and allow;
put the value into the jq program, pipe the output to `jq` with its own
options, or wrap a program that starts with a minus sign in parentheses,
`--jq '(-length)'`); **ask** when any call is a
write, or when the command (heredoc bodies aside) uses ANSI-C quoting
(`$'...'`), an unquoted `#` comment, or brace expansion (`{a,b}`), which
Bash parses unlike the guard, so a word could become a hidden flag or
command; **allow** when every call is a read or routine and the command
holds nothing else but safe helpers (items joined by `;` or `&&`, each a
`gh api` call, optionally piped into `head`/`tail -n N`, `wc -l`, or
`sort -n -r -u -k K -t C`, or a standalone `cd <path>`, `sleep <n>`,
`echo <text>`, or `true`; `2>&1` as the only redirect; no `$`, backticks,
globs, subshells, or loops). One narrow exception: a whole `for VAR in TOKEN
…; do BODY; done` loop is allowed when its header contains only unquoted
literal IDs (letters, digits, `.`, `_`, `-`; not starting with `-`) and a
plain shell-name counter that cannot override command lookup, gh settings,
or proxy settings. Uppercase ordinary counters such as `ID` are allowed.
Its body can only contain classified `gh api` reads with `$VAR` / `${VAR}`
in the endpoint path (not the first segment or query string), allowlisted
`gh run view/list` or `gh pr view` reads with the counter only as a positional
argument, and literal/counter `echo` items, joined by `;` or `&&` and
optionally piped into the same safe filters. No surrounding commands,
unvetted options, other expansions, nested loops, or file redirects are
approved. A `gh api` write in the body still asks. Otherwise **no decision**,
so the allow list or the Auto-mode classifier decides, because an allow would
also approve code the guard has not read (an arbitrary loop, a `python3`
heredoc, a `$VAR` redirect). `gh api` text that is only data (`git commit -m`, `grep`,
`echo`) is ignored.

**Auto mode.** A hook's ask prompts a human even in Auto mode, so when the
hook payload's `permission_mode` is `auto` the asks that only mean "the
guard cannot trust this command's shell shape" give **no decision** instead,
and the Auto-mode classifier judges the command. Those are an unparseable
command, a `gh api` call that could run hidden, ANSI-C quoting / an unquoted
`#` comment / brace expansion, and an unquoted expansion in a `gh api`
argument. A call the guard reads and classifies as a write (an unreadable
call included) still asks, a flag between `gh` and `api` still asks, the
malformed-jq deny and every allow are unchanged, and the command is never
allowed when one of those doubts fired. A command that an allow rule could
match is **denied** in Auto mode instead, with a reason that says how to
rewrite it (one plain `gh api` call per ID with the ID typed out, no `$VAR`
file redirects, the Edit or Write tool for text that only mentions `gh api`),
because Auto mode resolves a matching allow rule before its classifier runs,
so a no-decision there would approve the command unprompted, and an ask would
stop on a prompt. A deny runs nothing. That covers `gh api repos/` and `gh api user`
(including quoted spellings such as `'gh' api repos/`), and any `Bash(...)`
allow rule in the project's `.claude/settings.json` /
`.claude/settings.local.json`, `~/.claude/settings.json`, `CLAUDE_CONFIG_DIR`
or the managed settings files that matches the command or one of its parts
(for example `Bash(test *)` around a hidden `$(gh api -X DELETE ...)`). A part
that starts with a wrapper such as `timeout`, `nice`, `env`, `sudo` or `xargs`
is also matched with its leading words dropped, so
`timeout 60 git diff "$(...)"` counts as matching `Bash(git diff *)`. A
settings file that exists but cannot be read is treated the same way (deny),
and so is a brace / ANSI-C / comment hazard in a command that holds an `@` or
an escape that can produce one, since it could hide a file-backed
`-F key=@file` field. Every other mode keeps the prompts.
The headless Actions runs use `dontAsk` or `bypassPermissions`, where an ask
is a denial; `tests/test_gh_api_write_guard.py` fails if a script under
`scripts/` starts Claude in Auto mode.

It fails **closed**, unlike the §21/§25 hooks: an unreadable, invalid,
or non-object payload, or an internal error, asks, in Auto mode too. Empty input is allowed
silently. It issues no GitHub API calls (§15), runs only
`git config --get remote.origin.url` (and only when a call could be
routine), and has no environment-variable escape hatch. It does not see
`gh` behind an expansion (`$GH api ...`), which the old ask rules did not
either. The hook and the settings entry ship to consumer repos through the
same `.claude/` sync as the other hooks; `tests/test_gh_api_write_guard.py`
covers the rules and the wiring and runs in its own `ci.yml` step.

### I) Permission Prompt Reports

Retired on 2026-10-03. The session-based Claude automation was replaced by
the Claude CLI engine in the Actions pipelines
(docs/plans/replace-claude-sessions-with-cli-engine-plan.md). Section
number kept per §6.
