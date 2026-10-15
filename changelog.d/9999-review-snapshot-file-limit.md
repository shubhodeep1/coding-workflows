<!-- changelog: fixed -->
- **Reviews no longer fail on repositories with large tracked source files.** The review sandbox now reads files up to 8 MiB (previously 2 MiB), and it leaves a larger file out of the editor workspace instead of rejecting the whole review.

The review editor snapshot (`scripts/review_untrusted_workspace.py snapshot`, called by `scripts/review_untrusted_sandbox.sh`) rejected the whole review when any tracked file was over 2 MiB. On `shubhodeep1/tele-funtoken-msg-scoring` that meant every review failed, with `Review isolation snapshot or transfer rejected (ValueError) reason=unknown`, because of `ft.games/app.py` (2.1 MB) and `ft.games/templates/Game-pages/mega-fishing/mega-fishing.html` (3.3 MB). The per-file limit is now 8 MiB. In the review snapshot, a file above that is skipped and logged as `REVIEW_ISOLATION_SNAPSHOT oversized_skipped=<n>`, and the editor cannot overwrite it. The heal editor and the pre-push test gate get the higher limit but still stop on a larger file, so a missing file never shows up there as a test failure. A snapshot rejection now reports `reason=unsafe_file` or `reason=file_changed` instead of `reason=unknown`.

| The numbers that matter | Value |
| --- | --- |
| Per-file limit | 2 MiB → 8 MiB |
| tele-funtoken reviews that failed on 2026-10-15 | 10 |
| tele-funtoken review snapshot after the change | 1,145 files, 33 MB (64 MiB total cap unchanged) |

What this means for consumer repositories: reviews run on repositories that keep multi-megabyte source files. A file over 8 MiB is reviewed from the diff only, and its PR has to be edited by hand.
