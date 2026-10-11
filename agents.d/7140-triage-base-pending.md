<!-- agents: section="Workflow architecture" -->
Check-failure triage defers instead of filing when the base branch's newest run
of the failing check is still in progress (`CHECK_TRIAGE skip reason=base_pending
base=<ref> pr=<n> check=<name>`). The base-gate reads return `pending` for that
case: the workflow-run read when the newest push run on the base branch has not
completed, the check-run read when every returned base check run fits on the page
and at least one has not completed. #7140 was filed for PR #7135's own test
failure while main's CI run was in progress; a PR-specific failure belongs to
the PR's review/autofix loop, and a real base breakage is filed by the next
triage after the base run completes.
