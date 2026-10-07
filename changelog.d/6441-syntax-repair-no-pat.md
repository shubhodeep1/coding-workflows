<!-- changelog: security -->
- **Keep GitHub credentials out of post-implementation syntax repair.** Repair attempts hide checkout authentication once and leave restoration to the separate pinned-helper step, so editor-modified shell code runs without the GitHub token.
