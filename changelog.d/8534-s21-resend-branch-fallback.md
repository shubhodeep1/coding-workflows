<!-- changelog: changed -->
- **Interactive sessions no longer stall when a ruleset refuses the force-push after a merged-PR reset.** New CLAUDE.md §21.F tells the session to push the work to `<branch>-resend` and open the PR from there, without asking.

Before, a session whose PR merged mid-session reset its branch per §21.A, hit a ruleset that bans force-pushes (for example on `ccr-*` branches, `GH013` / `Cannot force-push to this branch`), and then stopped to ask the user how to proceed. §21.F makes the new-branch option the default: create `<branch>-resend` (or the first free `-resend-2`, `-resend-3`, ...), push it with a plain `git push -u origin <branch>-resend`, open the PR from it and keep working there. Deleting the old branch, changing repository rules and retrying the force-push in another form stay ask-first under §23.C, and a refused `-resend` push is reported and asked about.

What this means for consumer repos: CLAUDE.md and `.claude/docs/s21-merged-pr-commit-guard.md` arrive with the next `@stable` sync, and interactive sessions there follow the same fallback. No guard, setting or workflow changes.
