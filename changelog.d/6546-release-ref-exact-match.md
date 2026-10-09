<!-- changelog: security -->
- **The release workflows now accept only an exact `refs/heads/stable` dispatch, and only for a commit in `stable`'s history.**

`mark-stable.yml` and `test-and-mark-stable.yml` used to put `${{ github.ref_name }}` straight into the shell of their `source` job. A writer could dispatch from a branch named `$(echo${IFS}stable)`: the shell expanded the name, the check passed, and the release job then checked out that branch's commit with `GH_PAT` and ran its scripts. The ref now reaches the shell only through an environment variable and must equal `refs/heads/stable`. This also rejects a dispatch from the `stable` tag. A new `Verify dispatched commit is on stable` step then confirms through the API, without checking anything out, that the dispatched commit is the `stable` tip or an ancestor of it. It fails closed with `RELEASE_UNTESTED_HEAD` otherwise.

| The numbers that matter | Value |
| --- | --- |
| Accepted release ref | `refs/heads/stable` only |
| API reads added per release dispatch | 1, or 2 when the branch has moved since dispatch |
| `source` job `timeout-minutes` | 3 (was 1) |

What this means for operators: `promote-main-to-stable.yml` and `auto-release-stable.yml` now dispatch with `--ref refs/heads/stable`. Use the same form for a manual release (`gh workflow run test-and-mark-stable.yml --ref refs/heads/stable`). `gate_only` runs still accept any ref and never release. If you edit the workflow file itself on your own branch, these in-file checks can be bypassed; branch protection on `stable` is still required to close that path.
