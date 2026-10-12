<!-- agents: section="Integration-sync verifier + bootstrap contract" -->
**Unblock judge: CI-failure reissue and its owning PR (#7032).** Before a
standalone, non-security `reissue`, `scripts/unblock_judge.sh`
(`unblock_resolve_ci_owner`) looks for the PR that owns a CI-failure issue:
label `ai:check-triage` / `ai:check-triage-escalated`, or the line-anchored
`<!-- check-failure-triage:pr=<n> -->` marker, plus the body's
`**Pull request:**` line into this repository. It trusts that body text only
when the issue author is the pipeline login, `github-actions[bot]` or an
OWNER/MEMBER/COLLABORATOR (`op=ci_owner outcome=none reason=untrusted_author`
otherwise), then reads the same-repository PR once. A PR merged into the
default branch or the issue's declared integration branch counts as superseding
the issue only when `_pr_checks_completed` returns `ok` (not `allow_all`) with
at least one check-run on its head and the named CI check
(`AUTO_MERGE_REQUIRED_CI_CHECK`, state `PR_CHECKS_LAST_CI_STATE`) ran and
succeeded on it in an `AUTO_MERGE_WAIT_WORKFLOWS` run on that head
(`PR_CHECKS_CI_RUN_IDS`, as the auto-merge wait binds it; an unreadable runs
listing never supersedes); with no named check configured (`disabled`, the
consumer default) the issue is never superseded. `unblock_judge.yml` forwards
both variables. The issue is then closed
`not_planned` without a replacement
(`UNBLOCK_JUDGE item=<n> op=reissue outcome=superseded pr=<n> head=<sha>`).
Otherwise the replacement carries the marker, and, when the PR is open and its
head ref exists, a standalone `- Integration branch:` line with the PR head
ref (never the base ref or model text). Unverifiable PR facts keep the
unchanged reissue.
