# Route file edits through the Edit and Write tools, not inline interpreter heredocs

Source issue: shubhodeep1/coding-workflows#4678 (https://github.com/shubhodeep1/coding-workflows/issues/4678)
Base branch: main
Security pass: run

## Summary

An unattended `/implement-plan-claude` stage session stopped at a permission
prompt when it rewrote two `workflow-templates/.claude/commands/` files with an
inline `python3 - <<'PYEOF' … PYEOF` script. Nothing can approve that command
shape. Add a CLAUDE.md §23.I rule that edits go through the Edit and Write
tools. Also make the §23.I protected-path triage clause say that only the
repository root's `.claude/**` is protected.

## Context

- Issue #4678 (filed by `.claude/scripts/permission_prompts.py`): pattern
  `python3 * << * ; git diff --stat -- *`, 1 occurrence at
  2026-09-27T23:49:14Z in session `session_01N5tvkRbtiio6j9CJTm7QnA`. The
  example is `python3 - <<'PYEOF' <body omitted> PYEOF` followed by
  `git diff --stat -- workflow-templates/.claude`.
- That session was the `claude-fixer-unattended-convergence` phase 2/4 stage.
  Its log records `Protected-path approval: phase 2 — operator: implement
  every change outside .claude/** …` and ends `Status: BLOCKED` waiting on
  the operator to copy "the workflow-templates twins" into `.claude/commands/`.
  Its phase-2 commit `e353799` (2026-09-28T00:48Z, after the prompt) changes
  `workflow-templates/.claude/commands/fix-claude-pr.md` and
  `workflow-templates/.claude/commands/implement-plan-claude.md`. So the
  heredoc was a file edit of the template twins, and the `git diff --stat`
  checked it.
