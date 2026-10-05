<!-- changelog: security -->
- **The unblock judge no longer abandons an unlabeled project that has resumed.** Before treating a tracking issue as failed, it verifies the pipeline's latest project-state comment and checks again before recording or applying its verdict. Stale or unverifiable project state leaves the project untouched.
