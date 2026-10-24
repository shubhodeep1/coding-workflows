<!-- agents: section="Stable log prefixes (contractual)" -->
- `E2E_REVIEW_DEADLINE_EXTENDED` (`scripts/stable_e2e_step_wait_review.sh`, the body of `test-and-mark-stable.yml` "Phase 4: Wait for review & autofix to complete": `run= status= base=<epoch> ceiling=<epoch>`; logged once when Phase 4 keeps waiting past its 90-minute base deadline for a queued or active review run, never past the job-start ceiling, #8004)

LOG_PREFIX.name=E2E_REVIEW_DEADLINE_EXTENDED
