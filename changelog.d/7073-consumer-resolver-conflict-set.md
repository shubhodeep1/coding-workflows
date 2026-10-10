<!-- changelog: fixed -->
- **The PR conflict resolver works in consumer repos again.** Since Oct 6 every merge conflict outside coding-workflows stopped with `reason=sandbox_path_unsupported` before any model ran.

`scripts/review_conflict_resolve.sh` checked the conflicted paths against `conflicted_paths.txt`, a list `scripts/review_conflict_prepare.sh` writes only on the workflow source repo. In a consumer repo the file was missing, the sandbox path check failed without naming anything, and the autofix run failed (for example `drhyg_ecommerce_automation` PR #66). Consumer repos now check the merge-replay allowlist, the same unmerged paths the resolver is asked to fix. A conflict on a host-only path such as `.claude/settings.json` now stops with `sandbox_path_host_only` and names the path that needs a manual merge.

What this means for operators: conflicts on ordinary files in consumer PRs are resolved automatically again; a "needs a manual merge" error names exactly which file a person has to merge.
