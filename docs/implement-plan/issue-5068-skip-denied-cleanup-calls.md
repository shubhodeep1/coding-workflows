# Implement-Plan Log — Unattended stages skip a denied cleanup call instead of retrying it

- Plan: docs/plans/issue-5068-skip-denied-cleanup-calls-plan.md
- Source issue: shubhodeep1/coding-workflows#5068
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5068-skip-denied-cleanup-calls   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: Project started by /implement-issue-claude (session_01Auy2sKWXHcPHqbLY2N3RmG); phase 1 is being implemented twin-first.

## Phases
1. [ ] Phase 1 — skip denied cleanup calls   — protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/claude-issue-pickup.md` (twins only; the pickup, which has no twin, goes into the sync blocker as a diff)
   - [ ] CLAUDE.md §26.I plus pointers in §26.C, §26.D, §26.G
   - [ ] `implement-plan-claude.md` twin: resume hygiene (with the `get_trigger` ownership check), zombie-checker cleanup, arming the wait, two-step start, hand-back, steps 12–13, checker prompt 4b, Rules, Output Format
   - [ ] `fix-claude-pr.md` twin: step 8 and Rules
   - [ ] `claude-issue-pickup.md`: exact diff + sha256 for the sync blocker
   - [ ] `agents.md` bullet
   - [ ] Tests in `test_implement_plan_claude_command.py`, `test_check_in_status_hand_back.py`, `test_implement_issue_claude_command.py`
   - [ ] `changelog.d/5068-skip-denied-cleanup-calls.md`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the rule live? — Picked: A — one definition in a new CLAUDE.md §26.I, plus a short sentence at each command's cleanup site and a Rules bullet. Alternatives: B — the full rule repeated in each command only. Why: the §26 flows are in CLAUDE.md, and one definition keeps six call sites from drifting; CLAUDE.md is not a protected path. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Which calls count as cleanup? — Picked: A — every `delete_trigger`, `archive_session`, and `set_session_title` in the listed flows, including the §26.G sweep's deletes, the checker prompt's step 4b delete, the two-step start's archive after a failed start trigger, and the end-of-project archives. Alternatives: B — only the three sites the issue names. Why: the acceptance criterion is that no flow retries a denied cleanup call. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] What does a failed cleanup call other than a denial do? — Picked: A — not found counts as done; any other failure is recorded as `cleanup skipped: <tool> failed (<error>)` and not retried, and only a denial skips the rest of the step's cleanup. Alternatives: B — leave other failures unspecified. Why: a transient failure on housekeeping is not worth a retry either, and the leftover is swept later; skipping the rest only after a denial is what the issue asks. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] When is `get_trigger` needed before a delete? — Picked: A — for a trigger named by id (the `— resume.` block, the log, a report, the checker's own hand-back trigger); a trigger picked from a `list_triggers` result in the same step is checked on the listing's `name` and `persistent_session_id`, and the §26.G sweep keeps its script-chosen list. Alternatives: B — `get_trigger` before every delete. Why: the listing already carries both fields (§15), and the sweep's script already restricts names. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] How does the ownership check apply to the pickup, whose triggers are bound to other pickup sessions by design? — Picked: A — the pickup deletes only triggers named exactly `Claude issue pickup: hourly` (its existing filter), and the skip-on-denial rule applies unchanged. Alternatives: B — require the trigger to be bound to this session. Why: B would stop `— restart` and `stop` from working at all. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Where is `cleanup skipped` recorded in flows without a progress log? — Picked: A — in the flow's report (the fix-claude-pr report, the pickup's one-line report, the checker's reply, the §26.D report). Alternatives: B — as a PR or issue comment. Why: a comment per skipped cleanup is noise, and the report is where each flow already records its outcome. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] How is `.claude/commands/claude-issue-pickup.md`, which has no twin, changed? — Picked: A — not edited by this stage; its exact diff and the resulting file's sha256 go into the twin-sync blocker, and its test reads the root file (red until the sync). Alternatives: B — add a `workflow-templates/` twin for the pickup. Why: the pickup is coding-workflows-only, and B would ship it to consumers (§5); A is the Q40 no-twin rule. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by /implement-issue-claude for #5068 (session session_01Auy2sKWXHcPHqbLY2N3RmG, started by the dispatch trigger trig_014NQAzdbcy3kAeXTjXFejP9); start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Protected-path approval: phase 1 — twin-first per Q40 (issue #5068 spec, 2026-09-29). The owner-authored issue body says "Twin-first: the commands are protected paths. Edit the `workflow-templates/.claude/**` twins (Q40 / #4948)": edit only the twins, open the phase PR, post a `hold` claim on its head, and stop BLOCKED for the `[claude-twin-sync]` copy.
- `security_pass_skip.py`: `{"skip": false, "label": null, "reason": "no skip label"}` → `Security pass: run`.
- Stale Routine sweep: deleted 3 ended one-shots (`trig_012pdEMdh4XyfjbcvQbUC1Bb`, `trig_01JbsquVNzafeNQYorYjNRwg`, `trig_015mGEKqaoybeytgGwozzS5s`).
- Issue progress comment: 5885202067.
