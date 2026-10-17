<!-- agents: section="Workflow architecture" -->
`gh api` permission guard and the consumer audit claims (#7555). Both
`gh_api_write_guard.py` copies stay byte-identical. A write to
`repos/{owner}/{repo}/...` is routine only when `placeholder_repo_slug` (one
`git config --get-regexp '^remote\..*\.(url|gh-resolved)$'`) finds every
remote URL naming one parseable github.com repository equal to origin's (an
unparseable remote, a second repository or a `gh-resolved` value other than
`base` or that repository returns `""`), the command has no `cd`/`pushd`/`popd`
word (also checked after quotes and backslashes are removed, so `'cd'`, `\cd`
and `c''d` count) and no `GH_*` or `GIT_*` assignment, the hook's environment
has no other `GH_REPO` and no `GH_HOST` other than github.com, and
`CLAUDE_ENV_FILE` (read up to 64 KiB, regular file only; unreadable or larger
counts as a redirect) does not assign `GH_REPO` or `GH_HOST`. An explicit-slug
write also prompts with a `GH_*` assignment, a redirecting session
`GH_HOST`/`CLAUDE_ENV_FILE`, or `--hostname` other than github.com. `classify`
takes an optional `placeholder_lookup`; without it a placeholder write is not
routine. The early filter (`_may_invoke_gh_api`) also matches quoted spellings
(`'gh' api`, `gh a''pi`, `gh \<newline>api`), `gh <expansion>`, and a word with
`$` or a backtick before `api` (`$G api`, `${G} api`, `` `printf gh` api ``,
`$(printf gh) api`, also after quote removal, so `$G "api"` and `$G a''pi`
count; `_has_expanding_word_before_api`, which also matches a `)` before `api` in text holding `$(`, so nested substitutions such as `$(printf $(printf gh)) api` count), `gh` glued to an expansion (`gh${IFS}api`, `_GH_GLUED_EXPANSION_RE`, also checked on the command word in `has_hidden_gh_api`), a brace expansion with a `gh` element (`{gh,api}`, `_BRACE_WITH_GH_RE`, which then asks as a shell-rewrite hazard), and `gh` followed by a flag and later
`api` (`_GH_FLAG_BEFORE_API_RE`). `evaluate` asks when a `gh` token is followed
by a flag and later an `api` token (`_gh_flag_before_api`, for example
`gh -R other/repo api ...`), because the guard does not read gh's global flags.
`_unquoted_gh_api_expansion` also removes backslash-newlines first. `shell_segments` removes
a backslash-newline instead of turning it into a space, so `gh a\<newline>pi`
parses as `gh api`. `gh $'api'`, an expanding word before `api` and
`env -S '...'` count as hidden calls and prompt. The guard reads only
`GH_REPO`, `GH_HOST` and `CLAUDE_ENV_FILE` from the environment, and each can
only turn an allow into a prompt. CLAUDE.md §23.H's routine row still says
"(or `{owner}/{repo}`)" without these conditions; an interactive session
updates both CLAUDE.md copies, which pipeline editors cannot write.
