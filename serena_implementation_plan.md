# Serena MCP Integration — Implementation Plan

## Goal

Reduce editor LLM token usage in the `implement` and `review_autofix` workflows by integrating the [Serena MCP server](https://github.com/oraios/serena) as a semantic code navigation and editing layer. Instead of the editor LLM reading entire files and outputting full rewrites, it will use Serena's LSP-backed symbol-level tools to surgically read and edit code.

## Current State

### Token Cost Breakdown

| Phase | Model | Token Driver |
|---|---|---|
| **Implement** | `gpt-5.3-codex` (via OpenRouter) | Reads full files to understand code, outputs full file rewrites |
| **Review (x7 models)** | Mixed (minimax, gemini, deepseek, etc.) | Each receives full PR diff + last-run diff + comments context |
| **Autofix (editor)** | `gpt-5.3-codex` | Reads reviewer reports + re-reads source files + outputs full edits |
| **Conflict resolution** | `gpt-5.3-codex` | Reads conflict markers + surrounding code |

### Why Serena Helps

The editor LLM currently uses `codex exec --full-auto`, which gives it shell + file read/write tools. When it needs to understand code, it `cat`s entire files. When it edits, it rewrites entire files. Serena replaces this with:

- **`find_symbol`** — Jump to a function/class definition without reading the whole file
- **`get_symbols_overview`** — See a file's structure (classes, functions, exports) in a few tokens
- **`find_referencing_symbols`** — Find all callers/usages without grepping the entire codebase
- **`replace_symbol_body`** — Edit a single function body without outputting the entire file
- **`insert_after_symbol` / `insert_before_symbol`** — Add code at precise locations

The system instructions (`codex_system_instructions.md`) already reference Serena tools and mark them as "STRONGLY PREFERRED" — but **no Serena MCP server is actually running** in CI, so the tools are unavailable and the LLM silently falls back to full-file reads.

---

## Implementation Plan

### Phase 1: CI Environment Setup

#### 1.1 Install Serena in GitHub Actions

Add Serena installation to both `implement.yml` and `review_autofix.yml` alongside the existing Codex install step.

**Option A — Install via `uv` + `uvx` (recommended for flexibility):**

```yaml
- name: Install uv and Serena MCP server
  uses: astral-sh/setup-uv@v4

- name: Warm Serena cache
  run: |
    set -euo pipefail
    SERENA_VERSION="${{ vars.SERENA_VERSION || 'main' }}"
    echo "SERENA_VERSION=${SERENA_VERSION}" >> "$GITHUB_ENV"
    uvx --from "git+https://github.com/oraios/serena@${SERENA_VERSION}" serena --version
```

**Option B — Use the official Docker image (for heavier CI or multi-language):**

```yaml
services:
  serena:
    image: ghcr.io/oraios/serena:latest
    ports:
      - "9121:9121"
    volumes:
      - ${{ github.workspace }}:/workspaces/projects/repo
    # Pre-built with Node.js 22, Rust + rust-analyzer, Python
    command: >
      serena start-mcp-server
      --transport streamable-http --port 9121 --host 0.0.0.0
      --project /workspaces/projects/repo
      --context codex --mode one-shot --mode editing
      --open-web-dashboard false
```

**Key decisions:**
- Pin Serena version via `vars.SERENA_VERSION` for reproducibility (default: `main`)
- Use `astral-sh/setup-uv@v4` GitHub Action instead of raw `curl` install
- Language server installation is repo-dependent — start with TypeScript + Python as they cover most use cases
- Consumer repos can override via `vars.SERENA_LANGUAGES` in the future
- The Docker image (`ghcr.io/oraios/serena:latest`) ships with Node.js, Rust, and Python pre-installed — good for multi-language repos
- Some languages need NO extra setup: Bash, JavaScript, TypeScript, Python, Java, Dart, Swift, YAML, Markdown

#### 1.2 Create Serena Project Configuration

Add a `.serena/project.yml` template that consumer repos should include, and a fallback auto-generation step in the workflow.

**File: `.serena/project.yml` (template for consumer repos)**

Uses the [full project config schema](https://github.com/oraios/serena/blob/main/src/serena/resources/project.template.yml):

```yaml
project_name: auto
read_only: false                        # Stage 1: set true; Stage 2: set false

# Languages for LSP backends
# Serena auto-detects, but explicit is better for CI
languages:
  - typescript
  - python

# File handling
encoding: utf-8
ignore_all_files_in_gitignore: true
ignored_paths:
  - node_modules
  - dist
  - build
  - __pycache__
  - .next
  - vendor

# Tool configuration
excluded_tools: []                      # e.g. ["execute_shell_command"] to restrict
included_optional_tools: []             # e.g. ["restart_language_server"]

# Modes — override defaults for CI
base_modes: []
default_modes:
  - editing

# Symbol info retrieval timeout (seconds, null = global default 10s)
symbol_info_budget: null

# Initial prompt injected when project activates
initial_prompt: ""
```

A `project.local.yml` alongside overrides without version control.

**In the workflow, auto-generate if missing:**
```bash
# Auto-create .serena/project.yml if not present
if [ ! -f .serena/project.yml ]; then
  mkdir -p .serena
  cat > .serena/project.yml <<'SERENA_EOF'
project_name: auto
read_only: false
ignore_all_files_in_gitignore: true
ignored_paths:
  - node_modules
  - dist
  - build
  - __pycache__
  - .next
  - vendor
SERENA_EOF
fi
```

#### 1.3 Configure Codex to Use Serena as MCP Server

Codex supports MCP servers. Add the Serena MCP server config to the Codex configuration step.

**Modify the "Create Codex config" step in both workflows.**

**Option A — stdio transport (Codex launches Serena as subprocess):**

```bash
# Add Serena MCP server to Codex
cat > ~/.codex/mcp.json <<MCP_EOF
{
  "mcpServers": {
    "serena": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/oraios/serena@${SERENA_VERSION:-main}",
        "serena", "start-mcp-server",
        "--context", "codex",
        "--mode", "one-shot",
        "--mode", "editing",
        "--project-from-cwd",
        "--open-web-dashboard", "false"
      ]
    }
  }
}
MCP_EOF
```

**Option B — HTTP transport (if using Docker sidecar from 1.1 Option B):**

```bash
cat > ~/.codex/mcp.json <<MCP_EOF
{
  "mcpServers": {
    "serena": {
      "url": "http://localhost:9121/mcp"
    }
  }
}
MCP_EOF
```

**Also create the global Serena config to disable GUI and enable stats:**

```bash
mkdir -p ~/.serena
cat > ~/.serena/serena_config.yml <<'SERENA_CFG'
language_backend: LSP
gui_log_window: false
web_dashboard: false
web_dashboard_open_on_launch: false
record_tool_usage_stats: true
log_level: 20
tool_timeout: 240
default_modes:
  - editing
SERENA_CFG
```

**Context choice: `--context=codex`** because:
- Serena has a dedicated `codex` context optimized for OpenAI Codex
- Disables tools that duplicate Codex's built-ins (basic file read/write, shell commands)
- Forces the LLM to use Serena only for its unique value: **symbol-level operations**
- Alternative: `--context=agent` for non-Codex editors (includes all tools)

**Available contexts:**
| Context | Use case |
|---|---|
| `codex` | OpenAI Codex — disables duplicate tools |
| `claude-code` | Claude Code CLI — similar dedup |
| `agent` | Autonomous agents (Agno, etc.) — full toolset |
| `desktop-app` | Claude Desktop — full toolset (default) |
| `ide` | VSCode/Cursor/Cline |

**Mode choice: `--mode=one-shot --mode=editing`** because:
- `one-shot` — complete task autonomously without waiting for user input (CI appropriate)
- `editing` — enables all editing tools (`replace_symbol_body`, `insert_after_symbol`, etc.)
- Modes are composable — both are active simultaneously
- Alternative for plan phase: `--mode=planning` (read-only analysis)

---

### Phase 2: System Instructions Update

#### 2.1 Strengthen Serena Preference in `codex_system_instructions.md`

The current instructions say "STRONGLY PREFERRED" but the LLM has no enforcement. Update to make the instructions more actionable now that Serena is actually available.

**Replace the current Serena section with:**

```markdown
## Serena (MCP) semantic tooling (MANDATORY when available)

Goal: reduce token usage + speed up code understanding by using Serena's LSP-backed
semantic tools instead of full-file reads and full-file rewrites.

Rules:
- ALWAYS use Serena semantic tools for code navigation over full-file reads.
- NEVER read an entire source file if you can get what you need from symbol tools.
- NEVER rewrite an entire file if you can use `replace_symbol_body` or `insert_after_symbol`.

### Reading code (use INSTEAD of cat/read):
- `mcp__serena__get_symbols_overview` — See file structure (classes, functions, exports)
- `mcp__serena__find_symbol` — Jump to a specific symbol definition
- `mcp__serena__find_referencing_symbols` — Find all callers/usages of a symbol
- `mcp__serena__find_referencing_code_snippets` — Get code context around references
- `mcp__serena__search_for_pattern` — Regex search (replaces grep)

### Editing code (use INSTEAD of full-file writes):
- `mcp__serena__replace_symbol_body` — Replace a function/class body surgically
- `mcp__serena__insert_after_symbol` — Add code after a symbol definition
- `mcp__serena__insert_before_symbol` — Add code before a symbol definition
- `mcp__serena__insert_at_line` — Insert at a specific line
- `mcp__serena__delete_lines` — Remove a range of lines
- `mcp__serena__rename_symbol` — Rename across codebase (LSP refactor)
- `mcp__serena__create_text_file` — Create new files

### Workflow:
1. Start with `get_symbols_overview` to understand file structure
2. Use `find_symbol` to drill into specific functions
3. Use `find_referencing_symbols` to understand impact of changes
4. Edit with `replace_symbol_body` or `insert_after_symbol` — NOT full-file rewrites

### Fallback:
- If Serena tools are unavailable or return errors, fall back to normal file reads/writes.
- Do not stall or fail the task if Serena is down.
```

#### 2.2 Add Serena Guidance to Implement Prompt

In the `implement.yml` Codex prompt (`CODEX_PROMPT_FILE`), add a line after the existing instructions:

```
- When modifying code, prefer Serena MCP tools (replace_symbol_body, insert_after_symbol)
  over reading and rewriting entire files. This reduces token usage significantly.
```

#### 2.3 Add Serena Guidance to Autofix Editor Prompt

In the `review_autofix.yml` editor prompt, add similar guidance:

```
- Use Serena MCP symbol-level editing tools to apply fixes surgically.
  Do NOT read entire files when you only need to modify a specific function.
  Do NOT output full file contents when replace_symbol_body can target the exact symbol.
```

---

### Phase 3: Review Workflow Optimization

#### 3.1 Generate Symbol-Level Diff Context for Reviewers

Instead of sending raw unified diffs to the 7 reviewer models, pre-process the diff into a symbol-level summary. This can be done with a pre-review step using Serena.

**Add a new step before the reviewer loop in `review_autofix.yml`:**

```bash
- name: Generate symbol-level diff summary
  run: |
    set -euo pipefail
    # Use Serena to create a structured summary of what changed at the symbol level
    # This is much more compact than raw unified diffs
    python3 scripts/generate_symbol_diff_summary.py \
      --diff-file "${PR_DIFF_FILE}" \
      --changed-files "${PR_CHANGED_FILES_FILE}" \
      --output "${RUNTIME_DIR}/symbol_diff_summary.txt"
```

**New script: `scripts/generate_symbol_diff_summary.py`**

This script would:
1. Parse the unified diff to extract changed file paths and line ranges
2. For each changed file, call Serena's `get_symbols_overview` to identify which symbols were affected
3. Output a compact summary like:
   ```
   FILE: src/auth/login.py
     MODIFIED: function authenticate_user (lines 45-67)
     ADDED: function validate_token (after line 89)
     MODIFIED: class AuthProvider.refresh_session (lines 102-118)

   FILE: src/api/routes.py
     MODIFIED: function register_routes (lines 23-25) — added /auth/refresh endpoint
   ```
4. This symbol-level summary replaces or supplements the raw diff for reviewers, dramatically cutting reviewer input tokens

#### 3.2 Selective File Context for Reviewers

Currently all 7 reviewers get the full PR diff. Instead:

1. Split the diff by file
2. Group files by domain/module
3. Send each reviewer only the files relevant to their review pass
4. Use `get_symbols_overview` on each changed file to provide structural context without full file contents

This is a larger change and can be done as a follow-up optimization.

---

### Phase 4: Language Server Management

#### 4.1 Auto-Detect Required Language Servers

Add a detection step that scans the consumer repo and installs only the needed language servers.

```bash
- name: Install language servers for project
  run: |
    set -euo pipefail

    # Detect languages from file extensions
    LANGS=""
    [ -n "$(find . -maxdepth 3 -name '*.ts' -o -name '*.tsx' | head -1)" ] && LANGS="${LANGS} typescript"
    [ -n "$(find . -maxdepth 3 -name '*.py' | head -1)" ] && LANGS="${LANGS} python"
    [ -n "$(find . -maxdepth 3 -name '*.go' | head -1)" ] && LANGS="${LANGS} go"
    [ -n "$(find . -maxdepth 3 -name '*.rs' | head -1)" ] && LANGS="${LANGS} rust"
    [ -n "$(find . -maxdepth 3 -name '*.java' | head -1)" ] && LANGS="${LANGS} java"

    # Allow override via vars
    LANGS="${{ vars.SERENA_LANGUAGES || '' }} ${LANGS}"

    for lang in ${LANGS}; do
      case "${lang}" in
        typescript) npm install -g typescript-language-server typescript --no-audit --no-fund ;;
        python)     pip install python-lsp-server ;;
        go)         go install golang.org/x/tools/gopls@latest ;;
        rust)       rustup component add rust-analyzer 2>/dev/null || true ;;
        java)       echo "Java LSP requires manual setup — skipping" ;;
      esac
    done
```

#### 4.2 Serena Health Check

Add a validation step after Serena setup to confirm the MCP server starts correctly:

```bash
- name: Validate Serena MCP server
  run: |
    set -euo pipefail
    # Quick smoke test — start and immediately stop
    timeout 15s uvx --from "git+https://github.com/oraios/serena@${SERENA_VERSION:-main}" \
      serena start-mcp-server --context=agent --project-from-cwd --help \
      && echo "Serena MCP server validated" \
      || echo "::warning::Serena MCP server validation failed — will fall back to file-based editing"
```

---

### Phase 5: Token Usage Monitoring

#### 5.1 Track Token Savings

Add token usage logging to compare before/after Serena integration.

**In both workflows, after the Codex exec step, capture usage stats:**

```bash
- name: Log token usage
  if: always()
  run: |
    set -euo pipefail
    # Codex outputs usage to stderr — capture and log
    if [ -f "${CODEX_OUTPUT_FILE}" ]; then
      echo "::group::Editor LLM output size"
      wc -c < "${CODEX_OUTPUT_FILE}"
      echo "::endgroup::"
    fi
    # Log Serena tool call stats if available
    if [ -f .serena/tool_usage_stats.json ]; then
      echo "::group::Serena tool usage"
      cat .serena/tool_usage_stats.json
      echo "::endgroup::"
    fi
```

#### 5.2 Set Serena to Record Tool Usage

In the global Serena config, enable stats:

```bash
mkdir -p ~/.serena
cat > ~/.serena/serena_config.yml <<'EOF'
record_tool_usage_stats: true
included_optional_tools: []
EOF
```

---

## Rollout Plan

### Stage 1: Read-Only Integration (Low Risk)
- Install Serena in CI
- Configure as MCP server for Codex
- Set `read_only: true` initially
- Update system instructions to prefer Serena reads
- **Expected impact:** ~30-40% reduction in input tokens for implement phase (symbol reads vs full-file reads)
- **Risk:** Minimal — if Serena fails, LLM falls back to file reads

### Stage 2: Enable Editing Tools (Medium Risk)
- Set `read_only: false`
- Enable `replace_symbol_body`, `insert_after_symbol`, etc.
- **Expected impact:** ~40-60% reduction in output tokens (surgical edits vs full-file rewrites)
- **Risk:** LSP edits may not cover all languages or edge cases — fallback behavior already in system instructions

### Stage 3: Reviewer Token Optimization (Medium Effort)
- Build `generate_symbol_diff_summary.py`
- Send symbol-level summaries to reviewers instead of / alongside raw diffs
- **Expected impact:** ~20-30% reduction in reviewer input tokens across 7 models
- **Risk:** Summary generation adds a step; must handle edge cases (binary files, generated code)

### Stage 4: Monitoring & Tuning
- Compare token usage metrics before/after
- Tune `ignored_dirs` and language server selection per consumer repo
- Consider adding `--mode=planning` for the plan phase (read-only, no editing tools exposed)

---

## File Changes Summary

| File | Change |
|---|---|
| `.github/workflows/implement.yml` | Add Serena install, config, MCP setup, health check steps |
| `.github/workflows/review_autofix.yml` | Add Serena install, config, MCP setup steps; optional symbol-diff pre-processing |
| `codex_system_instructions.md` | Strengthen Serena section from "STRONGLY PREFERRED" to "MANDATORY when available" with full tool reference |
| `.serena/project.yml` | New template file for consumer repos (auto-generated in CI if missing) |
| `scripts/generate_symbol_diff_summary.py` | New script (Phase 3) — converts unified diffs to symbol-level summaries |

## Complete Serena Tool Inventory

Tools available via MCP, grouped by category. Availability depends on the `--context` and `--mode` flags.

### Symbol Tools (LSP-powered — the core value)
| Tool | Description |
|---|---|
| `find_symbol` | Global/local symbol search via language server |
| `find_referencing_symbols` | Find all references to a symbol |
| `find_referencing_code_snippets` | Get code context around symbol references |
| `get_symbols_overview` | Top-level symbols in a file (classes, functions, exports) |
| `replace_symbol_body` | Replace a symbol's full definition body |
| `insert_after_symbol` | Insert code after a symbol definition |
| `insert_before_symbol` | Insert code before a symbol definition |
| `rename_symbol` | Rename across entire codebase (LSP refactor) |
| `restart_language_server` | Restart LSP backend (optional tool) |

### File Tools (some disabled in `codex` context since Codex has its own)
| Tool | Description |
|---|---|
| `read_file` | Read a project file (disabled in `codex`/`claude-code` contexts) |
| `create_text_file` | Create or overwrite a file (disabled in `codex`/`claude-code` contexts) |
| `list_dir` | List directory contents |
| `find_file` | Find files by name/pattern |
| `replace_content` | Regex/string replacement (disabled in `codex`/`claude-code` contexts) |
| `delete_lines` | Delete a range of lines |
| `replace_lines` | Replace a range of lines |
| `insert_at_line` | Insert content at a specific line |
| `search_for_pattern` | Regex search across project |

### Workflow & Thinking Tools
| Tool | Description |
|---|---|
| `onboarding` | Project structure discovery (first-run) |
| `check_onboarding_performed` | Check if onboarding was done |
| `think_about_collected_information` | Reasoning tool (optional) |
| `think_about_task_adherence` | Check if agent is on track (optional) |
| `summarize_changes` | Summarize codebase changes (optional) |

### Memory Tools
| Tool | Description |
|---|---|
| `write_memory` | Persist project info as markdown |
| `read_memory` | Retrieve stored memory |
| `list_memories` | List available memories |
| `delete_memory` | Remove a memory |

### Config Tools
| Tool | Description |
|---|---|
| `activate_project` | Activate a project by name/path |
| `get_current_config` | Print current config state |
| `switch_modes` | Change active modes during session |

## Alternative: Programmatic Python Integration (No MCP)

For Phase 3 (symbol-diff summary generation), Serena can be used directly via Python without the MCP layer:

```python
from serena.agent import SerenaAgent

agent = SerenaAgent(project_path="/path/to/repo")
agent.activate_project()

# Get symbol overview for a changed file
symbols = agent.call_tool("get_symbols_overview", {"file_path": "src/auth/login.py"})

# Find what references a modified function
refs = agent.call_tool("find_referencing_symbols", {
    "file_path": "src/auth/login.py",
    "symbol_name": "authenticate_user"
})
```

This avoids MCP overhead and is ideal for the `generate_symbol_diff_summary.py` script.

## Dependencies & Requirements

- **uv** — Serena's package manager (use `astral-sh/setup-uv@v4` GitHub Action)
- **Language servers** — Per-language; many need no extra install (JS/TS, Python, Java, Bash, etc.)
  - Extra install needed: Go (`gopls`), Rust (`rust-analyzer` via rustup), C/C++ (`clangd`)
- **Codex MCP support** — Codex must support `~/.codex/mcp.json` for MCP server configuration (verify with pinned version)
- **No new secrets required** — Serena runs locally, no API keys needed
- **Docker image** — `ghcr.io/oraios/serena:latest` (optional, ships Node.js 22 + Rust + Python)

## Expected Token Savings

| Component | Before | After (estimated) | Saving |
|---|---|---|---|
| Implement — input tokens | ~50K per run | ~20-30K | 40-60% |
| Implement — output tokens | ~15K per run | ~5-8K | 47-67% |
| Autofix editor — input tokens | ~40K per run | ~15-25K | 38-63% |
| Autofix editor — output tokens | ~10K per run | ~3-5K | 50-70% |
| Reviewers (x7) — input tokens | ~30K each | ~20-25K each | 17-33% |
| **Total per issue lifecycle** | **~350-400K** | **~180-250K** | **~35-50%** |

These are conservative estimates. The actual savings depend on repo size, file sizes, and how well the LLM adheres to Serena tool usage over full-file operations.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Serena MCP server fails to start in CI | Health check step + fallback instructions in system prompt |
| Language server not available for repo's language | Auto-detection + `vars.SERENA_LANGUAGES` override + graceful fallback |
| Codex version doesn't support MCP | Pin and test Codex version; gate Serena setup behind version check |
| LLM ignores Serena tools and reads full files anyway | Strengthen system instructions; monitor tool usage stats; consider removing file read tools from Codex sandbox |
| LSP edits produce invalid code | LLM validates with build/test after edits; Serena's LSP ensures syntactic correctness |
| CI runtime increases due to LSP startup | LSP cold start is ~2-5s; negligible vs LLM call time (~60-120s) |
