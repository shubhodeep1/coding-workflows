#!/usr/bin/env bash
# render_prompt.sh — Renders a mode prompt template by replacing Serena
# efficiency placeholders with content from the canonical fragment.
#
# Usage:
#   source scripts/render_prompt.sh
#   render_mode_prompt "prompts/mode-plan.txt"          # outputs rendered text
#   render_mode_prompt "prompts/mode-implement.txt"     # outputs rendered text
#
# Placeholders recognised:
#   {{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}
#   {{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}
#
# The canonical fragment lives at prompts/serena-efficiency-block.txt and
# uses ## BEGIN <VARIANT> / ## END <VARIANT> delimiters.
#
# Graceful fallback: if the canonical fragment is missing, the template is
# emitted as-is (with placeholders intact) and a warning is printed to
# stderr. This ensures consumer repos running older refs still function.

# Directory of this script (works when sourced from any CWD).
_RENDER_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REPO_ROOT="$(cd "${_RENDER_SCRIPT_DIR}/.." && pwd)"

_SERENA_FRAGMENT="${_REPO_ROOT}/prompts/serena-efficiency-block.txt"

# _extract_variant <variant>
# Extracts lines between ## BEGIN <variant> and ## END <variant> (exclusive).
_extract_variant() {
  local variant="$1"
  sed -n "/^## BEGIN ${variant}\$/,/^## END ${variant}\$/{ /^## /d; p; }" "${_SERENA_FRAGMENT}"
}

# render_mode_prompt <template_path>
# Reads the template, replaces placeholders with canonical content, and
# prints the result to stdout.  Falls back to plain cat if the canonical
# fragment is missing.
render_mode_prompt() {
  local template="$1"
  local rendered

  if [ ! -f "${_SERENA_FRAGMENT}" ]; then
    echo "render_prompt.sh: WARNING — canonical fragment not found; falling back to cat" >&2
    cat "${template}"
    return 0
  fi

  rendered="$(cat "${template}")"

  # Replace READ_ONLY placeholder
  if grep -qF '{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}' <<< "${rendered}"; then
    local ro_block
    ro_block="$(_extract_variant "READ_ONLY")"
    # Use awk for safe multi-line replacement
    rendered="$(awk -v block="${ro_block}" '{
      if (index($0, "{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}") > 0) {
        print block
      } else {
        print
      }
    }' <<< "${rendered}")"
  fi

  # Replace READ_WRITE placeholder
  if grep -qF '{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}' <<< "${rendered}"; then
    local rw_block
    rw_block="$(_extract_variant "READ_WRITE")"
    rendered="$(awk -v block="${rw_block}" '{
      if (index($0, "{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}") > 0) {
        print block
      } else {
        print
      }
    }' <<< "${rendered}")"
  fi

  # Safety: warn if any placeholder token survived rendering.
  if grep -qE '\{\{SERENA_EFFICIENCY_BLOCK_(READ_ONLY|READ_WRITE)\}\}' <<< "${rendered}"; then
    echo "render_prompt.sh: WARNING — unresolved placeholder in ${template}" >&2
  fi

  printf '%s\n' "${rendered}"
}
