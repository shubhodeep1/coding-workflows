<!-- changelog: fixed -->

- Review/autofix no longer treats evidence-free degraded fingerprints as deterministic failures, and a changed shared helper engine now gets a fresh attempt on an unchanged PR head.
- The largest review/autofix step bodies now load from staged support scripts, keeping the workflow below GitHub's file-size limit without changing step behavior.
