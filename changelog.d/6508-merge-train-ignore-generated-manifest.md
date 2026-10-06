<!-- changelog: fixed -->
- **Unrelated AI PRs no longer queue behind the generated workspace inventory.** The merge train ignores `.ai/.workspace_source_manifest.txt` by default, and implementation and review commits no longer stage its regenerated contents.

The manifest is removed from the tracked tree while remaining a gitignored workspace runtime file. Older branches that still track it retain deterministic conflict handling; real shared paths still queue as before. Set `MERGE_TRAIN_IGNORE_PATHS=none` to restore the former overlap comparison.
