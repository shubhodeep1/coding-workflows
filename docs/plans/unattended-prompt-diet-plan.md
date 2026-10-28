# Cut the unattended pipelines' static prompt prefix

## Summary
Stop pasting the whole of `agents.md` (about 708 KB with `agents.d/` fragments
folded in) into every unattended prompt. Paste a generated section index
instead, and let the model read the sections it needs from its sandbox copy.
This cuts the static prefix from about 750 KB (about 190k tokens) to under
60 KB per call.

## Automation (§18.E)
- Scripts: one new helper, `scripts/build_agents_index.py`, called by the
  existing prompt builders at prompt-build time. No standalone or manual
  script. Everything else extends existing scripts.
- Scheduler / entry points: the existing phase workflows that already build
  the prefix (`clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate.yml`,
  `orchestrate_clarify_respond.yml`, `orchestrate_poll.yml` through
  `scripts/orchestrate_poll_process.sh`, `validate.yml` through
  `scripts/validate_process.sh`, and `review_autofix.yml`). No new triggers.
- Long-running supervisor: none.
- DB work: none.
- `docs/scripts-pending-removal.md`: no entry, because the helper is a
  permanent part of prompt assembly rather than a single-use or long-running
  script.

## Decisions

### D1 — Default mode
- **Chosen:** `STATIC_CONTEXT_AGENTS_MODE` defaults to `index` in each phase as
  it lands.
- **Alternatives considered:** default `inline`, then flip in a final phase.
- **Why:** the user asked for the cost cut. The variable gives a one-step
  rollback per repo, and the before/after comparison after each phase is the
  quality check.

### D2 — Check-failure triage
- **Chosen:** keep its inline copy.
- **Alternatives considered:** switch it to the index too.
- **Why:** its sandbox hides agent-instruction files from the untrusted PR head
  on purpose, so the inline copy is its only safe access.

### D3 — Index granularity
- **Chosen:** all `##`/`###` headings, plus bullet-level entries for the 6
  sections over 30 KB, with line ranges.
- **Alternatives considered:** headings only (1.6 KB but far too coarse, since
  one section is 158 KB), or a full per-bullet map (about 55 KB).
- **Why:** it keeps the index at or under 15 KB while still pointing at specific
  facts.

## Context
- `scripts/build_static_context.sh` builds the prefix for clarify, plan and
  implement (`clarify.yml:1262`, `plan.yml:1625`, `implement.yml:2700`). The
  same full agents file is hand-inlined by `orchestrate.yml:697`,
  `orchestrate_clarify_respond.yml:919`,
  `scripts/orchestrate_poll_process.sh:1433-1488` (`assemble_judge_static_context`),
  `scripts/validate_process.sh:4134-4172`, `review_autofix.yml:2630-2695`
  (editor, judges and consolidator), `scripts/workflow_failure_heal_intake.sh:2340`
  and `scripts/check_failure_triage.sh:1382`.
- Measured locally on main @ 95f36e7: clarify 764,453 B, plan 764,961 B,
  implement 745,754 B. The agents file is 707,815 B of that, about 93%. The
  README trim is about 17.7 KB and the system instructions are 36 KB.
- Consumer implement runs also get coding-workflows' own `agents.md` as
  `agents_canonical.md` (`implement.yml:1770-1777`), on top of their own.
- Every agentic tool step re-reads the prefix (cache reads), so the cost
  scales with run length, not just run count.
- There is already a precedent: the review panel's
  `reviewer_static_context.txt` (`review_autofix.yml:2690`) carries a
  "=== REPOSITORY NOTES ===" pointer instead of agents.md, and
  `review_run_reviewers.sh` prefers it.
- The Claude engine also passes `unattended_system_instructions.md` as
  `--system-prompt-file` (`scripts/ai_engine.sh:604`) while stdin starts with
  the same 36 KB, so it is sent twice.
- The sandboxes already hold the repo's own `README.md`, `agents.md` and
  `agents.d/` (clarify `/source` whitelist `clarify_isolated_run.sh:126-144`;
  `codex_isolated_workspace.py snapshot-readonly`; the implement workspace).
  The consumer-run canonical file sits outside the workspace and needs
  `codex_isolated_exec.sh --include` to be visible.

## Goals
- The prefix of clarify, plan, implement, orchestrate, clarify-respond,
  orchestrate judges, validate, review editor/judges/consolidator and
  workflow-heal intake is below 60 KB in this repo when the new mode is on.
- The model can still reach every agents.md section, including pending
  `agents.d/` fragments and, in consumer runs, coding-workflows' canonical
  file.
- Each run logs `STATIC_CONTEXT_SIZE phase=<p> mode=<inline|index> bytes=<n>`.
- The Claude engine no longer sends the system instructions twice.

## Non-goals
- `check_failure_triage`: it hides agent-instruction files from `/source` on
  purpose because the PR head is untrusted, so it keeps its inline copy and
  its 256 KB cap.
- Rewriting or shortening `agents.md` itself.
- Codex-only behaviour beyond what the shared builders already do.

## Constraints
- §6: keep `pre_assembled_static.txt`, `build_static_context.sh`'s phase
  arguments and every existing log key; new names must not collide.
- §4: the new repo variable gets a default.
- §15: no new GitHub API calls.
- §18: no manual scripts; the index is generated at prompt-build time.
- §20 and §30: changelog and agents fragments for each phase.
- §27: check `wc -c` on every workflow file touched.

## Approach
1. New `scripts/build_agents_index.py` renders the agents file through
   `assemble_agents.py render` into the sandbox (or `--include`) as
   `agents.rendered.md`. It emits an index of every `##`/`###` heading and
   every bold-lead or first-level bullet in the 6 sections over 30 KB, with
   line ranges. Target index size is 15 KB or less.
