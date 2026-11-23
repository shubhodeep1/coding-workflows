# Claude Code Tooling Learnings — Adoption Plan

## Summary

Adopt a focused set of additive learnings from four external Claude Code
ecosystem repos (`davila7/claude-code-templates`,
`shanraisshan/claude-code-best-practice`,
`VoltAgent/awesome-claude-code-subagents`,
`Piebald-AI/claude-code-system-prompts`) into the unattended codex-cli
phase prompts (`prompts/mode-*.txt`, `prompts/review-*.txt`,
`prompts/conflict-resolver.txt`), the unattended pipeline system context
(`unattended_system_instructions.md`), and the interactive Claude Code
system context (`CLAUDE.md`). The most visible structural change is an
additive split of `CLAUDE.md` into per-section files under
`.claude/rules/*.md` with the top-level `CLAUDE.md` rewritten as a thin
index that preserves every existing `§<N>` anchor, so interactive
sessions can lazy-load rules without breaking the ~12 in-tree
`CLAUDE.md §<N>` references in `.github/workflows/`, `scripts/`, and
`prompts/`. No identifier is renamed; no MongoDB contract is touched;
the `.claude/commands/` and `workflow-templates/.claude/` surfaces are
explicitly out of scope per the clarification round.

## Context

The trigger is the user's question "go through the below and tell me
what improvements and learnings we can incorporate into our
repo/project" pointing at:

1. `davila7/claude-code-templates` — NPM CLI that installs Claude Code
   components (agents, commands, MCPs, settings, hooks, skills) plus a
   plugin dashboard / analytics surface. Most of its product surface is
   marketplace-shaped and orthogonal to a workflow-library repo; the
   reusable learning is the **catalog packaging convention** for
   subagent / command / skill bundles.
