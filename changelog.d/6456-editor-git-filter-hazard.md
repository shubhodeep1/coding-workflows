<!-- changelog: security -->
- **Implementation refuses editor-written Git command drivers before restoring credentials and before staging.** Checkout-local and global filter, diff, merge, signing, LFS and related command settings, as well as untracked driver attributes, now block the editor credential restore; the commit step rechecks before staging and disables fsmonitor for `git add`.
