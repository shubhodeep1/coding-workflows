# Let a spot-fix reissue declare the new files its follow-up must create

Source issue: shubhodeep1/coding-workflows#4665 (https://github.com/shubhodeep1/coding-workflows/issues/4665)
Base branch: stable
Security pass: skip (ai:workflow-heal: automation-produced issue)

## Summary

A review-blocked `close_and_reissue` with `reissue_mode: spot-fix` writes a
`files_touched` allowlist that can only hold files the judge cited or files the
closed PR already changed, so a follow-up that must *create* a file (a
changelog fragment, a new test fixture) is refused by the implement-time scope
guard and latched `ai:scope-blocked`. Add a narrow, validated
`new_output_paths` field to the judge contract and union it into the
allowlist.

## Context

- Issue #4664 (reissue of #4605 / PR #4607) was refused by the scope guard with
  five out-of-scope staged paths: `changelog.d/4605-security-issue-base-binding.md`
  and four new `tests/fixtures/integration_ref_resolver/*.json` files. None of
  them existed at the closed PR head, so neither union source could list them.
- The allowlist is built in the `close_and_reissue)` arm of
  `scripts/review_rb_judge.sh`: judge-cited `remaining_issues[].file` entries
  (each must exist at the closed head, `review_rb_judge.sh:2555-2587`), then the
  closed PR's changed files that still exist at its head
  (`review_rb_judge.sh:2620-2660`, log `REISSUE_FILES_TOUCHED_UNION`), then the
  footer (`review_rb_judge.sh:2757-2763`). Line numbers are for `stable` at
  d58d7bc; `main` carries the same logic.
- `scripts/files_touched_scope_guard.py:201-220` rejects staged paths outside a
  non-empty allowlist; its matcher treats `*`, `?`, `[` as globs, a trailing
  `/` as a directory prefix, and a bare entry as "this path or anything beneath
  it". `.github/workflows/implement.yml:3498-3518` refuses the commit and
  `implement.yml:1757-1771` blocks redispatch while `ai:scope-blocked` remains.
- The judge contract lives in `prompts/mode-judge-review-blocked.txt` and its
  include-based twin `prompts/_templates/mode-judge-review-blocked.txt`
  (agents.md "Prompt assembly note"); both bodies must change together.
- The issue's fix constraints: minimal and backward compatible, never weaken
  the guard, never infer a directory exemption from prose.

## Goals

- The judge output schema carries an optional `new_output_paths` string array,
  documented in both prompt files, for exact repo-relative paths of new files
  the spot-fix follow-up must create.
- In a spot-fix reissue, each declared path that passes validation is appended
  to `files_touched` after the judge files and the closed-PR files; a path that
  fails validation is skipped, never a reason to fall back to `redo`.
- Validation rejects: anything `_rb_valid_repo_relative_path` rejects; a path
  with a glob metacharacter (`*`, `?`, `[`); a trailing `/`; a path that
  already exists at the closed head (file or directory); duplicates; and
  entries beyond the first 10.
- One `REISSUE_FILES_TOUCHED_NEW_OUTPUTS pr=<n> declared=<d> added=<a> skipped=<s> total=<t>`
  line is logged whenever the judge declared any entry; each skip logs its
  reason without echoing an unvalidated path.
- A reissue whose judge declares no `new_output_paths` produces a byte-identical
  issue body and the same log lines as today.
- The scope guard still blocks an unrelated staged path when the allowlist
  carries the new fixture and changelog entries.

## Non-goals

- No change to `scripts/files_touched_scope_guard.py`, its exemptions, or
  `implement.yml`; the guard keeps behaving as designed.
- `redo` reissues keep carrying no allowlist.
- Issue #4664 itself is not edited here (see AD-1): auditing its five paths,
  editing its `files_touched`, and removing `ai:scope-blocked` is the existing
  human-gated procedure.
- No new env var or repo var.

## Constraints

- §5 minimal change set; §6 no rename or removal. `REISSUE_FILES_TOUCHED_UNION`
  and its fields stay unchanged; `new_output_paths` and
  `REISSUE_FILES_TOUCHED_NEW_OUTPUTS` are new identifiers, checked unique in
  the repo.
- §7: README `REISSUE_PRESERVE_BASELINE_ENABLED` row and agents.md reissue
  paragraph describe the allowlist sources, so both are updated; the new log
  prefix is registered in both agents.md prefix lists.
