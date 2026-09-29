#!/usr/bin/env bash
# Body of the "Hand unresolved protected conflict to Claude session" step in
# .github/workflows/review_autofix.yml. The step sources this file in its own
# shell, so the step's if:, env: and continue-on-error: stay in the workflow;
# edit those there.
#
# Plan D14: on a Claude-fixer head, a pre-review merge conflict whose unmerged
# paths include a protected `.claude/**` path goes to the GPT conflict
# resolver instead of a Claude hand-off (the merge-topology gate sets
# CLAUDE_FIXER_PROTECTED_CONFLICT=true and CLAUDE_FIXER_PROTECTED_CONFLICT_PATHS).
# This step runs after the resolver when it left the conflict unresolved
# (failed or escalated, CONFLICT_RESOLVED != true) and posts today's
# kind=conflict hand-off through scripts/review_autofix_step_claude_fixer_handoff.sh,
# with one extra line naming the protected paths so the Claude session stops
# at once instead of trying to edit them.
#
# Inputs (environment): everything the hand-off script reads (see its
# header), plus CLAUDE_FIXER_PROTECTED_CONFLICT_PATHS. No API calls of its
# own; the hand-off script makes its usual comment read and post.
set -euo pipefail

export CLAUDE_FIXER_PROTECTED_CONFLICT_FALLBACK=true
echo "AUTOFIX_GATE_CLAUDE_FIXER_PROTECTED_CONFLICT pr=${PR_NUMBER:-} head=${HEAD_SHA:-} paths=${CLAUDE_FIXER_PROTECTED_CONFLICT_PATHS:-unknown} resolver=unresolved action=claude_fixer_handoff"
claude_fixer_protected_handoff_script="${SUPPORT_SCRIPTS_DIR:-}/review_autofix_step_claude_fixer_handoff.sh"
[ -f "${claude_fixer_protected_handoff_script}" ] || claude_fixer_protected_handoff_script="${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/review_autofix_step_claude_fixer_handoff.sh"
if [ ! -f "${claude_fixer_protected_handoff_script}" ]; then
  echo "::warning::review_autofix_step_claude_fixer_handoff.sh not found; the unresolved protected conflict gets no hand-off (the next run retries)."
  exit 0
fi
source "${claude_fixer_protected_handoff_script}"
