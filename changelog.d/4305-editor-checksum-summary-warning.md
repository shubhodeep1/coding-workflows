<!-- changelog: added -->
- **Review editor attempts now log one `EDITOR_REVIEWER_CHECKSUM_SUMMARY` warning that counts the reviewer-file hashes the editor miscopied.**

`scripts/review_apply_fixes.sh` already logs `EDITOR_REVIEWER_CHECKSUM_UNVERIFIED` for each reviewer file whose sha256 the editor summary got wrong, and accepts the attempt on the file path and issue audit. Each attempt with at least one mismatch now also logs `EDITOR_REVIEWER_CHECKSUM_SUMMARY attempt=<n> files_checked=<n> checksum_mismatches=<n> validation_ok=<true|false>`. Replaying the three editor attempts from release gate run 35802596362 gives `checksum_mismatches=2`, `2` and `1` out of 6 reviewer files. Nothing that passed or failed before changes outcome.

What this means for operators: search for `EDITOR_REVIEWER_CHECKSUM_SUMMARY` to see how often the editor miscopies hashes, and treat an attempt where every file mismatches as a sign the editor made up its reviewer list rather than read the files.
