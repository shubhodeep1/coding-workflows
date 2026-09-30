<!-- changelog: added -->
- **Guard hook changes are now run old-against-new in CI.** A new `Guard differential check (issue #5174)` step fails a pull request whose `.claude/hooks/*_guard.py` change allows a command the base hook blocked, denied, or asked about.

The reviewer panel reads hook diffs but never executes them. #5144's rewrite of `pr_merge_status_guard.py` (PR #5173 at `b6dd693`) passed review with three push shapes (`HEAD:heads/<branch>`, unexpanded `$VAR` refspecs, glob refspecs) that the old hook blocked and the new one allowed with no warning. `scripts/guard_differential.py` now runs every shape in `tests/guard_corpus/<hook>.txt` through the base branch's hook and the PR's hook in a scratch repository with a stub `gh`, and fails on any shape that became less strict, even when the new hook prints a warning. A loosening that is meant needs an approval merged into the base branch first: an entry in `.github/guard_differential/intended_loosening.json` naming the hook, the exact corpus shape, and the PR's head commit. It counts as loosening under the retire-master Q3: A rule, so a #4785 sync carrying it waits for the operator. The PR body's `Intended loosening:` list is only reported, never trusted, because the PR author writes it (#5326). The check makes no GitHub API calls and skips PRs that change no hook.

| The numbers that matter | Value |
| --- | --- |
| Corpora shipped | 4 (`pr_merge_status_guard`, `gh_api_write_guard`, `pr_watch_guard`, `inline_edit_guard`) |
| Shapes flagged on PR #5173 at `b6dd693` | 22, covering all three review findings plus the `cd`-separator and `-`-source shapes |
| Shapes flagged on PR #5173 at `03c2487` (the fix) | 0 |
| Where it runs | `tests-hooks-and-orchestrator` job of `.github/workflows/ci.yml` (reported through `CI / lint`), pull requests into `main` and `stable` |

What this means for operators: a guard change that reaches `main`, including a #4785 twin-sync PR, has been executed against the known bypass shapes first. When a review or incident finds a new bypass, add the shape to the hook's corpus file.

### For contributors

Decisions rank block = deny > ask > none > allow = error (a crashed hook does not block). A hook missing on one side counts as `none`, so deleting a guard fails and a new hook fails only where it answers `allow`. A changed guard, new ones included, with no corpus file, or with one that holds no shape, fails. Phase PRs into a `claude/implement-plan-*` project branch do not run `ci.yml`; the project's final PR into `main` does. Details are in `agents.md` under "Guard differential check".
