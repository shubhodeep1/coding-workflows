Take a software task described in `$ARGUMENTS`, clarify every ambiguity with the user via CLAUDE.md §2-style Q1/Q2 multiple-choice questions, then write a detailed implementation plan to `docs/plans/<slug>-plan.md` and open a PR. `$ARGUMENTS` is **free-form prose** describing the task. It may contain optional references — issue / PR URLs, `#1234` refs, Actions run URLs, commit SHAs, file paths, related prior plans — but they are not required.

$ARGUMENTS

## Procedure

1. **Parse `$ARGUMENTS`.** Capture the free-form task description verbatim. Extract any references mentioned (URLs, file paths, issue numbers, SHAs) and note them for the clarification + research steps. If `$ARGUMENTS` is empty, contains no actionable description, or is so vague that no slug can be derived, **stop and ask the user to describe the task** before proceeding. Do not silently default to a guessed topic.

2. **Read project context.** Always read `README.md`, `agents.md`, and `CLAUDE.md` at the repo root before drafting. If the task plausibly touches a MongoDB collection, also read every relevant `/db/contracts/*.yml` (per CLAUDE.md §10). If references in `$ARGUMENTS` point at issues / PRs / files / prior plans, fetch and read them in full — use `mcp__github__*` tools or the `gh` CLI for GitHub reads (see [Tool Access](#tool-access)), the `Read` tool for local files, and `Grep` / `Glob` to locate related code. Do not guess at code, env vars, or workflow inputs — read the source.

3. **Clarify until zero items remain open.** Identify every ambiguity the task introduces — scope, behavior, edge cases, interfaces, data model, operational concerns, success criteria, propagation / consumer impact, rollout. Batch every blocking question in a single round using the [Clarification Format](#clarification-format) below. **Do NOT ask the user to name, confirm, or choose the plan-doc filename or the PR branch** — derive the slug automatically from the task topic per the [Slug rules](#rules) below (both `docs/plans/<slug>-plan.md` and the branch `claude/write-plan-<slug>` are generated from it). Wait for the user's answers. **If any answer introduces new ambiguity, ask a follow-up batch — keep looping until every clarification item is resolved.** Do not proceed to step 4 while any question is still open: this command's contract is to ship a plan with zero open questions. Items that genuinely cannot be answered before drafting (e.g. an env value that only exists in prod, an integration result that depends on staging) are NOT recorded as open questions — surface them in the clarification round as proposed `## Risks & Mitigations` entries with `ACCEPTED — pending <discovery>` wording and have the user accept them explicitly before drafting.

4. **Draft the plan.** Write a markdown plan to `docs/plans/<slug>-plan.md` following the structure in [Plan Structure](#plan-structure) below. Cite project constraints by section number where relevant (e.g. "§6 — renames are breaking unless the old name is preserved as an alias"). Surface every assumption you made and every risk you spotted. By the time you reach this step, there are no open questions left — step 3's loop must have resolved every one. Create the `docs/plans/` directory if it does not yet exist.

5. **Sanity check.** Re-read the plan as if you were the reviewer. Does it answer: what is changing, why, how, what could break, what is verified, how is the work split into independently-mergeable phases (each production-safe and complete on its own per [Phases & Merge Strategy](#phases--merge-strategy)), what is rolled out? If any of those is hand-wavy, fix it before pushing. No commit-and-iterate cycle on the plan itself.

6. **Branch, commit, push, open PR.** Create a new branch `claude/write-plan-<slug>` (append `-2`, `-3`, … if a branch with that exact name already exists on the remote). Commit the new plan file with message `docs: plan for <topic>`. Push with `git push -u origin <branch>` (retry up to 4 times with exponential backoff on transient network errors per the project's git policy). Open a PR via `mcp__github__create_pull_request`:
   - **Base:** the repo's default branch, resolved with `gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>`. Do NOT hardcode `main`.
   - **Title:** `docs: plan for <topic>`
   - **Body:** the [PR Body Template](#pr-body-template) below.
   - **Draft:** `false` (ready for review).

7. **Report.** Emit the [Output Format](#output-format) in chat — short summary, file path, branch, PR URL.

## Clarification Format

Follow CLAUDE.md §2 exactly. Stable IDs `Q1`, `Q2`, …; letter-only answers (`A`, `B`, `C`, or `A+C`); mark at least one option `(RECOMMENDED)`; one decision per Q-ID; multi-select allowed only when stated explicitly. Never use numeric (1, 2, 3) prefixes — only Q-IDs.

Common questions to consider in the first batch (skip any already unambiguously answered in `$ARGUMENTS`):

- **Slug is NOT a question** — never ask the user to name, confirm, or choose the slug, the plan-doc filename, or the branch name. Derive the slug automatically from the task topic per the [Slug rules](#rules); it feeds both `docs/plans/<slug>-plan.md` and the branch `claude/write-plan-<slug>`.
- **Scope** — which repo / module / service / runtime; prod vs staging vs dev.
- **Backward compatibility** — does any existing identifier get renamed or removed? Per §6, those are breaking unless aliased.
- **Data model** — collections touched, index changes, contract updates per §10.
- **Interfaces** — API / CLI / env vars / log keys affected; observability impact.
- **Success criteria** — what is the done condition; how is correctness verified.
- **Rollout** — feature flag, dark launch, gradual ramp, instant cutover, rollback path.
- **Out-of-scope explicitly** — what is NOT being planned here.
- **Propagation** — if the change must reach consumer repos per `.github/ai/consumer_repos.json` (CLAUDE.md §14), how.
- **GitHub API hygiene** — if the plan adds new `gh api` / MCP calls, how they batch / reuse existing calls per §15.
- **Phase breakdown** — propose how the work splits into independently-mergeable phases (count + one-line scope per phase). The plan is implemented by the AI orchestrator (unattended pipeline), and every merge lands in production directly, so each phase MUST be production-safe at merge time, independently mergeable, and complete on its own (see [Phases & Merge Strategy](#phases--merge-strategy)). If the task genuinely cannot be split, surface that as its own Q and have the user accept a single-phase plan explicitly.

Add task-specific questions as needed. Skip empty rounds — if `$ARGUMENTS` is already exhaustive and unambiguous, ask nothing and proceed straight to drafting (the slug is always auto-derived, never asked).

Example shape:

```
**Q1: How should the new rate limiter roll out?**

Choices:
- **A** — Feature-flag dark launch, ramp gradually (RECOMMENDED)
- **B** — Instant cutover on merge
- **C** — Staging-only until manual sign-off

Reply: `Q1: A`
```

## Plan Structure

Default sections (drop any that are genuinely N/A; never add filler):

```
# <Title — usually identical to PR title without the `docs: plan for ` prefix>

## Summary
1–2 sentences. What is being built, why now.

## Context
What in the codebase / product motivates this. Link prior work, issues, related plans, design docs. Quote constraints from CLAUDE.md by section number where they bind the design.

## Goals
Bulleted, verifiable. Each goal must be falsifiable on review.

## Non-goals
What this plan deliberately does NOT cover. Out-of-scope work goes here, not as a footnote.

## Constraints
Project rules that bind this work: §6 naming immutability, §10 MongoDB rules, §14 consumer-repo registry, §15 GitHub API hygiene, security, performance, backward compatibility. Cite by section.

## Approach
The chosen design at a high level. If multiple designs were considered, briefly note the alternatives and why this one won.

## Phases & Merge Strategy
The plan is executed by the AI orchestrator (unattended pipeline), not by a human iterating in a terminal. Every merge lands directly in production — there is no staging branch and no human-driven cherry-pick — and the orchestrator ships each phase as its own PR.

Split the work into phases — one PR per phase — with each phase satisfying ALL of:

- **Independently mergeable** — no required ordering with other phases; reviewers can merge phases in any order without breakage. No phase may assume another phase has already merged.
- **Complete on its own** — at the moment of merge the system is in a working, shippable state. Feature flags default-off are fine; half-wired functionality that requires the next phase to compile / run / pass tests is not.
- **Production-safe at merge** — the PR ships to prod on merge. Each phase must be safe to deploy as-is, with its own rollback path.

List the phases (numbered). For each phase give: one-line scope, files / modules touched, "done" condition that proves the phase is independently shippable, and rollback path (how to revert just this phase without disturbing others). If the task genuinely cannot be split into independently-mergeable phases, this section MUST state that explicitly and cite the user's accepted clarification answer authorising a single-phase plan.

## Implementation Steps
Numbered, grouped by phase (per [Phases & Merge Strategy](#phases--merge-strategy)). Each step lists the files touched (with line ranges if known), the change in one sentence, and any preconditions / ordering constraints. Steps should be small enough to land as individual commits within a phase's PR, and no step may straddle a phase boundary.

## Files & Modules
Bulleted list of every file the implementation will create, edit, or delete. Mark new files with `[new]`, deletions with `[del]`.

## Data Model / Index Changes
Only if applicable. Per §10: name every collection touched, every index added / changed / removed, and link to the matching `/db/contracts/<collection>.yml` update.

## Tests
What new tests; what existing tests need updating; how the plan is verified end-to-end. Distinguish unit / integration / e2e / manual.

## Risks & Mitigations
Bulleted. Each risk gets a one-line mitigation or `ACCEPTED — <why>`. Items that depend on future discovery (ops checks, prod-only values, staging integration results) are recorded here as `ACCEPTED — pending <discovery>` and MUST have been explicitly accepted by the user during step 3's clarification round — they are NOT open questions.

## Rollout
Feature flag? Dark launch? Migration order? Rollback procedure? Consumer-repo propagation timing if §14 applies?

## References
Links to related issues, PRs, prior plans, external docs, RFCs.
```

## PR Body Template

```
## Summary
<1–2 sentences — same wording as the plan's Summary section.>

## What this plan covers
- <one bullet per major section: Goals, Approach, Phases & Merge Strategy, Implementation Steps, Tests, Rollout>

## Plan file
[`docs/plans/<slug>-plan.md`](./docs/plans/<slug>-plan.md)
```

The body does NOT need to duplicate the full plan — the PR diff shows it.

## Output Format

After the PR is open, emit in chat:

```
Plan: <topic>
File: docs/plans/<slug>-plan.md
Branch: claude/write-plan-<slug>
PR: <url>

Summary: <1–2 lines from the plan's Summary section>
```

No prose padding. A bare "Plan written, see PR #X" is not acceptable — the user wants summary + file + branch + PR in chat. Do NOT add an "Open questions" block — by step 3's contract every clarification item is resolved before the plan is drafted (items that depend on future discovery are captured as `ACCEPTED — pending <discovery>` entries under `## Risks & Mitigations` in the plan itself, not as open questions in chat).

## Tool Access

Same surface as `/investigate-issue` and `/analyze-log` — pick whichever is exposed in the session:

- **`mcp__github__*` MCP tools** — always available. Use `mcp__github__create_pull_request` for opening the PR. Use `mcp__github__get_file_contents`, `mcp__github__pull_request_read`, `mcp__github__issue_read`, `mcp__github__search_code`, `mcp__github__list_branches` for research and branch-collision checks.
- **`gh` CLI** — available when `GH_TOKEN` or `GITHUB_TOKEN` is set in the session environment. Verify auth state directly with `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status` (nounset-safe) — don't infer it from the SessionStart log. Use for default-branch detection (`gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>`) and any read that is awkward via MCP.
- **`-R <owner>/<repo>` is mandatory** on `gh` calls that need repo context. In Claude Code Web sessions the only git remote points at a local proxy, so `gh` cannot auto-detect the GitHub repo from `git remote -v`; bare `gh repo view …` fails with `failed to determine base repo`. The SessionStart hook prints the resolved slug — use that value.

Local file reads use `Read`. Local code search uses `Grep` / `Glob`. Git operations use `Bash`.

## Rules

- **No code changes — plan only.** This command produces a markdown plan and a PR containing only that plan file. Do not edit source files, configs, workflows, scripts, schemas, or contracts during a `/write-plan` invocation. Implementation comes later, via `/investigate-issue` or direct work.
- **Unattended implementation — phased, independently mergeable, production-direct.** Plans authored by this command are executed by the AI orchestrator (unattended pipelines), not by a human iterating in a terminal — so steps MUST be unambiguous and machine-actionable, and the plan MUST split the work into phases per [Phases & Merge Strategy](#phases--merge-strategy). Every merge lands directly in production (no staging branch), and the orchestrator ships each phase as its own PR. Each phase MUST be (i) independently mergeable — no inter-phase merge ordering required, (ii) complete on its own — the system stays working and shippable after each phase merges, (iii) production-safe at merge time. If the task genuinely cannot be split into independently-mergeable phases, surface that as a clarification question per §2 and get explicit user acceptance of a single-phase plan before drafting — do not paper over it with a single mega-phase.
- **Always open the PR.** Do not stop at "plan committed locally." The deliverable is a reviewable plan-PR, not a local file.
- **Mandatory pre-task reads.** Per CLAUDE.md, read `README.md`, `agents.md`, and `CLAUDE.md` before drafting. For MongoDB-touching tasks, also read every relevant `/db/contracts/*.yml`. Missing or unclear context is a hard stop — surface it as a clarification question, do not paper over it.
- **Honor §6 (naming immutability).** If the planned work renames or removes any identifier (variable, function, class, module, CLI flag, env var, URL path, JSON/DB field, index/event/metric name, log key), the plan MUST explicitly flag this as a breaking change and propose an alias / backward-compat path.
- **Honor §10 (MongoDB).** Any collection / index / contract impact MUST be enumerated in the plan with the corresponding `/db/contracts/*` update path.
- **Honor §14 (consumer repos).** If the planned change reaches workflow templates or `.claude/` assets that propagate to consumer repos, the plan MUST state which consumers are affected and reference `.github/ai/consumer_repos.json`.
- **Honor §15 (GitHub API hygiene).** Plans that add `gh api` / MCP calls MUST justify the new call surface and explain how it batches / reuses existing calls.
- **Clarify aggressively; never default silently.** Per §0 + §2 — when in doubt, ask. If the answers to the first batch open new ambiguities, ask a follow-up batch. The user provided "ask clarifying questions until its completely clear" as the contract — honor that.
- **Zero open questions in the final plan.** Step 3 MUST loop until every clarification item is resolved before step 4 runs. The `## Open Questions` section has been removed from Plan Structure, PR Body Template, and Output Format — there is no slot to record unresolved items. Items that depend on future discovery are recorded as `ACCEPTED — pending <discovery>` entries under `## Risks & Mitigations`, and only after explicit user acceptance during the clarification round — they are not open questions.
- **Slug rules.** Lowercase ASCII alphanumeric + hyphens, ≤ 60 chars, derived **automatically** from the task topic — pick the most specific, action-focused phrase that captures the task (e.g. `rate-limit-public-api`, `fix-orchestrator-stall`). Auto-pick it yourself; never ask the user to name, confirm, or choose the slug, the plan-doc filename, or the branch name. (Branch-collision handling below still applies.)
- **Branch collision.** If `claude/write-plan-<slug>` already exists on the remote (check via `mcp__github__list_branches` or `git ls-remote --heads origin claude/write-plan-<slug>`), append `-2`, `-3`, … to the slug. Never force-push.
- **Default branch.** Resolve dynamically via `gh repo view --json defaultBranchRef -q .defaultBranchRef.name -R <owner>/<repo>`. Do not hardcode `main` — some consumer repos use a different default branch.
- **Final chat reply.** Always emit the [Output Format](#output-format) — even when the PR is open and linked. The PR alone is not the user-facing report.
