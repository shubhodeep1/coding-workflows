<!-- changelog: fixed -->
- **The merged-PR guard now checks Git writes inside a nested shell that reads its script from a heredoc.** After #6777, `bash -c "bash <<'EOF' ... git push ... EOF"` still passed unchecked because inner scripts lost their heredoc bodies before the Git scan; the reviewers of #6777 found it and the sandboxed editor could not fix a host-only file.

Both copies of `pr_merge_status_guard.py` keep heredoc bodies at every wrapper depth and let the segment parser drop only data heredocs, the same rule the top level already used, so a nested shell fed by a heredoc gets the normal merged-PR check while `git commit -F - <<'EOF'` messages inside a wrapper still read as one commit.

What this means for operators: wrapping a push in a heredoc-fed inner shell no longer bypasses the guard in interactive sessions; consumer repositories pick the change up with their next template sync.
