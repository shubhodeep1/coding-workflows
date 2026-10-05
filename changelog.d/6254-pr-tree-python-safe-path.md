<!-- changelog: fixed -->
- **Review preflight Python probes no longer import modules from a PR checkout.** The review job checks for safe-path support before running host-side PR-tree helpers.

Dispatched reviews now run Semble and Serena bootstrap probes from neutral directories, rather than the PR checkout. Shared pre-review Python calls and the partial-finalize timeout extractor use `PYTHONSAFEPATH=1` to exclude checkout modules from interpreter startup. If a neutral directory is unavailable, the affected bootstrap remains fail-soft and reports its tool unavailable. The isolation checks in `tests/test_review_pr_tree_python_isolation.py` run in `.github/workflows/ci.yml`; checkout credentials are unchanged.

What this means for operators: pre-review probes and partial-finalize timeout extraction avoid loading modules from the PR tree, while existing installation fallback behavior remains in place.
