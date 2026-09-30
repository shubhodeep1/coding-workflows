# Implement-Plan Log — Group permission-prompt reports by command family, not by exact shape

- Plan: docs/plans/issue-5668-group-permission-prompt-families-plan.md
- Source issue: shubhodeep1/coding-workflows#5668 (https://github.com/shubhodeep1/coding-workflows/issues/5668)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5668-group-permission-prompt-families   Final PR: (opening)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from main; phase 1 starts twin-first

## Phases
1. [ ] Phase 1 — family key and family-aware filing (twin script, tests, CLAUDE.md §23.I, agents.md, README.md, changelog)   — protected paths: `.claude/scripts/permission_prompts.py`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] What is the family's command word? — Picked: A — the first command word plus its subcommand (tools whose shape keeps one) or its script basename, as the shape keeps them. Alternatives: B — the first command word alone. Why: allow rules are keyed on command and subcommand or script, and #5668's table keeps `git status` and `git fetch` apart. It gives 21 families on the 60 issues, with no harmful merges. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the family id go? — Picked: A — a separate `<!-- ai:permission-prompt-family:v1 family=<id> -->` line, with the v1 `sig=` marker unchanged. Alternatives: B — a `family=` field inside the v1 marker. Why: exact `sig=… -->` readers (old checkouts, #4867's `duplicate-check`) keep working (§6), following #4867's class-marker precedent. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How are legacy issues (sig marker only) matched by family? — Picked: A — derive the family from their recorded tool, event, and example command (Bash only), in addition to the signature. Alternatives: B — signature only. Why: new variants land on #4678 from the first run, with no bootstrap duplicate per family and no manual marker edits (§18). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Which issues can be a family's issue? — Picked: A — open first, then closed as completed, not_planned, or with no reason; never one closed as duplicate. Alternatives: B — any family issue. Why: #5668 names completed / not_planned, and a comment on a duplicate hides a new cause. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Which match wins, the exact signature or the family? — Picked: A — the exact signature (as today), then the family, then a new issue. Alternatives: B — an open family issue before a closed signature issue. Why: §5; the family only replaces the "new issue" branch. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] What is the family for non-Bash tools? — Picked: A — event, tool, and shape (signature granularity). Alternatives: B — event and tool only. Why: `.claude/**` edit prompts are closed as not planned by design and must not absorb other edits. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Which prefixes are stripped? — Picked: A — leading `NAME=value` words, assignment-only and `export`-of-assignments segments, `cd …` followed by `&&` or `;`, and `timeout [options] <duration>`. Alternatives: B — only the literal list in #5668. Why: #4905 and #5470 show the same harmless variations. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-30] Which script file do the tests load? — Picked: A — the `workflow-templates/.claude/` twin, with parity pinning the root copy. Alternatives: B — the root copy, with new tests red until the sync. Why: the twin-first rule of `/implement-plan-claude` step 4. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-30] What counts as a substitution? — Picked: A — `$(` outside single quotes, excluding `$((`. Alternatives: B — also backticks. Why: #5668 names `$(…)`, and arithmetic is not command substitution. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-30] For a heredoc behind another command, which command names the family? — Picked: A — the first command word, as #5668 says. Alternatives: B — the heredoc's receiving command. Why: B saves one family out of 22 and misplaces #5202. A is literal. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Started by the Claude issue dispatcher routine (trigger trig_018T61YPs5MaxqQHrsfsxnaz) in session session_016vGbjSkXz28Wenb3VreJQs (Auto mode).
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "label": null, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- In-flight projects editing the same script: #4750, #4858, #4867, #5124. Whichever lands second resolves the textual conflict at its project-branch sync.
