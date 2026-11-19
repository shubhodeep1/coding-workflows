# Future Improvements from GSD (Deferred / Contentious Items)

## Purpose

This is a **backlog / consideration doc**, not an action plan. It enumerates
the items surfaced by the research into
[`gsd-build/get-shit-done`](https://github.com/gsd-build/get-shit-done) that
the companion implementation plan
(`docs/plans/gsd-inspired-improvements-plan.md`) deliberately did NOT adopt.

Each item lists what it would do, the source mechanism in gsd-build, pro /
con, estimated cost, the open decisions that have to be made before a real
implementation plan can be drafted, and any identified conflicts with this
repo's existing conventions (CLAUDE.md §6 naming immutability, §10 MongoDB,
§14 consumer repos, §15 GitHub API hygiene).

The list is ordered roughly by structural cost (smallest first). Items
**S1–S5** are "structural / scope refactor" candidates excluded from the
adopted-now plan because they touch the interactive Claude Code surface or
require a renderer / installer / packaging refactor. Items **C1–C5** are
"contentious" candidates flagged because they conflict with — or materially
reinterpret — an existing rule or convention.

This doc is shipped alongside the implementation plan so reviewers can see
what was considered and consciously deferred. When a future PR decides to
adopt any of these items, the corresponding section here should be moved
to a new `docs/plans/<slug>-plan.md` with the open decisions resolved.

The naming and structure of this doc mirror `docs/ai-tools-future-improvements.md`
verbatim so reviewers familiar with that file can navigate this one without
re-learning the layout.

---

## S1. Interactive `.claude/commands/` surface mirroring gsd's user-loop

### What it would do

Add a slash-command surface to `.claude/commands/` that mirrors the
six-step gsd-build loop adapted to our org-scale model:

- `/discuss-issue` (mirrors `/gsd-discuss-phase`) — gather phase-specific
  implementation decisions (layouts, API shapes, error handling) for an
  open issue before triggering `/clarify` or `/plan`.
- `/verify-pr` (mirrors `/gsd-verify-work`) — walk a human reviewer
  through a merged PR with an auto-diagnosis sub-agent attached.
- `/map-repo` (mirrors `/gsd-map-codebase`) — produce a structured
  one-shot codebase analysis for a consumer repo before its first
  pipeline run.

Today we have three interactive commands (`/analyze-log`,
`/investigate-issue`, `/write-plan`). gsd-build ships 67 plus six
namespace meta-skills. The proposed three are the minimum-viable subset
that fills genuine gaps in our pipeline.

### Source

- gsd-build `commands/gsd/discuss-phase.md`,
  `commands/gsd/verify-work.md`, `commands/gsd/map-codebase.md`.
- gsd-build README §"How It Works" — the loop diagram.
- gsd-build `docs/INVENTORY.md` §"Commands (67 shipped)".

### Pro

- **Closes a real gap.** Our `/clarify` is binary
  (Q&A-or-clear); `/discuss-issue` would let a human walk the issue
  through "here's how I want this built" *before* clarify runs, so
  clarify becomes a sanity check on a richer input. This is exactly
  the niche `/gsd-discuss-phase` fills.
- **Reusable across consumer repos.** Slash commands in
  `.claude/commands/` propagate via the `update_workflows.yml` flow
  (after extending it to copy `.claude/commands/` alongside
  `.github/workflows/`).
- **Low-blast-radius.** Slash commands are opt-in user surface; no
  unattended pipeline behaviour changes.

### Con

- **User scoped this doc to NOT adopt the interactive surface.** The
  clarification round explicitly excluded "Interactive Claude Code
  surface" from the target areas. This item is in the companion doc
  precisely because the user wants it surfaced but not shipped now.
- **Parallel-surface risk.** A `/discuss-issue` command competes
  with the existing `/clarify` phase. Operators may not know which
  to run first. Needs explicit precedence rules.
- **Propagation lift.** `.claude/commands/` is not in
  `update_workflows.yml`'s copy set today; adding it touches the
  consumer-repo propagation contract (§14).

