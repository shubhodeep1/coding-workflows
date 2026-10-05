<!-- changelog: fixed -->
- **The merged-PR guard no longer trusts a redirected `cd` or `exit` that may fail.** It checks the session checkout and asks for confirmation before an uncertain-directory push unless that checkout is already blocked. Literal `/dev/null` redirects retain the existing behaviour. Consumer repos receive the same guard through the template sync.
