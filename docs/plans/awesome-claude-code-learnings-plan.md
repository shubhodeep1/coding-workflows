# Apply Learnings from `hesreallyhim/awesome-claude-code`

## Summary

Adopt nine high-leverage patterns from the curated
`hesreallyhim/awesome-claude-code` repo into this project's prompts,
scripts, docs, helpers, and CI. Companion doc
`docs/awesome-cc-future-improvements.md` (shipped in the same PR)
enumerates deferred and explicitly-excluded items so future revisit is
cheap. Plan is strictly additive: no rename or removal of any existing
identifier (CLAUDE.md §6), no MongoDB / contract impact (§10), and no
consumer-repo propagation (§14).

## Context

The trigger is the user's question "what improvements and learnings we
can incorporate from <https://github.com/hesreallyhim/awesome-claude-code>".
A research subagent surveyed the upstream repo on 2026-05-16 and
returned this inventory:

- **`THE_RESOURCES_TABLE.csv`** — 226 catalog rows × 20 columns; columns
  include `ID`, `Display Name`, `Category`, `Sub-Category`, `Active`,
  `Stale`, `Removed From Origin`, `Latest Release`. Twelve top-level
  categories (Slash-Commands 59, Tooling 51, Workflows & Knowledge
  Guides 37, CLAUDE.md Files 28, Agent Skills 19, Hooks 13, Status
  Lines 7, Alternative Clients 5, Output Styles 4, Official
  Documentation 3, plus minor categories).
- **`README_ALTERNATIVES/`** — 41 generated `.md` files (3 style
  variants × 9 flat category cuts × 4 sort orders). All carry an
  auto-generated banner.
- **`resources/`** — vendored copies of catalog entries (24 example
  `CLAUDE.md` files, 22 slash commands like `pr-review`,
  `fix-github-issue`, `create-prp`, `commit`, `optimize`, `release`,
  `todo`, plus 8 ready-to-copy `.yml` GitHub Actions templates under
  `Claude-Code-GitHub-Actions/`).
- **`templates/`** — `README_*.template.md`, `footer.template.md`,
  `categories.yaml` (single source of truth for categories /
  sub-categories / icon / prefix), `announcements.yaml`,
  `resource-overrides.yaml` (per-resource field locks).
- **`tools/readme_tree/`** — utility that keeps embedded directory
  listings in `docs/README-GENERATION.md` in sync via
  `<!-- TREE:START -->` / `<!-- TREE:END -->` markers; supports
  `--check` for CI drift detection.
- **`scripts/`** — Python package with `validation/`, `resources/`,
  `readme/generators/`, `ids/`, `categories/`, `maintenance/`,
  `badges/`, `ticker/`, `testing/`, and `utils/` subdirectories.
