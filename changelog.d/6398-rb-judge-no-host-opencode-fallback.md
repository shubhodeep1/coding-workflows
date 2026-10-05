<!-- changelog: security -->
- **Review-blocked judge OpenCode fallback now stays isolated.** Verdict and fix passes run in fresh credential-free sandboxes when Claude is disabled or unavailable; sandbox failures defer the judge instead of executing PR-controlled prompts on the host.
