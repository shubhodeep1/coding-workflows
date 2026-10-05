<!-- changelog: security -->
- **Quoted numeric push refspecs now receive the merged-PR guard check.** Empty quotes before digits no longer cause the hook to mistake a branch such as `2` for a file descriptor when redirecting output. Both the source and consumer-template hooks retain the existing behavior for unquoted file-descriptor redirects.