- Claude Code's protected paths are the project root's `.claude/` (except
  `.claude/worktrees`) and `~/.claude/`
  (https://code.claude.com/docs/en/permission-modes.md#protected-paths).
  `workflow-templates/.claude/**` is an ordinary path, so Edit and Write on it
  are approved like any other in-repo edit. What prompted was the inline
  interpreter: "when Claude Code can't fully parse a command, it asks for
  approval" (https://code.claude.com/docs/en/permissions.md#read-only-commands).
  No `permissions.allow` rule can approve an arbitrary inline script, and
  §23.I forbids widening one to do so.
- CLAUDE.md §23.I (lines 1343-1387) already tells unattended sessions to use
  the allowlisted helpers "instead of hand-built pipelines, loops, `$(...)`,
  or heredocs". The only writes it covers are GitHub writes, and it says
  nothing about file edits. Its triage clause says an issue "for a
  protected-path edit (`.claude/**`)" is closed as not planned. Read loosely,
  that clause would close this issue even though its path is not protected.
- No command file instructs a heredoc edit. The call was the model's own
  choice, so fix step 1 in the issue ("change the command file that produced
  the call") has no command line to change. The rule belongs where every
  interactive session reads it: CLAUDE.md, which consumer repos receive
  through `workflow-templates/CLAUDE.md` (a symlink to `../CLAUDE.md`) on the
  `@stable` sync.

## Goals

- CLAUDE.md §23.I says that file edits, including the byte-identical
  `workflow-templates/` twins, use the Edit and Write tools and never an
  inline interpreter (`python3 - <<…`, `python3 -c`, `node -e`, `perl -e`, a
  heredoc fed to a shell). It gives the reason: the script cannot be parsed,
  so it prompts in every permission mode. It cites issue #4678.
- The §23.I triage clause names the protected path precisely: the repository
  root's `.claude/**`, not `workflow-templates/.claude/**`.
- CLAUDE.md §23.I says that once an Edit or Write to the repository root's
  `.claude/**` is blocked or denied, a session never retries that write
  through Bash, `python3`, `sed`, `tee`, `cp`, a heredoc, or any other tool.
  It stops at `Status: BLOCKED`, naming each file and its exact edit, and the
  operator's watched session applies it. This goal was added by the operator
  (decision Q15: A, 2026-09-28; AD-6).
- Tests in `tests/test_permission_prompts.py` fail if any of these rules is
  removed.
- `agents.md` ("Unattended helpers and permission prompt reports") and a
  `changelog.d/` fragment record the rule.

## Non-goals

- No edit to `.claude/**` (commands, hooks, scripts, `settings.json`) or its
  `workflow-templates/.claude/**` twins, and no new allow rule or helper.
- No change to `permission_prompts.py` pattern grouping or filing.
- Issue #4677 (`python3 * << *`, same session) is not folded in as its own
  scope. The operator closed it as a duplicate of #4678 on 2026-09-28.
- No change to the `claude-fixer-unattended-convergence` project or its
  branches.

## Constraints

- §5: two sentences in §23.I, one test, one `agents.md` line, one fragment.
- §6: no identifier, heading, or section number changes.
  `tests/test_claude_md_section_numbers.py` must still pass, and so must the
  existing `test_claude_md_documents_the_reports` assertions.
- §9: the new test uses tabs, like the rest of the file.
- §14: CLAUDE.md is synced to consumers. The fragment says so (§20.A: it
  changes what consumers receive on the next `@stable` sync).
- §23.I: the fix never widens a permission.
- §28.C: the phase touches no `.claude/**` path, so no protected-path stop
  applies.

## Approach

1. In CLAUDE.md §23.I, add one paragraph after the "Prompt reports" bullet
   and before the closing `tests/…` sentence:

   > **File edits use the Edit and Write tools.** Edit repository files with
   > the Edit and Write tools, including the byte-identical
   > `workflow-templates/` twins, which get the same edit in each copy. Never
   > edit a file with an inline interpreter (`python3 - <<'EOF'`,
   > `python3 -c`, `node -e`, `perl -e`, or a heredoc fed to a shell). Claude
   > Code cannot parse the script, so no allow rule approves it, and it stops
   > at a permission prompt in every mode (issue #4678).

2. In the same section, change "a protected-path edit (`.claude/**`)" to "a
   protected-path edit (the repository root's `.claude/**`;
   `workflow-templates/.claude/**` is not protected)".

Alternatives considered: editing `.claude/commands/implement-plan-claude.md`
step 4 and its twin (AD-2 B). That puts the rule in one command only, while
the model's choice of tool can happen in any session. It is also a
protected-path phase, which stops this unattended chain at `BLOCKED`.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
change is two sentences of one CLAUDE.md section plus its test, docs line, and
fragment, and it cannot be split usefully.

1. **Phase 1 — file-edit rule and protected-path precision in CLAUDE.md §23.I.**
   - Files: `CLAUDE.md` (§23.I), `tests/test_permission_prompts.py`,
     `agents.md`, `changelog.d/4678-edit-files-without-python-heredocs.md`
     [new].
   - Done when: both sentences are in §23.I, the new test passes, and so do
     `tests/test_permission_prompts.py` and
     `tests/test_claude_md_section_numbers.py`. No `.claude/**` or
     `workflow-templates/.claude/**` file changes.
   - Rollback: revert the phase PR.

## Implementation Steps

1. Phase 1 — `CLAUDE.md` ~1376-1387: insert the Approach step 1 paragraph
   before the "`tests/test_dispatch_workflow.py`, … cover the helpers" line,
   and apply the Approach step 2 wording change to the triage sentence.
2. Phase 1 — `tests/test_permission_prompts.py`: add
   `test_claude_md_routes_file_edits_through_edit_tools`. It asserts that the
   §23.I slice of CLAUDE.md contains the file-edit rule, the inline
   interpreter examples, `issue #4678`, and the root-only protected-path
   wording.
2a. Phase 1 (AD-6) — `CLAUDE.md` §23.I: add the "A blocked `.claude/**`
   edit is never retried another way" paragraph after the file-edit rule,
   and pin it with `test_claude_md_never_retries_a_blocked_claude_dir_edit`.
3. Phase 1 — `agents.md` "Unattended helpers and permission prompt reports":
   add one bullet that file edits use the Edit and Write tools, never inline
   interpreters, and that only the root `.claude/**` is a protected path.
4. Phase 1 — `changelog.d/4678-edit-files-without-python-heredocs.md` [new],
   `<!-- changelog: changed -->`, per §20.D/§20.G.

## Files & Modules

- `CLAUDE.md` (and so `workflow-templates/CLAUDE.md`, a symlink to it)
- `tests/test_permission_prompts.py`
- `agents.md`
- `changelog.d/4678-edit-files-without-python-heredocs.md` [new]

## Tests

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_permission_prompts.py tests/test_claude_md_section_numbers.py tests/test_changelog_fragment_contract.py`
- The new test fails against the current CLAUDE.md (checked before the edit)
  and passes after it.
- `git diff --stat` on the phase branch shows only the four files above.

## Risks & Mitigations

- A session still reaches for a heredoc: the rule narrows it but cannot
  enforce it. The prompt report files the next occurrence under the same
  signature, as a comment on this issue.
- Editing a twin pair with the Edit tool is two edits instead of one copy.
  The existing parity tests (`test_template_parity` and the others) catch a
  missed twin.
- The protected-path wording goes stale if Claude Code widens its protected
  set. The sentence cites the doc page, so a reviewer can re-check it.

## Rollout

Instructions only. The change takes effect for new interactive sessions in
this repo on merge, and in consumer repos on the next `@stable` sync of
CLAUDE.md. Nothing to activate.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Is this prompt by design, so the issue closes as not planned? — Picked: A — no, fix it: the heredoc edited `workflow-templates/.claude/commands/*.md` (commit e353799), which Claude Code does not protect (only the root `.claude/` and `~/.claude/` are protected), and the prompt came from the unparseable inline interpreter. Alternatives: B — close as not planned under §23.I's protected-path clause. Why: the path is not protected, so the prompt is avoidable. Closing an issue this session did not open is also a §23.C ask-first operation. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Where does the fix live? — Picked: A — CLAUDE.md §23.I, which every interactive session reads and consumer repos receive through the CLAUDE.md sync. Alternatives: B — `.claude/commands/implement-plan-claude.md` step 4 and its `workflow-templates/` twin; C — both. Why: no command instructs the heredoc, so this is the model's tool choice. A needs no protected-path edit, so the unattended chain can run without a `BLOCKED` stop. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Clarify §23.I's "protected-path edit (`.claude/**`)" clause? — Picked: A — yes, name the repository root's `.claude/**` and say `workflow-templates/.claude/**` is not protected. Alternatives: B — leave the clause as is. Why: the loose reading would have closed this fixable issue as not planned. The clause is the triage rule for every future `ai:permission-prompt` issue. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Fold sibling issue #4677 (`python3 * << *`, same session) into this project? — Picked: A — no, it keeps its own issue-mode project. Alternatives: B — fold it in. Why: `/implement-issue-claude` runs one issue per chain, and #4677's heredoc body is unknown, so it may not be a file edit. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-28] Does the change need a `changelog.d/` fragment? — Picked: A — yes, `changed`. Alternatives: B — none (docs only). Why: §20.A requires one for anything that changes what a consumer repo receives on the next `@stable` sync, and CLAUDE.md is synced. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-28] Add a rule that a blocked or denied write to the root `.claude/**` is never retried through another tool? — Picked: A — yes, in CLAUDE.md §23.I in this phase, with a contract test: the session stops at `Status: BLOCKED` naming each file and its exact edit, and the operator's watched session applies it. Alternatives: B — leave it out of scope. Why: operator decision Q15: A (2026-09-28), relayed by the supervising session `session_01VwSvLnEGmUoaQD42DKapiU` and confirmed by the owner's closing comment on #4677. It is the flow `claude-fixer-unattended-convergence` phase 2 used (commit 6b8a1a3), and it lives in CLAUDE.md, so the phase still needs no protected-path stop. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` printed `{"skip": false, "label": null, "reason": "no skip label"}`.

## References

- Issue #4678; sibling issue #4677
- `docs/implement-plan/claude-fixer-unattended-convergence.md` on
  `claude/implement-plan-claude-fixer-unattended-convergence-phase-2`
- CLAUDE.md §23.D, §23.H, §23.I, §28.C
- https://code.claude.com/docs/en/permission-modes.md#protected-paths
