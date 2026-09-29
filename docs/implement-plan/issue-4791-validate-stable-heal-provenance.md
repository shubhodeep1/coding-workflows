# Implement-Plan Log — validate.yml authorizes a stable target only for a verified workflow-heal issue bound to the PR

- Plan: docs/completed/issue-4791-validate-stable-heal-provenance-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4791
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4734-validate-stacked-stable-targets
- Project branch: claude/implement-plan-issue-4791-validate-stable-heal-provenance   Final PR: #4793 draft (into claude/implement-plan-issue-4734-validate-stacked-stable-targets)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4734-validate-stacked-stable-targets)
- Waiting on: the completion PR from claude/implement-plan-issue-4791-validate-stable-heal-provenance-complete
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01XWRiu3ZyGe3ooZRkGRuffy   safety net and hand-back: see the completion stage report
- Last updated: 2026-09-29
- Last note: completion stage: plan moved to docs/completed/; conformance run 1 CONFORMANT; security skipped (plan header); validation skipped under Q2: A (a deadlock with project #4734, which covers this code in its own validation). Next: final-merge 1/1 marks #4793 ready and closes #4791 with `ai:merged` once it merges.

## Phases
1. [x] Phase 1 — verify heal provenance and PR binding for stable targets in validate.yml   — PR #4803 merged 2026-09-29 (1407d6c, merged by the operator under Q1: A); review rounds: 4; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT (Implemented COMPLETE, Correctness PASS, no findings) — no fixes (pre-security). Checks: tests/test_validate_target_ref_input.py + checkout audit + size limit 17 passed; the 35 suites that read validate.yml apart from the poller suite 633 passed (69 need `jsonschema` and `jinja2`, which the container lacked; they fail identically on the pre-merge base f6426be); tests/test_orchestrate_poll_process.py could not run (over 13 minutes, does not exercise this step); actionlint 1.7.12 clean; validate.yml 72,816 bytes.

## Security pass
- Skipped (ai:security: automation-produced issue, per the plan header).

## Validation
- Skipped (covered by #4734's project validation) — Q2: A, 2026-09-29 (issue comment 5884718839, standing decision Q17: A in docs/operations/master-session.md). `validate.yml@main` lists only target PRs into `main`, and final PR #4793 goes into the #4734 project branch, which is what #4734 adds support for; #4734 waits on this issue, so neither could proceed. #4734 re-runs its security audit and runtime validation on a branch that contains this fix before anything reaches `main`.

## Completion
- Completion PR from claude/implement-plan-issue-4791-validate-stable-heal-provenance-complete (open) — doc moved to docs/completed/issue-4791-validate-stable-heal-provenance-plan.md
- Final PR #4793 draft

## Activation
- n/a: the base is the #4734 project branch, so this change goes live with project #4734.

## Auto-decisions
- AD-1 [plan, 2026-09-28] Which provenance rules decide a genuine heal issue? — Picked: A — the `security_pass_skip.py` rules (automation author, label applied at creation by the author, fp marker) plus the issue naming `stable` as its branch. Alternatives: B — author and marker only, no events read; C — a new provenance record written by the heal intake. Why: reuses the rules already reviewed for #4623 at one extra read; C needs a new producer and still would not bind the PR. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How is the target PR and its head bound to the issue? — Picked: A — the PR author login must equal the issue author login; the head stays pinned to the PR listing's 40-hex SHA. Alternatives: B — also require the head commit's author to be the issue author (one more read; commit author metadata is set by whoever commits); C — require a workflow-posted record naming the head SHA (no workflow writes one today). Why: A blocks the collaborator-opened PR in the finding at no extra read; B adds no real assurance and C needs a new producer. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Which changelog fragment records the change? — Picked: A — amend `changelog.d/4734-validate-stacked-and-stable-targets.md` in place. Alternatives: B — add `changelog.d/4791-…` with a `security` entry. Why: the label-only rule never shipped (it lives only on the unmerged #4734 project branch), so the fragment that ships should describe the final rule. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Update the #4734 plan doc's Goal 3 and Risks? — Picked: A — leave the #4734 plan and log to that project's chain. Alternatives: B — edit them in this PR. Why: they are that project's records and its chain records its security follow-ups itself; §5. Applied in: no code change. Status: pending review
- AD-5 [phase 1/1 — review round 1, 2026-09-28] The reviewer panel (1 of 6) flagged that the events check fails a genuine heal issue with 100 or more events. How? — Picked: A — read every events page (`--paginate --slurp`) and verify the whole label history: one read per 100 events, three reads for a typical heal issue. Alternatives: B — reject it as the plan's deliberate fail-closed rule (goal 1.5; rejection needs the dedicated verdict bot, which this web session cannot post as, so the PR would block); C — read a fixed number of pages. Why: A is at least as strict (every `ai:workflow-heal` labeled event is still checked) and removes the availability gap; the issue #4665 project ended with 9 events, so the extra reads are rare. Applied in: PR #4803 (review round 1 commit). Status: pending review
- AD-6 [phase 1/1 — review round 1, 2026-09-28] The reviewer panel (1 of 6) flagged that the PR author login pattern accepts logins GitHub does not allow (double or trailing hyphens). How? — Picked: A — follow GitHub's rules: alphanumeric segments joined by single hyphens, at most 39 characters, optional `[bot]` suffix. Alternatives: B — reject it: the login is API data compared by exact equality to the issue author, and the pattern only keeps `read -r` word-splitting safe (rejection needs the verdict bot, so the PR would block). Why: A only narrows what is accepted (fail closed, §1) and both heal authors (`shubhodeep1`, `github-actions[bot]`) still pass. Applied in: PR #4803 (review round 1 commit). Status: pending review
- AD-7 [phase 1/1 — review round 2, 2026-09-29] The reviewer panel (1 of 6, confidence 2) flagged that the author login pattern admits a 39-character name plus `[bot]` (44 characters). How? — Picked: A — cap the whole login at 39 characters, `[bot]` included (a GitHub App slug is at most 34 characters). Alternatives: B — reject it as defence-in-depth only, since the login must equal the issue author exactly (rejection needs the verdict bot, so the PR would block). Why: A only narrows what is accepted (fail closed, §1), finishes AD-6's "follow GitHub's rules", and both heal authors (`shubhodeep1`, `github-actions[bot]`) still pass. Applied in: PR #4803 (review round 2 commit). Status: pending review
- AD-8 [phase 1/1 — review round 3, 2026-09-29] The reviewer panel (1 of 6) repeated round 2's `--slurp` shape finding and flagged the PR description as still stating the single 100-item events page. Neither holds as stated (a live read returns an array of page arrays; the description was fixed in round 2), but the quoted one-page wording survives in this plan doc. How? — Picked: A — reject both in the round reply, amend the plan doc's Goal 1.5, Goal 5, Approach 1 and 3, Constraints §15, and Tests to match AD-5 to AD-7, document the `[[event, ...], ...]` shape at the jq check, and pin a bare event list as refused in the test. Alternatives: B — reject both and post a verdict (needs the dedicated verdict bot, which this web session cannot post as, so the PR would block); C — reject both and only add the validate.yml comment. Why: the plan doc is the spec the conformance audit and the reviewer panel judge against, and it contradicted the shipped rule; the comment and test address the root cause of a twice-repeated misread. No behaviour change. Applied in: PR #4803 (review round 3 commit). Status: pending review

## Lessons
- [source:plan-deviation] A fail-closed "one 100-item page" read borrowed from a budget-bound check (security_pass_skip.py) is an availability gap in a workflow gate; paginate with `--paginate --slurp` and verify every page when the call runs rarely. (files: .github/workflows/validate.yml)
- [source:intervention] When an auto-decision changes the approach mid-project, amend the plan doc's goals, approach, and tests in the same PR; reviewers and the conformance audit read the plan as the spec and keep flagging the superseded wording. (files: docs/plans/issue-4791-validate-stable-heal-provenance-plan.md)
- [source:validation] A security follow-up of a project that changes `validate.yml` target authorization is built on that project's branch, but validation runs `validate.yml@main`, so the follow-up cannot be validated until the parent merges while the parent waits on the follow-up; decide the validation route when the follow-up is planned (the parent's validation covers it). (files: .github/workflows/validate.yml, .github/workflows/internal-validate.yml)

## Notes
- 2026-09-29: phase 1 review round 4 repeated round 3's findings and no verdict bot is configured; blocked as Q1 on the issue. Q1: A (operator, comment 5883581758): PR #4803 merged into the project branch as 1407d6c under standing merge approval Q46.
- 2026-09-29: validation blocked as Q2 (comment 5884489061); Q2: A (comment 5884718839).
- Issue mode; base branch `claude/implement-plan-issue-4734-validate-stacked-stable-targets` (project #4734, final PR #4746 into main, open); security pass: skip (`security_pass_skip.py`: `ai:security` created and labelled by the issue automation).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4791#issuecomment-5868284059
- Protected paths: none (phase 1 touches no `.claude/**` path).
