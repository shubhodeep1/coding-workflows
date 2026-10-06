<!-- changelog: security -->
- **Merged-PR push checks validate Git's effective destination.** The guard now checks URL rewrites and inline Git configuration before looking up PR status. Refs #6434; related to #3576.

Named remotes and literal GitHub URLs that rewrite to non-GitHub destinations, as well as empty configured push URLs, are blocked before the push; raw non-GitHub remotes rewritten to GitHub are checked against that GitHub repository. Commands that change configuration earlier in the same call or use `env -S` for a push ask for confirmation; literal `env -S` commits are checked normally.
Git writes that use process substitution ask for confirmation, since the substituted configuration cannot be resolved safely from the command text.
The effective destination is checked for branch deletions and tag-only pushes as well, without a PR-history lookup for those operations.
The guard also checks inline overrides when `env` or `git` is invoked by an absolute path.
Literal non-GitHub URLs rewritten to GitHub are checked against the effective GitHub repository too.
Rejection messages show remote names, not literal push URLs that may carry credentials in query strings.
Git history fallback does not fetch through a non-GitHub network mirror when its configured push URL resolves to GitHub.

What this means for operators: a local mirror configured through `insteadOf` blocks pushes unless a `pushInsteadOf` rule keeps the effective push on GitHub. Use the guard override only for an intentional non-GitHub push.
