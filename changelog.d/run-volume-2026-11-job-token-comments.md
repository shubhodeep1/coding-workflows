<!-- changelog: changed -->
- **Three kinds of purely informational bot comments now post with the job's own token, so they no longer start the four `issue_comment` wrapper workflows.** These are the AI Materiality Advisory, the review-blocked judge's decision and hold notices, and the security-audit tracker report.

Every comment the pipeline posts with `GH_PAT` starts `internal-implement.yml`, `internal-clarify.yml`, `internal-plan.yml` and `internal-orchestrate-clarify-respond.yml` (or the consumer `ai-*` wrappers), and almost all of those runs skip themselves. On 2026-11-14 coding-workflows got about 60 such comments of these three kinds. A comment posted with the job's `GITHUB_TOKEN` starts no workflows, and none of these three is ever read back by author. Single-issue security-pass comments stay on `GH_PAT`, because the next pass trusts its own earlier markers only when the PAT login wrote them.

| The numbers that matter | Value |
| --- | --- |
| Comments moved per day in coding-workflows (2026-11-14 sample) | about 60: 33 tracker reports, 16 judge notices, 11 advisories |
| Skipped wrapper runs avoided per day here | about 240 |
| Comment kinds kept on `GH_PAT` | security-pass results, attempt journal, autofix editor summary, orchestrator state, merge-base sync, phase markers |

What this means for operators and consumer repos: these comments now show `github-actions[bot]` as their author instead of the PAT user. When the job token is refused (missing permission), the post falls back to `GH_PAT` as before. An existing materiality advisory is still refreshed in place with `GH_PAT`, so a PR that already has one keeps a single advisory comment. Consumers get the change on their next `@stable` sync.

### For contributors

The posting sites are:
- the `Post AI Materiality Advisory comment` step in `review_autofix.yml` (`MATERIALITY_COMMENT_TOKEN`);
- `rb_judge_post_issue_comment` in `scripts/review_rb_judge.sh` (`RB_JUDGE_COMMENT_TOKEN`), which carries every judge issue comment;
- the tracker comment in `scripts/security_audit.sh` (`SECURITY_AUDIT_COMMENT_TOKEN`, set by `security-audit.yml`; poller-driven project audits keep `GH_PAT`).

The model sandboxes start from `env -i`, so the job token never reaches a model. Tests are in `tests/test_review_rb_judge_head_binding.py` and `tests/test_security_audit_workflow_contract.py`.