- **`data/`** — `repo-ticker.csv` snapshots.
- **`docs/`** — `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `COOLDOWN.md`,
  `HOW_IT_WORKS.md` (label state machine), `README-GENERATION.md`.
- **`.claude/commands/`** — exactly one command,
  `evaluate-repository.md` (≈150-line static-analysis prompt scoring
  1–10 on Code Quality / Security / Documentation / Functionality /
  Hygiene with a Claude-Code-specific checklist for hooks, persistent
  state, and implicit execution). No `.claude/agents/`, no
  `.claude/hooks/`.

Eleven patterns were rated high-leverage; six items were rated weak /
inapplicable to a workflow-automation repo. The user (in the Q1–Q4
clarification batch, 2026-05-16) selected:

- **Q1 → A** — slug `awesome-claude-code-learnings`.
- **Q2 → A** — dual-doc shape: implementation plan plus companion
  future-improvements doc.
- **Q3 → A+B+C+D+E+F+G+H+I** — adopt every high-leverage theme.
- **Q4 → A+B+C+D+E+F** — exclude SVG / ticker presentation, CSV
  catalog cloning, generic `pr-review.md`, consumer-repo propagation,
  cross-repo cooldown, and any §6-breaking rename.

Closest precedent in this repo: `docs/plans/apply-ai-tools-learnings-plan.md`
— same "borrow mechanisms from an external system, drop the parts that
conflict with our values" template, same dual-doc shape, same
phase-per-theme commit grouping. This plan re-uses that precedent's
structure verbatim where applicable.

CLAUDE.md sections that bind this work (cited verbatim where they
constrain the design):

- **§5 Minimal Change Set** — "Do NOT change formats, types, or
  unrelated logic. Extend existing mechanisms — never compete with
  them." Every adoption extends an existing surface or adds a small
  new helper alongside existing code.
- **§6 Backward Compatibility / Naming Immutability** — no identifier
  is renamed. Theme E (stable-ID convention) is additive: the existing
  `make_record_id` helper at `scripts/ai_memory_lib.py:480` is the
  canonical implementation and is pinned by a new contract test;
  nothing is renamed. Theme G (utility helpers) introduces new
  identifiers that did not previously exist.
- **§9 Code Style** — Makefile recipe bodies use literal TAB
  indentation; YAML files use 2-space indentation.
- **§13 Repository Hygiene** — no writes into `.git/**`.
- **§14 Consumer Repo Registry** — per Q4-D, no `workflow-templates/`
  edits, no `.github/ai/consumer_repos.json` change, no
  `repository_dispatch` triggered.
- **§15 GitHub API Call Hygiene** — zero new `gh api` / `gh_retry` /
  `curl` calls. Phase 4's informal-detector operates on issue body
  text already supplied by the existing clarify-phase wrappers.

## Goals

Each goal is falsifiable by re-reading the target file after the
corresponding phase lands.

1. **Reviewer / judge curator rubric (Theme A).**
   `prompts/review-reviewer-checklist.txt` gains an 8th lens heading
   for Claude-Code-specific implicit-execution / trust-boundary risks
   (hooks, MCP servers, watcher loops, declared-vs-effective capability
   gaps). `prompts/mode-judge.txt` gains two evaluation criteria covering
   the same risk surface. Source:
   `awesome-claude-code/.claude/commands/evaluate-repository.md:1-150`.
2. **Model-catalog drift check (Theme B).**
   `scripts/codex_model_catalog.json` is the source of truth for a new
   generated `docs/codex-model-reference.md`. A new top-level `Makefile`
   ships `generate` and `generate-check` targets. `make generate-check`
   runs in `.github/workflows/ci.yml` and fails when the doc has
   drifted.
3. **Doc-tree drift check (Theme C).**
   `<!-- TREE:START id=<marker> --> ... <!-- TREE:END id=<marker> -->`
   markers wrap embedded directory listings in `agents.md`. A new
   `tools/repo_tree/update_repo_tree.py` updates and verifies them.
   Wired into the same `make generate-check` CI step.
4. **Informal-issue detector (Theme D).**
   `scripts/clarify_informal_detect.py` scores 0.0–1.0 on five signals
   (`template_paste`, `no_ask`, `no_acceptance`, `pure_log_dump`,
   `link_only`) and emits `CLARIFY_INFORMAL_SCORE: <float>; signals=<csv>`
   to stdout. Three fixtures under `tests/fixtures/informal_issues/`
   pin the scoring behavior. `prompts/mode-clarify.txt` gains an
   advisory paragraph instructing the clarifier to treat the score as
   informational. The detector is NOT wired into the clarify wrapper
   in this PR (advisory only; wiring is deferred to a follow-up).
5. **Stable-ID convention documented and pinned (Theme E).**
   `agents.md` documents `make_record_id` (`scripts/ai_memory_lib.py:480`,
   format `<prefix>_<YYYYMMDDHHMMSS>_<10hex>`) as the canonical ID
   format for any new ID surface. New contract test
   `tests/test_record_id_format_contract.py` pins the regex.
6. **Overrides-with-auto-locking (Theme F).**
   `scripts/codex_model_catalog_overrides.yaml` and
   `prompts/.overrides.yaml` let maintainers pin specific fields or
   files as untouchable by future regenerators. Phase 2's generator
   reads the catalog overrides at render time; the prompt overrides
   file is consumed by any future prompt-rewriter (none ships in this
   plan; the file documents the convention).
7. **Repo-root + ID-generator helpers (Theme G).**
   `scripts/repo_root.py` exposes `repo_root()` (walks upward from
   `__file__` looking for `CLAUDE.md` + `.git/` markers).
   `scripts/generate_resource_id.py` exposes `generate_id(prefix, salt)`
   (wraps `make_record_id`; deterministic when a salt is supplied).
8. **State-machine documented and optional structured form (Theme H).**
   `docs/how-it-works.md` enumerates every `ai:*` label transition in
   the issue → PR pipeline plus the full command vocabulary
   (`/answer`, `/approved`, `/reclarify`, `/clarify-now`, `[judge-fix]`,
   `[ai-autofix]`, `[ai-merge-resolve]`).
   `.github/ISSUE_TEMPLATE/structured-task.yml` is a new optional
   structured-form alternative to free-form issues — does not change
   the default UX.
9. **Vendored starter material (Theme I).**
   `examples/awesome-cc-references.md` is a curated annotated index of
   relevant awesome-claude-code resources with rationale for each
   pointer. No upstream content is vendored (license risk + drift);
   pure annotated links.

## Non-goals

- **No identifier renames per §6.** `record_id`, `make_record_id`, every
  `ai:*` label, every `LABEL_REPAIR*` / `AUTOFIX_*` / `SEMBLE_*` /
  `SERENA_*` log prefix, every workflow file name, every env-var name
  is preserved verbatim.
- **No catalog cloning, SVG badges, animated tickers, or multi-style
  README alternatives.** Q4 A+B+C — presentation tooling for an
  awesome-list; we are a workflow-automation repo. The `README.md` is
  an operator runbook, not a discoverability page.
- **No adoption of upstream's generic `pr-review.md` slash command.**
  Q4-C — our CLAUDE.md §12 PR-review-mode policy is multi-tier,
  model-multiplexed, and ledger-tracked; adopting the upstream's
  4-role PM/Dev/QA/Security rubric would dilute, not augment.
- **No consumer-repo propagation.** Q4-D — `workflow-templates/`,
  `.github/ai/consumer_repos.json`, and `repository_dispatch` are not
  touched. Consumer repos pick up downstream impact (none expected
  from this plan) via their normal `@stable` tag update path.
- **No cross-repo cooldown / sanctions enforcement.** Q4-E — the
  upstream `awesome-claude-code-ops` private state-store pattern is
  justified only when you have abusive contributors; we do not.
- **No behavioural gate from Phase 4's informal-detector in this PR.**
  The helper, fixtures, tests, and advisory clarify-prompt rule ship;
  wiring into `.github/workflows/clarify.yml` is deferred to a
  follow-up plan (see `docs/awesome-cc-future-improvements.md` EXT1).
- **No prompt-rewriter tooling shipped in this PR.**
  `prompts/.overrides.yaml` (Phase 6) documents the freeze convention
  for prompt files; the rewriter that would consume the file is
  out-of-scope.
- **No new MongoDB collections, contracts, indexes, or
  `/db/contracts/*` files.** §10 does not apply.
- **No new env vars, model swaps, infrastructure changes, or runtime
  feature flags.** Every adoption is either a docs / test addition (no
  runtime impact), a new helper script (only invoked when a caller
  opts in), or a CI step (gates contributor workflow only).

## Constraints

- **CLAUDE.md §5 Minimal Change Set** — every adoption extends an
  existing mechanism (prompt, script, doc, CI workflow) or adds a
  small new helper alongside existing code. No rewrites of existing
  scripts or workflows.
- **CLAUDE.md §6 Naming Immutability** — no identifier renamed. Theme
  E adds an alongside contract test for the existing `make_record_id`
  format; never replaces it. Theme G wraps the existing helper; never
  renames it. The new YAML override files
  (`scripts/codex_model_catalog_overrides.yaml`, `prompts/.overrides.yaml`),
  the new tree-marker convention (`<!-- TREE:START id=... -->`), the
  new log prefix (`CLARIFY_INFORMAL_SCORE:`), and the new Makefile
  targets (`generate`, `generate-check`) are all net-new identifiers
  that did not previously exist; §6 does not bind their introduction.
- **CLAUDE.md §9 Code Style** — Makefile recipe bodies use literal TAB
  indentation per "Makefile recipe bodies must use a literal TAB."
  All YAML files (`tools/repo_tree/config.yaml`,
  `scripts/codex_model_catalog_overrides.yaml`,
  `prompts/.overrides.yaml`, `.github/ISSUE_TEMPLATE/structured-task.yml`)
  use 2-space indentation per the YAML clause.
- **CLAUDE.md §10 MongoDB Rules** — N/A. No collection, query, or
  index work.
- **CLAUDE.md §13 Repository Hygiene** — N/A. No `.git/**` writes.
- **CLAUDE.md §14 Consumer Repo Registry** — N/A. Per Q4-D no
  consumer-repo propagation; `.github/ai/consumer_repos.json` not
  edited.
- **CLAUDE.md §15 GitHub API Call Hygiene** — N/A. Plan adds zero new
  `gh api` / `gh_retry` / `_safe_gh_jq` / `curl` calls. Phase 4's
  informal-detector accepts the issue body text on stdin / via a file
  argument; the caller (existing clarify-phase wrapper) already
  fetches the issue body.
- **`unattended_system_instructions.md` §15 Role descriptions** —
  Phase 1's reviewer / judge prompt additions extend, do not replace,
  the existing Reviewer / Judge role contracts.
- **No Makefile exists today.** Adding one is a new top-level addition
  that creates a `make generate` / `make generate-check` contract. The
  existing CI in `.github/workflows/ci.yml` is the only consumer in
  this PR.
- **Contract test on `make_record_id` format** — Phase 5's
  `tests/test_record_id_format_contract.py` pins the format that
  exists today (`^[a-z0-9_]+_\d{14}_[0-9a-f]{10}$`). Any future plan
  that needs to change the format must update this test in the same
  commit and ship a §6 alias-shim path.

## Approach

Group the nine themes into nine thematic commits (one per phase). Each
phase touches a small, related set of files so review and rollback are
surgical. Phases are mutually independent except for one shared file
(`Makefile`, created by Phase 2 and amended by Phase 3) and one shared
generated artifact (`docs/codex-model-reference.md`, generated by
Phase 2 and augmented by Phase 6). The phase order ensures these
foundation commits land before their dependents.

**Why nine commits, not one or nineteen?** Reviewer attention is the
binding constraint. One mega-commit obscures cause / effect for any
regression that surfaces after merge. Nineteen single-file commits
inflate the PR commit log without giving more rollback granularity
than the per-theme grouping below. Per-theme commits give
"revert one commit, restore one theme" rollback.

Alternatives considered:

- **One PR per theme.** Rejected: items share rationale and a single
  PR description gives reviewers context; items are mutually
  independent so PR-to-PR sequencing buys no isolation; one PR keeps
  the dual-doc structure (plan + future-improvements doc) coherent.
- **Plan only — no companion doc.** Rejected: the precedent
  (`docs/plans/apply-ai-tools-learnings-plan.md` +
  `docs/ai-tools-future-improvements.md`) shows that surfacing
  deferred items in a sibling doc materially helps reviewers
  understand the scope cut.
- **Companion doc only — no plan.** Rejected: the user (Q2 → A)
  explicitly asked for the implementation plan.

## Implementation Steps

### Phase 1 — Curator-rubric additions to reviewer / judge prompts (Theme A)

**Target files:**

- `prompts/review-reviewer-checklist.txt` — append an 8th lens heading
  after `NAMING / BACKWARD COMPATIBILITY`.
- `prompts/mode-judge.txt` — append two evaluation criteria to the
  existing numbered list.

**Commit message:**
`docs(prompts): add Claude-Code-specific implicit-execution lens to reviewer and judge prompts`

Two edits:

1. **`prompts/review-reviewer-checklist.txt`** — after the existing
   seven-heading block ending with `NAMING / BACKWARD COMPATIBILITY`,
   add the new heading:

   > IMPLICIT-EXECUTION & TRUST-BOUNDARY RISKS
   >
   > Surface findings only when the diff touches workflow YAML,
   > `.claude/hooks/`, `.claude/commands/`, MCP server config,
   > `scripts/*` entry points, env-var defaults, or any code path
   > that spawns a subprocess or registers a watcher.
   >
   > Findings under this lens cover three patterns:
   > - Implicit execution without a documented gate — a hook, watcher,
   >   import-time side effect, or cron-style trigger that runs code
   >   without a corresponding env-var kill switch or invocation gate
   >   documented in `agents.md` or `CLAUDE.md`.
   > - State-scope creep — a function that reads or writes state
   >   outside its natural scope (global config, shared cache,
   >   cross-PR ledger) without the cross-cutting access surfacing in
   >   the function signature, docstring, or call site.
   > - Declared-vs-effective capability gap — a comment, docstring, or
   >   PR body that claims one capability scope (e.g. "read-only
   >   audit") but the runtime behavior is wider (e.g. writes to a
   >   cache, makes a network call, mutates env state).
   >
   > Emit `NONE` if the diff does not touch the listed surfaces, or if
   > every touched surface has a documented gate.

2. **`prompts/mode-judge.txt`** — extend the existing 5-item
   evaluation list (`1. Does the merged code match…` through
   `5. Are there new issues that emerged…`) with:

   > 6. Implicit-execution audit: does any merged file in `.claude/`,
   >    `scripts/`, `prompts/`, or `.github/workflows/` introduce
   >    implicit execution (a hook, watcher, side effect on import, or
   >    cron-style trigger) without the corresponding env-var kill
   >    switch or invocation gate documented in `agents.md` or
   >    `CLAUDE.md`?
   > 7. Declared-vs-effective scope: does any merged change declare
   >    one capability scope (in a comment, docstring, or PR body) but
   >    expose a wider effective scope at runtime — e.g. a "read-only
   >    audit" helper that writes to a cache, a "dry-run" flag that
   >    still triggers a network call, a "local-only" helper that
   >    reads cross-PR state?

**Estimated effort:** 20 minutes including a re-read for flow with
the existing prompt block.

### Phase 2 — Model-catalog drift check (Theme B)

**Target files:**

- `Makefile` `[new]` — `.PHONY: generate generate-check`, with `generate`
  / `generate-check` targets.
- `scripts/generate_codex_model_reference.py` `[new]` — generator.
- `docs/codex-model-reference.md` `[new]` — committed generated
  artifact.
- `.github/workflows/ci.yml` `[edit]` — append a `Drift check
  (generated docs)` step to the `lint` job after the existing `YAML lint`
  step.

**Commit message:**
`feat(docs): generate codex-model-reference.md from catalog with CI drift check`

Steps:

1. Create `Makefile` at repo root. Recipe body indentation MUST be a
   literal TAB per CLAUDE.md §9:

   ```
   .PHONY: generate generate-check

   generate:
   <TAB>python3 scripts/generate_codex_model_reference.py --write
   <TAB>python3 tools/repo_tree/update_repo_tree.py --write

   generate-check:
   <TAB>python3 scripts/generate_codex_model_reference.py --check
   <TAB>python3 tools/repo_tree/update_repo_tree.py --check
   ```

   The `tools/repo_tree/...` lines anticipate Phase 3 — wiring both
   generators into a single Makefile from the first phase that ships it
   keeps the contract clean. Until Phase 3 lands, the Phase 2 commit
   includes a stub `tools/repo_tree/update_repo_tree.py` that just
   exits 0; the stub is overwritten in Phase 3.

2. Create `scripts/generate_codex_model_reference.py`:
   - Reads `scripts/codex_model_catalog.json` via `json.load`.
   - Reads `scripts/codex_model_catalog_overrides.yaml` if present
     (Phase 6 — fail-open with a `::warning::` if missing; emit empty
     overrides dict).
   - Renders a markdown table with one row per model. Columns: `slug`,
     `default_verbosity`, `support_verbosity`, `apply_patch_tool_type`,
     `notes`. `notes` is the per-row `notes` field from the overrides
     file when set.
   - For each row where the overrides file pins ≥1 field, append
     `(frozen)` to the row's `notes` cell so the rendered doc surfaces
     the freeze state.
   - `--write` mode: writes to `docs/codex-model-reference.md` with a
     leading `<!-- GENERATED FILE: do not edit. Run \`make generate\` after
     editing scripts/codex_model_catalog.json. -->` banner.
   - `--check` mode: reads the existing `docs/codex-model-reference.md`,
     compares byte-for-byte to the freshly-rendered output, exits 0 on
     match, exits 1 with the unified diff on stderr on mismatch.

3. Run `make generate` once locally on the planning commit; commit the
   resulting `docs/codex-model-reference.md`.

4. Edit `.github/workflows/ci.yml`. After the existing `YAML lint`
   step in the `lint` job, append:

   ```yaml
   - name: Drift check (generated docs)
     run: |
       set -euo pipefail
       make generate-check
   ```

   The step inherits the existing `lint` job's setup (Python 3.12,
   `pyyaml` already installed).

**Estimated effort:** 90 minutes including the generator script and
a re-run to confirm `make generate-check` returns clean on a fresh
clone.

### Phase 3 — Doc-tree drift check (Theme C)

**Target files:**

- `tools/repo_tree/update_repo_tree.py` `[edit]` — overwrites the
  Phase-2 stub with the full implementation.
- `tools/repo_tree/config.yaml` `[new]`.
- `agents.md` `[edit]` — add a new section `## Repo-tree
  (auto-generated)` near end-of-file with two
  `<!-- TREE:START id=... -->` / `<!-- TREE:END id=... -->` blocks.

**Commit message:**
`feat(tools): repo-tree drift check via TREE:START/END markers in agents.md`

Steps:

1. Replace the Phase-2 stub at `tools/repo_tree/update_repo_tree.py`
   with the real implementation:
   - Loads `tools/repo_tree/config.yaml` — a `trees:` list, each
     entry having `file:` (target path), `marker_id:` (unique within
     the file), `source_glob:` (glob to expand).
   - For each entry, expands the glob (sorted, deterministic), builds
     a fenced code block (one path per line, no annotation).
   - In `--write` mode: opens `file`, finds the exact pair
     `<!-- TREE:START id={marker_id} -->` and
     `<!-- TREE:END id={marker_id} -->`, replaces the content between
     them, writes back.
   - In `--check` mode: reads existing content between markers,
     compares to freshly-rendered content, exits 0 on match, exits 1
     with the unified diff on stderr on mismatch.
   - Failure cases that fail loudly (exit 2): marker pair missing,
     multiple START or END markers with the same `marker_id` in the
     same file, START without matching END (or vice versa).

2. Create `tools/repo_tree/config.yaml`:

   ```yaml
   trees:
     - file: agents.md
       marker_id: workflows
       source_glob: ".github/workflows/*.yml"
     - file: agents.md
       marker_id: workflow_templates
       source_glob: "workflow-templates/*.yml"
   ```

3. Edit `agents.md`. At end-of-file (after the existing "Review
   pipeline consolidator + ledger contract" section), append:

   ```
   ## Repo-tree (auto-generated)

   Active workflow files (regenerate with `make generate`):

   <!-- TREE:START id=workflows -->
   <!-- TREE:END id=workflows -->

   Consumer-facing workflow templates (regenerate with `make generate`):

   <!-- TREE:START id=workflow_templates -->
   <!-- TREE:END id=workflow_templates -->
   ```

   Run `make generate` to populate both blocks with the actual file
   listings.

**Estimated effort:** 60 minutes including marker placement and a
clean `make generate` run.

### Phase 4 — Informal-issue detector (Theme D)

**Target files:**

- `scripts/clarify_informal_detect.py` `[new]`.
- `tests/fixtures/informal_issues/clean_issue.json` `[new]`.
- `tests/fixtures/informal_issues/copy_pasted_template.json` `[new]`.
- `tests/fixtures/informal_issues/empty_body.json` `[new]`.
- `tests/test_clarify_informal_detect.py` `[new]`.
- `prompts/mode-clarify.txt` `[edit]` — append a single advisory
  paragraph.

**Commit message:**
`feat(clarify): informal-issue detector heuristic with fixtures (advisory only)`

Steps:

1. Create `scripts/clarify_informal_detect.py`. Standard library only;
   no third-party deps. CLI:

   ```
   python3 scripts/clarify_informal_detect.py --issue-body-file <path>
   python3 scripts/clarify_informal_detect.py --issue-body-stdin
   ```

   Output (always to stdout, single line):

   ```
   CLARIFY_INFORMAL_SCORE: <0.000-1.000>; signals=<csv>
   ```

   Five binary signals (each scored 0 or 1):
   - `template_paste` — body contains ≥3 of these literal markdown
     headings as bare lines: `## Steps to reproduce`,
     `## Expected behavior`, `## Actual behavior`,
     `### Acceptance criteria`, `## Context`, `## Description`,
     `## Environment`.
   - `no_ask` — no `?` character in the body AND the first 50 words
     contain no imperative verb from this set: `add`, `fix`, `create`,
     `update`, `remove`, `implement`, `support`, `enable`, `disable`,
     `refactor`, `migrate`, `port`, `test`.
   - `no_acceptance` — no occurrence of any of these tokens
     (case-insensitive): `should`, `must`, `expect`, `assert`,
     `verify`, `complete when`, `done when`, `acceptance criteria`.
   - `pure_log_dump` — ≥80% of non-blank lines match the regex
     `^\s*(\[\w+\]|\d{4}-\d{2}-\d{2}|::|>\s)` (log-line prefix:
     bracketed level, ISO date, GitHub Actions `::` notice, or
     blockquote marker).
   - `link_only` — body trimmed length < 50 chars AND contains
     exactly one URL (regex `https?://\S+`).

   Score = (count of triggered signals) / 5, rounded to three
   decimals.

   Fail-open: any parse error (file unreadable, unicode error, stdin
   timeout > 5s) returns `CLARIFY_INFORMAL_SCORE: 0.000;
   signals=parse_error` to stdout and exits 0. The detector never
   blocks the caller.

2. Create the three fixtures under `tests/fixtures/informal_issues/`.
   Each fixture is a JSON file with shape:

   ```json
   {
     "body": "...",
     "expected_score": 0.000,
     "expected_signals": [],
     "description": "..."
   }
   ```

   - `clean_issue.json` — well-formed task description with clear ask,
     acceptance criteria; score 0.000, signals empty.
   - `copy_pasted_template.json` — body is the literal GitHub issue
     template with placeholder text; score ≥ 0.600, signals include
     `template_paste`, `no_ask`, `no_acceptance`.
   - `empty_body.json` — body is "see attached log" + a single URL;
     score ≥ 0.400, signals include `link_only`, `no_acceptance`.

3. Create `tests/test_clarify_informal_detect.py`:
   - Iterates the three fixtures.
   - Invokes `scripts/clarify_informal_detect.py --issue-body-stdin`
     via `subprocess.run` with the fixture body on stdin.
   - Parses the `CLARIFY_INFORMAL_SCORE:` line.
   - Asserts `abs(score - expected_score) < 0.001` and
     `set(actual_signals) == set(expected_signals)`.

4. Edit `prompts/mode-clarify.txt`. Append after the existing rules
   block (before any trailing examples or formatting hints):

   > Advisory signal: when the wrapper supplies a
   > `CLARIFY_INFORMAL_SCORE: <float>; signals=<csv>` line at the top
   > of the issue context, treat it as informational only. The score
   > is a 0.0-to-1.0 heuristic; the signals list names which of
   > `template_paste`, `no_ask`, `no_acceptance`, `pure_log_dump`,
   > `link_only` triggered. Do not refuse the issue based on the
   > score, do not include the score in the user-visible Q-batch, and
   > do not echo the literal `CLARIFY_INFORMAL_SCORE:` token back to
   > the user. You may use a specific signal to focus a Q that needs
   > asking (e.g. when `no_acceptance` is set, the acceptance-criteria
   > Q is usually worth asking).

   The wrapper integration that supplies the score is deferred. See
   `docs/awesome-cc-future-improvements.md` EXT1.

**Estimated effort:** 2 hours including fixtures and tests.

### Phase 5 — Stable-ID convention documented and pinned (Theme E)

**Target files:**

- `agents.md` `[edit]` — add a new section `## Stable-ID convention`
  after the existing `## Workflow architecture` section.
- `tests/test_record_id_format_contract.py` `[new]`.

**Commit message:**
`docs: pin make_record_id format convention with contract test`

Steps:

1. Edit `agents.md`. After the existing `## Workflow architecture`
   section, before `## Models in use`, add:

   ```
   ## Stable-ID convention

   Any new identifier emitted by an AI-pipeline component (memory
   record, run-ledger entry, judge verdict, validation cycle,
   evaluation snapshot, review-ledger row) MUST use the
   `make_record_id(prefix)` helper at
   `scripts/ai_memory_lib.py:480`.

   Format: `<prefix>_<YYYYMMDDHHMMSS>_<10hex>`. The prefix is
   sanitized via `sanitize_segment` (lowercase, allowed chars
   `[a-z0-9_]`). Existing prefixes in use: `mem`, `run_event`. New
   prefixes (e.g. for a new ledger surface) must be one-token, lower
   snake-case, ≤16 chars.

   Examples:
   - `mem_20260516120000_a1b2c3d4e5`
   - `run_event_20260516120100_f6e7d8c9b0`

   §6 binds this section: the format string is contractual.
   `tests/test_record_id_format_contract.py` pins the regex. Any
   change to the format requires a paired alias-emitting shim per
   §6 and a synchronous update to the test.
   ```

2. Create `tests/test_record_id_format_contract.py`:
   - Imports `make_record_id` from `scripts.ai_memory_lib`.
   - Asserts the returned id matches regex
     `^[a-z0-9_]+_\d{14}_[0-9a-f]{10}$`.
   - Asserts `make_record_id("mem")` starts with `mem_`.
   - Asserts `make_record_id("run_event")` starts with `run_event_`.
   - Edge-case tests: empty prefix falls back to `mem`; whitespace
     prefix sanitized; mixed-case prefix sanitized to lowercase.

**Estimated effort:** 30 minutes including contract test.

### Phase 6 — Overrides-with-auto-locking (Theme F)

**Target files:**

- `scripts/codex_model_catalog_overrides.yaml` `[new]`.
- `prompts/.overrides.yaml` `[new]`.
- `scripts/generate_codex_model_reference.py` `[edit]` — already
  reads overrides in Phase 2; this phase verifies the override path
  is exercised by adding one real override and re-running
  `make generate`.
- `agents.md` `[edit]` — add a new section `## Override conventions`.

**Commit message:**
`feat(overrides): per-row and per-file override files with skip_validation auto-lock`

Steps:

1. Create `scripts/codex_model_catalog_overrides.yaml`:

   ```yaml
   # See agents.md "Override conventions" section.
   # Fields under `overrides:` for a given slug are merged over
   # catalog defaults at render time and emitted with a `(frozen)`
   # marker in docs/codex-model-reference.md. Any field listed here
   # is excluded from future automated rewrites of
   # scripts/codex_model_catalog.json.
   models:
     - slug: openai/gpt-5.4
       overrides:
         apply_patch_tool_type: function
       notes: |
         apply_patch_tool_type was flipped from "freeform" to
         "function" on 2026-05-07 after the codex#11151 ablation
         suite identified freeform as the root cause of
         announce-without-emit failures on the OpenRouter Responses
         path. Do not auto-rewrite.
   ```

2. Create `prompts/.overrides.yaml`:

   ```yaml
   # See agents.md "Override conventions" section.
   # Files listed here are skipped by any future automated
   # prompt-rewriter tooling. Listing a file does NOT prevent human
   # edits; it only blocks automated regeneration / "improve this
   # prompt" passes from a future tool.
   files:
     - path: prompts/conflict-resolver.txt
       skip_validation: true
       reason: |
         Wording is deliberately terse; prior LLM-driven rewrites
         introduced verbosity that broke the resolver's narrow
         single-purpose scope.
     - path: prompts/integration-sync-conflict-resolver.txt
       skip_validation: true
       reason: |
         Sibling of the file above; inherits the same constraint.
   ```

3. Re-run `make generate` and confirm
   `docs/codex-model-reference.md` shows the `openai/gpt-5.4` row
   with `apply_patch_tool_type: function` plus a `(frozen)` marker
   in the `notes` cell.

4. Edit `agents.md`. After the new `## Stable-ID convention` section
   from Phase 5, add:

   ```
   ## Override conventions

   Two override files let maintainers pin specific catalog fields
   or prompt files as untouchable by future regenerators:

   - `scripts/codex_model_catalog_overrides.yaml` — per-model-slug
     field overrides for `scripts/codex_model_catalog.json`. The
     fields listed under each slug's `overrides:` block are merged
     over the catalog defaults at render time by
     `scripts/generate_codex_model_reference.py` and emitted with a
     `(frozen)` marker in the generated `docs/codex-model-reference.md`.

   - `prompts/.overrides.yaml` — per-file freeze markers for any
     prompt file under `prompts/`. Files listed with
     `skip_validation: true` are excluded from any future automated
     prompt-rewriter tooling.

   §6 implication: overrides are the canonical way to preserve
   intentional non-default values across future regenerations
   without renaming or removing the underlying identifier. To freeze
   a new value, add it to the appropriate overrides file with a
   `notes:` / `reason:` block explaining why.
   ```

**Estimated effort:** 75 minutes.

### Phase 7 — Repo-root + ID-generator helpers (Theme G)

**Target files:**

- `scripts/repo_root.py` `[new]`.
- `scripts/generate_resource_id.py` `[new]`.
- `tests/test_repo_root_helper.py` `[new]`.
- `tests/test_generate_resource_id.py` `[new]`.
- `agents.md` `[edit]` — add a new section `## Utility helpers`.

**Commit message:**
`feat(scripts): repo_root and generate_resource_id helpers`

Steps:

1. Create `scripts/repo_root.py`:

   - Function `repo_root() -> Path`: walks upward from
     `Path(__file__).resolve().parent` looking for the first
     directory containing BOTH `CLAUDE.md` AND `.git/`. Returns the
     resolved path. Raises `RuntimeError` with a clear message if no
     such directory is found within 10 levels.
   - Function `repo_root_from(start: Path) -> Path`: same walk
     starting from `start` (resolved). Used by callers that want
     explicit start.
   - `if __name__ == "__main__":` CLI prints the resolved root and
     exits 0; exits 1 on `RuntimeError`.

2. Create `scripts/generate_resource_id.py`:

   - Imports `make_record_id` and `sanitize_segment` from
     `scripts.ai_memory_lib`.
   - Function `generate_id(prefix: str, salt: str | None = None) -> str`:
     - When `salt is None`: returns `make_record_id(prefix)` (delegates
       to the canonical implementation; non-deterministic uuid suffix).
     - When `salt` is provided: returns
       `f"{sanitize_segment(prefix, 'mem')}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{hashlib.sha256(salt.encode('utf-8')).hexdigest()[:10]}"`
       (deterministic suffix per salt; timestamp is still wall-clock).
   - CLI:
     `python3 scripts/generate_resource_id.py --prefix <p> [--salt <s>]`.

3. Tests:

   - `tests/test_repo_root_helper.py`:
     - `test_repo_root_returns_path_with_claudemd_and_git()`.
     - `test_repo_root_from_intermediate_directory()` — starts in
       `scripts/` and resolves correctly.
     - `test_repo_root_from_failure_raises_runtimerror()` — pass
       `/tmp` (or similar marker-less dir), assert raises.

   - `tests/test_generate_resource_id.py`:
     - Format-pinning: returned id matches the §5 regex.
     - With salt: two invocations with the same salt return ids that
       differ only in the timestamp segment.
     - Without salt: two invocations with the same prefix return
       different suffixes.
     - Prefix sanitization: `"Mem.with.dots"` becomes `"memwithdots"`
       in the prefix segment.

4. Edit `agents.md`. After the new `## Override conventions` section
   from Phase 6, add:

   ```
   ## Utility helpers

   Reusable Python helpers for scripts that need cross-script
   primitives without re-implementing them inline:

   - `scripts/repo_root.py` — `repo_root()` walks upward from the
     script's own location to find the directory containing both
     `CLAUDE.md` and `.git/`. Use this in any new Python script
     that needs the repo root without depending on `cwd`.
   - `scripts/generate_resource_id.py` — `generate_id(prefix, salt)`
     produces a stable id in the §5 format. Wraps
     `scripts/ai_memory_lib.py:480`'s `make_record_id` for callers
     outside the memory subsystem. Pass a salt for a deterministic
     suffix (useful for idempotent generators).
   ```

**Estimated effort:** 60 minutes including tests.

### Phase 8 — State-machine documentation + optional structured issue form (Theme H)

**Target files:**

- `docs/how-it-works.md` `[new]`.
- `.github/ISSUE_TEMPLATE/structured-task.yml` `[new]`.
- `README.md` `[edit]` — add a single link to `docs/how-it-works.md`
  near the top of the `## Overview` section.

**Commit message:**
`docs: how-it-works state machine + optional structured-task issue form`

Steps:

1. Create `docs/how-it-works.md` with these sections:

   - **Pipeline overview** — one paragraph mapping the 12 phases from
     `agents.md` to their workflow files.
   - **Label state machine** — a mermaid diagram of the `ai:*` label
     transitions:
     - Happy path: `ai:clarification → ai:planning →
       ai:awaiting-approval → ai:implementing → ai:done →
       ai:ready-to-merge → ai:merged`.
     - Validation branch: `... → ai:validating →
       ai:validation-failed → (judge) → ai:done | ai:closed`.
     - Error branches: `ai:blocked`, `ai:implementation-failed`,
       `ai:review-blocked`, `ai:review-skipped`, `ai:closed`.
     - Below the mermaid block, include an ASCII fallback listing
       the same transitions, in case a viewer cannot render
       mermaid.
   - **Command vocabulary** — table with columns: command, where it's
     issued (issue comment, PR comment, PR title), who consumes it
     (workflow file path), what state transition it triggers,
     idempotency key. Include `/answer`, `/approved`, `/reclarify`,
     `/clarify-now`, `[judge-fix]`, `[ai-autofix]`,
     `[ai-merge-resolve]`, `[force-review]`, `force-review` (label),
     `force-review` (title token).
   - **Stall recovery ladder** — references the
     `STALL_THRESHOLD_*` env-var schedule documented in `README.md`
     and the `STALL_RECOVERY_ACTIONS` array in
     `scripts/orchestrate_lib.py`. Does not duplicate the table;
     links to it.
   - **Source** — one line citing
     `awesome-claude-code/docs/HOW_IT_WORKS.md` as the structural
     inspiration; notes that our state machine is much richer (12
     phases vs upstream's 4-state label flow).

2. Create `.github/ISSUE_TEMPLATE/structured-task.yml`:

   ```yaml
   name: Structured task
   description: File a task with explicit acceptance criteria for the AI pipeline.
   title: "[task] "
   labels: ["ai:clarification"]
   body:
     - type: markdown
       attributes:
         value: |
           Use this form for tasks you want the AI pipeline to pick
           up directly. The free-form issue path still works; this
           form lets clarify skip straight to `STATUS: CLEAR` when
           the fields below are complete.
     - type: textarea
       id: task_description
       attributes:
         label: Task description
         description: What needs to change, in 2-4 sentences.
       validations:
         required: true
     - type: textarea
       id: acceptance_criteria
       attributes:
         label: Acceptance criteria
         description: How to know the task is done. Use bullet points.
       validations:
         required: true
     - type: textarea
       id: scope_in
       attributes:
         label: In scope
         description: What this task does cover. Optional.
     - type: textarea
       id: scope_out
       attributes:
         label: Out of scope
         description: What this task explicitly does not cover. Optional.
     - type: input
       id: target_files
       attributes:
         label: Target files / modules
         description: Comma-separated paths if you know them. Optional.
     - type: textarea
       id: references
       attributes:
         label: References
         description: Links to issues, PRs, docs, or prior plans. Optional.
   ```

   The `labels:` field auto-applies `ai:clarification` so the
   orchestrator picks the issue up via the same entry point as
   free-form issues.

3. Edit `README.md`. Near the top of the `## Overview` section (after
   the existing numbered list of 9 reusable workflows), add a single
   line:

   ```
   For the issue → PR pipeline state machine and the full command
   vocabulary, see [`docs/how-it-works.md`](docs/how-it-works.md).
   ```

**Estimated effort:** 90 minutes including the mermaid diagram, the
ASCII fallback, and the issue-form YAML.

### Phase 9 — Vendored starter material (Theme I)

**Target files:**

- `examples/awesome-cc-references.md` `[new]`.

**Commit message:**
`docs(examples): curated index of awesome-claude-code references`

Create `examples/awesome-cc-references.md` as a curated annotated
index. Each entry has three lines:

- Resource name + link to upstream.
- Why we point at it — one-sentence rationale.
- Where it intersects this repo — file path in our codebase that is
  analogous, with a one-phrase note on how they differ.

Sections (entry counts indicative, refine during writing):

- **Slash commands worth studying** (4–6 entries) — entries from
  `resources/slash-commands/` whose patterns are reusable. Candidates:
  `evaluate-repository` (intersects `prompts/mode-judge.txt`),
  `create-prp` / `create-prd` (intersect `prompts/mode-plan.txt`),
  `fix-github-issue` (intersects `prompts/mode-implement.txt`).
- **GH Actions templates worth comparing** — entries from
  `resources/official-documentation/Claude-Code-GitHub-Actions/`.
  Candidates: `ci-failure-auto-fix.yml` (intersects
  `.github/workflows/review_autofix.yml`), `issue-triage.yml`
  (intersects `.github/workflows/clarify.yml`),
  `pr-review-comprehensive.yml` (intersects
  `.github/workflows/review_autofix.yml`).
- **Knowledge guides for further reading** (3–5 entries) — entries
  from the `Workflows & Knowledge Guides` category in
  `THE_RESOURCES_TABLE.csv`. Candidates: `Claude Code Handbook`,
  `Claude Code System Prompts`, `Compound Engineering Plugin`,
  `Encyclopedia of Agentic Coding Patterns`.
- **Tooling we deliberately did NOT adopt and why** — short list
  linking to `docs/awesome-cc-future-improvements.md` for each
  exclusion category (EX1 SVG/ticker, EX2 catalog cloning,
  EX3 generic pr-review, EX5 cross-repo cooldown).

No upstream content is vendored — pure annotated links. Vendoring
introduces license and drift risk; the curation note plus the link
is sufficient.

**Estimated effort:** 60 minutes including curation.

## Files & Modules

`[new]` = new file; `[edit]` = additive edit; `[del]` = deletion.

New files (19):

- `[new]` `docs/plans/awesome-claude-code-learnings-plan.md` — this
  file.
- `[new]` `docs/awesome-cc-future-improvements.md` — companion
  deferred-items doc, shipped same PR.
- `[new]` `Makefile` — top-level Makefile with `generate` and
  `generate-check` targets (Phase 2).
- `[new]` `scripts/generate_codex_model_reference.py` — generator for
  `docs/codex-model-reference.md` (Phase 2; reads catalog + overrides).
- `[new]` `docs/codex-model-reference.md` — generated reference table
  (Phase 2; committed, regenerated by CI).
- `[new]` `tools/repo_tree/update_repo_tree.py` — tree-marker drift
  checker (Phase 2 stub; Phase 3 real implementation).
- `[new]` `tools/repo_tree/config.yaml` — `trees:` list of
  `{file, marker_id, source_glob}` (Phase 3).
- `[new]` `scripts/clarify_informal_detect.py` — informal-issue
  heuristic (Phase 4).
- `[new]` `tests/fixtures/informal_issues/clean_issue.json` (Phase 4).
- `[new]` `tests/fixtures/informal_issues/copy_pasted_template.json`
  (Phase 4).
- `[new]` `tests/fixtures/informal_issues/empty_body.json` (Phase 4).
- `[new]` `tests/test_clarify_informal_detect.py` (Phase 4).
- `[new]` `tests/test_record_id_format_contract.py` — pins the
  `make_record_id` regex (Phase 5).
- `[new]` `scripts/codex_model_catalog_overrides.yaml` — per-slug
  field overrides + freeze markers (Phase 6).
- `[new]` `prompts/.overrides.yaml` — per-prompt-file freeze markers
  (Phase 6).
- `[new]` `scripts/repo_root.py` — `repo_root()` helper (Phase 7).
- `[new]` `scripts/generate_resource_id.py` — `generate_id(prefix,
  salt)` helper wrapping `make_record_id` (Phase 7).
- `[new]` `tests/test_repo_root_helper.py` (Phase 7).
- `[new]` `tests/test_generate_resource_id.py` (Phase 7).
- `[new]` `docs/how-it-works.md` — pipeline state-machine doc
  (Phase 8).
- `[new]` `.github/ISSUE_TEMPLATE/structured-task.yml` — optional
  structured-form issue template (Phase 8).
- `[new]` `examples/awesome-cc-references.md` — curated annotated
  index (Phase 9).

Edited files (6):

- `[edit]` `prompts/review-reviewer-checklist.txt` — append 8th lens
  heading (Phase 1).
- `[edit]` `prompts/mode-judge.txt` — append 6th and 7th evaluation
  criteria (Phase 1).
- `[edit]` `prompts/mode-clarify.txt` — append advisory paragraph for
  `CLARIFY_INFORMAL_SCORE` (Phase 4).
- `[edit]` `agents.md` — four new sections appended in order:
  `## Stable-ID convention` (Phase 5), `## Override conventions`
  (Phase 6), `## Utility helpers` (Phase 7), `## Repo-tree
  (auto-generated)` (Phase 3).
- `[edit]` `README.md` — single-line link to `docs/how-it-works.md`
  in the `## Overview` section (Phase 8).
- `[edit]` `.github/workflows/ci.yml` — add `Drift check (generated
  docs)` step to the `lint` job after the existing `YAML lint` step
  (Phase 2).

Totals: 2 plan/companion docs + 17 new code/doc/test/fixture files +
6 edited files. No deletions. No renames. No new env vars. No new
log prefixes (the `CLARIFY_INFORMAL_SCORE:` token is a single new
log-prefix surface — additive per §6). The only other new identifiers
(`make` targets `generate` / `generate-check`, YAML keys in the two
overrides files, `<!-- TREE:START id=... -->` marker convention) are
all net-new and §6 does not bind their introduction.

## Data Model / Index Changes

N/A. No MongoDB collection, query, index, or `/db/contracts/*` file
is touched. CLAUDE.md §10 does not apply.

## Tests

New automated tests (Python, run by existing test discovery in
`tests/`):

- `tests/test_record_id_format_contract.py` (Phase 5) — pins
  `make_record_id` format with regex match + prefix assertions.
- `tests/test_clarify_informal_detect.py` (Phase 4) — fixture-driven;
  three fixtures cover clean, template-paste, and minimal-link cases.
- `tests/test_repo_root_helper.py` (Phase 7) — covers normal
  resolution, intermediate-directory start, missing-marker failure.
- `tests/test_generate_resource_id.py` (Phase 7) — format-pinning,
  salt determinism, salt-less variability, prefix sanitization.

Existing tests are not modified.

CI (Phase 2): a new `Drift check (generated docs)` step in
`.github/workflows/ci.yml`'s `lint` job runs both generators in
`--check` mode and fails the lane on diff.

Manual verification after merge:

1. Run `make generate` locally on the merge commit; confirm no diff
   (idempotent).
2. Edit `scripts/codex_model_catalog.json` (e.g. add a fake model
   entry) without running `make generate`, push to a throwaway
   branch — confirm CI's drift-check step fails with a unified diff.
3. Trigger one `clarify.yml` smoke run on a deliberately-vague test
   issue. Confirm `prompts/mode-clarify.txt`'s new advisory paragraph
   is present in the prompt assembly log; clarifier behavior on
   issues that do not carry a `CLARIFY_INFORMAL_SCORE:` line is
   unchanged.
4. Trigger one `review_autofix.yml` round on a small PR. Confirm the
   reviewer bundle includes the new 8th lens heading
   (`IMPLICIT-EXECUTION & TRUST-BOUNDARY RISKS`) — even if `NONE` for
   that PR — and the judge prompt's new criteria 6 and 7 appear in
   the assembled judge prompt.
5. Verify the structured issue template appears as an option on the
   "New issue" page in the GitHub UI.

## Risks & Mitigations

- **Risk:** Phase 2's drift-check step fails on every PR that
  touches `scripts/codex_model_catalog.json` without running `make
  generate` locally. **Mitigation:** the CI step's error message
  explicitly instructs the contributor to run `make generate && git
  add docs/codex-model-reference.md`. The Makefile target is fast
  (< 1 sec) and idempotent. Worst-case rollback: revert the Phase 2
  commit only.

- **Risk:** Phase 3's marker-based tree updater silently rewrites
  the wrong section if `marker_id` values are accidentally
  duplicated across or within files. **Mitigation:** updater fails
  loudly (exit 2) when it finds zero or >1 START or END markers for
  a given `marker_id` in a target file. Failure is at `--check`
  time, before `--write`, so duplicate markers cannot corrupt the
  doc.

- **Risk:** Phase 4's informal-detector mis-scores legitimate issues
  as informal. **Mitigation:** Phase 4 ships the detector + advisory
  rule but does NOT wire it into `clarify.yml`. No issue is scored
  automatically until a follow-up plan opts in. The clarify prompt
  rule explicitly says "do not refuse the issue based on the score,
  do not include the score in the user-visible Q-batch."

- **Risk:** Phase 5's contract test breaks any future legitimate
  format change. **Mitigation:** the contract test pins the format
  that exists today. A future format change must update the test in
  the same commit and ship a §6 alias-shim path — which is the
  intended workflow.

- **Risk:** Phase 6's overrides files are out-of-band with the
  source-of-truth JSON / prompt files. **Mitigation:** the generator
  reads the overrides at render time and emits a `(frozen)` marker
  in the rendered doc so readers can see freeze state without
  reading the overrides file. The overrides file is plain YAML and
  diffable.

- **Risk:** Phase 7's `generate_id(prefix, salt)` deterministic
  variant could be misused to produce IDs that collide across
  callers with the same salt. **Mitigation:** the docstring and
  `agents.md` documentation explicitly call out the deterministic
  semantics; the timestamp segment still differs per second of
  invocation. Callers needing strict global uniqueness should not
  pass a salt.

- **Risk:** Phase 8's mermaid diagram in `docs/how-it-works.md` may
  not render on every viewer. **Mitigation:** the doc includes both
  the mermaid block and a textual ASCII fallback rendered below it.
  GitHub renders mermaid natively (2022 onwards); local Markdown
  viewers can read the fallback.

- **Risk:** Phase 8's optional `.github/ISSUE_TEMPLATE/structured-task.yml`
  changes the issue-creation UX. Contributors may file structured
  issues whose label / title shape the orchestrator does not handle
  correctly. **Mitigation:** the structured form is OPTIONAL. The
  free-form path remains the default. The structured form's
  auto-applied label (`ai:clarification`) matches what the free-form
  path also produces, so the orchestrator's entry point is the same
  in both cases. Title prefix `[task]` is informational and does not
  affect routing.

- **Risk:** Phase 1's new "implicit execution" lens encourages
  reviewers to flag every hook addition, swamping the review bundle.
  **Mitigation:** the lens explicitly says "Surface findings only
  when the diff touches workflow YAML, `.claude/hooks/`,
  `.claude/commands/`, MCP server config, `scripts/*` entry points,
  env-var defaults, or any code path that spawns a subprocess or
  registers a watcher." Hooks with documented kill switches do not
  trigger a finding.

- **Risk:** Plan-level — total file count is high (19 new + 6
  edited). **Mitigation:** each phase is independent and commits
  separately; reviewers can read phase-by-phase. Per-phase rollback
  is trivial. The dual-doc structure (plan + future-improvements)
  surfaces the rationale for every adoption to reviewers without
  requiring them to re-read this plan.

## Rollout

- **Branch:** `claude/review-claude-code-repo-RgR99` — assigned per
  session instructions. The default `claude/write-plan-<slug>`
  naming is overridden by the session-mandated branch (same precedent
  as `docs/plans/apply-ai-tools-learnings-plan.md`).
- **Base:** resolved dynamically via `gh repo view --json
  defaultBranchRef -q .defaultBranchRef.name -R
  shubhodeep1/coding-workflows`. Do not hardcode `main`.
- **PR:** opened as ready-for-review, not draft. PR body follows the
  template in `/write-plan` and links to both `docs/plans/
  awesome-claude-code-learnings-plan.md` and
  `docs/awesome-cc-future-improvements.md`.
- **Commits:** nine implementation-phase commits, one per phase, in
  numerical order. Phase ordering ensures the Makefile (created in
  Phase 2) and `tools/repo_tree/update_repo_tree.py` (stubbed in
  Phase 2, finalized in Phase 3) land before the
  dependents. Phase 6's overrides are exercised against Phase 2's
  generator; Phase 7's `generate_id` wraps Phase 5's
  `make_record_id`.
- **No feature flag.** Every edit is either a docs / test addition
  (no runtime impact), a new helper script (only invoked when a
  caller opts in), or a CI step (gates contributor workflow only).
  The structured issue template is the only runtime-visible UX
  change and is opt-in by definition.
- **Rollback path:** `git revert <commit-sha>` of any single phase
  removes its changes cleanly. Phases 2 and 3 share the Makefile and
  the `tools/repo_tree/` skeleton; reverting Phase 3 leaves the
  Phase-2 stub in place (idempotent, no-op). Phase 6 depends on
  Phase 2's overrides-read path; reverting Phase 6 leaves the read
  path in place but with no overrides to consume (fail-open path
  exercised). All other phases revert independently.
- **Propagation timing:** immediate take-up on the next pipeline run
  after merge to the default branch. Per Q4-D, no consumer-repo
  propagation; `.github/ai/consumer_repos.json` is not touched; no
  `@stable` tag dispatch is triggered.
- **Acceptance window:** 7 days of post-merge `workflow-log-analysis`
  reports. If reports flag any new `AI_PHASE_FAILURE_V1` lines or
  any regression in `LABEL_REPAIR` / `AUTOFIX_*` log patterns,
  isolate the responsible phase and revert that commit only.

## Open Questions

The four clarification questions (Q1–Q4) were resolved in the
planning conversation:

- Q1 → slug = `awesome-claude-code-learnings`.
- Q2 → dual-doc shape (implementation plan + companion
  future-improvements doc).
- Q3 → adopt all nine themes A through I.
- Q4 → exclude SVG / ticker presentation, CSV catalog cloning,
  generic `pr-review.md`, consumer-repo propagation, cross-repo
  cooldown, and any §6-breaking rename.

One open question remains for the reviewer to resolve before
implementation starts:

- **OQ1 — Auto-applied label for structured-task form.** Phase 8's
  `.github/ISSUE_TEMPLATE/structured-task.yml` auto-applies
  `ai:clarification` so the orchestrator's entry point is identical
  to the free-form issue path. If the orchestrator's "well-formed
  structured submission" path should skip clarify entirely and land
  on `ai:planning` directly, the implementer should change the
  template's `labels:` line accordingly. Recommended default
  (matches free-form path): keep `ai:clarification`. Reviewer to
  confirm during implementation.

Deferred / explicitly-excluded items are enumerated in
`docs/awesome-cc-future-improvements.md` (shipped same PR) with
rationale and revisit triggers. That doc has its own Open Questions
section; do not duplicate here.

## References

- **External source:**
  <https://github.com/hesreallyhim/awesome-claude-code> — curated
  catalog of Claude Code skills, hooks, slash-commands, agent
  orchestrators, applications, and plugins. 43.9k stars at survey
  time (2026-05-16). 226 catalog rows × 20 columns in
  `THE_RESOURCES_TABLE.csv`.

- **Per-source files cited in goals (path is relative to the
  awesome-claude-code repo):**
  - `.claude/commands/evaluate-repository.md:1-150` — curator
    rubric. Goal 1, Phase 1.
  - `THE_RESOURCES_TABLE.csv` + `scripts/readme/` +
    `.pre-commit-config.yaml:30-36` — CSV-source + drift-check
    pattern. Goal 2, Phase 2.
  - `tools/readme_tree/update_readme_tree.py` +
    `tools/readme_tree/config.yaml` — tree-marker drift checker.
    Goal 3, Phase 3.
  - `scripts/resources/detect_informal_submission.py` +
    `tests/fixtures/informal_issues/*.json` — informal-issue
    heuristic + fixtures pattern. Goal 4, Phase 4.
  - `scripts/ids/generate_resource_id.py` — resource-ID scheme.
    Goals 5 and 7, Phases 5 and 7.
  - `templates/resource-overrides.yaml` —
    overrides-with-auto-locking pattern. Goal 6, Phase 6.
  - `scripts/utils/repo_root.py` — repo-root resolver. Goal 7,
    Phase 7.
  - `.github/ISSUE_TEMPLATE/recommend-resource.yml` +
    `docs/HOW_IT_WORKS.md` — issue-form + state machine inspiration.
    Goal 8, Phase 8.
  - `resources/slash-commands/` +
    `resources/official-documentation/Claude-Code-GitHub-Actions/`
    — curated material for our annotated index. Goal 9, Phase 9.

- **In-repo precedent:**
  - `docs/plans/apply-ai-tools-learnings-plan.md` — dual-doc shape
    template (plan + companion future-improvements doc). This plan
    mirrors its structure.
  - `docs/ai-tools-future-improvements.md` — sibling companion doc
    from the prior precedent.
  - `docs/completed/ai-code-review-learnings-plan.md` — similar
    "borrow from external system" plan.

- **In-repo constraint sources:**
  - `CLAUDE.md` §§5, 6, 9, 10, 13, 14, 15 — interactive-session
    constraints cited above.
  - `agents.md` "Stable log prefixes (contractual)" — cited by
    Phase 5's stable-ID convention.
  - `scripts/ai_memory_lib.py:480` `make_record_id` — the existing
    helper Phase 5 pins and Phase 7 wraps.
  - `scripts/codex_model_catalog.json` — source-of-truth JSON
    Phase 2 generates from.
  - `.github/workflows/ci.yml` — CI host for Phase 2's drift-check
    step.

- **Companion doc:** `docs/awesome-cc-future-improvements.md`
  (shipped same PR) — deferred items, excluded items, observed
  notes, and revisit triggers.
