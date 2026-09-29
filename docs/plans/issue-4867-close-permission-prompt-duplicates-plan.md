# Let issue sessions close pipeline-filed ai:permission-prompt duplicates themselves

Source issue: shubhodeep1/coding-workflows#4867 (https://github.com/shubhodeep1/coding-workflows/issues/4867)
Base branch: main
Security pass: run

## Summary

An `/implement-issue-claude` session that finds its `ai:permission-prompt` issue is a duplicate of a fix already in flight or merged may close it as a duplicate without asking, but only under four narrow conditions (operator decision Q58: A). `permission_prompts.py` also stops opening a new issue when a new pattern belongs to the same command class as an open fix issue, and comments there instead.

## Context

- **The cost.** `permission_prompts.py file` (CLAUDE.md §23.I) opens one `ai:permission-prompt` issue per new command shape, and the signature covers the whole shape. One cause therefore files several issues: an inline-interpreter edit followed by a different trailing read (#4677, #4726, #4843), or the same classifier outage hit by different commands. The Claude issue pickup starts an Opus session for each one. The session finds the duplicate, but closing an issue it did not open is a §23.C ask-first operation, so it stops BLOCKED. On 2026-09-28 the operator answered 15 such questions (#4677, #4726, #4749, #4751, #4759–#4762, #4767, #4779, #4780, #4808, #4820, #4824, #4843).
- **Where the stop happens today.** `/implement-issue-claude` has no explicit duplicate step. Sessions notice the duplicate while reading context (step 5) and stop "before planning" with an `ai:claude-blocked:v1` comment (for example #4843, closed later as a duplicate of #4678 under Q47: A).
- **What pipeline-filed means.** Every such issue body is written by `issue_body()` in `.claude/scripts/permission_prompts.py`. It says "Filed by `.claude/scripts/permission_prompts.py`" and ends with `<!-- ai:permission-prompt:v1 sig=<sig> -->`. The issue is posted with the labels `ai:permission-prompt` and `ai:claude` in the same request, under the session's GitHub identity. In Claude Code Web that identity is the account that connected the GitHub App (`gh api user` → `shubhodeep1`, association `OWNER`), and the author of #4843 is the same account.
- **Label-at-creation check already exists.** `.claude/scripts/security_pass_skip.py` (`_label_applied_at_creation`) verifies that a label was applied by the issue author within 120 s of creation, using one page of issue events (issue #4623). The same rule tells a pipeline-filed issue from one relabelled later.
- **Twin-first (#4785, still open).** Unattended sessions edit `.claude/**` only through the `workflow-templates/.claude/**` twins, and the supervising session copies them into `.claude/**` (operator #4750 Q40: A, restated in this issue's body). `.claude/commands/implement-issue-claude.md` and `.claude/scripts/permission_prompts.py` both have byte-identical twins; parity is pinned by `tests/test_implement_issue_claude_command.py::test_template_parity` and `tests/test_permission_prompts.py::test_template_parity`.
- **Related work in flight.** #4750 (final PR #4770, draft) adds the classifier-outage exclusion to `permission_prompts.py`: outage denials are no longer patterns. It is the model for the class match. #4858 (open) will add a guard that denies inline-interpreter writes; its item 1 defines an inline-interpreter write. #4678 (final PR #4684) adds the written rule against inline-interpreter edits.
- **Binding rules.** §23.C (ask-first), §23.I (permission prompt reports), §28.C (ask-first operations are never auto-decided), §15 (API budget), §6 (identifiers), §20 (changelog fragment), §9 (tabs in Python, 2-space YAML).

## Goals

1. CLAUDE.md §23.C names the carve-out and points to §23.I; §23.I states the four conditions exactly as the issue does; §28.C's ask-first bullet names the exception. Anything outside the conditions keeps the §23.C ask.
2. `/implement-issue-claude` (twin) has a duplicate step that runs before the plan is written. It applies the carve-out when every condition holds and ends the project. Otherwise it keeps today's `ai:claude-blocked` stop.
3. A deterministic `permission_prompts.py duplicate-check` subcommand (twin) decides conditions 1 and 2 from REST reads and prints one JSON line. The session never judges them from labels by itself.
4. `permission_prompts.py file` (twin) tags each new issue whose pattern is an inline-interpreter write (the #4858 definition) with `<!-- ai:permission-prompt-class:v1 class=inline-interpreter-write -->`. A later new pattern of that class comments "Seen again" on the open issue carrying that class marker instead of filing a new one.
5. Tests pin the carve-out conditions in CLAUDE.md and the command twin, and cover the class match and the duplicate check. They are wired into `ci.yml`. A `changelog.d/` fragment is added.

## Non-goals

- Human-filed or relabelled issues, PRs the session did not open, and every other §23.C operation (the issue's own limit).
- Deleting branches. Branch deletion stays §23.C ask-first (AD-5).
- Duplicate closing in `/implement-plan-claude` stages or `/fix-claude-pr`. The issue names the `/implement-issue-claude` step only (AD-1).
- The #4858 guard itself, and the classifier-outage exclusion (#4750).
- Editing `.claude/**` directly. Twins only, until #4785 lands.

## Constraints

- §5: only the files listed below; no reflow of unrelated text.
- §6: no identifier, step number, section number, marker, or label is renamed. New step `5a` keeps steps 0–8 stable. `existing_issues()` keeps its signature.
- §15: `duplicate-check` issues at most 5 REST GETs (authenticated user, issue, one events page, target issue, fix PR). The class match reuses the one existing `ai:permission-prompt` list read and adds no call.
- §19: phase and completion PRs use `Refs #4867`; the final PR into `main` uses `Fixes #4867` (#4867 is not an `ai:orchestrator-tracking` issue).
- §20: one fragment, `changelog.d/4867-close-permission-prompt-duplicates.md`, section `changed`.
- §23.D: the session closes and comments through the GitHub MCP tools, never `gh api … --input`.
- §28.C: the phase edits `.claude/**` only through its twins (the interim twin-first rule). `.claude/**` parity is restored by the supervising session's `[claude-twin-sync]` commit.

## Approach

- **Rule text.** §23.I gets a "Closing pipeline-filed duplicates" paragraph with the four conditions and the evidence comment. §23.C gets a short named carve-out that points to it. §28.C's ask-first bullet names the exception, so issue mode does not read it as still forbidden.
- **The script decides eligibility.** `permission_prompts.py duplicate-check --repo R --issue N --target M --fix-pr P` checks conditions 1 and 2:
  - **Issue:** open, not a PR, labelled `ai:permission-prompt`, body carries the "Filed by" line and the sig marker, author equals the authenticated login, and the label was applied by the author at creation. This reuses `security_pass_skip._label_applied_at_creation`, loaded like `check_in_status`.
  - **Target:** a different issue that is open, or closed as `completed`.
  - **Fix PR:** references the target (head branch `…issue-<M>-…`, or `#<M>` in its title or body). It must be open or merged; for a closed target it must be merged into the default branch.

  It prints `eligible`, the failed `reasons`, and evidence fields (signature, class markers, the occurrence line). It lives in the already-allowlisted `permission_prompts.py *` command, so no settings change is needed.
- **The session supplies evidence and acts.** Step 5a runs after context is read and before the plan. If the session concludes the issue is a duplicate, it runs the check. When eligible, it posts one comment naming the target, the fix PR, the matching denial reason or command class, and the session ids and timestamps. It then closes with `state_reason: duplicate` and `duplicate_of`, and ends. When not eligible, it keeps today's `ai:claude-blocked` stop with the script's reasons.
- **Class match in the filer.** Following the #4750 outage model, the check is a small pure function over the raw log record (heredoc bodies included). It reuses the existing single list read. Signature match still wins (open or closed). A class match only routes to an **open** issue, so a new shape after the fix merged still files.

Alternatives considered:

- Instruction text only, with the session checking labels and events itself (rejected, AD-2: §1, and a hand-built read chain prompts in unattended sessions).
- A new helper script (rejected: it needs a new allow rule in `settings.json`, a guard file that #4785 reserves for owner review).

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` turns one standalone issue into exactly one phase.

1. **Phase 1: duplicate-close carve-out, duplicate-check, and class routing.**
   - Scope: the CLAUDE.md §23.C/§23.I/§28.C text; the two twins; `agents.md` and `README.md`; one new test file wired into `ci.yml`; and one changelog fragment.
   - Protected paths: `.claude/commands/implement-issue-claude.md` and `.claude/scripts/permission_prompts.py`, edited only through `workflow-templates/.claude/commands/implement-issue-claude.md` and `workflow-templates/.claude/scripts/permission_prompts.py`. The supervising session copies both into `.claude/**`.
   - Done when:
     - every goal is present;
     - the new tests pass;
     - with the twins copied, `tests/test_permission_prompts.py`, `tests/test_implement_issue_claude_command.py`, and `tests/test_claude_md_section_numbers.py` pass;
     - ruff is clean.
   - Rollback: revert the phase PR. There is no state, schema, or label change. Issues already carrying a class marker are harmless to older filers, which ignore it.

## Implementation Steps

Phase 1:

1. **`CLAUDE.md`:**
   - **§23.C:** after the "Command-invoked dispatches are already approved" paragraph, add "**Pipeline-filed permission-prompt duplicates may be closed.**", naming the carve-out and pointing to §23.I.
   - **§23.I:** after "Opening these issues is approved by this section…", add a "Closing pipeline-filed duplicates" paragraph with conditions 1–4, the `duplicate-check` command, the evidence comment, and the limit.
   - **§28.C:** in the "Ask-first operations" bullet, add the §23.I exception.
2. **`workflow-templates/.claude/commands/implement-issue-claude.md`:** add step **5a. Duplicate check**, between step 5 and step 6. Add one Rules bullet. Add `mcp__github__issue_read` to the Tool Access line if needed. Keep "stop and ask" out of the file (`test_issue_command_auto_decides_everything`).
3. **`workflow-templates/.claude/scripts/permission_prompts.py`:**
   - Constants: `INLINE_INTERPRETER_WRITE_CLASS`, `CLASS_MARKER_TEMPLATE`, `CLASS_MARKER_RE`, `FILED_BY_LINE`.
   - `command_class(record)` over the raw command: python/python3 with `-c`, or `-` plus a heredoc, whose program text holds a write (`write_text`, `write_bytes`, `open(` with a `w`/`a`/`x`/`r+` mode, `os.replace`, `shutil.`, `.unlink(`, `os.remove`); or `sed -i`, `perl -i`/`-pi`, `ruby -i`, `awk`/`gawk -i inplace`.
   - `group_patterns` records `class`. `issue_body` adds the class marker and one duplicate-close sentence. `class_comment_body`.
   - `list_permission_prompt_issues`, `index_by_signature`, and `open_issues_by_class`, with `existing_issues` kept as a wrapper. `file_patterns` routes by signature, then by class.
   - The `duplicate-check` subcommand: `duplicate_check()` (pure decision) plus reads through `check_in_status.gh_api`.
   - Docstring updates, including the API budget.
4. **`tests/test_permission_prompt_duplicates.py`** [new], run against the twin module and the command twin:
   - class detection: positive and negative cases from #4858 item 1/4;
   - filing: class marker in the body, class routing to an open issue, no routing to a closed one, signature precedence;
   - `duplicate_check` decisions: each condition failing and the eligible case;
   - CLI JSON;
   - instruction text pinning the four conditions in CLAUDE.md §23.C/§23.I/§28.C and the command twin.

   Wire it into the `ci.yml` step "Unattended helper and permission prompt report tests (CLAUDE.md §23.I)".
5. **`agents.md`** (§23.I section) and **`README.md`** (unattended helpers callout): one short paragraph each on class routing and the duplicate-close carve-out.
6. **`changelog.d/4867-close-permission-prompt-duplicates.md`** [new], `<!-- changelog: changed -->`, per §20.D.

## Files & Modules

- `CLAUDE.md` (the symlink `workflow-templates/CLAUDE.md` follows)
- `workflow-templates/.claude/commands/implement-issue-claude.md` (→ `.claude/commands/implement-issue-claude.md` at the twin sync)
- `workflow-templates/.claude/scripts/permission_prompts.py` (→ `.claude/scripts/permission_prompts.py` at the twin sync)
- `tests/test_permission_prompt_duplicates.py` [new]
- `.github/workflows/ci.yml`
- `agents.md`
- `README.md`
- `changelog.d/4867-close-permission-prompt-duplicates.md` [new]

## Tests

- New `tests/test_permission_prompt_duplicates.py` (unit):
  - **Classes:** the #4755/#4786/#4791-style `python3 - <<'EOF' … write_text` edit, `python3 -c "open(p,'w')…"`, `sed -i`, and `perl -pi` are `inline-interpreter-write`. A read-only heredoc, `pytest`, `python3 scripts/x.py`, and a `git commit -m` mentioning `python3 -` are not.
  - **Filing:** a classed pattern's new issue carries the class marker. A second classed pattern with a different shape comments "Seen again" on the open class issue and files nothing. A closed class issue does not attract it. A signature match still wins.
  - **`duplicate_check`:** eligible case; ineligible for human-filed (no marker or "Filed by" line), relabelled (label event late or by another actor), wrong author, closed issue, target equal to issue, target closed as `not_planned`, fix PR closed unmerged, fix PR not referencing the target, closed target with an open fix PR, and a merged fix PR on a non-default base. A read failure exits 2 with `eligible: false`.
  - **Instruction text:** the four conditions in CLAUDE.md §23.C/§23.I/§28.C and the command twin, with step 5a before step 6.
- Existing suites (pass once the twins are copied): `tests/test_permission_prompts.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_claude_md_section_numbers.py`, `tests/test_security_pass_skip.py`.
- End to end: the next `ai:permission-prompt` duplicate session closes the issue with an evidence comment instead of an `ai:claude-blocked` question. Observed on GitHub, not a test.

## Risks & Mitigations

- **A wrongly closed issue.** Mitigation: the script checks the pipeline origin and the target/fix state; the evidence comment makes the call auditable. A closed issue can be reopened, and any new occurrence of the same signature still comments on it (closed issues are commented on, not reopened).
- **The owner account files an issue by hand with a copied marker.** ACCEPTED: it would also need the "Filed by" line and the label at creation, and the author equals the session account by design. The issue's limit is "pipeline-filed", and this is the strongest check available without a separate bot identity.
- **The class match swallows a genuinely different cause.** Mitigation: it routes only to an **open** issue of the same class, as a visible "Seen again" comment naming the new pattern. Nothing is dropped.
- **Merge overlap with #4750 in `permission_prompts.py`.** Mitigation: separate hunks (constants, a new function, the list read in `file_patterns`); the later merge resolves them keeping both.
- **`duplicate_of` unsupported by the MCP tool.** Mitigation: the step closes with `state_reason: duplicate` and the comment names the target; the fallback is written into step 5a.
- **Red parity tests until the twin sync.** ACCEPTED: the interim twin-first rule. The phase PR is held with a `hold` claim until the supervising session's `[claude-twin-sync]` commit.

## Rollout

Text, one script, and tests. The carve-out takes effect for issue sessions once the final PR merges into `main` and `.claude/**` carries the synced twins. The class match takes effect in the next stage report after that. Consumer repos receive the twins through the existing `@stable` sync, but filing and duplicate closing only happen in coding-workflows (`FILING_REPO`). No flag, no migration.

## References

- Issue #4867 (operator decision Q58: A, 2026-09-28)
- #4785 (twin-first sync), #4750 / PR #4770 (outage exclusion), #4858 (inline-interpreter guard), #4678 / PR #4684 (inline-interpreter rule), #4843 (a duplicate stop), #4623 (label-at-creation check)
- CLAUDE.md §23.C, §23.I, §28.C

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does the duplicate check run? — Picked: A — a new step 5a in `/implement-issue-claude`, after context is read and before the plan is written. Alternatives: B — a gate in step 2 before any context is read; C — also in `/implement-plan-claude` stages. Why: sessions find duplicates while reading context (the #4843 stop came "before planning"); the issue names this command only; `5a` keeps step numbers stable (§6). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Who decides conditions 1 and 2? — Picked: A — a `permission_prompts.py duplicate-check` subcommand that prints a JSON verdict, reusing `security_pass_skip`'s label-at-creation check. Alternatives: B — instruction text only, with the session reading labels and events itself; C — a new helper script. Why: §1 and the repo's "the script decides" pattern; A needs no new allow rule, while C would need a `settings.json` change, which #4785 reserves for owner review. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What is "the workflow/session account"? — Picked: A — the authenticated login of the session running the check (`GET /user`); the issue author must equal it. Alternatives: B — any `OWNER` author, as in `security_pass_skip.py`; C — `github-actions[bot]` only. Why: the filer posts under the session's identity (the owner account through the proxy), and A is the literal condition; C would never match. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How must the fix PR relate to the target? — Picked: A — its head branch contains `issue-<target>-` or its title or body contains `#<target>`. It must be open or merged; for a closed target it must be merged into the default branch. Alternatives: B — any open or merged PR the session names. Why: condition 2 needs the fix to be the target's fix, and both links are how this repo's chains name their PRs. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] What does condition 4 do with a branch the session created? — Picked: A — close only PRs this session opened, as not merged, with a comment linking the target; leave branches in place. Alternatives: B — also delete the branches. Why: branch deletion is still §23.C ask-first and outside the operator's narrow approval; step 5a runs before step 6, so a fresh start has created nothing. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Implement the optional `permission_prompts.py` class match? — Picked: A — yes, one class, `inline-interpreter-write`, as #4858 item 1 defines it. The first open issue of a class is its fix issue (class marker in the body); signature match keeps precedence; only open class issues attract new shapes. Alternatives: B — skip the optional change; C — also class inline-interpreter reads. Why: the issue marks it preferred; C goes beyond the #4858 definition the issue cites. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Which transport closes the issue? — Picked: A — `mcp__github__add_issue_comment`, then `mcp__github__issue_write` `update` with `state: closed`, `state_reason: duplicate`, `duplicate_of: <target>`. When the tool has no `duplicate_of`, close with `state_reason: duplicate` alone; the comment names the target. Alternatives: B — `gh api -X PATCH`. Why: §23.D and the §23.H guard prompt on `gh api` writes of `state`. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] Which module do the new tests load? — Picked: A — the `workflow-templates/.claude/` twins, in a new `tests/test_permission_prompt_duplicates.py`. Alternatives: B — extend `tests/test_permission_prompts.py` against `.claude/scripts`. Why: the twin leads under twin-first, so the new tests pass before the sync; the existing parity tests keep `.claude` equal to it afterwards. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` returned `{"skip": false, "label": null, "reason": "no skip label"}`.
- The issue body restates the interim twin-first rule ("Edit the `workflow-templates/.claude/commands/**` twins first (twin-first rule until #4785 lands; the supervising session syncs them)"). That, plus the operator's standing #4750 Q40: A, is recorded as the phase's protected-path approval, as for #4817.
