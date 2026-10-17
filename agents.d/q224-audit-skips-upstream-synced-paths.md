<!-- agents: section="Stable log prefixes (contractual)" -->
- `SECURITY_AUDIT_UPSTREAM_MANAGED` (`scripts/security_audit.sh`: `outcome=enabled patterns=<n>`, `outcome=skipped reason=disabled|repository_unknown|upstream_repository|upstream_tree_missing|upstream_tree_untrusted|manifest_failed|empty_list`, and per finding `outcome=suppressed finding= file= line= severity= confidence=`; a consumer finding on a path the `@stable` sync overwrites is kept out of the follow-up and fix-cycle queue)
- `SECURITY_AUDIT_FINDING_DROPPED` (`scripts/security_audit.sh` filter: `reason=file_missing|invalid|duplicate_finding_id finding= file= line=`; a model finding discarded before it can be filed, with `file_missing` meaning the cited file is not in the audited checkout)
LOG_PREFIX.name=SECURITY_AUDIT_UPSTREAM_MANAGED
LOG_PREFIX.name=SECURITY_AUDIT_FINDING_DROPPED
