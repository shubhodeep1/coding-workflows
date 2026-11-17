<!-- agents: section="Workflow architecture" -->
Informational comments posted with the job token (run-volume plan item 6, `docs/plans/reduce-actions-run-volume-plan.md`). A comment posted with `GH_PAT` starts the four `issue_comment` wrappers (`internal-implement.yml`, `internal-clarify.yml`, `internal-plan.yml`, `internal-orchestrate-clarify-respond.yml`, or the consumer `ai-*` ones); one posted with the job's `GITHUB_TOKEN` starts none.

Three kinds that nothing reads back by author now try the job token first, for that one call. They fall back to `GH_PAT` only when the token is unset or GitHub refuses it (401/403, `Resource not accessible by integration`, e.g. a read-only fork token). Any other failure is not resent, because `gh_retry` sends a POST once and a timeout or 5xx may already have created the comment:

- the AI Materiality Advisory: a new post uses `MATERIALITY_COMMENT_TOKEN`; the in-place refresh of an existing advisory stays on `GH_PAT`;
- every issue comment `scripts/review_rb_judge.sh` posts, through `rb_judge_post_issue_comment` with `RB_JUDGE_COMMENT_TOKEN`. The security-block and extension markers in `scripts/review_rb_judge_security_pass.sh` stay on `GH_PAT`, because `rb_security_block_already_reported` matches them against the PAT login. The judge's retry count comes from commit history (`review_autofix_step_count_iterations.sh`), and its context keeps every comment with its author;
- the security-audit tracker report in `scripts/security_audit.sh`, with `SECURITY_AUDIT_COMMENT_TOKEN`, set by `security-audit.yml` only. The run markers live in the tracker body, and `security_pass_skip.py` checks the tracker issue's author.

Anything trusted by author must stay on `GH_PAT`, because a `github-actions[bot]` copy would be ignored:

- single-issue security-pass comments (`SECURITY_PASS_AUTHOR_LOGIN` markers);
- the attempt journal;
- the autofix editor summary;
- orchestrator state;
- merge-base sync;
- phase markers.

Every token is set per call (`GH_TOKEN=<job token> gh …`), so `gh api user` identity lookups stay on the PAT, and the model sandboxes start from `env -i`.
