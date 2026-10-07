<!-- changelog: security -->
- Integration-conflict judge resolutions now verify line provenance for every conflicted file.

The scheduled poller rejects invented lines, deleted files, mode changes, and unverified line reordering or duplication in all conflicted paths, including application source files. A resolution composed of lines from the merge sides can still be pushed; symlink and gitlink targets may be chosen intact from either side. Consumer repositories receive the stricter check with the next stable release.

What this means for operators: source conflicts requiring genuinely new glue lines are rejected without a push and follow the existing bounded retry and escalation path.
