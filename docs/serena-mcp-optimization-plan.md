# Serena MCP Optimization & Complementary MCP Servers

## Goal

Maximize token savings and workflow speed across all AI pipeline phases by:
1. Closing gaps in Serena MCP adoption (prompt files, consistency)
2. Consolidating Serena guidance into reusable blocks (DRY)
3. Integrating Context7 MCP for library documentation lookup
4. Investigating prompt caching on OpenRouter for prefix deduplication
5. Adding a Git MCP server for on-demand, scoped diff/log/blame access

## Current State

### What's Working
- Serena MCP installed in all 8 major workflows (clarify, plan, implement, review_autofix, validate, orchestrate, orchestrate_poll, orchestrate_clarify_respond)
- `codex_system_instructions.md` (lines 19-50) has comprehensive Serena guidance covering read tools, edit tools, and fallback rules
- `setup_serena.sh` is production-grade: auto-detects 30+ languages, installs language servers, resolves Serena binary from uvx cache, health checks, graceful fallback
- `serena_efficiency_report.py` integrated into implement.yml and review_autofix.yml
- `generate_symbol_diff_summary.py` wired into review_autofix.yml (line 690-707); output fed to both reviewers (line 820) and editor (line 1635)
- Inline `SERENA MCP EFFICIENCY (MANDATORY)` blocks injected in workflow YAML for implement.yml (line 609), plan.yml (line 600), clarify.yml (line 392), review_autofix.yml
- Judge, clarify-respond, validate-generate, validate-diagnose, and judge-review-blocked prompt files all have MANDATORY Serena sections

### Gaps to Close

| # | Gap | Status | Evidence |
|---|-----|--------|----------|
| G1 | `mode-implement.txt` Serena guidance inclusion | Closed | `prompts/mode-implement.txt` uses `{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}` include placeholder. |
| G2 | `mode-plan.txt` Serena guidance inclusion | Closed | `prompts/mode-plan.txt` uses `{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}` include placeholder. |
| G3 | `mode-orchestrate.txt` Serena wording not mandatory | Closed | `prompts/mode-orchestrate.txt` references `{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}` and says to inspect repo structure following SERENA MCP EFFICIENCY (MANDATORY). |
| G4 | `mode-clarify.txt` Serena wording not mandatory | Closed | `prompts/mode-clarify.txt` references `{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}` and explicitly requires SERENA MCP EFFICIENCY (MANDATORY). |
| G5 | Canonical Serena guidance consolidation | Closed | Canonical content is centralized in `prompts/serena-efficiency-block.txt` (`[READ_ONLY]` and `[READ_WRITE]`) and consumed via placeholders by mode prompts. |
| G6 | No complementary MCP server for library/framework documentation | Open | Context7 work tracked separately in this plan (Issue 5). |
| G7 | No Git MCP server for on-demand diff/blame | Open | Git MCP work tracked separately in this plan (Issue 7). |
| G8 | Prompt caching not investigated for OpenRouter | Open | Prompt-caching investigation tracked separately in this plan (Issue 6). |

### G1-G5 Verification Notes (2026-04-14)

- Render check passed for `prompts/mode-plan.txt`, `prompts/mode-implement.txt`, `prompts/mode-orchestrate.txt`, and `prompts/mode-clarify.txt` with `scripts/render_prompt.sh`; no unresolved `{{SERENA_EFFICIENCY_BLOCK_*}}` placeholders remained.
- Shared read-only Serena block is byte-stable across rendered plan/orchestrate/clarify prompts (SHA-256: `b28c424a060077c0036e7ffad60d5e6a74387bf807f9d19604107c0d6391355a`).
- Read-write Serena block for implement renders as expected and differs from read-only by design (SHA-256: `c134f1099f738ed36274d92264ec36f8eeca1803b69fca0e8ae17fc36bf3f1ad`).

## Implementation Plan

### Issue 1: Add MANDATORY Serena guidance to mode-implement.txt

**Priority:** 1 (foundational)

**Files to change:**
- `prompts/mode-implement.txt`

**Details:**
Add a `SERENA MCP EFFICIENCY (MANDATORY)` section to `mode-implement.txt` matching the block already injected inline in `implement.yml` (lines 609-618). This ensures Serena guidance is present regardless of how the prompt is assembled.

Section to add (after the "Restrictions" block, before the final output line):

```
SERENA MCP EFFICIENCY (MANDATORY):
- Do NOT call onboarding, initial_instructions, or check_onboarding_performed.
  Skip straight to activate_project and then use tools directly.
- When reading code, prefer Serena MCP tools (get_symbols_overview, find_symbol,
  find_referencing_symbols) over reading entire files. This reduces token usage.
- When modifying code, prefer Serena MCP tools (replace_symbol_body, insert_after_symbol,
  insert_before_symbol) over reading and rewriting entire files.
- Start with get_symbols_overview to understand file structure before diving in.
- Never read the same file region twice. Plan reads upfront.
- If Serena tools are unavailable or error, fall back to normal file operations.
```

