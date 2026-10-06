<!-- changelog: security -->
- **Merged-PR push checks validate Git's effective destination.** The guard now checks URL rewrites and inline Git configuration before looking up PR status. Refs #6434; related to #3576.

Named remotes and literal GitHub URLs that rewrite to non-GitHub destinations are blocked before the push; raw non-GitHub remotes rewritten to GitHub are checked against that GitHub repository. Commands that change configuration earlier in the same call or use `env -S` ask for confirmation.
Literal non-GitHub URLs rewritten to GitHub are checked against the effective GitHub repository too.

What this means for operators: a local mirror configured through `insteadOf` blocks pushes unless a `pushInsteadOf` rule keeps the effective push on GitHub. Use the guard override only for an intentional non-GitHub push.
