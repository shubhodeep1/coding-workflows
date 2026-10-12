<!-- changelog: security -->
- **The merged-PR push guard now asks before a push deletes a remote branch or writes to the default branch, whatever the branch is called.**

Interactive sessions may push `claude/*` branches without a prompt (#7204). The `.claude/settings.json` ask rules for deletions and `main`/`master` work by wildcard, and the trailing `*` of the `claude/*` allow rule also matched forms such as `git push origin claude/x +main`. They also missed a consumer repo whose default branch has another name. `.claude/hooks/pr_merge_status_guard.py` now reads the parsed refspecs. It asks for every branch deletion (`--delete`, `-d`, a `:ref` refspec, `--prune`) and for every destination equal to the repository's resolved default branch, `+`-prefixed or not. When the default branch cannot be resolved locally or with `git ls-remote --symref`, it asks for every destination outside `claude/*`. Plain pushes of the session's own `claude/*` branch still pass without a prompt.

What this means for consumer repos: the guard and its template copy reach you on the next `@stable` sync, and a push that could overwrite or delete a shared branch now waits for a person to confirm it.