**Acceptance criteria:**
- `mode-implement.txt` contains MANDATORY Serena section
- Editing tools (replace_symbol_body, insert_after_symbol, insert_before_symbol) are explicitly mentioned
- Fallback instruction present

---

### Issue 2: Add MANDATORY Serena guidance to mode-plan.txt

**Priority:** 1 (foundational, parallel with Issue 1)

**Files to change:**
- `prompts/mode-plan.txt`

**Details:**
Add a `SERENA MCP EFFICIENCY (MANDATORY)` section to `mode-plan.txt`. Since planning is read-only, only include read tools.

Section to add (after the "Rules" block, before "Output must be plain text"):

```
SERENA MCP EFFICIENCY (MANDATORY):
- Do NOT call onboarding, initial_instructions, or check_onboarding_performed.
  Skip straight to activate_project and then use tools directly.
- Prefer get_symbols_overview and find_symbol over raw file reads.
  This reduces token usage significantly.
- Use find_referencing_symbols to understand change impact before planning.
- Use search_for_pattern instead of shell grep when Serena is available.
- Never read the same file region twice. Plan reads upfront.
- If Serena tools are unavailable or error, fall back to normal file reads.
```

**Acceptance criteria:**
- `mode-plan.txt` contains MANDATORY Serena section
- Only read tools mentioned (no editing tools — plan mode is read-only)
- Fallback instruction present

---

### Issue 3: Strengthen Serena guidance in mode-orchestrate.txt and mode-clarify.txt

**Priority:** 2 (depends on nothing, but lower impact than Issues 1-2)

**Files to change:**
- `prompts/mode-orchestrate.txt`
- `prompts/mode-clarify.txt`

**Details:**

**mode-orchestrate.txt:** Replace the casual mention on line 31 ("Inspect the repo structure via Serena or file reads") with a MANDATORY section. Add after the "Rules" block:

```
SERENA MCP EFFICIENCY (MANDATORY):
- Do NOT call onboarding, initial_instructions, or check_onboarding_performed.
  Skip straight to activate_project and then use tools directly.
- Prefer get_symbols_overview and find_symbol over raw file reads.
- Use search_for_pattern instead of shell grep when Serena is available.
- Never read the same file region twice. Plan reads upfront.
- If Serena tools are unavailable or error, fall back to normal file reads.
```

Also update line 31 to: "Inspect the repo structure using Serena MCP tools (see below)." to point to the new section.

**mode-clarify.txt:** Strengthen line 10 from "Use Serena MCP tools or targeted file reads only for specific code symbols/functions you need to inspect" to a MANDATORY block. Add after the "Rules" block:

```
SERENA MCP EFFICIENCY (MANDATORY):
- Do NOT call onboarding, initial_instructions, or check_onboarding_performed.
  Skip straight to activate_project and then use tools directly.
- Prefer get_symbols_overview and find_symbol over raw file reads.
- Use search_for_pattern instead of shell grep when Serena is available.
- Never read the same file region twice. Plan reads upfront.
- If Serena tools are unavailable or error, fall back to normal file reads.
```

**Acceptance criteria:**
- Both prompt files have MANDATORY Serena sections
- Original weak references updated to point to the new MANDATORY section
- Consistent wording with other prompt files (judge, validate, clarify-respond)

---

### Issue 4: Extract canonical Serena guidance into a reusable prompt fragment

**Priority:** 3 (depends on Issues 1-3 being merged first)

**Files to change:**
- `prompts/serena-efficiency-block.txt` (new file — reusable fragment)
- `prompts/mode-implement.txt` — replace inline block with include reference
- `prompts/mode-plan.txt` — same
- `prompts/mode-orchestrate.txt` — same
- `prompts/mode-clarify.txt` — same
- `prompts/mode-judge.txt` — same
- `prompts/mode-clarify-respond.txt` — same
- `prompts/mode-validate-generate.txt` — same
- `prompts/mode-validate-diagnose.txt` — same
- `prompts/mode-judge-review-blocked.txt` — same
- Workflow YAML files that inject inline Serena blocks: `implement.yml`, `plan.yml`, `clarify.yml`, `review_autofix.yml`

**Details:**
Create `prompts/serena-efficiency-block.txt` with two variants:

1. **Read-only variant** (for planning/clarify/orchestrate/judge/validate-diagnose phases):
   - get_symbols_overview, find_symbol, find_referencing_symbols, search_for_pattern
   - No editing tools