### Estimated cost

- 3–4 hours to draft each of the three command files.
- 2 hours to extend `update_workflows.yml` for `.claude/commands/`
  propagation.
- 1 hour to document precedence vs `/clarify` in `agents.md`.
- Total: ~10–15 hours.

### Open decisions before this becomes a real plan

- **Precedence vs `/clarify`.** Does `/discuss-issue` always run
  before `/clarify`? Is it gated by an issue label? Operator-triggered
  only?
- **Output artefact.** gsd-build writes per-phase `CONTEXT.md` and
  `DISCUSSION-LOG.md`. We don't have an analogous filesystem state
  store; do we (a) write into a new `.ai/discussion/` directory,
  (b) post structured comments on the GitHub issue, or (c) persist
  into the existing `ai-memory` branch as a new schema?
- **Scope of `/verify-pr`.** Read-only check, or wired into the
  review_autofix loop?
- **Map-repo timing.** First time a consumer repo onboards, or on
  every `update_workflows.yml` dispatch?

### Known conflicts

- **§14 consumer repos.** `.claude/commands/` propagation is new;
  every repo in `.github/ai/consumer_repos.json` would receive the
  new commands by default. Either gate the copy behind
  `vars.WORKFLOW_PROFILE` (interacts with the adopted-now plan's
  item 9) or document the new propagation surface explicitly.
- **§6 naming.** Once a slash command is documented in a consumer
  repo's README, renaming `/discuss-issue` becomes a breaking
  change. Commit to vocabulary up front.

---

## S2. Two-stage hierarchical routing for slash commands (namespace meta-skills)

### What it would do

If S1 lands and our slash-command surface grows past ~10 commands,
adopt gsd-build's namespace meta-skill pattern: six top-level
"router" skills (`coding-workflow`, `coding-quality`,
`coding-ops`, …) each containing a routing table that points at
concrete sub-commands. The model sees ~120 tokens of router
descriptors per turn instead of a flat 50-command listing.

### Source

- gsd-build `commands/gsd/ns-workflow.md`, `commands/gsd/ns-project.md`,
  `commands/gsd/ns-quality.md`, `commands/gsd/ns-context.md`,
  `commands/gsd/ns-manage.md`, `commands/gsd/ns-ideate.md`.
- gsd-build [#2792](https://github.com/gsd-build/get-shit-done/issues/2792) —
  rationale: eager skill listing scales as O(N tokens) per turn.
- gsd-build `docs/ARCHITECTURE.md` §"Two-stage hierarchical routing".

### Pro

- **Bounds per-turn token cost** as the surface grows. gsd-build
  reports 2150 tokens → 120 tokens (~94 % saving).
- **Future-proof for surface growth.** If we adopt S1's three
  commands plus expand over time, routing scales linearly.
- **Tool Attention research backed.** gsd-build cites
  "keyword-dense tags outperform prose for routing at ~40 % the
  token cost."

### Con

- **Premature at current surface size.** We have three commands. The
  break-even point is somewhere around 15–20 commands.
- **Indirection cost.** New contributors must learn the namespace
  layer before finding the concrete command.

### Estimated cost

- 2 hours per namespace router skill × 4–6 routers = 8–12 hours.
- Migration of existing slash-command users to the namespace form is
  silent (commands remain directly invocable).

### Open decisions

- **Trigger threshold.** At what surface size do we adopt? Propose
  ≥15 commands.
- **Namespace vocabulary.** Borrow gsd's six (`workflow`, `project`,
  `quality`, `context`, `manage`, `ideate`) or invent our own?

### Known conflicts

- **§6 naming.** Namespace skill names become identifiers and
  cannot be renamed without an alias migration once consumer repos
  reference them.

---

## S3. Filesystem-native persistent per-issue workspace (`.planning/`-style)

### What it would do

