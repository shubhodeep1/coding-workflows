<!-- changelog: changed -->
- **Interactive sessions now push their own `claude/*` branch with `--force-with-lease` without a permission prompt, and pushes that use `--delete`, `-d` or `--prune`, or name a plain `main`/`master` destination, now always ask.**

After a session restarted its branch from `main` (CLAUDE.md §21.A), every `git push --force-with-lease` asked for approval: no allow rule matched it, and pushes wrapped in a shell retry loop leave the merged-PR guard unable to resolve the push directory. `.claude/settings.json` now allows the plain force-with-lease forms to `origin claude/*`, and the new CLAUDE.md §21.E tells sessions to push with one plain command and never inside a retry loop. Because a trailing `*` in a `claude/*` allow rule also matches `claude/x:main` or `claude/x --delete`, new `ask` rules prompt for pushes with `--delete`, `-d`, `--prune`, or `main`/`master` as a destination. The merged-PR guard did not catch those forms, so this also closes the gap for the existing `git push origin claude/*` rule. The ask rules do not catch a `src:dst` refspec to another branch, a colon deletion such as `claude/x :release`, a force-prefixed `+main` or `+master` refspec, or a lease on a different branch from the destination, so §21.E tells sessions to push one plain branch name with a matching lease and no `+` refspec.

| The numbers that matter | Value |
| --- | --- |
| New allow rules | 2 |
| New ask rules | 12 |
| Default branch names covered by ask rules | `main`, `master` |

What this means for consumer repos: after the next `@stable` sync, a session's force-with-lease push of its own branch goes through without a prompt, and a push that would write or delete a default branch named `main` or `master` stops for confirmation. A repo whose default branch has another name gets no ask rule for it.
