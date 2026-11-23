# Awesome Claude Code references for this repo

This is a curated orientation index for `coding-workflows`, not a mirror of upstream material. Every item below links to the original resource, explains why it is relevant here, and points to the closest in-repo analogue plus the main way our implementation differs.

## Slash commands worth studying

- `evaluate-repository` — <https://github.com/hesreallyhim/awesome-claude-code>
  Why it matters: the parent plan calls it out as a reusable repository-evaluation pattern, which is the closest fit to our repo-wide judging and cross-PR integration checks.
  In-repo analogue: `prompts/mode-judge.txt` — we express the same evaluation intent as a workflow-enforced judge contract with JSON output and file/line evidence requirements.

- `create-prp` — <https://github.com/Wirasm/claudecode-utils/blob/main/.claude/commands/create-prp.md>
  Why it matters: it shows how upstream turns a rough request into an implementation-ready planning artifact.
  In-repo analogue: `prompts/mode-plan.txt` — our planner is issue-driven, scope-locked, and emits automation/wiring requirements that go beyond a local project brief.

- `create-prd` — <https://github.com/dredozubov/prd-generator>
  Why it matters: it is useful for comparing upstream product-requirement scaffolding against our narrower implementation-planning contract.
  In-repo analogue: `prompts/mode-plan.txt` — this repo collapses PRD-style framing into a single approved plan format instead of keeping a separate product-doc workflow.

- `fix-github-issue` — <https://github.com/jeremymailen/kotlinter-gradle/blob/master/.claude/commands/fix-github-issue.md>
  Why it matters: it is the most direct upstream comparison point for turning an issue into a concrete code change.
  In-repo analogue: `prompts/mode-implement.txt` — our implement phase is non-interactive, scoped by the approved plan, and validated inside GitHub Actions rather than a local repair loop.

- `context-prime` — <https://github.com/elizaOS/elizaos.github.io/blob/main/.claude/commands/context-prime.md>
  Why it matters: it highlights the upstream idea of loading durable repo context before editing.
  In-repo analogue: `agents.md` — we keep the durable architecture facts in checked-in repo docs that every unattended phase receives automatically instead of relying on a one-off priming command.

## GitHub Actions templates worth comparing

- `ci-failure-auto-fix.yml` — <https://github.com/anthropics/claude-code-action/blob/main/examples/ci-failure-auto-fix.yml>
  Why it matters: it is the cleanest upstream comparison for automated post-failure diagnosis and repair.
  In-repo analogue: `.github/workflows/review_autofix.yml` — our flow is broader, with reviewer fan-out, consolidation, editor passes, and conflict resolution rather than a single autofix path.

- `issue-triage.yml` — <https://github.com/anthropics/claude-code-action/blob/main/examples/issue-triage.yml>
  Why it matters: it shows the upstream baseline for turning inbound issue traffic into structured next steps.
  In-repo analogue: `.github/workflows/clarify.yml` — we split triage into clarify, plan, implement, judge, and validate phases instead of handling the whole lifecycle in one lightweight triage pass.

- `pr-review-comprehensive.yml` — <https://github.com/anthropics/claude-code-action/blob/main/examples/pr-review-comprehensive.yml>
  Why it matters: it is useful for comparing upstream PR review automation with our own review stack.
  In-repo analogue: `.github/workflows/review_autofix.yml` — we treat review as a multi-model, fail-open, contract-heavy pipeline instead of a generic comprehensive review template.

## Knowledge guides for further reading

- `Claude Code Handbook` — <https://nikiforovall.blog/claude-code-rules/>
  Why it matters: it is a compact best-practices guide for structuring Claude Code work, so it is a good orientation read before digging into this repo's heavier automation.
  In-repo analogue: `README.md` — our overview is repo-specific and workflow-centric, not a general handbook for Claude Code usage.

- `Claude Code System Prompts` — <https://github.com/Piebald-AI/claude-code-system-prompts>
  Why it matters: it helps explain how upstream prompt contracts are composed and why certain behaviors show up repeatedly across tools.
  In-repo analogue: `unattended_system_instructions.md` — we ship a stricter execution contract for unattended phases rather than a prompt-collection reference.

- `Compound Engineering Plugin` — <https://github.com/EveryInc/compound-engineering-plugin>
  Why it matters: it is a strong example of packaging agents, skills, and commands into a disciplined development workflow.
  In-repo analogue: `agents.md` — we describe our workflow architecture and durable repo rules in checked-in docs instead of distributing a reusable plugin bundle.

- `Encyclopedia of Agentic Coding Patterns` — <https://aipatternbook.com>
  Why it matters: it is useful background reading when you want names and examples for the coordination patterns this repo operationalizes.
  In-repo analogue: `prompts/mode-judge.txt` — we embed a few concrete agentic patterns as fixed, production-bound phase prompts instead of maintaining a broad reference encyclopedia in-tree.

## Tooling we deliberately did NOT adopt and why

- SVG badges, animated tickers, README-style alternatives
  Why not adopted here: this repo already uses issue comments, labels, logs, and workflow summaries as the primary status surfaces, so adding decorative mirrors would increase maintenance without improving the core automation loop.
  See: `docs/awesome-cc-future-improvements.md` (EX1) for the tracked future-improvement discussion instead of re-vendoring the upstream ideas here.

- Cloning the curated catalog itself
  Why not adopted here: the repo has its own prompt, workflow, and shell-helper contracts, so a broad copy of the upstream catalog would add overlap and drift rather than a clean extension point.
  See: `docs/awesome-cc-future-improvements.md` (EX2) for the deliberate non-adoption note and any future revisit.

- Adopting the generic `pr-review` slash command
  Why not adopted here: this repository's review path depends on repo-specific reviewer fan-out, consolidator rules, floor tags, and merge gating that a generic template would not preserve.
  See: `docs/awesome-cc-future-improvements.md` (EX3) for the comparison and rationale.

- Cross-repo cooldown / sanctions enforcement
  Why not adopted here: our orchestration and review flows already have repo-local guardrails, while cross-repo enforcement would add governance complexity outside the narrow scope of this automation stack.
  See: `docs/awesome-cc-future-improvements.md` (EX5) for the tracked follow-up instead of expanding this doc into policy prose.