- §9: the script uses 2-space indentation inside that arm today; keep the
  surrounding style. Tests use tabs.
- §15: no new GitHub API call; validation uses the local checkout
  (`git cat-file` against the closed head SHA already verified in the arm).
- §20: behaviour visible to operators (fewer `ai:scope-blocked` latches, a new
  log line), so one `changelog.d/4665-…` fragment.
- §27: no workflow file grows.
- Security: judge output is untrusted model output derived from PR content.
  Only paths that pass the validator are ever printed or written to the footer.

## Approach

Extend the existing spot-fix allowlist block in `review_rb_judge.sh` with a
third union source, placed right after the closed-PR union and behind the same
gate (effective mode `spot-fix`, baseline branch pushed, judge files present):

1. Read `.new_output_paths` with `jq`, emitting each string entry that has no
   newline or carriage return; any other element becomes an empty line, which
   the validator rejects. A missing or non-array field declares nothing.
2. For each entry, in order: stop adding after 10 accepted-or-rejected
   declarations (`over_cap`); reject empty/unsafe paths (`invalid_path`), glob
   metacharacters (`glob`), a trailing `/` (`directory`), a path present at the
   closed head (`exists_at_head`, via `git cat-file -e <sha>:<path>`), and a
   path already in the list (`duplicate`); otherwise append it.
3. Emit the summary log line when at least one entry was declared.

Alternatives considered: a per-finding `new_files` list inside
`remaining_issues[]` (AD-2, rejected: new files often belong to no single
finding, such as a changelog fragment); falling back to `redo` on an invalid
entry (AD-3, rejected: it throws away the preserved baseline for a metadata
problem, unlike the closed-PR union, which skips); widening the guard's
exemptions (forbidden by the issue).

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
prompt contract, the script change, and their tests must land together, since
the prompt field is useless without the script and the script path is dead
without the prompt.

1. **Phase 1 — declare and union new output paths.**
   - Files: `prompts/mode-judge-review-blocked.txt`,
     `prompts/_templates/mode-judge-review-blocked.txt`,
     `scripts/review_rb_judge.sh`,
     `tests/test_review_rb_judge_label_propagation.py`,
     `tests/test_files_touched_scope_guard.py`,
     `tests/test_orchestrate_poll_workflow_contract.py`, `README.md`,
     `agents.md`, `changelog.d/4665-reissue-new-output-paths.md` [new].
   - Done: the new tests pass, the existing reissue, scope-guard, prompt, and
     contract tests pass unchanged, and a reissue without the field is
     byte-identical.
   - Rollback: revert the phase PR; judges that emit the field are then ignored
     (unknown JSON keys are not read), so the revert is safe.

## Implementation Steps

Phase 1:

1. `prompts/mode-judge-review-blocked.txt` and
   `prompts/_templates/mode-judge-review-blocked.txt`: add
   `"new_output_paths": ["<repo-relative path of a new file the follow-up must create>"]`
   to the schema after `remaining_issues`, plus rules: only with
   `close_and_reissue` and `reissue_mode: "spot-fix"`; exact file paths that do
   not exist at the closed PR head and that the `new_issue.body` explicitly
   requires (a `changelog.d/` fragment, a new test or fixture file); no
   directories, globs, or trailing `/`; at most 10; files that already exist
   go in `remaining_issues[]`; `[]` when none.
2. `scripts/review_rb_judge.sh`: after the `REISSUE_FILES_TOUCHED_UNION` block,
   add the new-output block described in Approach, with a comment citing
   #4664 / #4665.
3. `tests/test_review_rb_judge_label_propagation.py`: extend the prompt-schema
   test; add a spot-fix regression that declares a new fixture and a changelog
   fragment and asserts the footer order and log line; add a rejection test
   covering `../x`, a glob, a trailing `/`, a path that exists at head, a
   duplicate, a non-string entry, and the cap; add a test that the field is
   ignored for `redo`; assert the prefix is registered in agents.md.
4. `tests/test_files_touched_scope_guard.py`: a review-blocked footer whose
   `files_touched` carries the new fixture and fragment lets both commit and
   still blocks an unrelated staged path.
5. `tests/test_orchestrate_poll_workflow_contract.py`: add the new prefix to
   the registered REISSUE prefix list.
