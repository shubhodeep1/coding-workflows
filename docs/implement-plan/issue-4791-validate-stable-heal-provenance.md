# Implement-Plan Log — validate.yml authorizes a stable target only for a verified workflow-heal issue bound to the PR

- Plan: docs/plans/issue-4791-validate-stable-heal-provenance-plan.md
- Source issue: shubhodeep1/coding-workflows#4791
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4734-validate-stacked-stable-targets
- Project branch: claude/implement-plan-issue-4791-validate-stable-heal-provenance   Final PR: #4793 draft (into claude/implement-plan-issue-4734-validate-stacked-stable-targets)
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4803 (review round 3)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01XWRiu3ZyGe3ooZRkGRuffy   safety net and hand-back: see the stage report
- Last updated: 2026-09-29
- Last note: review round 2 on PR #4803 (head eb3490d) — 1 of 3 findings fixed, 1 settled in the PR body, 1 rejected: the stable-target author login's 39-character cap now counts a `[bot]` suffix too (AD-7); the PR description now states the paginated events read; the `all(.[]; type == "array")` finding misreads `--paginate --slurp`, which wraps each events page in an outer array (rejected). tests/test_validate_target_ref_input.py 12 passed (the new login cases fail against the previous workflow), related suites 156 passed, actionlint clean, validate.yml 72,658 bytes.

## Phases
1. [ ] Phase 1 — verify heal provenance and PR binding for stable targets in validate.yml   — PR #4803 open (waiting); review rounds: 2; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Which provenance rules decide a genuine heal issue? — Picked: A — the `security_pass_skip.py` rules (automation author, label applied at creation by the author, fp marker) plus the issue naming `stable` as its branch. Alternatives: B — author and marker only, no events read; C — a new provenance record written by the heal intake. Why: reuses the rules already reviewed for #4623 at one extra read; C needs a new producer and still would not bind the PR. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How is the target PR and its head bound to the issue? — Picked: A — the PR author login must equal the issue author login; the head stays pinned to the PR listing's 40-hex SHA. Alternatives: B — also require the head commit's author to be the issue author (one more read; commit author metadata is set by whoever commits); C — require a workflow-posted record naming the head SHA (no workflow writes one today). Why: A blocks the collaborator-opened PR in the finding at no extra read; B adds no real assurance and C needs a new producer. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Which changelog fragment records the change? — Picked: A — amend `changelog.d/4734-validate-stacked-and-stable-targets.md` in place. Alternatives: B — add `changelog.d/4791-…` with a `security` entry. Why: the label-only rule never shipped (it lives only on the unmerged #4734 project branch), so the fragment that ships should describe the final rule. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Update the #4734 plan doc's Goal 3 and Risks? — Picked: A — leave the #4734 plan and log to that project's chain. Alternatives: B — edit them in this PR. Why: they are that project's records and its chain records its security follow-ups itself; §5. Applied in: no code change. Status: pending review
- AD-5 [phase 1/1 — review round 1, 2026-09-28] The reviewer panel (1 of 6) flagged that the events check fails a genuine heal issue with 100 or more events. How? — Picked: A — read every events page (`--paginate --slurp`) and verify the whole label history: one read per 100 events, three reads for a typical heal issue. Alternatives: B — reject it as the plan's deliberate fail-closed rule (goal 1.5; rejection needs the dedicated verdict bot, which this web session cannot post as, so the PR would block); C — read a fixed number of pages. Why: A is at least as strict (every `ai:workflow-heal` labeled event is still checked) and removes the availability gap; the issue #4665 project ended with 9 events, so the extra reads are rare. Applied in: PR #4803 (review round 1 commit). Status: pending review
- AD-6 [phase 1/1 — review round 1, 2026-09-28] The reviewer panel (1 of 6) flagged that the PR author login pattern accepts logins GitHub does not allow (double or trailing hyphens). How? — Picked: A — follow GitHub's rules: alphanumeric segments joined by single hyphens, at most 39 characters, optional `[bot]` suffix. Alternatives: B — reject it: the login is API data compared by exact equality to the issue author, and the pattern only keeps `read -r` word-splitting safe (rejection needs the verdict bot, so the PR would block). Why: A only narrows what is accepted (fail closed, §1) and both heal authors (`shubhodeep1`, `github-actions[bot]`) still pass. Applied in: PR #4803 (review round 1 commit). Status: pending review
- AD-7 [phase 1/1 — review round 2, 2026-09-29] The reviewer panel (1 of 6, confidence 2) flagged that the author login pattern admits a 39-character name plus `[bot]` (44 characters). How? — Picked: A — cap the whole login at 39 characters, `[bot]` included (a GitHub App slug is at most 34 characters). Alternatives: B — reject it as defence-in-depth only, since the login must equal the issue author exactly (rejection needs the verdict bot, so the PR would block). Why: A only narrows what is accepted (fail closed, §1), finishes AD-6's "follow GitHub's rules", and both heal authors (`shubhodeep1`, `github-actions[bot]`) still pass. Applied in: PR #4803 (review round 2 commit). Status: pending review

## Lessons
- [source:plan-deviation] A fail-closed "one 100-item page" read borrowed from a budget-bound check (security_pass_skip.py) is an availability gap in a workflow gate; paginate with `--paginate --slurp` and verify every page when the call runs rarely. (files: .github/workflows/validate.yml)

## Notes
- Issue mode; base branch `claude/implement-plan-issue-4734-validate-stacked-stable-targets` (project #4734, final PR #4746 into main, open); security pass: skip (`security_pass_skip.py`: `ai:security` created and labelled by the issue automation).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4791#issuecomment-5868284059
- Protected paths: none (phase 1 touches no `.claude/**` path).
