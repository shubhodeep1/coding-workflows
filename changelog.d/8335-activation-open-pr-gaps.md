<!-- changelog: fixed -->
- **Activation verification no longer opens fix issues for work an open pull request already delivers, and plans no longer wait on another pull request.**

When a merge left a code gap that an open pull request was already fixing, `scripts/activation_verify.sh` still opened an `ai:activation-fix:v1` issue for it. The pipeline then planned "wait for that pull request", and implement, which can only change files, ended the run as `BLOCKED` (tele-funtoken-msg-scoring#4992, run 40815203511). The verifier now sees the repository's open pull requests and can mark such a gap as in progress in one of them. The verdict comment still lists the gap, but no fix issue is opened for it. The plan prompt now reports a `PLAN_SELF_CHECK: BLOCKER` naming the open pull request, so the issue waits on the blocked path instead of spending an implement run, and the clarify-respond prompt no longer writes steps that post comments or wait on other pull requests.

| The numbers that matter | Value |
| --- | --- |
| Open pull requests passed to the verifier | Same-repository PRs by trusted authors, among the 50 most recently updated |
| Extra GitHub API calls per verification | 1 (`GET pulls?state=open`) |
| Body text kept per pull request | 1500 characters |

What this means for operators: activation verdicts may now show `(fix in progress in open PR #N)` next to a code gap, and no fix issue appears for that gap. If that pull request is closed without merging, the gap is recorded only in the verdict comment.