2. **Read-write variant** (for implement/review-editor phases):
   - All read tools plus replace_symbol_body, insert_after_symbol, insert_before_symbol, rename_symbol
   - "NEVER rewrite an entire file if replace_symbol_body or insert_after_symbol can do it"

Each prompt file and workflow inline block should reference or include the appropriate variant. The mechanism for including depends on how prompts are assembled — if they're concatenated at runtime, the fragment file can be cat'd into the prompt. If they're loaded as strings, the workflow YAML can read the fragment file and inject it.

Inspect the prompt assembly mechanism in each workflow (how `mode-*.txt` files are loaded and combined with `codex_system_instructions.md`) to determine the best injection approach.

**Acceptance criteria:**
- Single source of truth for Serena guidance in `prompts/serena-efficiency-block.txt`
- All 9 prompt files reference the canonical block (no duplicated Serena paragraphs)
- Inline Serena blocks in workflow YAML files removed or replaced with fragment reference
- Two variants (read-only / read-write) clearly separated
- No behavioral change — LLM sees identical Serena guidance as before

---

### Issue 5: Add Context7 MCP server for library documentation lookup

**Priority:** 4 (independent of Issues 1-4)

**Files to change:**
- `scripts/setup_serena.sh` — add Context7 setup alongside Serena
- `README.md` — document new variables
- `codex_system_instructions.md` — add Context7 usage guidance