Mirror gsd-build's `.planning/` state model: a per-issue (or
per-tracking-issue) filesystem-native directory containing
`PROJECT.md`, `REQUIREMENTS.md`, `ROADMAP.md`, `STATE.md`,
`CONTEXT.md` that survives context resets and is human-inspectable.

Today we have the `ai-memory` git branch with structured candidate
records, which is conceptually similar but git-branch-shaped rather
than working-tree-shaped. The trade-off is inspectability vs
portability.

### Source

- gsd-build `docs/ARCHITECTURE.md` §"File-Based State" — design
  principles.
- gsd-build `docs/STATE-MD-LIFECYCLE.md` — state document
  lifecycle.
- gsd-build hooks `gsd-phase-boundary.sh` and
  `gsd-validate-commit.sh` — runtime hooks that detect
  `.planning/` writes and emit reminders.

### Pro

- **Inspectability.** A human checking out a consumer-repo branch
  sees the current issue state in `.planning/STATE.md` directly,
  without `gh api` or a memory branch checkout.
- **Survives context resets.** Today our `ai-memory` records do
  survive but require a separate branch checkout to inspect.
- **Hook-friendly.** The runtime hooks in gsd-build
  (`gsd-phase-boundary.sh`, `gsd-validate-commit.sh`) wire neatly
  into a filesystem state store.

### Con

- **Working-tree pollution.** Every consumer repo would gain a
  `.planning/` directory in its working tree. Some operators will
  see this as noise.
- **Race conditions.** Concurrent workflow runs writing into the
  same `.planning/` directory on the same branch race; our existing
  `ai-memory` branch model serialises via git push retries.
- **Duplicates `ai-memory`.** We'd have two state systems
  (filesystem `.planning/` + memory branch) unless one is retired.

### Estimated cost

- ~30 hours to design the schema (and reconcile with `ai-memory`).
- ~20 hours to wire writes from each phase.
- ~10 hours to migrate downstream consumers.
- Total: ~60 hours.

### Open decisions

- **Coexist with `ai-memory` or replace it?** Replace requires
  migrating every existing schema; coexist doubles maintenance.
- **`.gitignore` posture.** Commit `.planning/` to the consumer
  branch, or `.gitignore` it and rely on workflow-artefact upload?
- **Multi-issue isolation.** Per-issue subdirectory
  (`.planning/issue-<N>/`) vs single shared.

### Known conflicts

- **§10 MongoDB.** Our memory subsystem is documented as memory-branch
  backed (`README.md` §"Memory System"). Adding a filesystem store
  alongside is an architectural shift that requires a new contract
  document, even though there's no MongoDB collection involved.
- **§14 consumer repos.** Every consumer repo would gain
  `.planning/` directories; operators must opt-in or have the
  directory `.gitignore`d.

---

## S4. Active runtime hooks for interactive Claude Code sessions

### What it would do

Port the gsd-build hook surface into `.claude/hooks/` so interactive
Claude Code sessions in this repo (and propagated to consumer repos)
gain:

- **Context-rot warning hook** (`gsd-context-monitor.js`) —
  PostToolUse hook that injects WARNING at 35 % context remaining,
  CRITICAL at 25 %. Debounce 5 tool uses between warnings.
- **Statusline metrics writer** (`gsd-statusline.js`) — writes
  `/tmp/claude-ctx-{session_id}.json` consumed by the context
  monitor.
- **Phase-boundary reminder** (`gsd-phase-boundary.sh`) —
  PostToolUse hook that emits a `STATE.md` update reminder when a
  planning file is modified.
- **Prompt-injection PreToolUse guard** (`gsd-prompt-guard.js`) —
  scans content being written into a designated state directory
  (`.ai/` or `.planning/`) for 14 injection patterns. Advisory
  warning, does not block.
- **Read-injection scanner** (`gsd-read-injection-scanner.js`) —
  scans content being read INTO context for injection patterns.

This is the interactive-session counterpart to the adopted-now
**Item 5** (memory-write injection guard for the unattended
pipeline).

### Source

