<!-- agents: section="Workflow architecture" -->
`gh api` permission guard and the consumer audit claims (#7555). Both
`gh_api_write_guard.py` copies stay byte-identical. A write to
`repos/{owner}/{repo}/...` is routine only when `placeholder_repo_slug` (one
`git config --get-regexp '^remote\..*\.(url|gh-resolved)$'`) finds every
remote URL naming one parseable github.com repository equal to origin's (an
unparseable remote, a second repository or a `gh-resolved` value other than
`base` or that repository returns `""`), the command has no `cd`/`pushd`/`popd`
word and no `GH_*` or `GIT_*` assignment, the hook's environment has no other
`GH_REPO` and no `GH_HOST` other than github.com, and `CLAUDE_ENV_FILE` (read
up to 64 KiB, regular file only; unreadable or larger counts as a redirect)
does not assign `GH_REPO` or `GH_HOST`. An explicit-slug write also prompts
with a `GH_*` assignment, a redirecting session `GH_HOST`/`CLAUDE_ENV_FILE`, or
`--hostname` other than github.com. `classify` takes an optional
`placeholder_lookup`; without it a placeholder write is not routine. The early
filter (`_may_invoke_gh_api`) also matches quoted spellings (`'gh' api`,
`gh a''pi`, `gh \<newline>api`) and `gh <expansion>`, so the tokenizer
classifies them like the plain form; `gh $'api'`, `$G api` and `env -S '...'`
count as hidden calls. The guard reads only `GH_REPO`, `GH_HOST` and
`CLAUDE_ENV_FILE` from the environment, and each can only turn an allow into a
prompt. CLAUDE.md §23.H's routine row still says "(or `{owner}/{repo}`)"
without these conditions; an interactive session updates both CLAUDE.md
copies. The live copy's module docstring has `gh \<newline>api`, an invalid
escape that Python reports as a warning; fix it as `\\<` in both copies
together.
