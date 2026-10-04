<!-- changelog: fixed -->
- Security audits now give the isolated agent size-capped chunks of oversized files in the explicit audit scope, instead of silently omitting them. Audits fail before the model runs if those files exceed the cap; full scans report oversized files they did not inspect as a coverage note.
