# Cut the unattended pipelines' static prompt prefix

## Summary
Stop pasting the whole of `agents.md` (about 708 KB with `agents.d/` fragments
folded in) into every unattended prompt. Paste a generated section index
instead, and let the model read the sections it needs from its sandbox copy.
This cuts the static prefix from about 750 KB (about 190k tokens) to under
75 KB per call (about 57 KB of other prefix plus an index of at most 15 KB).

## Automation (§18.E)
- Scripts: one new helper, `scripts/build_agents_index.py`, called by the
  existing prompt builders at prompt-build time. No standalone or manual
  script. Everything else extends existing scripts. The helper is added to
  every staged-support list that already carries `build_static_context.sh`
  or `assemble_agents.py` (see Implementation Steps 1, 6 and 7), so consumer
  runs get it too.
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
  facts. With the measured non-agents remainder (56,638 B for clarify,
  57,146 B for plan) that puts the clarify and plan prefixes at about 72 KB,
  which is why the size goal is 75 KB, not 60 KB.

### D4 — Which agents copy the index points at
- **Chosen:** the index and `agents.rendered.md` are always built from the
  same trusted copy the inline mode pastes today: the verified support copy
  (`SUPPORT_AGENTS_FILE`, staged from the workflow support commit) in review,
  and the copy each builder already reads elsewhere. The rendered file is
  written outside the checkout (under `RUNTIME_DIR`) and mounted read-only
  into the sandbox, never read from the PR or branch checkout. The helper
  always calls `assemble_agents.py render` with an explicit `--agents-file`
  and `--fragments-dir`, never with a checkout as `--repo-root` (whose
  default `<repo-root>/agents.d` would fold in PR-controlled fragments). In
  review, where the inline mode pastes `SUPPORT_AGENTS_FILE` with no
  fragments today, `--fragments-dir` is an empty directory under
  `RUNTIME_DIR`; elsewhere it is the fragment directory the builder already
  folds today. A missing trusted fragment source falls back to `inline`.
- **Alternatives considered:** point the model at the checkout's `agents.md`,
  as the reviewer panel does.
- **Why:** the editor, judges and consolidator take repository conventions
  from this text. A PR that edits its own `agents.md` must not change the
  rules applied to it in the same run. A role whose sandbox cannot mount the
  trusted rendered file keeps the inline copy.

### D5 — Phase 4 gate and the codex fallback
- **Chosen:** the stdin system block is dropped only inside the Claude launch
  paths, on a temporary copy of the prompt file, and only while
  `STATIC_CONTEXT_AGENTS_MODE=index`. The original prompt file is left
  untouched for the codex fallback.
- **Alternatives considered:** drop the block when the prompt is assembled.
- **Why:** `claude_run_selected` in `scripts/ai_engine.sh` runs the codex
  command on the same prompt file after a Claude exit 75
  (`"$@" < "${prompt_file}"`, about line 799), and codex has no
  `--system-prompt-file`. Removing the block at assembly time would run that
  fallback without the unattended rules or the output contract. Gating on the
  same variable keeps one rollback switch for all four phases.

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
  workflow-heal intake is below 75 KB in this repo when the new mode is on.
- The model can still reach every agents.md section, including pending
  `agents.d/` fragments and, in consumer runs, coding-workflows' canonical
  file.
- Each run logs `STATIC_CONTEXT_SIZE phase=<p> mode=<inline|index> bytes=<n>`.
- The Claude engine no longer sends the system instructions twice, and the
  codex fallback still receives them.

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
1. New `scripts/build_agents_index.py` renders the trusted agents file (D4)
   through `assemble_agents.py render` as
   `${RUNTIME_DIR}/static-context/agents.rendered.md`, outside the repository
   tree, so no commit step, conflict resolver or `files_touched` scope guard
   can see it. Each phase mounts that file read-only into its sandbox at one
   constant, run-independent path, `/support/static-context/agents.rendered.md`
   (the same `/support/` root the Claude engine already uses for its
   settings, guard and instructions). `RUNTIME_DIR` embeds the run ID, and
   `codex_isolated_exec.sh --include` today mounts at the same absolute host
   path (`codex_isolated_exec.sh:37-38`), so the include path needs a mount
   that takes an in-sandbox target (or a fixed copy under `/support/`); the
   review sandbox's support mount and a new read-only file mount in
   `clarify_isolated_run.sh` (whose `/source` copy is a whitelist and would
   drop it) use the same constant path. The index names only that constant
   path, never a host path, so the static prefix stays byte-identical across
   runs and the provider prefix cache keeps working. It emits an index of
   every `##`/`###` heading and every bold-lead or first-level bullet in the
   6 sections over 30 KB, with line ranges. Target index size is 15 KB or
   less.
