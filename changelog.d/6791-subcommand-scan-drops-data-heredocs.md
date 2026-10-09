<!-- changelog: fixed -->
- **The merged-PR guard no longer treats a data heredoc inside a shell wrapper as a Git write.** After #6791, `bash -c "python3 <<'EOF' ... git push ... EOF"` made the guard's subcommand scan report a push that would never run, so a Bash call combining such a heredoc with an allowlisted API write was wrongly blocked as "API write plus git push in one call".

Both copies of `pr_merge_status_guard.py` now drop data heredoc bodies before the subcommand scan, the same step the invocation walker already took, so the two scans agree at every wrapper depth: shell-fed heredocs are still checked, data heredocs are ignored.
