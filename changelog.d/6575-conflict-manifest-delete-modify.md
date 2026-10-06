<!-- changelog: fix -->
- **A delete/modify conflict on `.ai/.workspace_source_manifest.txt` no longer stops the conflict resolver with `sandbox_path_unsupported`.**

The resolver's prepare step (`scripts/review_conflict_prepare.sh`) already settled two-sided conflicts on the generated workspace manifest without a model. When one side deleted the manifest and the other changed it, the file went to the model resolver instead. That resolver's sandbox never admits `.ai` paths, so the run refused host fallback and failed (PR #6209, run 37487619837, and further occurrences on PRs #6538, #6146 and #6498). The prepare step now keeps the side that still has the file, sorted and deduplicated, and stages it. If the manifest was the only conflict, the step commits the `[ai-merge-resolve]` merge itself, as it does for two-sided conflicts. `.ai` stays outside the sandbox. `CONFLICT_MANIFEST_UNION_ENABLED=false` turns this off together with the existing set-merge, and integration-sync branches (`orchestrator/project-*`) are still left to the model resolver.

`scripts/review_untrusted_workspace.py` gains a `report-rejections <host> <path-file> [<manifest>]` subcommand. For each path the sandbox would refuse, it prints `REVIEW_UNTRUSTED_PATH_REJECTED path=<path> rule=<rule>`. Paths are reduced to printable ASCII and capped at 200 characters, at most 10 lines are printed, and file contents are never read into the log. The admission rule itself (`allowed()`) is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Failed review runs reported to this heal before the fix | 7, on 4 pull requests |
| Paths newly admitted to the resolver sandbox | 0 |
| New GitHub API calls | 0 |
