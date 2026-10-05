<!-- changelog: fixed -->
- **Review preflight Python probes no longer import modules from a PR checkout.** The review job checks for safe-path support before running host-side PR-tree helpers.

Dispatched reviews now run Semble and Serena bootstrap probes from neutral directories, rather than the PR checkout. Shared pre-review Python calls, conflict prompt rendering, the optional break-glass scan, and the partial-finalize timeout extractor use `PYTHONSAFEPATH=1` to exclude checkout modules from interpreter startup. Serena's handshake probe uses safe-path, but its server does not inherit it so installed server scripts can import sibling modules. If a neutral directory is unavailable, the affected bootstrap remains fail-soft and reports its tool unavailable. The isolation checks in `tests/test_review_pr_tree_python_isolation.py` run in `.github/workflows/ci.yml`; checkout credentials are unchanged.

What this means for operators: host-side Python probes, prompt rendering, and break-glass detection avoid loading modules from the PR tree, while existing installation fallback behavior remains in place.
