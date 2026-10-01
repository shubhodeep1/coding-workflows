# Limit the Claude twin sync review skip to genuine sync PRs

Source issue: shubhodeep1/coding-workflows#5610 (https://github.com/shubhodeep1/coding-workflows/issues/5610)
Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
Security pass: skip (ai:security: automation-produced issue)

## Summary

`review_autofix.yml`'s review gate skips every PR whose head branch is named
`claude/claude-twin-sync-*` (`SKIP_REASON=claude_twin_sync`). The name alone decides it, so
a fork PR or a PR in any consumer repository can take the name and bypass the AI review
gate. This plan limits the exemption to sync PRs that `claude-twin-sync.yml` actually opened:
PRs in coding-workflows, from a same-repository head, authored by the account the sync
workflow writes as. Every other PR with that name is reviewed normally.

## Context

- Security audit finding `sync-name-skips-consumer-review` (A01:2021 Broken Access Control,
  medium, confidence 9/10), filed as #5610 by `.github/workflows/security-audit.yml` against
  the #4785 project branch (`Refs #3576`).
- The skip lives in the `Evaluate review gate` step of the `gate` job,
  `.github/workflows/review_autofix.yml:546-554` on the base branch.
- `claude-twin-sync.yml` runs only in `shubhodeep1/coding-workflows` (job `if:`), and
  `scripts/claude_twin_sync.py` pushes the sync branch into the same repository and opens
  the PR with `secrets.GH_PAT`. The review gate authenticates with the same secret
  (`GH_TOKEN: ${{ secrets.GH_PAT }}`), so the sync PR's author is the login `gh api user`
  returns inside the gate.
- `claude_twin_sync.py` already treats a sync PR as "same-repository head with the prefix"
  (`is_sync_pr_head`, `open_sync_prs`). The review gate never checked either.
- Genuine sync PR bodies carry `[skip ai]` (`render_body`), so they keep skipping review
  through the `skip_ai_marker` branch even when the name exemption does not apply.

## Goals

- A `claude/claude-twin-sync-*` PR in any repository other than `shubhodeep1/coding-workflows`
  is reviewed normally (no `claude_twin_sync` skip).
- A `claude/claude-twin-sync-*` PR in coding-workflows whose head repository is not
  coding-workflows (a fork) is reviewed normally.
- A `claude/claude-twin-sync-*` PR in coding-workflows authored by anyone other than the
  GH_PAT account is reviewed normally.
- A genuine sync PR (all three conditions hold) still skips with
  `AUTOFIX_GATE_SKIP reason=claude_twin_sync`.
- Every denied exemption logs one structured line,
  `AUTOFIX_GATE_TWIN_SYNC_NOT_EXEMPT reason=<…> pr=<n> head_ref=<ref>` (§8).

## Non-goals

- The §26.H catch-all sweep's prefix skip (`scripts/claude_pr_sweep.py`) and CI's
  `claude_twin_sync.py check` guard rule. Neither is a review-gate exemption (AD-5).
- Any change to `claude-twin-sync.yml` or `scripts/claude_twin_sync.py`.

## Constraints

- §1: security first; the exemption fails closed when the identity cannot be verified (AD-3).
- §5: minimal change; only the gate step, its test, and the docs that describe the skip.
- §6: `SKIP_REASON=claude_twin_sync` and the `AUTOFIX_GATE_SKIP reason=claude_twin_sync` log
  key keep their names; the new log key and shell variables are new, unique identifiers.
- §9: YAML keeps 2-space indentation.
- §15: the existing PR fetch (`_pr_gate`) is extended with `.user.login` and
  `.head.repo.full_name` instead of a new call. The one `gh api user` lookup the exemption
  needs is cached and reused by `gate_fetch_marker_comments`, which already issues that
  lookup, so a gate evaluation still makes at most one `gh api user` call.
- §20: a `changelog.d/` fragment (security fix).
- §27: `review_autofix.yml` stays well under 480,000 bytes (455,853 on the base branch).

## Approach

Add a small shell function, `gate_twin_sync_exempt`, before the gate's skip chain. It
returns success only when:

1. the head ref starts with `claude/claude-twin-sync-`;
2. `REPOSITORY` is `shubhodeep1/coding-workflows` (AD-1);
3. for a PR (numeric `PR_NUMBER`): the PR's `head.repo.full_name` equals `REPOSITORY`
   (case-insensitive), and the PR's `user.login` equals the GH_PAT login from `gh api user`
   (case-insensitive, AD-2). A missing value on either side denies the exemption (AD-3).
   The no-PR claude-branch push path (no PR number) keeps the exemption in coding-workflows,
   because that push can only come from this repository's own branch and the run is
   reviewer-comment-only (AD-4).

