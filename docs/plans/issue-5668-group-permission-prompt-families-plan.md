# Group permission-prompt reports by command family, not by exact shape

Source issue: shubhodeep1/coding-workflows#5668 (https://github.com/shubhodeep1/coding-workflows/issues/5668)
Base branch: main
Security pass: run

## Summary

`permission_prompts.py file` files one `ai:permission-prompt` issue per exact command shape, so harmless variants of one cause each get their own issue, and the master closes them by hand as duplicates (Q58). This plan adds a **family key** above the exact signature. A new shape whose family already has an issue is added to that issue as a comment, not filed as a new issue.

## Context

- **Today** (`workflow-templates/.claude/scripts/permission_prompts.py`, byte-identical to `.claude/scripts/permission_prompts.py`):
  - `group_patterns` groups log records by `signature(event, tool, shape)` (`command_shape` replaces values with `*`).
  - `file_patterns` reads every `ai:permission-prompt` issue once (`existing_issues`, one REST read per 100) and indexes them by the `<!-- ai:permission-prompt:v1 sig=<sig> -->` marker.
  - A pattern whose signature has an issue, open or closed, gets a "Seen again" comment. Any other pattern opens a new issue.
- **The audit in #5668, re-checked on 2026-09-30.** 60 issues and 60 signatures, 51 of them closed as duplicates. The closing comments name the canonical issues:
  - #4678 (inline `python3` heredocs): 29 issues;
  - #4750 (classifier-outage denials): 18 issues;
  - #4909 (loops): 2 issues;
  - #5139: 1 issue;
  - #4858: 1 issue.