2. The prefix then carries the index plus one instruction: read the listed
   line ranges of `agents.rendered.md` (and grep it) before relying on
   repo-architecture facts.
3. Repo variable `STATIC_CONTEXT_AGENTS_MODE` (`inline` | `index`) selects the
   mode, with `index` as the default (see Q2). Any failure to build the index
   falls back to `inline` and logs `STATIC_CONTEXT_SIZE ... fallback=inline`.
4. Consumer implement stages the canonical file into the sandbox with
   `--include` and indexes it under its own heading.
5. Prompt text that says everything is already loaded is updated
   (`clarify.yml:1466`, `run_plan_codex.sh:16`,
   `prompts/mode-validate-generate.txt:15`).
6. When the engine is Claude, the stdin copy of the system instructions is
   dropped.

## Phases & Merge Strategy
Each phase stands alone and leaves `inline` behaviour intact when the
variable is `inline`.

1. **Index builder and shared builder.** `build_agents_index.py`, the
   `index` mode in `build_static_context.sh` (clarify, plan, implement), the
   consumer `--include` staging, and the size log. Done: the measured prefix
   is under 60 KB in index mode and `tests/test_static_context_symlink_guard.py`
   passes in both modes. Rollback: set the variable to `inline`, or revert.
2. **Hand-built prefixes.** orchestrate, clarify-respond, orchestrate judges,
   validate and workflow-heal intake switch to the shared index helper.
   Rollback: same.
3. **Review pipeline.** The editor, judges and consolidator use the
   reviewer-style split (prefix without agents.md, plus the index). Rollback:
   same.
4. **No duplicate system prompt on the Claude engine.** Rollback: revert.

## Implementation Steps
Phase 1
1. Add `scripts/build_agents_index.py` (render, index, line ranges, size
   log) and `tests/test_build_agents_index.py`.
2. `scripts/build_static_context.sh`: read `STATIC_CONTEXT_AGENTS_MODE`
   (default `index`). In `index` mode, emit the index and the read-on-demand
   instruction in place of `emit_agents_md`, falling back to `inline` on
   any error.
3. `implement.yml:1770-1777`: stage the canonical file into the sandbox
   (`--include`) for consumer runs, and index it under its own heading.
4. `clarify.yml:1466` and `run_plan_codex.sh:16`: change the "already
   loaded" wording.
5. Pass the variable through `env:` in `clarify.yml`, `plan.yml` and
   `implement.yml`, and document it in the README variables table.

Phase 2
6. Use the index helper in `orchestrate.yml:697`,
   `orchestrate_clarify_respond.yml:919`, `assemble_judge_static_context`
   (`orchestrate_poll_process.sh:1433-1488`), `validate_process.sh:4134-4172`
   and `workflow_failure_heal_intake.sh:2340`. Update
   `prompts/mode-validate-generate.txt:15`.

Phase 3
7. `review_autofix.yml:2630-2695`: in index mode, the shared
   `pre_assembled_static.txt` carries the index instead of
   `SUPPORT_AGENTS_FILE`. The editor, judges and consolidator scripts keep
   their `cat` and get the smaller file.

Phase 4
8. `scripts/ai_engine.sh` and the prompt assemblers: when the engine is
   Claude, leave the system-instruction block out of stdin, since
   `--system-prompt-file` already carries it.

## Files & Modules
- `scripts/build_agents_index.py` [new]
- `scripts/build_static_context.sh`, `scripts/assemble_agents.py` (render
  target only, if needed)
- `.github/workflows/clarify.yml`, `plan.yml`, `implement.yml`,
  `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `review_autofix.yml`
- `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`,
  `scripts/self_heal_validation.sh`, `scripts/workflow_failure_heal_intake.sh`,
  `scripts/review_apply_fixes.sh`, `scripts/review_rb_judge.sh`,
  `scripts/review_run_judge_interim.sh`, `scripts/review_consolidate.sh`,
  `scripts/run_plan_codex.sh`, `scripts/ai_engine.sh`,
  `scripts/stage_workflow_support.sh`
- `prompts/mode-validate-generate.txt`
- `README.md` (Prompt Caching section and variables table), `agents.d/`
  fragment, `changelog.d/` fragment per phase
- Tests listed below

## Tests
- New `tests/test_build_agents_index.py`: index size budget, every heading
  present, line ranges point at the right text, fragments included.
- Update `tests/test_static_context_symlink_guard.py` (header and
  "contains agents text exactly once" asserts become mode-aware) and
  `tests/test_review_static_context_readme.py`.
- New size-guard test: the index-mode prefix of each phase stays under
  60 KB in this repo.
- After each phase merges, compare `STATIC_CONTEXT_SIZE` logs and Claude
  transcript usage on the next 5 runs of each phase against the last 5
  before it.

## Risks & Mitigations
- The model misses a fact it used to get for free, and quality drops.
  Mitigation: the index points at line ranges, the prompt says to read them,
  the per-phase rollout keeps the variable as a quick rollback, and runs are
  compared after each phase.
- A large section is indexed too coarsely. Mitigation: bullet-level index
  for the 6 sections over 30 KB.
- More tool calls offset part of the saving. ACCEPTED: reading 5–20 KB on
  demand is still far below re-reading 708 KB every step.

## Rollout
Phase by phase through the orchestrator. After each merge, the next
`@stable` release carries it to consumers. Rollback is
`STATIC_CONTEXT_AGENTS_MODE=inline` on the affected repo, no revert needed.

## References
- Session analysis, 2026-10-28 (prefix measurements above)
- Changelog fragment 8185 (the earlier self-repo duplicate-canonical incident)
- `README.md` "Prompt Caching"
