<!-- changelog: security -->
- **Merged-PR push checks validate Git's effective destination.** The guard now checks URL rewrites and inline Git configuration before looking up PR status. Refs #6434; related to #3576.

Named remotes and literal GitHub URLs that rewrite to non-GitHub destinations are blocked before the push; commands that change configuration earlier in the same call ask for confirmation.

What this means for operators: a local mirror configured through `insteadOf` also blocks pushes unless an identity `pushInsteadOf` rule keeps the push on GitHub. Use the guard override only for an intentional non-GitHub push.
