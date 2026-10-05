<!-- changelog: fixed -->
- **The merged-PR push guard now checks numeric branch names before output redirects.** A command such as `git push origin 123 > /dev/null` checks branch `123` for an already-merged PR instead of checking the current branch. Real file-descriptor redirects such as `2>/dev/null` still behave as before; unknown explicit push destinations require confirmation.