2. `shanraisshan/claude-code-best-practice` — Curated best-practices
   doc covering CLAUDE.md sizing, `.claude/rules/*.md` lazy-loading,
   skill design (Gotchas sections, dynamic shell injection), planning
   methodology (vertical-slice "tracer bullets", phase-wise gated
   plans), prompting techniques ("grill me on these changes", "prove
   this works"), and orchestration patterns (Command → Agent → Skill).
3. `VoltAgent/awesome-claude-code-subagents` — 131+ subagent catalog
   with a uniform YAML-frontmatter format (`name`, `description`,
   `tools`, `model`) and a hyphenated-lowercase naming convention, plus
   a tool-access tier philosophy (read-only / research / code-writer).
4. `Piebald-AI/claude-code-system-prompts` — Reference catalog of
   Claude Code's own ~110 prompt strings, each filed as a separate
   markdown with descriptive filename, token-count annotation, and
   conditional-inclusion context — across 180+ Claude Code versions.

The closest in-repo precedent is
`docs/plans/apply-ai-tools-learnings-plan.md`, which adopted 13
additive items from the `x1xhlol/system-prompts-and-models-of-ai-tools`
collection into the same two governance files (`CLAUDE.md`,
`unattended_system_instructions.md`) plus a subset of `prompts/mode-*.txt`.
This plan follows that precedent's shape: each adopted item names a
source repo, a target file in this repo, and the proposed wording.

The clarification round (`Q1`–`Q4`) pinned the scope tightly:

- **Q1 (slug) = A** — `claude-code-tooling-learnings`.
- **Q2 (target surface) = C + D** — unattended `prompts/mode-*.txt`
  and top-level `CLAUDE.md` / `unattended_system_instructions.md`
  only. **Repo-local `.claude/commands/` and the
  `workflow-templates/.claude/` propagation surface are out of scope**
  for this plan. (Note: Q4=B implicitly authorises **new** files under
  `.claude/rules/` because the CLAUDE.md restructure cannot land
  without them; existing `.claude/commands/` and `.claude/hooks/`
  remain untouched.)
- **Q3 (deliverable format) = B** — concrete plan only, no
  future-considerations sidecar. Items not adopted are dropped from
  this plan rather than parked.
- **Q4 (CLAUDE.md restructure) = B** — additive `.claude/rules/*.md`
  extraction with `CLAUDE.md` as the thin index. The §-numbers
  (anchors) remain authoritative and are preserved at the index level
  so existing `CLAUDE.md §<N>` references in workflow/script/prompt
  comments still resolve.

CLAUDE.md sections that bind this work (cited verbatim where they
constrain the design):

- **§5 Minimal Change Set** — "Extend existing mechanisms — never
  compete with them." The `.claude/rules/` split extends CLAUDE.md
  rather than replacing it.
- **§6 Backward Compatibility / Naming Immutability** — "NEVER
  rename, remove, or repurpose existing identifiers" — and crucially
  its last paragraph: "Section numbers in this file are also covered
  by §6 — they are referenced from `.github/workflows/`, `scripts/`,
  and `prompts/` and must not be renumbered." Section §0 through §17
  remain at their current numbers; the rule-file basenames and the
  index entries both encode the §-number so future renumbering would
  still be a §6 violation.
- **§7 Output Requirements** — "List all files changed with line
  ranges of major logic changes." Reflected in `## Files & Modules`.
- **§14 Consumer Repo Registry** — only touched if the plan
  propagated assets through `workflow-templates/`; this plan does
  not, so `.github/ai/consumer_repos.json` is unchanged.
- **§15 GitHub API Call Hygiene** — only binds if the plan added
  new `gh api` calls; this plan adds zero.

## Goals

Each goal is falsifiable by re-reading the named target file after
the corresponding step lands.

1. **`CLAUDE.md` as a thin index** — every §-section body is
   relocated to `.claude/rules/<NN>-<slug>.md`; the top-level
   `CLAUDE.md` retains the title, the pre-task context-loading
   preamble, the §-numbered table of contents, and per-section
   one-liners that point at the rule file. Total CLAUDE.md line
   count drops to ≤200 lines (shanraisshan target). The
   FINAL REMINDER block stays in CLAUDE.md as a non-skippable
   tail.
2. **No `CLAUDE.md §<N>` reference breaks** — every existing
   reference (`grep -rn "CLAUDE\.md §" .github/ scripts/ prompts/`)
   continues to resolve unambiguously after the split: the §-number
   is preserved in the CLAUDE.md index, the rule filename, and the
   `#L<n>` anchor of the per-rule heading.
3. **Lazy-loading frontmatter on every rule file** — each
   `.claude/rules/*.md` carries YAML frontmatter with a `description`
   field phrased as a trigger ("Applies when …") so interactive
   Claude Code sessions can lazy-load the file via Claude Code's
   skills-style discovery rather than including the full body in
   every system prompt. (Source: shanraisshan.)
4. **Codex-cli prompts unchanged in behavior, enriched in structure**
   — every `prompts/mode-*.txt` and `prompts/review-*.txt` gains a
   uniform header block ([`role:`, `description:`, `tool-access:`,
   `approx_tokens:`, `loaded_by:`]) modelled on the VoltAgent
   frontmatter convention and the Piebald-AI catalog convention.
   Header is plain-text key/value pairs (NOT YAML — codex-cli
   reads these files as raw prompts and would otherwise embed the
   frontmatter into the model's context as-is; we want it
   human-and-grep-friendly, not parsed). Behavior unchanged.
5. **"Gotchas" sections in the four largest mode prompts** —
   `mode-validate-generate.txt` (809 lines),
   `mode-validate-fix-harness.txt` (276 lines),
   `mode-validate-diagnose.txt` (198 lines), and
   `mode-judge-review-blocked.txt` (156 lines) each gain a
   `## Gotchas` section near the tail that codifies known failure
   patterns observed in workflow log analysis runs. (Source:
   shanraisshan skill-design.)
6. **"Challenge yourself" idioms in judge / repair prompts** —
   `mode-judge.txt`, `mode-judge-review-blocked.txt`,
   `mode-implement-diagnose.txt`, and `mode-validate-self-heal.txt`
   each gain a one-line "Before declaring done, grill yourself on:
   …" prompt that lists the 2–4 verifications the phase tends to
   skip in practice. (Source: shanraisshan.)
7. **`unattended_system_instructions.md` gains a verification-gate
   ordering rule and a "tool-access tier" annotation convention**
   under existing §15 (Role-Specific Behavior). Additive — §15
   already enumerates roles; the addition extends rather than
   competes. (Source: shanraisshan + VoltAgent.)
8. **`agents.md` documents the new header convention** —
   one new subsection ("Phase-prompt header convention") records the
   five-field plain-text header introduced by Goal 4 and references
   the Piebald-AI source. Repo-architectural facts only — no global
   engineering rules duplicated.

## Non-goals

- **No changes to `.claude/commands/`** (`analyze-log.md`,
  `investigate-issue.md`, `write-plan.md`). Interactive slash-command
  surface is explicitly excluded by Q2.
- **No changes to `.claude/hooks/session-start.sh`.** Same exclusion.
- **No changes to `workflow-templates/.claude/`** (commands, hooks,
  settings, CLAUDE.md symlink). The propagation-to-consumers surface
  is explicitly excluded by Q2.
- **No new subagents.** The codex-cli phase prompts already perform
  the role of subagents in our pipeline; adding parallel Claude Code
  subagents would create two-master ambiguity. The VoltAgent learning
  is the **header convention**, not the agent set.
- **No prompt-directory reorganization.** Moving `prompts/mode-*.txt`
  into category subdirectories (`prompts/orchestrate/`,
  `prompts/review/`, …) would break the literal-path references in
  `.github/workflows/*.yml` and `scripts/*.sh` — a §6 breaking
  change. Header annotation gives the category metadata without the
  move.
- **No MCP server additions.** Codex-cli already has its own tool
  surface; davila7's MCP integration catalog is not adopted.
- **No marketplace / plugin / dashboard work** from davila7.
- **No consumer-repo propagation.** `.github/ai/consumer_repos.json`
  is unchanged. The `workflow-templates/CLAUDE.md` symlink already
  resolves to `../CLAUDE.md`, so the thin-index restructure
  propagates automatically the next time `update_workflows.yml`
  dispatches — but no new asset is added to the templates tree by
  this plan.
- **No future-considerations sidecar.** Per Q3=B, deferred items
  are dropped from the deliverable.
- **No prompt-content compression of `mode-validate-generate.txt`**
  beyond the additive Gotchas section. The 66KB prompt has accreted
  organically and a compression pass is a separate, riskier task.
- **No token-counting telemetry.** The `approx_tokens` header field
  is set once at adoption time by a one-shot script run and is
  refreshed only when the prompt body is materially edited; no
  CI gate enforces freshness.

## Constraints

- **§5 Minimal Change Set** — every adopted item is additive. The
  CLAUDE.md restructure is the largest delta but is structured as
  an extraction (move text from CLAUDE.md to
  `.claude/rules/*.md`) plus an index rewrite. No section body is
  edited beyond the addition of YAML frontmatter at the top of each
  extracted file.
- **§6 Naming Immutability** —
  - `§0`–`§17` remain at their current numbers.
  - Every existing `CLAUDE.md §<N>` reference in workflow YAML,
    shell scripts, Python scripts, and codex prompts must continue
    to resolve. The CLAUDE.md index preserves the §-number as the
    heading anchor (`## §6 — naming-immutability` → anchor
    `#6--naming-immutability`); rule filenames follow
    `<NN>-<slug>.md`; the FINAL REMINDER block stays in CLAUDE.md.
  - Phase prompt filenames (`mode-implement.txt`, `mode-judge.txt`,
    …) and the prompts/ directory layout are unchanged.
  - No env-var, no log key, no metric name is touched.
- **§7 Output Requirements** — file/line ranges enumerated in
  `## Files & Modules` and `## Implementation Steps`.
- **§9 Code Style** — `.claude/rules/*.md` files use 2-space YAML
  indentation in the frontmatter block (YAML spec); markdown body
  uses tab indentation only where code fences contain shell or
  Python (which the existing CLAUDE.md does not). `.editorconfig`
  is not added or changed.
- **§10 MongoDB Rules** — no contract changes; no collection or
  index is touched. Not applicable.
- **§13 Repository Hygiene** — no writes into `.git/**`.
- **§14 Consumer Repo Registry** — no propagation, so
  `.github/ai/consumer_repos.json` is unchanged. Documented under
  `## Non-goals` to make the absence explicit.
- **§15 GitHub API Call Hygiene** — zero new `gh api` /
  `gh_retry` / `gh pr` / `gh run` calls. The plan adds prompt text
  and rule files only; no script that issues API calls is touched.

## Approach

The plan is structured around three layers, each landed as a
separate commit so the reviewer can audit blast radius
incrementally:

**Layer 1 — `CLAUDE.md` extraction (largest blast radius).** A
single commit that creates `.claude/rules/00-prime-directive.md`
through `.claude/rules/17-preferred-tools.md` plus
`.claude/rules/_README.md`, and rewrites `CLAUDE.md` itself into a
thin index. The extraction is mechanical: each `## §<N>. <title>` block
in current CLAUDE.md becomes one rule file with `description`
frontmatter; the body is copied verbatim with no text edits.
Rationale for landing this first: every subsequent layer wants to
add cross-references into `.claude/rules/*.md`, so doing the move
first avoids churn.

**Layer 2 — Phase-prompt header annotation.** A single commit that
adds the five-field plain-text header to every `prompts/mode-*.txt`,
`prompts/review-*.txt`, and `prompts/conflict-resolver.txt`. Header
goes between the existing `prompts/header.txt` include (if present
at the call site — these prompts are appended to
`unattended_system_instructions.md` at run time per agents.md
"Phase prompts under `prompts/mode-*.txt` … are appended to
`unattended_system_instructions.md` at run time") and the existing
prompt body. The header is plain-text key/value pairs, not YAML
frontmatter, because codex-cli reads the file verbatim and any YAML
fence would be embedded in the model's context. Body unchanged.

**Layer 3 — Targeted prompt additions.** One commit per goal cluster
to make per-goal review easy:

- Commit 3a — Gotchas sections on the four largest mode prompts
  (Goal 5).
- Commit 3b — "Challenge yourself" idioms on judge / repair / heal
  prompts (Goal 6).
- Commit 3c — Verification-gate ordering and tool-access tier
  annotation in `unattended_system_instructions.md` §15 (Goal 7).
- Commit 3d — `agents.md` "Phase-prompt header convention"
  subsection (Goal 8).

**Layer 4 — Source attribution + final README touch.** A small
commit that adds a per-rule "Source:" footer to each
`.claude/rules/*.md` where the originating CLAUDE.md section was
extracted verbatim (always "Source: extracted from
`CLAUDE.md` §<N>, <date>" — no behavioural import).

**Alternatives considered (and rejected):**

- **YAML-fenced header inside each prompt file** (rejected because
  codex-cli reads the file as raw prompt; YAML frontmatter would
  bleed into the model's context as literal text).
- **Per-prompt token-count CI gate** (rejected because the
  refresh cycle is operator-driven, not behavioral; a stale token
  count is a soft warning, not a deploy blocker).
- **Splitting `unattended_system_instructions.md` into the same
  per-section files under `.claude/rules/`** (rejected because
  codex-cli does NOT support `.claude/rules/`-style lazy-loading;
  the unattended pipeline reads `unattended_system_instructions.md`
  as one block. The interactive lazy-load mechanism is
  Claude-Code-specific and would not bind codex-cli runs).
- **Moving mode-prompts into category subdirectories** (rejected as
  a §6 breaking change — workflow YAML and shell scripts pin literal
  paths).
- **Adopting davila7's MCP integration catalog** (rejected because
  codex-cli has its own tool surface; pulling MCP into the
  unattended path is a separate, larger design discussion).

## Implementation Steps

1. **Create `.claude/rules/` directory and extract §0–§17 from
   `CLAUDE.md`.** New files (each ≈ the size of the originating
   section):
   - `.claude/rules/00-prime-directive.md` — §0 body verbatim,
     prefixed with YAML frontmatter (`description`: "Applies on
     every task before any action — the non-negotiable stop-and-ask
     rule.").
   - `.claude/rules/01-core-priorities.md` — §1 body.
   - `.claude/rules/02-ask-first-mode.md` — §2 body (incl. Q/A
     format spec).
   - `.claude/rules/03-production-code.md` — §3 body.
   - `.claude/rules/04-env-vars.md` — §4 body.
   - `.claude/rules/05-minimal-change-set.md` — §5 body.
   - `.claude/rules/06-naming-immutability.md` — §6 body; preserves
     the "section numbers covered by §6" paragraph verbatim.
   - `.claude/rules/07-output-requirements.md` — §7 body.
   - `.claude/rules/08-debugging-diagnostics.md` — §8 body.
   - `.claude/rules/09-code-style.md` — §9 body.
   - `.claude/rules/10-mongodb.md` — §10 body (A–H).
   - `.claude/rules/11-task-checklist.md` — §11 body.
   - `.claude/rules/12-pr-review.md` — §12 body (A–G).
   - `.claude/rules/13-repo-hygiene.md` — §13 body.
   - `.claude/rules/14-consumer-repos.md` — §14 body.
   - `.claude/rules/15-github-api-hygiene.md` — §15 body.
   - `.claude/rules/16-task-delegation.md` — §16 body.
   - `.claude/rules/17-preferred-tools.md` — §17 body.
   - `.claude/rules/_README.md` — short orientation note that
     points at `CLAUDE.md` as the index of record.
   Ordering precondition: this step lands before any cross-reference
   into `.claude/rules/` is introduced.

2. **Rewrite `CLAUDE.md` as the thin index.** Preserve:
   - The preface (lines 1–8 in current `CLAUDE.md`).
   - The "PRE-TASK MANDATORY CONTEXT LOADING" block.
   - Each `## §<N>. <title>` heading at the same nesting level so
     anchors like `#0-prime-directive-non-negotiable` continue to
     resolve.
   - A one-sentence pointer under each heading: "See
     `.claude/rules/<NN>-<slug>.md`." plus a 1-line summary copied
     from the rule file's frontmatter `description`.
   - The "FINAL REMINDER" block verbatim at the tail.
   Target length: ≤200 lines (Goal 1). Verify with `wc -l CLAUDE.md`
   before commit.

3. **Verify §-anchor preservation.** Run
   `grep -rn "CLAUDE\.md §" .github/workflows/ scripts/ prompts/`
   (and `unattended_system_instructions.md`, `agents.md`, `codex.md`,
   `ai_pipeline.md` for completeness); for each match, confirm the
   §-number still maps to a CLAUDE.md heading. No script edits
   expected — this is a read-only verification step.

4. **Add the phase-prompt header to every
   `prompts/mode-*.txt`, `prompts/review-*.txt`, and
   `prompts/conflict-resolver.txt`.** Plain-text header, five
   lines, terminated by a blank line before the existing body:
   ```
   role: <e.g. Implementer | Planner | Reviewer | Judge | Validator | Resolver>
   description: <one-line trigger, e.g. "Loaded by review_autofix.yml at autofix iteration N to consolidate reviewer findings.">
   tool-access: <one of: read-only | read-and-edit | shell-and-git | full>
   approx_tokens: <integer; one-shot estimate; refresh manually on material edits>
   loaded_by: <workflow file basename(s), comma-separated>
   ```
   Per-file estimates (`approx_tokens`) are produced via a single
   `python -c 'import sys, tiktoken; ...'` pass at commit time; the
   value is informational. Body unchanged.

5. **Add `## Gotchas` sections to the four largest mode prompts.**
   - `prompts/mode-validate-generate.txt` (currently 809 lines) —
     after the existing body, before any trailing examples, add a
     `## Gotchas` section enumerating: (a) "do not regenerate the
     harness when only the entrypoint changed", (b) "preserve the
     hand-edited validation/**/*.sh files when the JSON results
     map says they passed last cycle", (c) "ENV_VAR mismatches
     between consumer repo and harness manifest are the most
     common silent-failure mode", (d) "transient docker pulls
     should be retried, not surfaced as harness defects".
     Sourced from observed workflow-log-analysis findings.
   - `prompts/mode-validate-fix-harness.txt` (276 lines) — Gotchas:
     (a) "do not edit `validation/**` files that the discover phase
     marked authoritative", (b) "fix-harness is bounded by
     `MAX_SELF_HEAL_ATTEMPTS`; do not escalate to a fresh validate
     cycle inside this run".
   - `prompts/mode-validate-diagnose.txt` (198 lines) — Gotchas:
     (a) "distinguish `unknown_error:NameError` from real validator
     defects via the pyflakes preflight signal", (b) "do not propose
     `MAX_VALIDATE_CYCLES` increases as a fix — that is an operator
     decision".
   - `prompts/mode-judge-review-blocked.txt` (156 lines) — Gotchas:
     (a) "do not return `fix` more than `MAX_REVIEW_BLOCKED_RETRIES`
     times — at the cap, choose `merge_with_followup` or
     `close_and_reissue`", (b) "review-blocked PRs with no linked
     issue still need a verdict — fall back to PR title/body".

6. **Add "Before declaring done, grill yourself on:" idioms.**
   - `prompts/mode-judge.txt` — one block before the final-verdict
     section, listing: "Did every sub-issue's linked PR actually
     land? Did the wave's failures map to known stall-recovery
     actions? Is the IS_FINAL signal real or a phase-counter
     artefact?".
   - `prompts/mode-judge-review-blocked.txt` — extends the §5
     Gotchas with a parallel "grill" block: "Did the editor produce
     a productive `[ai-autofix]` commit since the last judge cycle?
     Is the review-blocked label authentic or stamped by a
     transient API failure?".
   - `prompts/mode-implement-diagnose.txt` — grill: "Is the
     proposed fix-up issue actually within the original plan's
     scope? Would the same diagnosis fire on the next codex run?".
   - `prompts/mode-validate-self-heal.txt` — grill: "Does the
     self-heal patch a prompt file rather than the harness? Is the
     `MAX_SELF_HEAL_ATTEMPTS` budget respected?".

7. **Extend `unattended_system_instructions.md` §15
   (Role-Specific Behavior).** Additive — keep existing role
   descriptions untouched, append:
   - A "Verification-gate ordering" paragraph: typecheck → lint
     → tests → build → smoke; stop at the first failing tier and
     emit a structured failure line keyed by tier. (Source:
     shanraisshan; the same idea is already informally present in
     `apply-ai-tools-learnings-plan.md` item 5 — this commit makes
     it explicit in the role context, not just in the implement
     prompt.)
   - A "Tool-access tier" paragraph that names the four tiers
     (`read-only`, `read-and-edit`, `shell-and-git`, `full`) and
     points at the phase-prompt header field introduced in
     Implementation Step 4. (Source: VoltAgent.)

8. **Add `agents.md` subsection "Phase-prompt header convention".**
   New ## subsection after the existing "Stable log prefixes"
   section. Documents the five-field header, the rationale (catalog
   convention from Piebald-AI; subagent-frontmatter convention from
   VoltAgent), and the rule that the header is plain-text key/value
   (NOT YAML) because codex-cli reads the file verbatim.

9. **Optional Layer 4 (source-attribution footer).** Append to each
   `.claude/rules/*.md` (only the ones extracted in step 1):
   ```
   ---
   _Source: extracted from `CLAUDE.md` §<N>, <ISO-date>._
   ```
   No behavioural impact; helps future audits trace the move.

10. **Smoke check.** Run `grep -c "^## §" CLAUDE.md` — must
    return 18 (sections §0 through §17). Run `ls .claude/rules/*.md
    | wc -l` — must return 19 (18 rule files + `_README.md`). Run
    `wc -l CLAUDE.md` — must return ≤200. Run the §-reference
    audit grep from step 3 again — must show zero references that
    fail to resolve.

## Files & Modules

**New files (≤210 KB total, additive):**

- `.claude/rules/_README.md` [new] — orientation only.
- `.claude/rules/00-prime-directive.md` [new] — §0 extraction.
- `.claude/rules/01-core-priorities.md` [new] — §1 extraction.
- `.claude/rules/02-ask-first-mode.md` [new] — §2 extraction.
- `.claude/rules/03-production-code.md` [new] — §3 extraction.
- `.claude/rules/04-env-vars.md` [new] — §4 extraction.
- `.claude/rules/05-minimal-change-set.md` [new] — §5 extraction.
- `.claude/rules/06-naming-immutability.md` [new] — §6 extraction.
- `.claude/rules/07-output-requirements.md` [new] — §7 extraction.
- `.claude/rules/08-debugging-diagnostics.md` [new] — §8 extraction.
- `.claude/rules/09-code-style.md` [new] — §9 extraction.
- `.claude/rules/10-mongodb.md` [new] — §10 extraction.
- `.claude/rules/11-task-checklist.md` [new] — §11 extraction.
- `.claude/rules/12-pr-review.md` [new] — §12 extraction (the
  largest; ≈200 lines of CLAUDE.md material).
- `.claude/rules/13-repo-hygiene.md` [new] — §13 extraction.
- `.claude/rules/14-consumer-repos.md` [new] — §14 extraction.
- `.claude/rules/15-github-api-hygiene.md` [new] — §15 extraction.
- `.claude/rules/16-task-delegation.md` [new] — §16 extraction.
- `.claude/rules/17-preferred-tools.md` [new] — §17 extraction.

**Edited files:**

- `CLAUDE.md` — rewritten as a ≤200-line index. Preserves: top
  preface (lines 1–8), PRE-TASK MANDATORY CONTEXT LOADING block,
  one heading per §, FINAL REMINDER block.
- `unattended_system_instructions.md` — additive paragraphs at the
  tail of §15. No existing line is edited.
- `agents.md` — one new `## Phase-prompt header convention`
  subsection inserted after the existing `## Stable log prefixes`
  block (lines 131-147 in current `agents.md`).
- `prompts/mode-clarify.txt`, `mode-clarify-respond.txt`,
  `mode-plan.txt`, `mode-orchestrate.txt`,
  `mode-orchestrate-poll-judge.txt`, `mode-implement.txt`,
  `mode-implement-diagnose.txt`, `mode-implement-repair.txt`,
  `mode-implement-repair-syntax.txt`, `mode-judge.txt`,
  `mode-judge-review-blocked.txt`, `mode-judge-stall-recovery.txt`,
  `mode-validate-discover.txt`, `mode-validate-generate.txt`,
  `mode-validate-fix-harness.txt`, `mode-validate-diagnose.txt`,
  `mode-validate-self-heal.txt`, `mode-workflow-analysis.txt`,
  `mode-workflow-api-redundancy.txt`, `mode-workflow-audit.txt`,
  `review-consolidator.txt`, `review-reviewer-checklist.txt`,
  `conflict-resolver.txt` — header annotation only (Implementation
  Step 4).
- `prompts/mode-validate-generate.txt`,
  `prompts/mode-validate-fix-harness.txt`,
  `prompts/mode-validate-diagnose.txt`,
  `prompts/mode-judge-review-blocked.txt` — additional `## Gotchas`
  sections (Implementation Step 5).
- `prompts/mode-judge.txt`, `prompts/mode-judge-review-blocked.txt`,
  `prompts/mode-implement-diagnose.txt`,
  `prompts/mode-validate-self-heal.txt` — additional "grill"
  blocks (Implementation Step 6).

**Untouched (explicitly preserved):**

- `.claude/commands/analyze-log.md`
- `.claude/commands/investigate-issue.md`
- `.claude/commands/write-plan.md`
- `.claude/hooks/session-start.sh`
- `.claude/settings.json`
- All of `workflow-templates/.claude/`
- `workflow-templates/CLAUDE.md` (the symlink to `../CLAUDE.md`;
  picks up the restructured file automatically).
- `.github/ai/consumer_repos.json`
- `codex.md`, `ai_pipeline.md`, `README.md`,
  `probably_unnecessary_but_read_if_stuck.md`, `CHANGELOG.md`.
- `prompts/header.txt`.
- All `.github/workflows/*.yml`.
- All `scripts/**`.

## Data Model / Index Changes

None. No MongoDB collection is touched. §10 not applicable.

## Tests

The plan is documentation- and prompt-text-only, so test
verification is mechanical:

- **Static checks (CI / pre-commit):**
  - `grep -c "^## §" CLAUDE.md` returns 18.
  - `ls .claude/rules/*.md | wc -l` returns 19.
  - `wc -l CLAUDE.md` returns ≤200.
  - `grep -rn "CLAUDE\.md §" .github/workflows/ scripts/ prompts/
    unattended_system_instructions.md agents.md codex.md
    ai_pipeline.md` — every match's §-number resolves to a
    heading in the rewritten CLAUDE.md.
  - YAML lint: `.claude/rules/*.md` frontmatter blocks parse as
    valid YAML (`python -c 'import yaml,sys,re; [yaml.safe_load(re.match(...).group(1)) ...]'`).
- **Smoke (interactive Claude Code session):**
  - Run the existing `.claude/commands/write-plan.md` command on
    a trivial task; verify the CLAUDE.md pre-task context-loading
    step still works (the index is short, but the rule files
    should be discoverable when the model decides it needs them).
  - Confirm an existing slash command that cites a CLAUDE.md
    §-number (e.g. `analyze-log.md` references §2) still resolves
    the citation.
- **Unattended-pipeline smoke (orchestrator dry-run):**
  - Trigger a smoke implement cycle on a known toy issue. The
    codex-cli pipeline reads `unattended_system_instructions.md`
    and the relevant `prompts/mode-*.txt` files; the header
    addition (Implementation Step 4) MUST be silently included in
    the prompt context without altering the model's behavior. Eye-
    ball the resulting plan / PR to confirm no regression.

No new automated test is added. Existing tests in `tests/` (which
predominantly target the orchestrator state machine, the codex
config writer, and the GitHub helpers) are not affected by prompt-
text or documentation edits.

## Risks & Mitigations

- **Risk:** A workflow YAML or shell script reads `CLAUDE.md` by
  line number rather than by §-anchor. Restructure would
  silently change the line index of every section.
  **Mitigation:** Implementation Step 3 includes a grep audit
  across `.github/`, `scripts/`, `prompts/`,
  `unattended_system_instructions.md`, `agents.md`, `codex.md`,
  `ai_pipeline.md` looking for any line-number-based reference
  (e.g. `sed -n '40,80p' CLAUDE.md`, `awk 'NR==N'
  CLAUDE.md`). Spot check of the existing `grep -rn "CLAUDE\.md §"`
  hits shows all references are conceptual (`per CLAUDE.md §6`,
  `per CLAUDE.md §15`) — none are line-pinned. Audit before commit.

- **Risk:** Interactive Claude Code sessions stop seeing the §
  body content when CLAUDE.md becomes a thin index — the rules
  in `.claude/rules/` would need lazy-loading the model doesn't
  invoke.
  **Mitigation:** The CLAUDE.md index keeps each §-heading and
  appends the one-line summary so the most important rule
  fragments (Prime Directive, FINAL REMINDER) remain inline.
  Lazy-loading is opportunistic in Claude Code's skills surface;
  the user-facing safety floor is the inline index plus the
  PRE-TASK MANDATORY CONTEXT LOADING block, which explicitly
  lists the files Claude must read before acting.

- **Risk:** `workflow-templates/CLAUDE.md` is a symlink to
  `../CLAUDE.md`. Restructure propagates immediately to anything
  in `workflow-templates/` that resolves the symlink at release
  time — including the templates checked out by
  `update_workflows.yml` in consumer repos.
  **Mitigation:** Documented in `## Non-goals` — the propagation
  is **automatic and intended**, but the templates tree itself
  is not modified by this plan. Consumer repos pick up the
  shorter CLAUDE.md on next `@stable` release without further
  action. If a downstream consumer pins to a specific line
  number this would break — but no in-tree workflow does, so the
  same property is expected to hold for consumers (they read the
  symlinked CLAUDE.md the same way this repo does).

- **Risk:** The phase-prompt header (plain-text k/v) accidentally
  parses as a markdown / YAML construct on a future codex-cli
  upgrade.
  **Mitigation:** Header uses bare `key: value` pairs with no
  fence, no `---` separator, no leading `#`. Codex-cli treats it
  as opaque prompt prefix. If a future codex-cli version starts
  parsing leading frontmatter, the header layout is trivial to
  switch to a `<!-- ... -->` HTML comment block in a follow-up
  PR.

- **Risk:** Gotchas / grill additions drift out of date as the
  underlying failure modes evolve.
  **Mitigation:** Each Gotcha bullet cites the observation that
  produced it (workflow-log-analysis finding, judge verdict,
  etc.) in a parenthetical; future workflow audits can detect
  stale Gotchas via the same log-prefix grep used for §15
  hygiene.

- **Risk:** Sections that grow (e.g. §12 PR Review Mode is
  already ~200 lines) might be edited in the future via
  CLAUDE.md edit habits, bypassing the rule file.
  **Mitigation:** Add a one-line directive at the head of each
  rule file: `_Edit this file, not the CLAUDE.md index._` The
  `_README.md` orientation note reinforces. No automated guard.

- **Risk:** The interactive `.claude/commands/write-plan.md`
  procedure (which itself references CLAUDE.md §2 by name)
  briefly disagrees with reality between the moment CLAUDE.md is
  restructured and the moment the command's procedure step is
  re-read by a session.
  **Mitigation:** Commands are not edited by this plan; their
  prose continues to reference "CLAUDE.md §2" which the
  restructured index still names at the same anchor.

## Rollout

- **No feature flag.** The restructure is text-only; no behavior
  toggles.
- **Per-commit landing order** is the rollout sequence (Layer 1
  → Layer 2 → Layer 3 sub-commits → Layer 4). Each commit is
  individually revertible; if Layer 1 (`CLAUDE.md` restructure)
  proves to break an interactive session habit, reverting that
  single commit restores the monolithic CLAUDE.md without
  touching the prompt edits.
- **No dark launch / gradual ramp.** Single PR, all commits
  merged together. The blast radius is fully contained in the
  governance / prompt surface and does not touch any
  rate-limited or stateful resource.
- **Rollback path.** `git revert <SHA>` of the offending
  commit(s); the rule files in `.claude/rules/` become orphaned
  but inert (nothing in the unattended pipeline reads them, and
  interactive sessions only consume them when discovered).
- **Consumer-repo propagation timing.** Not directly applicable
  per `## Non-goals`. The `workflow-templates/CLAUDE.md` symlink
  resolves to the restructured `CLAUDE.md` automatically; the
  next `@stable` release runs `update_workflows.yml` in every
  registered consumer repo (`.github/ai/consumer_repos.json`)
  and the consumer's checked-in `CLAUDE.md` becomes the new
  thin-index version on its own schedule. No `repository_dispatch`
  is initiated by this plan.

## Open Questions

1. Should `.claude/rules/*.md` be propagated to
   `workflow-templates/.claude/` so consumer repos receive the
   rule files alongside the symlinked CLAUDE.md? Q2 explicitly
   scoped consumer-repo propagation out, but the symlinked
   CLAUDE.md will become a thin index pointing at files the
   consumer does not have, which means consumers see "See
   `.claude/rules/06-naming-immutability.md`" with no resolvable
   target. Two answers are defensible: (a) leave consumers with
   the thin index only and rely on them following the
   referenced filenames as **convention-only labels** (consumers
   read `CLAUDE.md` for orientation; the binding rule is the
   one-line summary inline in the index); (b) propagate the
   rule files in a follow-up plan that touches
   `workflow-templates/`. **Recommend (a) for this plan**, with
   (b) as a separate decision after we see how consumers react.

2. Should the `approx_tokens` header field be computed via
   `tiktoken` (OpenAI tokenizer, mismatched with codex-cli's
   actual model) or via `anthropic`'s tokenizer (closer to the
   interactive Claude Code path but irrelevant to codex-cli)?
   Reasonable default: `tiktoken` with the `o200k_base`
   encoding for round-number prompts; the value is
   advisory-only.

3. Are the four Gotchas / grill sections enough, or should the
   pattern propagate to every mode-*.txt prompt? The plan picks
   the four largest / highest-leverage prompts; broader adoption
   is straightforward but increases the per-edit review surface.

## References

- `https://github.com/davila7/claude-code-templates` — CLI &
  catalog convention for Claude Code components.
- `https://github.com/shanraisshan/claude-code-best-practice` —
  CLAUDE.md sizing, `.claude/rules/*.md` lazy-loading, skill
  design (Gotchas, dynamic shell injection), planning, prompting
  techniques.
- `https://github.com/VoltAgent/awesome-claude-code-subagents` —
  Subagent YAML-frontmatter convention; tool-access tiers;
  hyphenated-lowercase naming.
- `https://github.com/Piebald-AI/claude-code-system-prompts` —
  Reference catalog of Claude Code's own system prompts; per-file
  metadata convention (descriptive filename, token count,
  conditional-inclusion context).
- `docs/plans/apply-ai-tools-learnings-plan.md` — Closest
  in-repo precedent; structurally identical adoption pattern
  applied to a different source repo (x1xhlol).
- `docs/plans/symphony-inspired-improvements-plan.md` — Earlier "borrow
  mechanisms from an external system" exercise in this repo.
- `CLAUDE.md` (current monolithic version, 489 lines) — extraction
  source for Layer 1.
- `unattended_system_instructions.md` (current 19-section,
  ~370-line version) — extension target for Layer 3 sub-commit
  3c.
- `agents.md` (current 194-line architectural-facts file) —
  extension target for Layer 3 sub-commit 3d.
