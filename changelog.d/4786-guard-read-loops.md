<!-- changelog: changed -->
- **The `gh api` permission guard now approves narrowly scoped read-only loops over literal IDs.** A complete loop can inspect `gh api`, `gh run`, or `gh pr` results without an unattended permission prompt; writes and unvetted commands remain subject to the existing safeguards.
