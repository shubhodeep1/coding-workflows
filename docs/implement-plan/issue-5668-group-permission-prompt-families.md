# Implement-Plan Log — Group permission-prompt reports by command family, not by exact shape

- Plan: docs/plans/issue-5668-group-permission-prompt-families-plan.md
- Source issue: shubhodeep1/coding-workflows#5668 (https://github.com/shubhodeep1/coding-workflows/issues/5668)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5668-group-permission-prompt-families   Final PR: #5685 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5697
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_0133V1fUz7ukod1UGEnYvTEY   safety net trig_01AJJgtdFW7jcN9GGgXXfypW   hand-back trig_01VZZSjFwM2fkidsfnb88DCN
- Last updated: 2026-10-02
- Last note: review round 6 on PR #5697 head 54d03f4 (owner's supervising session, 2026-10-02): a legacy issue whose body holds more than one line that could open the `**Latest example**` block (a heading planted in a multi-line reason or inside the recorded example) gets no family, never the planted command's; live file and twin edited together. Earlier: review round 5 on PR #5697 head 039630b (owner's supervising session, 2026-10-02): `gh --hostname <host>` is a global value option, so GHE commands keep their subcommand family; a legacy issue's example is read from the last `**Latest example**` heading, so a heading planted in the untrusted reason text is not read; each reason is rendered on one line. Live file and twin edited together. Earlier: review round 4 on PR #5697 head cfd7bae (owner's supervising session, 2026-10-02): `issue_family_marker` now accepts only the generated marker, on the line after the signature marker at the end of the body, so a marker in the untrusted reason text cannot claim an issue; `_substitution_end` reads the character before a `#` as Bash does (a backslash-newline is removed, an escaped character continues the word); a test comment's round attribution corrected; rejected: the index-0 read (the scan starts at the `(`). Earlier: review round 3 on PR #5697 head f454e88 (owner's supervising session, 2026-10-02): `_substitution_end` treated a `)` inside a `#` comment within `$(…)` as the close, so the family parser picked the wrong command; a `#` that starts a word now skips to the end of its line, as in Bash. Live file and twin edited together. Earlier: review round 2 on PR #5697 head 76ec5c8 (owner's supervising session, 2026-10-02): a `$(…)` or `$((…))` in a leading assignment, `export`, or `cd` prefix was split into simple commands of its own, so `ROOT=$(pwd) && git status` got an empty family and `cd $(pwd) && git status` the `cd` family; substitutions now count as one word (AD-11). Live file and twin edited together, no twin sync pending. Earlier: review round 1 (8962a63) and its twin sync 76ec5c8; twin sync landed as `c6494fd` (hold lifted); project branch synced with `main`; wait armed on PR #5697 review