**Details:**
[Context7](https://github.com/upstash/context7) is an MCP server that provides up-to-date library/framework documentation on demand. Instead of the LLM guessing API signatures or hallucinating method names, it can query Context7 for the exact docs.

**Setup (in `setup_serena.sh` or a new `setup_mcp_servers.sh`):**

```bash
# Context7 MCP — library documentation lookup
if [ "${CONTEXT7_DISABLED:-false}" != "true" ]; then
  # Context7 runs via npx, no install needed
  # Add to ~/.codex/config.toml:
  # [mcp_servers.context7]
  # command = "npx"
  # args = ["-y", "@upstash/context7-mcp@latest"]
  # required = false
fi
```

**New environment variables:**

| Variable | Default | Description |
|---|---|---|
| `CONTEXT7_DISABLED` | `false` | Disable the Context7 MCP server |

**System instruction addition (in `codex_system_instructions.md`):**

```
## Context7 Library Documentation (when available)

When you need to use a library/framework API you're unsure about:
- Use `mcp__context7__resolve-library-id` to find the library
- Use `mcp__context7__query-docs` to fetch current documentation
- This avoids hallucinating API signatures and reduces retry loops.
- If Context7 is unavailable, proceed normally.
```

**Acceptance criteria:**
- Context7 MCP configured in `~/.codex/config.toml` when not disabled
- `required = false` (graceful fallback)
- System instructions guide the LLM to use the Context7 resolve + query-docs sequence when unsure about library APIs
- README.md documents `CONTEXT7_DISABLED` variable
- No impact on existing Serena setup

---

### Issue 6: Investigate and enable OpenRouter prompt caching

**Priority:** 5 (independent research + config change)

**Files to change:**
- Workflow YAML files (implement.yml, plan.yml, clarify.yml, review_autofix.yml, etc.) — add caching headers/parameters if supported
- `README.md` — document findings and any new variables

**Details:**
OpenRouter supports prompt caching for some models. The system instructions (`codex_system_instructions.md`) and Serena tool definitions are identical across all calls within a pipeline run and across runs. If cached, this prefix would be served from cache at reduced cost.

**Investigation steps:**
1. Check if the model used (`openai/gpt-5.3-codex` via OpenRouter) supports prompt caching
2. Check if Codex CLI passes through cache-control headers to OpenRouter
3. If supported, determine how to enable it (API parameter, header, or automatic)
4. If Codex doesn't support cache headers, check if OpenRouter auto-caches based on prefix matching
5. Document findings in README.md under a new "Cost optimization" section

**If caching is available:**
- Enable it for all phases
- The cacheable prefix includes: system instructions (~2K tokens) + Serena tool definitions (~1K tokens) + pipeline spec + static context
- Estimate savings based on current token usage patterns from `serena_efficiency_report.py` outputs

**If caching is NOT available:**
- Document why and what would need to change (model switch, Codex update, etc.)
- Consider if restructuring prompts (putting static content first) would help with future caching

**Acceptance criteria:**
- Clear determination of whether prompt caching works with current stack
- If yes: enabled and documented
- If no: documented with actionable next steps
- README.md updated with findings

---

### Issue 7: Add Git MCP server for on-demand diff/blame access

**Priority:** 6 (lowest priority — depends on Issue 4 for clean prompt structure)

**Files to change:**
- `scripts/setup_serena.sh` (or new `setup_mcp_servers.sh`) — add Git MCP setup
- `README.md` — document new variables
- `codex_system_instructions.md` — add Git MCP usage guidance
- `prompts/serena-efficiency-block.txt` (from Issue 4) — add Git MCP tools to the guidance

**Details:**
Currently, raw `git diff` output is bulk-injected into reviewer context upfront. A Git MCP server would let reviewers fetch diffs on demand — per-file or per-hunk — reducing the amount of diff context loaded into the prompt.

**Candidate MCP servers:**
- `@anthropic/git-mcp` — if available
- `mcp-git` (community) — provides git log, diff, blame, status as MCP tools
- Custom lightweight wrapper around `git` CLI

**Setup:**
```bash
if [ "${GIT_MCP_DISABLED:-false}" != "true" ]; then
  # [mcp_servers.git]
  # command = "npx"
  # args = ["-y", "mcp-git", "--repository", "."]
  # required = false
fi
```

**New environment variables:**

| Variable | Default | Description |
|---|---|---|
| `GIT_MCP_DISABLED` | `false` | Disable the Git MCP server |

**System instruction addition:**
```
## Git MCP Tools (when available)

For reviewing changes, prefer on-demand Git MCP tools over pre-loaded diff context:
- `mcp__git__diff` — fetch diff for specific files or ranges
- `mcp__git__blame` — understand change history for specific lines
- `mcp__git__log` — fetch commit history for a path
- This avoids loading the entire PR diff upfront.
- If Git MCP is unavailable, use the pre-loaded diff files as before.
```

**Risk:** This changes the review workflow pattern from "pre-loaded diff" to "on-demand diff". Needs careful testing to ensure reviewers still see all relevant changes. Consider keeping the pre-loaded diff as fallback and adding the Git MCP as a supplementary tool rather than a replacement.

**Acceptance criteria:**
- Git MCP configured in `~/.codex/config.toml` when not disabled
- `required = false` (graceful fallback)
- System instructions guide reviewers to use Git MCP for targeted diff access
- Pre-loaded diff remains available as fallback
- README.md documents `GIT_MCP_DISABLED` variable

---

## Dependency Graph

```
Issue 1 (mode-implement.txt)  ──┐
                                 ├──► Issue 4 (canonical fragment) ──► Issue 7 (Git MCP)
Issue 2 (mode-plan.txt)        ──┤
                                 │
Issue 3 (orchestrate + clarify) ─┘

Issue 5 (Context7 MCP)         [independent]

Issue 6 (prompt caching)       [independent]
```

| from | to | reason |
|------|----|--------|
| `serena-implement-prompt` | `serena-canonical-fragment` | Must have per-file Serena blocks before extracting the canonical version |
| `serena-plan-prompt` | `serena-canonical-fragment` | Same |
| `serena-orchestrate-clarify-prompts` | `serena-canonical-fragment` | Same |
| `serena-canonical-fragment` | `git-mcp-server` | Fragment file must exist before adding Git MCP guidance to it |

Issues 5 (Context7) and 6 (prompt caching) are fully independent and can run in parallel with everything else.

## Expected Token Savings (Cumulative)

| Optimization | Phase(s) Affected | Estimated Savings |
|---|---|---|
| Serena prompt consistency (Issues 1-4) | All phases | 5-15% additional compliance → translates to ~10-20% fewer full-file reads |
| Context7 library docs (Issue 5) | Implement, review-editor | 5-10% fewer retry loops from hallucinated APIs |
| Prompt caching (Issue 6) | All phases | 50-90% on cached prefix (~3K tokens per call, 7+ calls per issue lifecycle = ~20K tokens saved) |
| Git MCP on-demand diffs (Issue 7) | Review (x7 reviewers) | 10-20% reviewer input reduction (only load diffs they actually inspect) |
| **Combined** | **All** | **Estimated 25-45% total cost reduction on top of existing Serena savings** |

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Context7 MCP server unavailable in CI (npx download failure) | `required = false` + fallback instructions |
| Git MCP changes review quality (reviewers miss changes they didn't fetch) | Keep pre-loaded diff as fallback; Git MCP is supplementary, not replacement |
| Prompt caching not supported by current model/provider | Document findings; no wasted effort if unsupported |
| Canonical fragment extraction (Issue 4) breaks prompt assembly | Test on a non-production consumer repo first; verify identical LLM output |
| Adding more MCP servers increases CI startup time | Each MCP server adds ~2-5s cold start; total <15s, negligible vs LLM call time |
| LLM ignores MCP tools despite MANDATORY instructions | Monitor via `serena_efficiency_report.py`; consider removing competing file-read tools |

## Testing Strategy

- After each issue merges, run a smoke test issue (`[E2E Smoke Test]` prefix) on a consumer repo
- Compare `serena_efficiency_report.py` output before/after to measure actual token savings
- Monitor for regressions in pipeline success rate (issues that fail to implement/review)
