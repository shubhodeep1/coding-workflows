# Implement-Plan Log — §21 merged-PR guard: read git push options in order, negations included

- Plan: docs/plans/issue-6088-negated-delete-skips-branch-push-plan.md
- Source issue: shubhodeep1/coding-workflows#6088
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5144-merge-guard-effective-repo
- Project branch: claude/implement-plan-issue-6088-negated-delete-skips-branch-push   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-02
- Last note: project started by /implement-issue-claude (session_01Enho8a3iFnMYBGi77DrFTF); plan and log committed to the project branch.

## Phases
1. [ ] Phase 1 — read git push options in order, negations included — protected paths: `.claude/hooks/pr_merge_status_guard.py`
   - Option table and resolver mirroring git 2.43 `parse_long_opt` / `parse_short_opt` (twin)
   - `_push_refspec_targets` decides from the final option state; unreadable option word → `unreadable_reason` → block
   - Helpers re-expressed on the resolver; `GuardTarget.unreadable_reason` added
   - Tests in `tests/test_pr_merge_status_guard.py` reading the twin; CLAUDE.md §21.B; `changelog.d/6088-merge-guard-push-option-negation.md`
   - Done: new tests pass against the twin; all other tests pass except `test_template_copies_are_identical` until the `[claude-twin-sync]` copy

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue) — `security_pass_skip.py` verified the issue automation's label and marker.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-02] Which negations are applied in order? — Picked: A — every `git push` option, via git 2.43's full option table with its abbreviation and `--no-` rules. Alternatives: B — only `--no-delete` and its prefixes. Why: `--tags --no-tags origin` is the same silent skip. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-02] What happens to a push option word the guard cannot read? — Picked: A — block with exit 2 and a reason naming the word. Alternatives: B — ask for confirmation like a bulk push; C — skip the word as today. Why: the issue asks to deny writes whose target cannot be determined; git 2.43 rejects every such word; a block gives an unattended session an actionable reason. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-02] Where does that block apply? — Picked: A — wherever the guard runs, default branch and unresolvable directory included, not with `CLAUDE_PR_MERGE_GUARD=off`. Alternatives: B — only off the default branch. Why: the written branch is unknown, as for the AD-10 bulk prompt. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-02] Should `--recurse-submodules <value>` take the next word? — Picked: A — yes (prefix `--recu`). Alternatives: B — leave it a flag. Why: git reads the next word as its value. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-02] Treat `--b` / `--m` as the bulk flags git expands them to? — Picked: A — yes, correcting the old `--m` assertion. Alternatives: B — keep the four-character minimum. Why: B lets a mirror push skip the bulk prompt. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-02] How are `-h`, `--help`, `--help-all`, `--git-completion-helper[-all]` treated? — Picked: A — no target; git prints and writes nothing. Alternatives: B — block as unreadable. Why: avoids a false block. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-10-02] Existing helpers and constants? — Picked: A — keep all, re-express the four helpers on the resolver. Alternatives: B — leave them unused. Why: §6, and consistency. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-10-02] Shell expansions in positional words as undeterminable? — Picked: A — no, out of scope. Alternatives: B — block any push with an expansion before its refspecs. Why: §5; B blocks `git push "$REMOTE" "$BRANCH"`. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Base is the #5144 project branch (draft final PR #5163 into `main`): steps 12–13 do not run; the final-merge stage closes #6088 and labels it `ai:merged`.
- Security pass skipped per the plan header (`ai:security: created and labelled by the issue automation`).
