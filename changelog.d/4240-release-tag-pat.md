<!-- changelog: fixed -->
- **Stable release Git pushes now prefer the repository PAT.** Release checkouts use `GH_PAT` for changelog and tag publication when configured, while retaining `github.token` as a fallback.
