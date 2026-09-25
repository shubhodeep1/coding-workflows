<!-- changelog: fixed -->
- **Isolated resolver validation and review-blocked judging now work with host `/tmp` artifacts.** The resolver copies the conflicted-set, conflict-spans, and clean-path manifests to the runner's shared temporary directory before the isolated validator reads them. An unavailable staging directory or failed copy still blocks the merge-resolution commit, and temporary copies are removed after validation.

The review-blocked judge parser also receives model output through stdin instead of opening a host `/tmp` file inside its isolated validator. Valid judge output can now be parsed under `PrivateTmp=yes`; unreadable or invalid output still leaves the decision unparsed for a later poll.
