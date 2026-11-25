<!-- changelog: fixed -->
- **The integration fingerprint verifier now accepts a file that the base branch deleted after capture.** Before this change, when `main` deleted a file that an orchestrator integration branch still edited, the conflict resolver could not take the deletion: a `must_contain` fingerprint on that file always failed with "file does not exist". `scripts/verify_integration_fingerprints.py` now treats that fingerprint as satisfied when all of these hold:
  - the base history (the verified `--ref`; else `MERGE_HEAD` during a merge; else `origin/$BASE_BRANCH`) no longer has the file;
  - the commit that deleted it is dated after the fingerprint's `captured_at`;
  - that commit is not an `[ai-merge-resolve]` commit.

  When this happens it logs `FINGERPRINT_BASE_DELETION_SUPERSEDED_V1`. Baseline capture and the compare modes now pass `captured_at` through. Anything else still fails as before (Refs #10120).

The check is on by default. Set `INTEGRATION_FINGERPRINT_BASE_DELETION_SUPERSEDE_ENABLED=false` to turn it off. Any value other than empty, `true` or `false` also turns it off and logs one warning.
