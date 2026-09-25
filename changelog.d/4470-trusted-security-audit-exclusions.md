<!-- changelog: fixed -->
- **Security audits no longer accept exclusion rules from the branch being audited.** The workflow resolves the exclusion catalog against its verified support checkout so branch-authored catch-all rules cannot suppress findings.

The weekly, manual and consumer security-audit workflow now passes an absolute path to unchanged tracked support content, and fails closed for missing, malformed or out-of-checkout catalogs. Overrides that relied on an audited-repository copy must be moved into the trusted workflow support checkout; other callers of `scripts/security_audit.sh` retain their existing resolution behavior.
