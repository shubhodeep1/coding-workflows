<!-- changelog: fixed -->
- **Stall-recovery re-issues keep their preserved PR baseline, and the implement editor can no longer poison the staged-support ledger through its own test run.**

Two implement-side defects kept project #4139's security-pass fix issue from ever producing a PR. First, when orchestrator stall recovery re-issued the review-blocked fix issue #4227 as #4242, it appended its `Re-issued from #4227` trailer after the `**Review-blocked reissue metadata**` footer, and the `Resolve trusted prior PR baseline branch` step in `.github/workflows/implement.yml` treated the trailer as part of the footer, logged `Baseline override ignored: review-blocked reissue metadata footer is incomplete`, and implemented against the planning ref instead of the preserved head of PR #4174. The parser now reads the footer up to the first `---` rule after its header, so any appended re-issue trailer is ignored while trailing prose without a rule still fails closed. Second, every codex editor launch now runs through `env -u STAGED_SUPPORT_LEDGER -u STAGED_SUPPORT_BASE_DIR -u STAGED_SUPPORT_EDITOR_HEAD_LEDGER -u IMPLEMENT_STAGED_SUPPORT_RUN_DIR`, so a `pytest` the editor starts cannot append fixture paths to the live run's editor-head ledger and fail the post-editor reinstall with `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=scripts/helper.sh`. PR #4243 already strips those variables in `tests/conftest.py`, but a review-blocked baseline branch or an unsynced integration branch checks out an older `conftest.py`, so the workflow now holds on its own.

| The numbers that matter | Value |
| --- | --- |
| Implement runs that lost the preserved baseline | 2 (35656715219, 35668070395) |
| Implement runs that failed the post-editor reinstall | 5 (35614385686, 35628923735, 35642366131, 35656715219, 35668070395) |
| Editor launch sites scrubbed | 2 (implementation attempt loop, post-Codex syntax repair loop) |

What this means for operators: the next implement run of a stall-recovered review-blocked re-issue checks out the closed PR's preserved head again, and a self-repo editor that validates its change with the staged-support tests completes instead of failing after the edit, whatever `conftest.py` the checked-out branch carries. No new variables, labels or workflow inputs; consumer repos never set the ledger paths and are unaffected.

### For contributors

`tests/test_review_rb_judge_reissue_baseline.py` covers the poller's two stall-recovery trailer wordings, stacked trailers, metadata-looking lines inside a trailer, and prose without a rule. `tests/test_implement_post_codex_recovery.py` pins both `env -u` launch lines and proves the `CODEX_THREAD_REUSE_*` prefix assignments still reach the helper.