- gsd-build `hooks/gsd-context-monitor.js` (8.1 KB).
- gsd-build `hooks/gsd-statusline.js` (22.6 KB — the largest hook).
- gsd-build `hooks/gsd-phase-boundary.sh` (1.9 KB, opt-in via
  `hooks.community: true`).
- gsd-build `hooks/gsd-prompt-guard.js` (3.5 KB).
- gsd-build `hooks/gsd-read-injection-scanner.js` (5.5 KB).

### Pro

- **Genuinely novel.** Context-rot warning to the *agent* (not just
  the user via the statusline) is a clever pattern — gsd-build
  literally comments "the statusline only shows the user; this
  hook makes the AGENT aware." We have no analogue.
- **Defence in depth on injection.** Item 5 covers memory writes;
  these hooks would cover Claude Code-side reads and writes too.
- **Opt-in per hook.** gsd-build's phase-boundary hook is opt-in via
  `.planning/config.json` `hooks.community: true`. Adoptable as
  `vars.CC_HOOKS_ENABLED`.

### Con

- **Out of scope per user's clarification.** Interactive Claude Code
  surface was explicitly excluded from the adopted-now plan.
- **`.claude/hooks/` propagation contract.** Today our `.claude/hooks/`
  contains only `session-start.sh`. Adding multi-hook surface
  changes the propagation footprint.
- **Node.js dependency.** Several hooks are `node` scripts. Today
  our `.claude/hooks/session-start.sh` is bash-only.

### Estimated cost

- ~4 hours per hook × 5 hooks = 20 hours.
- ~2 hours to wire propagation through `update_workflows.yml`.
- ~3 hours to document opt-in toggles.
- Total: ~25 hours.

### Open decisions

- **Which hooks to port first?** Context-monitor is the highest-leverage
  candidate (genuinely novel, immediately useful).
- **Node.js or bash port?** gsd-build's hooks are mostly Node. A
  bash port is feasible for the simpler hooks but loses gsd's
  structured-JSON output contract.
- **Statusline integration.** Claude Code's statusline mechanism
  is partially documented; depending on platform, the
  `/tmp/claude-ctx-*` bridge file pattern may or may not work
  unmodified.

### Known conflicts

- **§6 naming.** Hook filenames in `.claude/hooks/` are referenced
  by the Claude Code harness via `settings.json`; renames break
  the harness. Commit to filenames up front.
- **§14 consumer repos.** Adding hooks to the propagation set
  changes what every consumer repo gets on the next
  `update_workflows.yml` run.

---

## S5. Filesystem-native `@-reference` syntax in prompts

### What it would do

