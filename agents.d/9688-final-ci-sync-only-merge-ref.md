<!-- agents: section="Workflow architecture" -->
**Final-CI merge ref: sync-only stale ref accepted (2026-11-18, #9688; plan Phase 10, R7 / D10).** Final-CI jobs wait about an hour for runners, and an integration branch syncs `main` in that time, so `scripts/ci_final_merge_ref_fresh.sh` saw a merge ref one sync behind and stopped every job `outcome=stale` (#9602: 24, then 36 jobs).
- When the refresh attempts have run out (after at least one re-fetch worked), `ci_final_merge_ref_sync_only` runs, unless `CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED` (default `true`, read from the step env; `ci.yml` passes no repository variable) is off. It reads the tested base from `HEAD`'s parents, not from the refresh loop's `first`.
- It accepts only when all of these hold:
  - the base is not the default branch (otherwise `foreign_commit`, no extra call);
  - `compare/<tested_base>...<tip>` is `ahead`, with at most 250 commits, all listed, and fewer than 300 files;
  - every commit in that range is in `compare/<tested_base>...<default branch>`, or is a two-parent merge titled exactly `chore: sync <default branch> into <base>` whose second parent is in that set, or has a subject starting `[ai-merge-resolve]`;
  - no `filename` / `previous_filename` in the range is in the PR's file list (`pulls/<n>/files`, 100 per page, at most 30 pages; more than 3000 files, or a count that differs from `changed_files`, is `truncated`).
- The default branch and `changed_files` come from the step's existing first `pulls/<n>` read, so the fresh path makes no extra call. Any failed condition or unreadable response keeps the `::error::` and exit 1 and adds `detail=`.
- Tests: `tests/test_ci_final_merge_ref_fresh.py` (fake `gh` answers compare and file-list reads; accept, each rejection, switch off, base equal to the default branch, pagination, token-less reads).

<!-- agents: section="Stable log prefixes (contractual)" -->
- `CI_FINAL_MERGE_REF` addendum (#9688, `scripts/ci_final_merge_ref_fresh.sh`): new `outcome=stale_accepted reason=sync_only_no_overlap` with `base=`, `tested_base=`, `tip=`, `commits=<k>`, `files=<m>`; `outcome=stale reason=not_refreshed` gains a trailing `detail=not_ahead|truncated|foreign_commit|file_overlap|api_error` when the sync-only check ran. Existing tokens and their order are unchanged.