The `elif [[ "${pr_head_ref}" == claude/claude-twin-sync-* ]]` branch becomes
`elif gate_twin_sync_exempt; then`. A denied exemption falls through to the draft,
`[skip ai]`, and normal review branches (AD-6).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — verify twin sync provenance before the review skip.**
   - Files: `.github/workflows/review_autofix.yml`, `tests/test_review_autofix_claude_fixer_mode.py`,
     `agents.md`, `.github/workflows/claude-twin-sync.yml` (header comment only),
     `changelog.d/5610-twin-sync-review-skip-provenance.md` [new].
   - Done: the gate tests below pass, the whole `tests/test_review_autofix_*` suite and
     `tests/test_workflow_file_size_limit.py` pass, `yamllint`/`actionlint`-level YAML parse
     succeeds, and `agents.md` describes the narrowed skip.
   - Rollback: revert the PR; the gate returns to the name-only skip.

## Implementation Steps

1. `review_autofix.yml`, `_pr_gate` jq: add `author_login: (.user.login // "")` and
   `head_repo: (.head.repo.full_name // "")`; parse them into `pr_author_login` and
   `pr_head_repo` next to the other fields (initialised empty with the rest).
2. `review_autofix.yml`: define `gate_pat_login=""`, the once-only lookup
   `gate_resolve_pat_login`, and `gate_twin_sync_exempt` before the skip chain; replace the
   name test in the chain with the function; keep the existing comment, `SKIP_REASON`, and
   log line.
3. `review_autofix.yml`, `gate_fetch_marker_comments`: call `gate_resolve_pat_login` and
   read `gate_pat_login` instead of a second `gh api user` call.
4. Tests in `tests/test_review_autofix_claude_fixer_mode.py`: the existing genuine-sync test
   runs with `REPOSITORY=shubhodeep1/coding-workflows`, a same-repo head, and the GH_PAT
   author; new tests cover a consumer repository, a fork head, another author, an
   unavailable `gh api user` login, and the no-PR push path in and outside coding-workflows.
5. Docs: `agents.md` "Claude twin sync" skip bullet, the `claude-twin-sync.yml` header
   comment, and the changelog fragment.

## Files & Modules

- `.github/workflows/review_autofix.yml`
- `tests/test_review_autofix_claude_fixer_mode.py`
- `agents.md`
- `.github/workflows/claude-twin-sync.yml` (comment only)
- `changelog.d/5610-twin-sync-review-skip-provenance.md` [new]

## Tests

- `pytest tests/test_review_autofix_claude_fixer_mode.py` (gate end to end with the stubbed `gh`).
- `pytest tests/test_review_autofix_*.py tests/test_workflow_file_size_limit.py tests/test_claude_twin_sync.py`.
- YAML parse of `review_autofix.yml` and `bash -n` of the gate step via the test harness.

## Risks

- A genuine sync PR whose author lookup fails is reviewed instead of skipped. Its body
  carries `[skip ai]`, which still skips it; and GH_PAT failing would fail the rest of the
  run anyway.
- A same-repository sync-named PR from another collaborator is now reviewed and can
  auto-merge like any `claude/*` PR. CI's `claude_twin_sync.py check` still only lets a guard
  path become a copy of the twin already on the base branch (AD-6).

## Rollout

Lands on the #4785 project branch, then reaches `main` with that project's final PR and
consumers at the next `@stable`. No new env vars or repo variables.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which repository counts as "this repository" for the exemption? — Picked: A — the hardcoded `shubhodeep1/coding-workflows`, the same literal `claude-twin-sync.yml`'s job `if:` and the gate's support-repo checks use. Alternatives: B — a new repository variable; C — the resolved review-support repository output. Why: the sync workflow itself only runs there, and a variable could be set in a consumer to re-open the hole. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] How is "the sync workflow's bot identity" verified? — Picked: A — the PR author (`user.login`) must equal the login GH_PAT authenticates as (`gh api user`), because `claude_twin_sync.py` opens the PR with `secrets.GH_PAT` and the gate runs with the same secret. Alternatives: B — a new `CLAUDE_TWIN_SYNC_BOT_LOGIN` repository variable; C — also require the head commit's GitHub-attributed author/committer to be that login (one more API call). Why: no new configuration, no new API call beyond one cached `gh api user`, and PR authorship cannot be spoofed. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] What happens when the author, head repository, or GH_PAT login cannot be read? — Picked: A — deny the exemption and review normally (fail closed). Alternatives: B — keep the skip. Why: §1 security first; genuine sync PRs still skip through their `[skip ai]` marker. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Does the no-PR claude-branch push path (no PR number) keep the exemption? — Picked: A — yes in coding-workflows only, where the push can only come from this repository's own branch and the run cannot merge; elsewhere it is reviewed. Alternatives: B — never exempt the no-PR path. Why: keeps today's cost for genuine sync pushes that land before the PR exists, without widening access. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Should the §26.H sweep's prefix skip and CI's `is_sync_pr_head` get the same author check? — Picked: A — no, out of scope. Alternatives: B — align both. Why: §5; neither is a review-gate exemption (the sweep only decides whether to start a fixer, and CI's guard rule still requires guard paths to equal the twin already on the base). Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] How is a sync-named PR that fails the provenance check handled? — Picked: A — reviewed normally, exactly as the issue recommends (Claude-fixer mode for its `claude/*` head). Alternatives: B — keep skipping it; C — fail the gate. Why: the issue asks for normal review; skipping keeps the bypass and failing blocks legitimate manual PRs. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5610` returned
  `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