2. The prefix then carries the index plus one instruction: read the listed
   line ranges of `agents.rendered.md` (and grep it) before relying on
   repo-architecture facts.
3. Repo variable `STATIC_CONTEXT_AGENTS_MODE` (`inline` | `index`) selects the
   mode, with `index` as the default (see D1). Any failure to build the index,
   a missing helper, or a caller that cannot confirm the rendered file is
   mounted into its sandbox (the builder takes the in-sandbox path as an
   argument and refuses `index` mode without it) falls back to `inline` and
   logs `STATIC_CONTEXT_SIZE ... fallback=inline reason=<r>`. "Mounted" means
   the role that runs can read it, not only that the path exists: OpenCode
   roles need a read grant for that one path (see Phase 3 step 7), and any
   role that reuses a prefix built for another sandbox must mount the file
   itself or rebuild an inline prefix.
   Every workflow whose scripts read the variable passes it through `env:`
   as `${{ vars.STATIC_CONTEXT_AGENTS_MODE || 'index' }}`, and each script
   also defaults it to `index` (§8), so `inline` reaches every phase.
4. Consumer implement stages the canonical file into the sandbox with
   `--include` and indexes it under its own heading. Like the rendered file,
   it is mounted at a constant in-sandbox path,
   `/support/static-context/agents_canonical.md` (the same in-sandbox-target
   mount or fixed copy under `/support/` as step 1), never at its
   `${RUNTIME_DIR}/agents_canonical.md` host path, and the index names only
   that constant path. If it cannot be mounted there, consumer implement
   stays `inline`.
5. Prompt text that says everything is already loaded is updated
   (`clarify.yml:1466`, `run_plan_codex.sh:16`,
   `prompts/mode-validate-generate.txt:15` and its hand-maintained source
   `prompts/_templates/mode-validate-generate.txt:7-9`).
6. When the engine is Claude and the mode is `index`, the stdin copy of the
   system instructions is dropped inside the Claude launch path only (D5).
   The prompt sentences that say the system instructions are "provided ABOVE"
   are reworded so they stay true on both engines.

## Phases & Merge Strategy
Each phase stands alone and leaves `inline` behaviour intact when the
variable is `inline`.

1. **Index builder and shared builder.** `build_agents_index.py`, the
   `index` mode in `build_static_context.sh` (clarify, plan, implement), the
   consumer `--include` staging, the staged-support entries, and the size
   log. Done: the measured prefix is under 75 KB in index mode and
   `tests/test_static_context_symlink_guard.py` passes in both modes.
   Rollback: set the variable to `inline`, or revert.
2. **Hand-built prefixes.** orchestrate, clarify-respond, orchestrate judges,
   validate and workflow-heal intake switch to the shared index helper.
   Rollback: same.
3. **Review pipeline.** The editor, judges and consolidator get the index of
   the trusted support copy (D4) in place of the inlined file. Rollback:
   same.
4. **No duplicate system prompt on the Claude engine.** Only while the mode
   is `index`, and never for the codex fallback (D5). Rollback: same.

## Implementation Steps
Phase 1
1. Add `scripts/build_agents_index.py` (render, index, line ranges, size
   log) and `tests/test_build_agents_index.py`. Stage it wherever
   `build_static_context.sh` is staged today: the support-script lists of
   `clarify.yml`, `plan.yml` and `implement.yml`, plus `docs/INVENTORY.md`.
2. `scripts/build_static_context.sh`: read `STATIC_CONTEXT_AGENTS_MODE`
   (default `index`). In `index` mode, emit the index and the read-on-demand
   instruction in place of `emit_agents_md`, falling back to `inline` on
   any error.