6. `README.md` (`REISSUE_PRESERVE_BASELINE_ENABLED` row) and `agents.md`
   (reissue paragraph and both log-prefix lists): document the third source.
7. `changelog.d/4665-reissue-new-output-paths.md` [new], section `fixed`.

## Files & Modules

- `prompts/mode-judge-review-blocked.txt`
- `prompts/_templates/mode-judge-review-blocked.txt`
- `scripts/review_rb_judge.sh`
- `tests/test_review_rb_judge_label_propagation.py`
- `tests/test_files_touched_scope_guard.py`
- `tests/test_orchestrate_poll_workflow_contract.py`
- `README.md`
- `agents.md`
- `changelog.d/4665-reissue-new-output-paths.md` [new]

## Tests

- Unit/behavioural (the existing extract-and-run harness of the real
  `close_and_reissue)` arm with a mock `gh` and a real git repo): new
  acceptance, rejection, redo-ignored, and unchanged-without-field cases.
- Scope guard: parser + matcher regression on a review-blocked footer.
- Static: prompt schema text in both prompt files; prefix registration.
- Existing suites to re-run: `test_review_rb_judge_label_propagation.py`,
  `test_review_rb_judge_reissue_baseline.py`,
  `test_files_touched_scope_guard.py`,
  `test_orchestrate_poll_workflow_contract.py`,
  `test_review_rb_judge_reference_staging.py`,
  `test_render_prompt_review_judge_modes.py`,
  `test_render_prompt_foundation.py`, plus `bash -n` and shellcheck on the
  script where available.

## Risks & Mitigations

- A declared path could later be created as a directory, and the guard's bare
  entry matches anything beneath it. ACCEPTED — the entry must not exist at the
  closed head, cannot contain globs or a trailing `/`, is capped at 10, and the
  prompt asks for exact file paths; the exposure is one brand-new subtree the
  judge named, not an existing directory.
- A prompt-injected PR could steer the judge to declare paths. Mitigation: the
  same trust level as `remaining_issues[].file` today, plus the cap and the
  must-not-exist rule, so no existing file can be added this way.
- Judges on the old prompt never emit the field. Mitigation: absent field keeps
  today's behaviour exactly.

## Rollout

Ships on `stable` as a hotfix through this project's final PR, and reaches
`main` through `forward-merge-stable-to-main.yml`. Consumer repos pick it up
with their next `@stable` sync. No flag: the field is inert until a judge emits
it, and `REISSUE_PRESERVE_BASELINE_ENABLED=false` still disables the whole
spot-fix path.

## References

- Issue #4665 (this heal), source escalation #4664, original #4605 / PR #4607.
- Prior union fix: binance-blessings#294, README `REISSUE_PRESERVE_BASELINE_ENABLED`.
- `docs/completed/judge-loop-and-reissue-plan.md` (Phase E spot-fix design).

## Auto-decisions

- AD-1 [plan, 2026-09-27] Should this project also repair #4664 (edit its `files_touched`, remove `ai:scope-blocked`)? — Picked: A — no; leave it to the existing human-gated procedure and say so on the issue. Alternatives: B — edit #4664's body and labels from this session. Why: the issue calls that procedure human-gated, and editing another issue's scope allowlist is outside this code fix. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-27] Where does the judge declare new files? — Picked: A — a top-level `new_output_paths` string array. Alternatives: B — a `new_files` list on each `remaining_issues[]` entry. Why: a changelog fragment or shared fixture belongs to no single finding, and a flat list is simpler to validate. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What happens to an invalid declared path? — Picked: A — skip it, count it, log the reason, keep spot-fix. Alternatives: B — fall back to `redo` like an invalid `remaining_issues[].file`. Why: matches the closed-PR union's skip rule and keeps the preserved baseline; the worst case is today's behaviour. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] May a declared path already exist at the closed head? — Picked: A — no; it must be absent (neither file nor directory), and globs and trailing `/` are rejected. Alternatives: B — allow existing files too. Why: existing files belong in `remaining_issues[]` with line anchors, and absence rules out any existing-directory exemption. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Cap the number of declared paths? — Picked: A — a fixed cap of 10, excess entries skipped as `over_cap`. Alternatives: B — no cap; C — a new repo var. Why: bounds a runaway judge without a new configuration surface (§4, §5). Applied in: phase 1 PR. Status: pending review