- **Prototype check.** The family key below was prototyped and run over the 60 issues' recorded tool, event, and example command. It yields 21 families.
  - The 29 #4678 duplicates fall into one family, `python3` with a heredoc, apart from 5 edge variants (a `$(…)` inside, a denial, and a heredoc behind `grep` / `mkdir`).
  - No family mixes causes that the operator kept apart. The one mixed family is `gh api --help` (#5413 and #5475, both open with the same cause).
  - The outage denials are grouped by the command they denied. #4750's own project removes them from filing, so they need nothing here.
- **In-flight work on the same file.** #4750 (skip classifier-outage denials), #4858 (skip expected denies), #4867 (a command-class marker `ai:permission-prompt-class:v1` and a `duplicate-check` subcommand), and #5124 (credential masking, per-session reports) all edit `permission_prompts.py` on their project branches.
  - This plan keeps its own identifiers and marker, so it composes with them.
  - Whichever lands second resolves a textual merge conflict at its project-branch sync (step 2 of `/implement-plan-claude`).
- **Twin-first.** `.claude/**` is edited only through its `workflow-templates/.claude/**` twin until #4785 lands (Q40, #4948). `scripts/claude_twin_sync.py` does not exist yet.
- **Binding rules:**
  - §5: minimal change;
  - §6: the `sig=` marker, `MARKER_RE`, `existing_issues`, the JSON summary keys, and `filed-state.json` stay;
  - §9: tabs;
  - §15: no new API call;
  - §19: `Refs #5668` except on the final PR;
  - §20: changelog fragment;
  - §23.I: the documented behaviour;
  - §28.C: protected path.

## Goals

1. Every pattern carries a **family**: its event, its tool, and, for `Bash`, the command and the constructs present in it.
   - The command is the first command word (with its subcommand or script) after stripping env assignments, `export` of assignments, `cd …` prefixes, and `timeout <duration>`.
   - The constructs are a heredoc, a loop (`for` / `while` / `until`), and a `$(…)` substitution.
   - The family id is the first 12 hex digits of a SHA-1, like the signature.
2. A new issue carries the unchanged `sig=` marker and a new `<!-- ai:permission-prompt-family:v1 family=<id> -->` marker, plus a readable `**Family:**` line.
3. Filing order for each pending pattern:
   1. an issue with its exact signature → a "Seen again" comment, as today;
   2. else an issue of its family → a comment naming the new shape and its count, never a new issue;
   3. else → a new issue.
4. A family's issue is chosen as follows:
   - an open issue first;
   - else one closed as `completed` or `not_planned` (or with no reason), which gets a comment and is not reopened;
   - an issue closed as `duplicate` is never a family's issue. It still gets exact-signature comments, as today.
5. Legacy issues (sig marker only) keep matching by signature. They also match by family: the family is derived from their recorded tool, event, and example command.
6. Tests in `tests/test_permission_prompts.py` cover:
   - variants of each family in #5668's table mapping to one family and filing once;
   - distinct families filing separately;
   - legacy markers still matching;
   - the precedence and closed-state rules.
7. CLAUDE.md §23.I, `agents.md`, `README.md`, the module docstring, and a `changelog.d/` fragment describe families.

## Non-goals

- Changing `command_shape`, `signature`, the `sig=` marker, `MARKER_RE`, or the issue title. Existing signatures must not move.
- Skipping classifier-outage denials (#4750), skipping expected denies (#4858), closing duplicates (#4867), and credential masking (#5124).
- Closing, relabelling, or editing existing issues. No backfill.
- Editing `.claude/**` directly.

## Constraints

- §5 / §6: new identifiers only. Before adding each name, check that it does not clash in the module:
  - `FAMILY_MARKER_TEMPLATE` and `FAMILY_MARKER_RE`;
  - `command_family`, `pattern_family`, and `family_label`;
  - `legacy_issue_family` and `index_issues`;
  - `family_comment_body`.
  `existing_issues(slug)` keeps its return shape. The summary's `filed` / `commented` entries keep their keys, and family matches add a `family` key.
- §15: the family index reuses the one list read. The REST issue object already carries `state_reason`.
- §23.I: filing stays limited to `FILING_REPO`, and issue text stays redacted and truncated.
- §28.C: twin-first. The phase edits `workflow-templates/.claude/scripts/permission_prompts.py`, and the supervising session copies it to `.claude/scripts/permission_prompts.py` (`[claude-twin-sync]`).

## Approach

- **Family key** (`command_family(event, tool, command)`):
  - Parse the heredoc-stripped command with the same `shlex` settings as `command_shape`, split it into segments at shell operators, and drop redirect targets.
  - Constructs:
    - `heredoc`: a `<<` operator;
    - `loop`: a segment whose core starts with `for`, `while`, or `until`;
    - `subst`: a `$(` outside single quotes and not `$((`.
  - Skip leading segments that are only assignments (`F=x &&`, `export A=1 &&`) or a `cd …` followed by `&&` or `;`.
  - In the first remaining segment, drop leading `NAME=value` words and a `timeout [options] <duration>` prefix.
  - The command word is then built by the existing `_segment_shape` rules: the command, plus its second shape word when that word is a subcommand (git, gh, npm, …) or a script path (basename).
  - A command that does not parse, and any non-`Bash` tool, fall back to the pattern's shape. Their family is then exactly as wide as their signature.
- **Markers.** A separate family marker line keeps the v1 marker byte-identical for every exact-regex reader (AD-2). This follows the `ai:permission-prompt-class:v1` precedent from #4867.
  - The family marker is read from its **last** occurrence in the body. The generated marker is written after the example block, so a command example cannot hijack it.
- **Legacy issues.** A body without a family marker gets its family from its `for \`<tool>\`` line, its event wording ("permission prompt" / "Auto-mode denial"), and the ````text example (heredoc placeholder removed). This applies to `Bash` issues only, and non-`Bash` legacy issues keep matching by signature alone.
- **Choosing the family issue.** Open issues come first, by lowest number. Then come issues closed as `completed`, `not_planned`, or with no reason, by lowest number. Issues closed as `duplicate` are skipped.
  - A family issue filed earlier in the same run is registered at once. Later patterns of that family in the run comment on it. In a dry run they are reported as comments on the issue that would be filed.

Alternatives considered: the literal first-word key (AD-1 B, AD-10 B), a `family=` field inside the v1 marker (AD-2 B), and signature-only legacy matching (AD-3 B). They are rejected in the auto-decisions below.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` turns one standalone issue into exactly one phase.

1. **Phase 1 — family key and family-aware filing.**
   - **Scope:** the twin script, its tests, the docs, and the changelog fragment.
   - **Protected paths:** `.claude/scripts/permission_prompts.py`. It is edited through `workflow-templates/.claude/scripts/permission_prompts.py`, the twin-first automatic default.
   - **Done when:**
     - Goals 1–7 are met;
     - `tests/test_permission_prompts.py` passes against the twin, except `test_template_parity`, which stays red until the twin sync;
     - after the twin sync the whole file passes.
   - **Rollback:** revert the phase PR. Issues filed with a family marker stay valid, because the old code ignores that marker and still matches their `sig=` marker.

## Implementation Steps

Phase 1:

1. `workflow-templates/.claude/scripts/permission_prompts.py`:
   1. Add `FAMILY_MARKER_TEMPLATE` / `FAMILY_MARKER_RE`, plus the loop keyword set.
   2. Add a token helper shared with `command_shape`, without changing any shape.
   3. Add `command_family`, `pattern_family` (the id), and `family_label`.
   4. `group_patterns` stores `family` and `family_label` on each pattern.
   5. `issue_body` adds the `**Family:**` line and the family marker after the sig marker.
   6. Add `family_comment_body`.
   7. Add `legacy_issue_family` and `index_issues(slug)`: one list read returns the signature index and the family index. `existing_issues` returns the signature index unchanged.
   8. `file_patterns` gets the three-way order, the family-issue choice, and in-run registration.
   9. Update the module docstring.
2. `tests/test_permission_prompts.py`:
   - load the script from the twin (twin-first; `test_template_parity` pins the root copy);
   - add family-key cases for each family in #5668's table (`python3 * <<`, `PYTHONDONTWRITEBYTECODE=* python3`, `git status`, `git fetch`, `for *`, `echo *`);
   - add distinct-family cases, the prefix-stripping and construct cases, and filing through the fake issue list (family comment, open-first, closed `completed` / `not_planned` get comments, closed `duplicate` does not, signature precedence, legacy derivation, in-run registration, dry run, marker hijack).
3. CLAUDE.md §23.I (shared by `workflow-templates/CLAUDE.md`, a symlink), `agents.md` (the `permission_prompts.py file` bullet), and `README.md` (the §23.I summary): describe families.
4. `changelog.d/5668-permission-prompt-families.md` [new], section `changed`.

## Files & Modules

- `workflow-templates/.claude/scripts/permission_prompts.py` (twin of `.claude/scripts/permission_prompts.py`, copied by the twin sync)
- `tests/test_permission_prompts.py`
- `CLAUDE.md`
- `agents.md`
- `README.md`
- `changelog.d/5668-permission-prompt-families.md` [new]

## Tests

- **Unit** (`tests/test_permission_prompts.py`, already in `ci.yml`):
  - `command_family` over the #5668 table's variants and distinct families;
  - `pattern_family` for non-Bash tools;
  - `legacy_issue_family` over a body `issue_body` generated;
  - `file_patterns` against the existing `FakeIssues` double, extended with `state_reason`.
- **Twin overlay:** the full file also passes with the twin copied over the root script, which is the post-sync state.
- **Data check** (recorded in the log, not a committed test): the 60 current issues map to about 21 families, with no family mixing causes the operator kept apart.

## Risks & Mitigations

- **A coarse family swallows a distinct problem into a closed issue.** Mitigations:
  - subcommands and scripts are part of the key;
  - the constructs are part of the key;
  - a closed `duplicate` issue is never a family's issue;
  - non-Bash tools keep signature granularity.
- **A legacy example cut at 2,000 characters loses a later construct** → that legacy issue matches the construct-less family. ACCEPTED: the worst case is a comment on a related issue instead of a new one.
- **A spoofed family marker in an issue body attracts comments.** Only the last marker counts, so a command example cannot inject one. An attacker who can label issues can already spoof a `sig=` marker today, so this adds no new exposure. ACCEPTED.
- **Merge conflicts with #4750, #4858, #4867, and #5124.** Mitigations:
  - new identifiers only;
  - `file_patterns` changes are confined to the "no signature match" branch;
  - the later project resolves the conflict at its sync.
- **Old checkouts.** Sessions running old code ignore the family marker. They still match by `sig=`, and they file new shapes as today until they pick up the sync. ACCEPTED.

## Rollout

- The phase PR merges into the project branch.
- The supervising session copies the twin into `.claude/` (the twin-sync blocker).
- The final PR merges into `main` and carries `Fixes #5668`. #5668 is not an `ai:orchestrator-tracking` issue.
- The next `@stable` sync ships the script to consumers. Consumers only report, so there is no behaviour change there beyond the report data.
- Rollback: revert.

## Auto-decisions

- AD-1 [plan, 2026-09-30] What is the family's command word? — Picked: A — the first command word plus its subcommand (tools whose shape keeps one: git, gh, npm, …) or its script basename (`python3 x.py`), as the shape keeps them. Alternatives: B — the first command word alone. Why: allow rules are keyed on command and subcommand or script, so this is "the same permission rule", and #5668's own table keeps `git status` and `git fetch` apart. It also stops a closed family issue from absorbing unrelated scripts. On the 60 issues it gives 21 families, with no harmful merges. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the family id go? — Picked: A — a separate `<!-- ai:permission-prompt-family:v1 family=<id> -->` line, with the v1 `sig=` marker unchanged. Alternatives: B — a `family=` field inside the v1 marker, as #5668 proposes, with `MARKER_RE` widened. Why: under B, every exact `sig=… -->` reader stops recognising new issues: old checkouts in running sessions, and #4867's `duplicate-check`. A keeps them working (§6, §1 backward compatibility) and follows #4867's class-marker precedent, and the lookup still accepts both marker styles. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How are legacy issues (sig marker only) matched by family? — Picked: A — derive their family from the recorded tool, event, and example command (Bash only), in addition to signature matching. Alternatives: B — signature only. Why: with A, new variants land on the existing open issue (#4678) from the first run. B files one bootstrap duplicate per family, and those would need manual marker edits (§18). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Which issues can be a family's issue? — Picked: A — open first, then closed as `completed`, `not_planned`, or with no reason; never an issue closed as `duplicate`. Alternatives: B — any family issue, duplicates included. Why: #5668 names `completed` / `not_planned`. A duplicate points elsewhere, and a comment on it would hide a real new cause, such as a non-outage `git fetch` denial after the outage duplicates. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Which match wins, the exact signature or the family? — Picked: A — the exact signature, as today, then the family, then a new issue. Alternatives: B — an open family issue before a closed signature issue. Why: §5. The family only replaces the "new issue" branch, which is what #5668 asks for. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] What is the family for non-Bash tools? — Picked: A — event, tool, and shape (the signature's granularity). Alternatives: B — event and tool only. Why: an `Edit` prompt under `.claude/**` is closed as not planned by design (§23.I), and it must not absorb `Edit` prompts elsewhere. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Which prefixes are stripped? — Picked: A — leading `NAME=value` words, segments that are only assignments or `export` of assignments, `cd …` followed by `&&` or `;`, and `timeout [options] <duration>`. Alternatives: B — only env words on the command, `timeout <n>`, and `cd … &&`. Why: `F=x && python3 <<` (#4905) and `export X && python3 <<` (#5470) are the same harmless variations #5668 targets. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-30] Which script file do the tests load? — Picked: A — the `workflow-templates/.claude/` twin, with `test_template_parity` pinning the root copy. Alternatives: B — the root copy, with new tests red until the sync. Why: the `/implement-plan-claude` twin-first rule says tests of new `.claude/` behaviour read the twin. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-30] What counts as a substitution? — Picked: A — `$(` outside single quotes, excluding `$((`. Alternatives: B — also backticks. Why: #5668 names `$(…)`. Arithmetic is not command substitution, and backticks are rare in the logs. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-30] For a heredoc behind another command (`grep …; python3 - <<EOF`), which command names the family? — Picked: A — the first command word, as #5668 says. Alternatives: B — the command that receives the heredoc. Why: on the 60 issues B saves one family (21 vs 22) and misplaces #5202. A is simpler and literal. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` → `{"skip": false, "label": null, "reason": "no skip label"}`, so the security pass runs.

## References

- #5668 (this issue); #4867 (closing duplicates, the class marker); #4678 and #4858 (heredoc root causes); #4750 (classifier outages); #5124 (credential masking); #4785 / #4948 (twin-first); CLAUDE.md §23.I.
