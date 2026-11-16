<!-- agents: section="Workflow architecture" -->
Informational comments posted with the job token (run-volume plan item 6, `docs/plans/reduce-actions-run-volume-plan.md`). A comment posted with `GH_PAT` starts the four `issue_comment` wrappers (`internal-implement.yml`, `internal-clarify.yml`, `internal-plan.yml`, `internal-orchestrate-clarify-respond.yml`, or the consumer `ai-*` ones); one posted with the job's `GITHUB_TOKEN` starts none.

Three kinds that nothing reads back by author now try the job token first, for that one call, and fall back to `GH_PAT` when it is unset or refused:

- the AI Materiality Advisory: a new post uses `MATERIALITY_COMMENT_TOKEN`; the in-place refresh of an existing advisory stays on `GH_PAT`;
- every review-blocked judge issue comment, through `rb_judge_post_issue_comment` in `scripts/review_rb_judge.sh` with `RB_JUDGE_COMMENT_TOKEN`. The judge's retry count comes from commit history (`review_autofix_step_count_iterations.sh`), and its context keeps every comment with its author;
- the security-audit tracker report in `scripts/security_audit.sh`, with `SECURITY_AUDIT_COMMENT_TOKEN`, set by `security-audit.yml` only. The run markers live in the tracker body, and `security_pass_skip.py` checks the tracker issue's author.

Anything trusted by author must stay on `GH_PAT`, because a `github-actions[bot]` copy would be ignored:

- single-issue security-pass comments (`SECURITY_PASS_AUTHOR_LOGIN` markers);
- the attempt journal;
- the autofix editor summary;
- orchestrator state;
- merge-base sync;
- phase markers.

Every token is set per call (`GH_TOKEN=<job token> gh …`), so `gh api user` identity lookups stay on the PAT, and the model sandboxes start from `env -i`.