3. `implement.yml:1770-1777`: stage the canonical file into the sandbox
   for consumer runs at `/support/static-context/agents_canonical.md` (not
   its run-specific host path, see Approach step 4), and index it under its
   own heading. Mount
   `agents.rendered.md` read-only in the clarify, plan and implement
   sandboxes (`clarify_isolated_run.sh` gets the new read-only file mount).
   `scripts/implement_diagnose_post_codex_failure.sh` copies
   `./pre_assembled_static.txt` (about line 424) and runs its own isolated
   read-only container with no include: give both its codex path
   (`--include`) and its `claude_run` path (`AI_ENGINE_INCLUDE_PATHS`) the
   rendered file, or have it rebuild an inline prefix when it cannot.
4. `clarify.yml:1466` and `run_plan_codex.sh:16`: change the "already
   loaded" wording for agents.md.
5. Pass the variable through `env:` in `clarify.yml`, `plan.yml` and
   `implement.yml`, and document it in the README variables table.

Phase 2
6. Use the index helper in `orchestrate.yml:697`,
   `orchestrate_clarify_respond.yml:919`, `assemble_judge_static_context`
   (`orchestrate_poll_process.sh:1433-1488`), `validate_process.sh:4134-4172`
   and `workflow_failure_heal_intake.sh:2340`. Mount the rendered file into
   each of those sandboxes (heal intake today includes only `HEAL_SOURCE_DIR`
   and `HEAL_BRANCH_TIP_DIR`; add the file to both its codex include list
   `heal_isolated_args` and its Claude list `heal_include_paths`, which feeds
   `AI_ENGINE_INCLUDE_PATHS`); a phase that cannot mount it stays `inline`.
   Pass `STATIC_CONTEXT_AGENTS_MODE` through `env:` in `orchestrate.yml`,
   `orchestrate_clarify_respond.yml`, `orchestrate_poll.yml`, `validate.yml`
   and `workflow-failure-heal-intake.yml`. In that workflow two steps reach
   `scripts/workflow_failure_heal_intake.sh`: the intake step (about line
   306) and "Run heal-scope recovery scan" (about line 494), whose
   `workflow_failure_heal_recovery_scan.sh` runs the intake script itself
   (`bash "${INTAKE_SCRIPT}"`, about line 394). Pass the variable to both
   steps (or at job level for both jobs). The reporter
   `workflow_failure_heal.yml` only sends a `repository_dispatch` and never
   runs the intake script, so it needs no change. Add the helper to the
   staged-support lists of `orchestrate.yml`, `orchestrate_clarify_respond.yml`,
   `orchestrate_poll.yml` and `validate.yml` (its `required_scripts`
   manifest); the intake workflow runs `scripts/` from its trusted checkout
   and stages nothing, so the helper only has to exist in that checkout. Update `prompts/mode-validate-generate.txt:15` and
   `prompts/_templates/mode-validate-generate.txt:7-9`.

Phase 3
7. `review_autofix.yml:2630-2695`: in index mode, the shared
   `pre_assembled_static.txt` carries the index of `SUPPORT_AGENTS_FILE`
   (the verified support copy, D4) instead of the file itself. The rendered
   file goes under `RUNTIME_DIR` and is mounted read-only by
   `review_untrusted_sandbox.sh` for the editor, judges and consolidator at
   the constant path `/support/static-context/agents.rendered.md` (Approach
   step 1). The prompt names only that constant path, never the
   `RUNTIME_DIR` host path, and says the checkout's `agents.md` is PR data.
   Add the helper and `assemble_agents.py` (which review does not stage
   today) to `REQUIRED_BOOTSTRAP_SCRIPTS` in
   `scripts/stage_workflow_support.sh` and `REVIEW_PREFLIGHT_REQUIRED_SUPPORT_SCRIPTS`
   in `review_autofix.yml`. `scripts/write_opencode_config.sh` defines no
   `external_directory` permission for the reviewer roles, so either add a
   read grant scoped to the single rendered-file path or mount the file
   inside the root those roles already read; the index mode is used only
   after the role's read of that path is shown to work, else `inline`. Pass
   `STATIC_CONTEXT_AGENTS_MODE` through `env:` in `review_autofix.yml`. The
   editor, judges and consolidator scripts keep their `cat` and get the
   smaller file.

