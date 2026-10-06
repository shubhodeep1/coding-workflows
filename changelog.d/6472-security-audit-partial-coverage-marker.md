<!-- changelog: security -->
- Full security audits now leave the last-audited-commit marker unchanged when a tracked text file exceeds the export caps. Findings still post, but the tracker records partial coverage and a warning is sent; binary skips do not hold the marker.