Replace the `{{REFERENCE_*}}` placeholder mechanism (the adopted-now
plan's item 2) with gsd-build's filesystem-native `@-reference`
syntax: phase prompts contain literal
`@~/.claude/get-shit-done/references/verification-loop.md`
references that the runtime resolves at load time.

### Source

- gsd-build `agents/gsd-planner.md` line:
  `@~/.claude/get-shit-done/references/mandatory-initial-read.md`.
- gsd-build `docs/ARCHITECTURE.md` §"References" — shared knowledge
  documents.

### Pro

- **Renderer-free.** No `scripts/render_prompt.sh` placeholder
  resolution step; the agent runtime handles the include.
- **Standard convention.** Matches Claude Code's documented
  `@-reference` syntax for skills.

### Con

- **Runtime-dependent.** codex-cli does not currently resolve
  `@-references` in mode prompts; only the Claude Code harness
  does. Adopting this would require either (a) a renderer-side
  expansion that *implements* `@-reference` resolution, defeating
  the purpose, or (b) an upstream codex-cli feature request.
- **`{{...}}` mechanism already works.** The adopted-now plan's
  item 2 reuses existing renderer behaviour.

### Estimated cost

- ~10 hours to design the resolution mechanism.
- ~15 hours to migrate every reference from `{{...}}` to `@-ref`.
- Total: ~25 hours.

### Open decisions

- **Runtime support.** Wait for codex-cli `@-reference` support,
  or layer a Claude-Code-only path that uses native `@-refs` and a
  codex-cli-only path that uses `{{...}}`?

### Known conflicts

- **§5 minimal change set.** Two mechanisms for the same purpose
  competes; we should converge before adopting.

---

## C1. CONTEXT.md predicate-only format replacing prose in `agents.md`

### What it would do

Refactor `agents.md` from its current prose-and-tables format to
gsd-build's machine-greppable single-line predicate format:

```
LOG_PREFIX.LABEL_REPAIR=present
PHASE.clarify.default_model=openai/gpt-5.4
PHASE.implement.editor_idle_timeout=1200
```

Today `agents.md` mixes prose, tables, and code-fence blocks. The
gsd-build pattern is one fact per line, no prose.

### Source

- gsd-build `CONTEXT.md` header rule:
  > "Format: this document is machine-greppable. Each operational
  > fact is a single-line predicate (`CLASS.subkey=value`). Agent
  > briefs cite predicates by ID verbatim — never paraphrase from
  > this file."

### Pro

- **Grep-friendly.** `grep '^PHASE\.implement\.' agents.md` would
  return every implement-phase fact in one shot.
- **Drift-resistant.** Predicate format is parseable; we could
  layer a test that asserts every predicate corresponds to a real
  config knob.

### Con

- **Human readability.** Predicate format is hard to read at scale.
  gsd-build mitigates with a glossary at the top; ours would need
  the same.
- **Conflicts with the adopted-now item 6.** That item adds
  predicates *alongside* prose. C1 is the wholesale replacement.

### Estimated cost

- ~8 hours to refactor every existing prose section into predicates.
- ~3 hours to add a glossary header.
- ~4 hours to add a drift-test that asserts predicate ↔ config
  parity.
- Total: ~15 hours.

### Open decisions

- **Prose alongside or instead?** The adopted-now item 6 picks
  "alongside"; C1 is "instead."
- **Predicate vocabulary commitment.** Once committed, predicate
  names are §6-protected identifiers.

### Known conflicts

- **§6 naming.** Predicate names (`LOG_PREFIX.LABEL_REPAIR`,
  `PHASE.implement.editor_idle_timeout`) become stable identifiers.
- **Existing prose consumers.** Some prompts grep for prose strings
  in `agents.md` today (e.g.
  `unattended_system_instructions.md` references "Stable log
  prefixes" by string). Removing the prose breaks those.

---

## C2. Default flip to gsd's "absent = enabled" feature-flag pattern

### What it would do

Switch our feature-flag defaults from explicit-true (today every
env-var has an explicit default) to gsd-build's pattern: absent
key in `vars.*` defaults to `true`. Users explicitly disable
features they don't want.

### Source

- gsd-build `docs/ARCHITECTURE.md` §"Absent = Enabled":
  > "Workflow feature flags follow the absent = enabled pattern.
  > If a key is missing from `config.json`, it defaults to `true`.
  > Users explicitly disable features; they don't need to enable
  > defaults."

### Pro

- **Onboarding simplicity.** Consumer repos start with every
  feature on by default; operators turn things off if they don't
  want them.
- **Aligns with our existing `OPENROUTER_PROMPT_CACHE_DISABLED`
  pattern** (kill-switch model) — the inverse of an enable-flag.

### Con

- **Conflicts with §0 prime directive.** "If you are not 100 %
  certain the outcome matches the user's expectations: STOP."
  Defaulting absent to enabled means the system silently does
  more than the operator may have configured.
- **Bad for unattended pipelines.** Our pipelines run on cron;
  silent feature activation that the operator missed in the
  release notes is a real risk.
- **Explicit-default discipline is already in CLAUDE.md §4** —
  "Always provide defaults for new env vars unless explicitly
  told otherwise. Preserve all existing env var names."

### Estimated cost

- ~5 hours to refactor every default-true env-var (~30 such vars).
- ~2 hours to document the migration.
- Total: ~7 hours.

### Open decisions

- **Scope.** All vars, only new vars, or only a designated
  subset?
- **Migration.** Existing consumer repos with `vars.X=false`
  continue to work, but absent → true means an operator who
  *expected* `vars.X` to mean "off by default" gets surprised.

### Known conflicts

- **CLAUDE.md §0 / §4.** This is a deliberate inversion of our
  current explicit-default discipline.
- **§14 consumer repos.** Existing consumer repos have implicit
  contracts with the explicit-default model; switching is a
  documented breaking change.

---

## C3. Adversarial / FORCE-stance language in judge and reviewer prompts

### What it would do

Add gsd-build's `<adversarial_stance>` block — verbatim:
> "FORCE stance: Assume every plan set is flawed until evidence
> proves otherwise. Your starting hypothesis: these plans will not
> deliver the phase goal. Surface what disqualifies them."

— to:

- `prompts/mode-judge.txt`
- `prompts/mode-judge-review-blocked.txt`
- `prompts/review-reviewer-checklist.txt`

The adopted-now plan's item 4 already requires explicit severity
classification (a milder form of the same discipline). C3 is the
stronger language.

### Source

- gsd-build `agents/gsd-plan-checker.md` `<adversarial_stance>`
  block.

### Pro

- **Reduces "looks-good rubber-stamp" risk** on long-running
  autofix loops where a tired judge approves a PR that subtly
  drifts from the issue.
- **Forces evidence-backed verdicts.** Pairs naturally with the
  reviewer-fabrication ban already adopted in
  `apply-ai-tools-learnings-plan.md` goal 13.

### Con

- **Over-rejection risk.** A reviewer that "assumes flawed until
  proven" can wedge a benign PR. Today's reviewer floor
  (`scripts/review_floor_rules.sh`) already promotes 2-reviewer-
  agreed nearby findings; layering adversarial stance on top
  could push the panel to manufacture findings to satisfy the
  framing.
- **Cost.** Adversarial-stance language tends to expand reviewer
  output (defending the rejection becomes part of the loop). Our
  consensus summariser already truncates at
  `XPOLL_SUMMARISER_LINES_PER_REVIEWER=160`; bigger reviewer
  outputs mean more aggressive truncation.

### Estimated cost

- ~2 hours per prompt × 3 prompts = 6 hours.
- ~4 hours of bake on a smoke consumer repo to measure rejection-
  rate impact.
- Total: ~10 hours.

### Open decisions

- **Calibration.** "Assume flawed" or softer "verify before
  accepting"? The adopted-now item 4 picks the softer form;
  this is the explicit-flag version.
- **Per-pass calibration.** Apply only to pass 2 reviewers (after
  pass 1 cross-pollination) so pass 1 stays broad?

### Known conflicts

- **None per §6/§10/§14/§15.** This is purely prompt language.
- **Existing reviewer two-pass discipline.** `ENABLE_REVIEWER_TWO_PASS`
  changes panel behaviour; adversarial stance would interact with
  that and may need per-pass scoping.

---

## C4. Forced per-phase prompt size shrink to <500 lines

### What it would do

Push every `prompts/mode-*.txt` and `prompts/review-*.txt` under
the gsd-build DEFAULT tier (1000 lines for workflows, but gsd's
*agent* prompts target <500 lines after the v1.39+ progressive
disclosure refactor). The adopted-now item 1 commits to soft
tiers (250 / 500 / 800 lines) with budget enforcement; C4 is the
forced shrink to a single sub-500 ceiling.

### Source

- gsd-build `docs/ARCHITECTURE.md` §"Progressive disclosure for
  workflows" — the v1.39 budget regime.
- gsd-build `workflows/discuss-phase/` decomposition pattern
  (parent dispatches to `modes/<mode>.md` + `templates/*.md`).

### Pro

- **Smaller prompts cache better.** Reduces per-turn token cost
  across every phase.
- **Forces decomposition.** Long prompts decay; smaller ones
  stay focused.

### Con

- **Aggressive.** `mode-validate-generate.txt` is 809 lines and
  has 22 self-referential sections; forcing <500 needs a real
  decomposition into `prompts/mode-validate-generate/` subdir.
- **Conflicts with existing prompt-evolution patterns.** Our
  prompts grow organically as fix-up discoveries land
  (`apply-ai-tools-learnings-plan.md` added ~10 items to several
  prompts).

### Estimated cost

- ~6 hours per prompt that exceeds the new ceiling × 4 prompts
  (validate-generate, validate-fix-harness, validate-diagnose,
  judge-review-blocked) = 24 hours.
- ~4 hours of regression smoke.
- Total: ~30 hours.

### Open decisions

- **One ceiling or tiered?** The adopted-now item 1 picks tiered;
  C4 is single-ceiling.
- **Decomposition target.** Subdirectories (gsd's pattern) or
  `{{...}}` reference blocks (our pattern)?

### Known conflicts

- **§5 minimal change set.** Decomposition is a structural
  refactor.

---

## C5. NPM-installable consumer-repo bootstrap (replace `update_workflows.yml`)

### What it would do

Replace the `update_workflows.yml` `repository_dispatch`-based
propagation model with an npm CLI: `npx coding-workflows-cc@latest`
installs the wrappers into a consumer repo, à la
`npx get-shit-done-cc@latest`.

### Source

- gsd-build `package.json` + the
  `npx get-shit-done-cc@latest` install path.
- gsd-build `docs/USER-GUIDE.md` install profiles.

### Pro

- **Operator UX.** A single `npx` command vs four GitHub
  Secrets + repo-vars setup.
- **Versioned releases.** npm tags map naturally to our
  `@stable` ref scheme.
- **Cross-runtime.** Same installer can target multiple AI
  runtimes (Claude Code, codex-cli, Cursor, Windsurf).

### Con

- **Loses our `@stable` dispatch model.** The current model
  pushes updates to consumer repos automatically when
  `coding-workflows@stable` is tagged; an npm installer is
  pull-based and operators must re-run.
- **New surface to maintain.** A Node CLI is a new package,
  release pipeline, npm credentials, etc.
- **Conflicts with our §14 consumer registry.** The dispatch
  model is built around the registry; an npm pull model
  doesn't need a registry at all.

### Estimated cost

- ~40 hours to design the CLI.
- ~30 hours to implement (Node code, install routines,
  permission rewrites).
- ~10 hours of consumer-repo migration documentation.
- Total: ~80 hours.

### Open decisions

- **Coexist or replace?** Coexist means double-maintenance.
- **Auto-update.** If npm, do consumer repos get a stale
  install warning? gsd-build runs `gsd-check-update-worker.js`
  for this.
- **`@stable` semantics.** Map npm tags to `@stable` /
  `@beta` / `@canary`?

### Known conflicts

- **§14 consumer repos.** Replaces the dispatch-driven
  propagation model entirely. Every repo in
  `.github/ai/consumer_repos.json` would need a migration.
- **§5 minimal change set.** This is a structural rewrite of
  the propagation contract.

---

## Index

- **S1** — Interactive `.claude/commands/` surface mirroring gsd's
  user-loop. ~15 h.
- **S2** — Two-stage hierarchical routing for slash commands. ~12 h.
- **S3** — Filesystem-native persistent per-issue workspace. ~60 h.
- **S4** — Active runtime hooks for interactive Claude Code. ~25 h.
- **S5** — Filesystem-native `@-reference` syntax in prompts. ~25 h.
- **C1** — CONTEXT.md predicate-only format. ~15 h.
- **C2** — Default flip to "absent = enabled". ~7 h.
- **C3** — Adversarial / FORCE-stance language. ~10 h.
- **C4** — Forced per-phase prompt size shrink. ~30 h.
- **C5** — NPM-installable consumer-repo bootstrap. ~80 h.
