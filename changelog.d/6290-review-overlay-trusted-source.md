<!-- changelog: security -->
- **Review and validation now load workflow prompt overrides from the repository's default branch, not the PR checkout.** Merge-decision judge prompts cannot be replaced by an overlay.

`review_autofix.yml` and `validate.yml` pin one default-branch commit and copy its `.github/ai/WORKFLOW.md` and fragments into private runtime storage. PR changes to those files take effect only after merge; an unavailable trusted source disables the overlay and retains stock prompts. Judge-mode `replace_path` overrides are ignored with a warning, while trusted `append_path` overrides remain available.

What this means for operators: a PR cannot supply its own judge instructions through the workflow overlay.
