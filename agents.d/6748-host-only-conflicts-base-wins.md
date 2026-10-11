<!-- agents: section="Workflow architecture" -->
Host-only conflicted paths resolved on the host (#6748): outside
integration-sync PRs, `review_conflict_prepare.sh` resolves host-only
conflicted paths before the sandboxed resolver runs. Paired live/template
copies (#6595) are split out first exactly as the resolver does and keep the
pairing path; an unpaired live copy whose template also conflicts leaves the
whole set on the manual-merge path (`reason=unpaired_live_copy`). When none
of the remaining host-only paths is covered by the linked issues'
`files_touched:` allowlist, each takes the base version
(`CONFLICT_RESOLVER_HOST_ONLY_TAKE_BASE`, dropped PR-side diff posted with
`<!-- ai:host-only-take-base:v1 -->`; with no other conflict the merge is
committed without a model). When any is covered, the resolver re-issues the
PR through `unblock_actions.py`'s `reissue` ops and `unblock_run_ops`
(a non-default PR base is passed as `pr_base_branch` and becomes the
replacement's `Integration branch` when the linked issue declares none)
(create, PR comment with `<!-- ai:host-only-conflict-reissue:v1 -->`, which
counts for deduplication only from an OWNER/MEMBER/COLLABORATOR author, then
close; `CONFLICT_RESOLVER_HOST_ONLY_REISSUE`) before failing with
`sandbox_path_host_only`. Unknown scope (linked issues from
`LINKED_ISSUES_RAW_FILE` unavailable, empty, or a full 50-reference page;
`CONFLICT_RESOLVER_HOST_ONLY_SCOPE outcome=unknown`) or
`HOST_ONLY_CONFLICT_TAKE_BASE_ENABLED=false` (forwarded by the
`review_autofix.yml` prepare step) keeps the manual-merge failure.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `CONFLICT_RESOLVER_HOST_ONLY_SCOPE` (`scripts/review_conflict_prepare.sh`: `pr= outcome=classified|in_scope|unknown|skip reason= host_only= in_scope=`)
- `CONFLICT_RESOLVER_HOST_ONLY_TAKE_BASE` (`scripts/review_conflict_prepare.sh`: `pr= path= base= stages=`, `outcome=skip reason=stage_shape`)
- `CONFLICT_RESOLVER_HOST_ONLY_REISSUE` (`scripts/review_conflict_resolve.sh`: `pr= issue= paths= outcome=reissued|skip|create_failed|close_failed|untrusted_closed|metadata_unsafe reason=`)
LOG_PREFIX.name=CONFLICT_RESOLVER_HOST_ONLY_SCOPE
LOG_PREFIX.name=CONFLICT_RESOLVER_HOST_ONLY_TAKE_BASE
LOG_PREFIX.name=CONFLICT_RESOLVER_HOST_ONLY_REISSUE