## Phases
1. [ ] Phase 1 — family key and family-aware filing (twin script, tests, CLAUDE.md §23.I, agents.md, README.md, changelog)   — protected paths: `.claude/scripts/permission_prompts.py` — PR #5697 open (waiting on review; review round 1 twin-synced in `76ec5c8`, review rounds 2–6 edit live and twin together); review rounds: 6; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] What is the family's command word? — Picked: A — the first command word plus its subcommand (tools whose shape keeps one) or its script basename, as the shape keeps them. Alternatives: B — the first command word alone. Why: allow rules are keyed on command and subcommand or script, and #5668's table keeps `git status` and `git fetch` apart. It gives 22 families on the 60 issues, with no harmful merges. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the family id go? — Picked: A — a separate `<!-- ai:permission-prompt-family:v1 family=<id> -->` line, with the v1 `sig=` marker unchanged. Alternatives: B — a `family=` field inside the v1 marker. Why: exact `sig=… -->` readers (old checkouts, #4867's `duplicate-check`) keep working (§6), following #4867's class-marker precedent. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How are legacy issues (sig marker only) matched by family? — Picked: A — derive the family from their recorded tool, event, and example command (Bash only), in addition to the signature. Alternatives: B — signature only. Why: new variants land on #4678 from the first run, with no bootstrap duplicate per family and no manual marker edits (§18). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Which issues can be a family's issue? — Picked: A — open first, then closed as completed, not_planned, or with no reason; never one closed as duplicate. Alternatives: B — any family issue. Why: #5668 names completed / not_planned, and a comment on a duplicate hides a new cause. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Which match wins, the exact signature or the family? — Picked: A — the exact signature (as today), then the family, then a new issue. Alternatives: B — an open family issue before a closed signature issue. Why: §5; the family only replaces the "new issue" branch. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] What is the family for non-Bash tools? — Picked: A — event, tool, and shape (signature granularity). Alternatives: B — event and tool only. Why: `.claude/**` edit prompts are closed as not planned by design and must not absorb other edits. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Which prefixes are stripped? — Picked: A — leading `NAME=value` words, assignment-only and `export`-of-variables segments, `cd …` followed by `&&` or `;`, and `timeout [options] <duration>`. Alternatives: B — only the literal list in #5668. Why: #4905 and #5470 show the same harmless variations. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-30] Which script file do the tests load? — Picked: A — the `workflow-templates/.claude/` twin, with parity pinning the root copy. Alternatives: B — the root copy, with new tests red until the sync. Why: the twin-first rule of `/implement-plan-claude` step 4. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-30] What counts as a substitution? — Picked: A — `$(` outside single quotes, excluding `$((`. Alternatives: B — also backticks. Why: #5668 names `$(…)`, and arithmetic is not command substitution. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-30] For a heredoc behind another command, which command names the family? — Picked: A — the first command word, as #5668 says. Alternatives: B — the heredoc's receiving command. Why: B saves one family out of 22 and misplaces #5202. A is literal. Applied in: phase 1 PR. Status: pending review
- AD-11 [phase 1 review round 2, 2026-10-02] How should a `$(…)` in a prefix segment be read for the family? — Picked: A — collapse every `$(…)` and `$((…))` outside single quotes (nested, with quoted parentheses) into one word before splitting into simple commands, leaving the text unchanged when one is not closed. Alternatives: B — teach `_is_family_prefix` to accept the `(`/`)` operators. Why: B would still treat the substitution's own commands as top-level segments (`X=$(cd a) && git status`); A keeps the `subst` construct (read from the original text) and matches the documented prefix rule (AD-7). Applied in: PR #5697. Status: pending review (owner standing instruction: recommended option)

## Lessons

## Notes
- Started by the Claude issue dispatcher routine (trigger trig_018T61YPs5MaxqQHrsfsxnaz) in session session_016vGbjSkXz28Wenb3VreJQs (Auto mode).
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "label": null, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- In-flight projects editing the same script: #4750, #4858, #4867, #5124. Whichever lands second resolves the textual conflict at its project-branch sync.
- Phase 1 verification (2026-09-30): `tests/test_permission_prompts.py` 93 passed in the real tree, and `test_template_parity` fails until the twin sync, as expected. In a twin overlay 94 passed. `ruff check --select E,F --ignore E501` is clean. The changelog and CLAUDE.md section tests passed (50).
- Data check: with the real code the 60 existing `ai:permission-prompt` issues map to 22 families, and legacy derivation agrees with the raw examples on all 60. The first prototype (heredoc receiver, AD-10 B) gave 21.
- Twin sync: `workflow-templates/.claude/scripts/permission_prompts.py` → `.claude/scripts/permission_prompts.py`, twin sha256 `9a7a24efe37d6e39cd4e656599db4bc7af0b5c884e9b1d5dbcfae4c515403750`.
- The broader doc-reading test sweep (with `-x`) stopped at `tests/test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean`. That test needs `gawk`, which is not installed in this container, and it does not involve the script or tests this phase changes.
- Resumed 2026-09-30 by `/reclarify` (Q1: A) in session session_012ZrseTpDtRZsMwcDto9jhv: the owner's `[claude-twin-sync]` commit `c6494fd` matches the twin sha256 `9a7a24ef…`. Re-verified here: `tests/test_permission_prompts.py`, `test_update_workflows_guardrails.py`, `test_changelog_fragment_contract.py`, `test_claude_md_section_numbers.py` 130 passed; `ruff check --select E,F --ignore E501` clean. `check_in_status.py --hand-back` shows no claim on `c6494fd`.
- Project branch synced with `main` 2026-09-30 (`b34a223`, clean merge of 6 commits; `test_claude_issue_route.py`, `test_internal_review_push_pr_grace.py`, `test_workflow_file_size_limit.py` 243 passed).
