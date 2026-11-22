<!-- agents: section="Integration-sync verifier + bootstrap contract" -->
**Base-deletion supersede (Refs #9421, #9930).**

In `scripts/verify_integration_fingerprints.py`, a `must_contain` whose file is missing is satisfied, and logs `FINGERPRINT_BASE_DELETION_SUPERSEDED_V1` on stderr, when `_base_deletion_superseding_commit` confirms the base branch deleted or moved that file after capture. All of these must hold:
- The history commit resolves.
- `git ls-tree` shows the path absent there.
- The newest `git log --no-renames --diff-filter=D` commit for the path in that history is dated after `captured_at`.
- That commit's subject does not start with `[ai-merge-resolve]`.

Merge commits show no diff, so a resolver merge that drops a file the base kept never counts.

The history comes from:
- the verified `--ref` in ref mode. That ref is the integration branch, so a non-resolver deletion made on it (a later sub-issue's squash-merge) also counts, as with the post-capture evolution defense; `[ai-merge-resolve]` commits stay excluded;
- `MERGE_HEAD` in working-tree mode while a merge is in progress;
- otherwise `refs/remotes/origin/$BASE_BRANCH`, where `BASE_BRANCH` must match a safe branch-name pattern.

Unlike the two post-capture line defenses, this one applies in working-tree mode on purpose: taking the default branch's deletion is the correct resolution, and the resolver cannot write the base side's history. The compare/baseline call sites pass the fingerprint's `captured_at` as `deletion_captured_at`. Only this defense reads it, so the evolution and union defenses keep their existing behaviour in those modes.

The defense fails closed: a git error, an unparseable `captured_at`, no history ref, the path still present on the base, or a pre-capture or resolver deletion all keep the violation. `INTEGRATION_FINGERPRINT_BASE_DELETION_SUPERSEDE_ENABLED` is an off switch: unset, empty or `true` (any case) means on, and `false` means off. Any other value also means off and logs one `::warning::` on stderr.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `FINGERPRINT_BASE_DELETION_SUPERSEDED_V1` (`scripts/verify_integration_fingerprints.py`, stderr `::warning::`: `issue=#<n> pr=#<p> file=<path> commit=<sha12> base_ref=<MERGE_HEAD|ref|refs/remotes/origin/<base>> reason=must_contain_file_deleted_on_base_after_capture_by_non_resolver_commit`; a `must_contain` on a file the base branch deleted after capture in a non-`[ai-merge-resolve]` commit is treated as satisfied, #9930)
LOG_PREFIX.name=FINGERPRINT_BASE_DELETION_SUPERSEDED_V1