Phase 4
8. The prompt assemblers wrap the system-instruction block in fixed begin
   and end markers. In every Claude launch path that passes
   `--system-prompt-file` (`claude_run` in `scripts/ai_engine.sh`,
   `clarify_isolated_run.sh`, `review_untrusted_sandbox.sh`), when the mode is
   `index`, feed Claude a temporary copy of the prompt with that block removed.
   The original prompt file is unchanged, so the exit-75 codex fallback still
   gets the block. The strip matches only a block that starts at byte 0 with
   an exact full-line BEGIN marker and ends at the next exact full-line END
   marker; if either is missing it leaves the copy unchanged and logs the
   reason, so marker-like text in the untrusted dynamic part of the prompt
   can never remove trusted text. Reword the "system instructions ... provided ABOVE"
   sentences in `run_plan_codex.sh:15-17` and both `mode-validate-generate.txt`
   copies so they are true on both engines.

## Files & Modules
- `scripts/build_agents_index.py` [new]
- `scripts/build_static_context.sh`, `scripts/assemble_agents.py` (render
  target only, if needed)
- `.github/workflows/clarify.yml`, `plan.yml`, `implement.yml`,
  `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `orchestrate_poll.yml`,
  `validate.yml`, `review_autofix.yml` (staged-support lists and `env:`)
- `scripts/clarify_isolated_run.sh`, `scripts/codex_isolated_exec.sh` callers,
  `scripts/review_untrusted_sandbox.sh` (read-only mount of the rendered file)
- `scripts/implement_diagnose_post_codex_failure.sh` (mount for the reused
  implement prefix), `scripts/write_opencode_config.sh` (read grant for the
  rendered file)
- `.github/workflows/workflow-failure-heal-intake.yml` (`env:` pass-through)
- `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`,
  `scripts/workflow_failure_heal_intake.sh`,
  `scripts/review_apply_fixes.sh`, `scripts/review_rb_judge.sh`,
  `scripts/review_run_judge_interim.sh`, `scripts/review_consolidate.sh`,
  `scripts/run_plan_codex.sh`, `scripts/ai_engine.sh`,
  `scripts/stage_workflow_support.sh`
- `prompts/mode-validate-generate.txt`, `prompts/_templates/mode-validate-generate.txt`
- `docs/INVENTORY.md`
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
  75 KB in this repo.
- Staging test: every workflow that stages `build_static_context.sh` also
  stages `build_agents_index.py`.
- Trust test: in review index mode the index and rendered file come from
  `SUPPORT_AGENTS_FILE`, never from the checkout, and the rendered file is not
  inside the repository tree (so no commit helper can stage it).
- Fallback tests: `index` mode without a confirmed sandbox mount, or without
  the helper, produces the inline prefix and `fallback=inline`.
- Phase 4 test: after a forced Claude exit 75, the codex command's stdin
  still contains the system-instruction block; with the mode `inline`, the
  Claude stdin keeps it too. Marker-shaped lines injected into the dynamic
  part of the prompt leave the trusted block intact.
- Render-source test: the helper never renders with a checkout as
  `--repo-root`, and an `agents.d/` fragment added in the review checkout
  does not appear in the review index or rendered file.
- Rollback test: every workflow step whose scripts read
  `STATIC_CONTEXT_AGENTS_MODE`, directly or through a script they run (for
  example the heal-scope recovery scan, which runs the heal intake), passes
  it through `env:`, checked per step rather than per workflow.
- Cache-stability test: the index text is byte-identical for two runs with
  different `RUNTIME_DIR` values, and names only
  `/support/static-context/agents.rendered.md` (plus
  `/support/static-context/agents_canonical.md` in consumer implement).
- Diagnose test: with the implement prefix in `index` mode, the diagnose
  sandbox can read the rendered file or gets an inline prefix.
- After each phase merges, compare `STATIC_CONTEXT_SIZE` logs and Claude
  transcript usage on the next 5 runs of each phase against the last 5
  before it.

## Risks & Mitigations
- The model misses a fact it used to get for free, and quality drops.
  Mitigation: the index points at line ranges, the prompt says to read them,
  the per-phase rollout keeps the variable as a quick rollback, and runs are
  compared after each phase.
- An index points at a file the sandbox cannot open, so the content is lost
  silently. Mitigation: the builder refuses `index` mode unless its caller
  passes the confirmed in-sandbox path, and falls back to `inline`. A path
  that exists but the role's tool permissions refuse (OpenCode roles have no
  `external_directory` grant today) counts as not mounted, and a prefix
  reused by another role (implement diagnose) needs its own mount.
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
