# Consumer sync takes hook and settings files from the owner-reviewed `.claude/` tree, not the twin

Source issue: shubhodeep1/coding-workflows#5607 (https://github.com/shubhodeep1/coding-workflows/issues/5607)
Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
Security pass: skip (ai:security: automation-produced issue)

## Summary

The consumer updater (`.github/workflows/update_workflows.yml`, step "Sync .claude/ assets from upstream") copies every file of the `stable` tag's `workflow-templates/.claude/` twin tree into consumer repos, so a hook or settings twin that reached `main` while its owner-only `.claude/` sync PR is still pending ships to every consumer unreviewed. This plan makes the sync take guard paths (`hooks/**`, `settings.json`, `settings.local.json`) from the same `stable` commit's `.claude/` tree, and keep the consumer's existing guard file when the twin and the `.claude/` copy differ.

## Context

- Security finding `unapproved-guard-twin-reaches-consumers` (#5607, A01:2021, high), opened by the security audit of the #4785 project (`Refs #3576`). Location `.github/workflows/ci.yml:248`: a push to `stable` that `main` already contains (a promotion) skips the twin sync-state check, so the check cannot stop a pending guard twin from reaching `stable`.
- Twin-first (#4785, CLAUDE.md §28.C): sessions edit only `workflow-templates/.claude/**`; `claude-twin-sync.yml` copies twins into `.claude/**`. A sync PR that touches a guard path is merged only by the repository owner (`GUARD_PATH_PREFIXES` / `GUARD_PATH_FILES` in `scripts/claude_twin_sync.py`). Until the owner merges it, `main`'s guard twin can be ahead of `main`'s `.claude/` copy, and promotion carries both to `stable`.
- The consumer updater fetches `refs/tags/stable` with a sparse checkout of `workflow-templates` and `scripts` only, and mirrors `workflow-templates/.claude/` byte for byte (whitespace-tolerant change detection). `/seed-repo` (twin `workflow-templates/.claude/commands/seed-repo.md`) copies the same twin tree into a new consumer.
- Release (`mark-stable.yml`, `test-and-mark-stable.yml`, `promote-main-to-stable.yml`) only moves the `stable` tag/branch to a tested `main` commit. It copies no `.claude/` asset; the sourcing happens entirely at consumer sync and seed.
- Today every guard twin on the base branch equals its `.claude/` copy (7 files checked), so the change has no effect on the next sync; it only matters while a guard sync PR is pending.

## Script and scheduler wiring (§18.E)

- **Scripts:** none introduced. The change extends the existing "Sync .claude/ assets from upstream" step and the upstream fetch step of `.github/workflows/update_workflows.yml`.
- **Entry point:** `.github/workflows/update_workflows.yml`, called from consumers' `ai-update-workflows.yml` (daily cron `0 4 * * *`, `repository_dispatch` `coding-workflows-stable-released`, `workflow_dispatch`). No new trigger.
- **Supervisor:** none required (§18.C).
- **DB work:** none (§18.D, §10 not touched).
- **Removal registry (§18.F):** no entry; nothing single-use or long-running is added.

## Goals

- For every twin file that is a guard path, the consumer sync installs the file from the `stable` commit's `.claude/` tree, never from `workflow-templates/.claude/`.
- When the guard twin and its `.claude/` copy differ (bytes) and the consumer already has the file, the consumer's file is left unchanged and a `::warning::` names it.
- When the `.claude/` copy is missing at the `stable` commit, nothing is installed for that path (warning), whether or not the consumer has the file.
- Non-guard twin files sync exactly as before.
- `/seed-repo`'s twin says the same: guard files come from the release's `.claude/` tree, and a guard whose twin differs is not seeded from the twin.

## Non-goals

- No change to `ci.yml`'s promotion skip, the release workflows, `claude-twin-sync.yml`, or `scripts/claude_twin_sync.py`. Sibling findings #5608 and #5609 cover those files.
- No deletion of consumer files, and no change to the non-guard sync semantics (upstream wins, consumer-local extras kept, orphans kept).
- No edit to `.claude/commands/seed-repo.md` itself (twin-first, §28.C); the twin sync copies it.

## Constraints

- §1 security first; §5 minimal change (extend the existing step); §6 no rename of step names, ids, outputs (`claude_changed`, `claude_has_changes`) or `/tmp/claude_changed_files.txt`; new shell names must not collide with existing ones in the step.
- §9 YAML stays 2-space; §27 `update_workflows.yml` is ~42 KB, far under 480,000 bytes.
- §14/§20: the change reaches consumers on the next `@stable` sync, so a `changelog.d/5607-*.md` fragment (security) is required.
- §28.C twin-first: the seed command is edited only in `workflow-templates/.claude/commands/seed-repo.md`.
- §15: no GitHub API calls added (the sparse checkout just includes one more directory).

## Approach

1. Fetch step: add `.claude` to the upstream sparse checkout (`git sparse-checkout set "${TEMPLATES_DIR}" "${SCRIPTS_DIR}" ".claude"`), so the same `stable` commit's `.claude/` tree is on disk next to the twin.
2. Sync step: inside the existing loop, when `rel` matches `hooks/*`, `settings.json`, or `settings.local.json` (the same set as `GUARD_PATH_PREFIXES` / `GUARD_PATH_FILES`, pinned by a parity test):
   - source = `<upstream root>/.claude/<rel>`;
   - source missing → warning, skip;
   - `cmp -s twin source` fails (twin differs) and the consumer file exists → warning, keep the consumer file;
   - otherwise copy from the source with the existing change detection.
   The loop still enumerates the twin tree, so the file set does not change.
3. Seed twin: reword the `.claude/` bullet of step 4 and the "byte-identical" rule to the same rule.

Alternatives: a new `claude_twin_sync.py` subcommand called from the consumer job (rejected: a new CLI surface for ~15 lines of shell, and the consumer's `@stable` workflow would depend on the script's version); a release gate that refuses to tag while a guard twin differs (rejected, AD-2: release sources nothing, and blocking every release while an owner review is pending stalls unrelated fixes).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — guard assets from the `.claude/` tree at consumer sync and seed.** Files: `.github/workflows/update_workflows.yml`, `workflow-templates/.claude/commands/seed-repo.md`, `tests/test_update_workflows_guardrails.py`, `agents.md`, `README.md`, `changelog.d/5607-consumer-guard-source.md`. Done when: the extracted sync step, run against a fake upstream checkout, installs guard files from `.claude/`, keeps the consumer file when the twin differs, skips a guard missing from `.claude/`, and syncs non-guard files as before; the parity test ties the shell pattern to `claude_twin_sync.py`'s guard constants; existing updater and changelog tests pass. Rollback: revert the phase PR; the sync returns to mirroring the twin tree.

## Implementation Steps

1. `update_workflows.yml` fetch step: add `".claude"` to `git sparse-checkout set`, update the comment.
2. `update_workflows.yml` "Sync .claude/ assets from upstream": define the `.claude/` source dir from `UPSTREAM_DIR/..`, add the guard branch in the loop, keep outputs unchanged, and update the header comment.
3. `workflow-templates/.claude/commands/seed-repo.md`: state the guard-source rule in step 4 and the Rules list.
4. Tests in `tests/test_update_workflows_guardrails.py`: update the sparse-checkout assertion; add a behavioural test that extracts the step's `run:` body and runs it in a temp dir (guard equal → copied from `.claude/`; guard twin differs + consumer has file → kept; guard twin differs + consumer lacks file → `.claude/` copy installed; guard missing from `.claude/` → skipped; non-guard → copied from twin); add the pattern parity test.
5. Docs: `agents.md` "Claude twin sync" gets a "Consumer sync" bullet; `README.md` hooks note gets one sentence; changelog fragment.

## Files & Modules

- `.github/workflows/update_workflows.yml`
- `workflow-templates/.claude/commands/seed-repo.md`
- `tests/test_update_workflows_guardrails.py`
- `agents.md`, `README.md`
- `changelog.d/5607-consumer-guard-source.md` [new]

## Tests

- Unit/behavioural: the new tests above (bash run of the real step body with `GITHUB_OUTPUT` pointed at a temp file).
- Existing: `tests/test_update_workflows_guardrails.py`, `tests/test_changelog_fragment_contract.py`, `tests/test_workflow_file_size_limit.py`, the twin sync-state check `scripts/claude_twin_sync.py check --base origin/<base> --head HEAD`, `actionlint`/YAML parse if available.
- End to end: the next consumer sync after release; with guards equal, it reports the same changes as before.

## Risks & Mitigations

- The guard list in shell drifts from `claude_twin_sync.py` → parity test.
- The sparse checkout of `.claude` fails or is absent on an old tag → every guard is skipped with a warning (fail safe: consumers keep what they have).
- A consumer keeps an old guard while the twin differs → intended; the warning names each kept file, and the next release after the owner merges the sync PR brings them back in line.
- Whitespace-only twin/`.claude` differences count as "differ" (byte compare) → ACCEPTED, safer direction.

## Rollout

Ships on the project branch, then with #4804 into `main`, then to consumers with the next `stable` release (§14). No flag; revert the PR to roll back.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which sync surfaces does the fix cover? — Picked: A — the consumer updater and the `/seed-repo` twin. Alternatives: B — the updater only; C — also add a release gate. Why: both copy the stable twin tree into consumers; seed is edited twin-first at no extra cost (§1). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What changes at release? — Picked: A — nothing; release sources no `.claude/` asset, and the consumer side reads the tagged commit's `.claude/` tree. Alternatives: B — fail the release while any guard twin differs from `.claude/`; C — rewrite the twin at release. Why: B stalls unrelated releases behind an owner review, C writes to protected history (§5). Applied in: no code change. Status: pending review
- AD-3 [plan, 2026-09-30] A guard twin differs from `.claude/` and the consumer lacks the file: what is installed? — Picked: A — the `.claude/` copy from the same commit (owner-reviewed). Alternatives: B — nothing. Why: follows the recommendation ("source guard assets from the .claude tree") and never leaves a new consumer without a reviewed guard. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] The `.claude/` copy is missing at the stable commit (a new guard still pending sync): what happens? — Picked: A — skip with a warning; never install the twin. Alternatives: B — install the twin. Why: B is the finding's exploit path (§1). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Where does the rule live? — Picked: A — in the existing shell step, with a parity test against `claude_twin_sync.py`'s constants. Alternatives: B — a new `claude_twin_sync.py` subcommand. Why: smallest change, no new CLI surface (§5). Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py --repo shubhodeep1/coding-workflows --issue 5607` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Sibling findings from the same audit touching nearby code: #5608 (`ci.yml:280`), #5609 (`claude_twin_sync.py:156`), #5610 (`review_autofix.yml:546`). Out of scope here.

## References

- #5607, #4785 (twin-first), #5246 / #5247 (guard provenance), #3576 (audit tracker), final PR #4804.
