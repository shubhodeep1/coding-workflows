#!/usr/bin/env bash
# Body of the "Resolve AI engine" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# at or under 380,000 bytes, well under the 430,000-byte contract-test
# ceiling and GitHub's 512,000-byte workflow file limit (#10166). The step
# sources this file in its own shell, so the step's id:, if: and env: stay in
# the workflow; edit those there.
set -euo pipefail
any_claude=false
for role in REVIEW_EDITOR REVIEW_CONSOLIDATOR CONFLICT_RESOLVER RB_JUDGE; do
  engine="codex"
  if [ -f "${SUPPORT_SCRIPTS_DIR}/ai_engine.sh" ]; then
    # shellcheck disable=SC1091
    engine="$(source "${SUPPORT_SCRIPTS_DIR}/ai_engine.sh" && ai_engine_for_role "${role}" || echo codex)"
  fi
  [ "${engine}" = "claude" ] || engine="codex"
  [ "${engine}" = "codex" ] || any_claude=true
  echo "AI_ENGINE_RESOLVED_${role}=${engine}" >> "$GITHUB_ENV"
done
# An anthropic/claude-* REVIEWER_MODELS slot runs on the Claude
# account pool (engine role PANEL_REVIEWER, read-only sandbox,
# scripts/review_run_reviewers.sh run_pool_reviewer); the slot is
# skipped when the role resolves to codex.
panel_engine="codex"
if printf '%s\n' "${REVIEWER_MODELS:-}" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -Eq '^anthropic/claude-'; then
  if [ -f "${SUPPORT_SCRIPTS_DIR}/ai_engine.sh" ]; then
    # shellcheck disable=SC1091
    panel_engine="$(source "${SUPPORT_SCRIPTS_DIR}/ai_engine.sh" && ai_engine_for_role PANEL_REVIEWER || echo codex)"
  fi
  [ "${panel_engine}" = "claude" ] || panel_engine="codex"
  [ "${panel_engine}" = "codex" ] || any_claude=true
fi
echo "AI_ENGINE_RESOLVED_PANEL_REVIEWER=${panel_engine}" >> "$GITHUB_ENV"
echo "any_claude=${any_claude}" >> "$GITHUB_OUTPUT"
# ChatGPT account reviewer slot: resolved once from the trusted
# support copy of chatgpt_pool.json, never the PR checkout. A
# missing helper, a failure or any invalid value means off.
chatgpt_slot=false
chatgpt_model=""
chatgpt_replaces=""
chatgpt_reasoning=""
chatgpt_line=""
if [ -f "${SUPPORT_SCRIPTS_DIR}/claude_engine.py" ]; then
  chatgpt_line="$(PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/claude_engine.py" chatgpt-slot \
    --config "${GITHUB_WORKSPACE:-${PWD}}/.codex-workflow-src/.github/ai/chatgpt_pool.json" \
    --override "${REVIEWER_CHATGPT_SLOT_ENABLED:-}" --model "${REVIEWER_CHATGPT_MODEL:-}" \
    --replaces "${REVIEWER_CHATGPT_REPLACES:-}" 2>/dev/null)" || chatgpt_line=""
fi
for chatgpt_kv in ${chatgpt_line}; do
  case "${chatgpt_kv}" in
    enabled=true) chatgpt_slot=true ;;
    model=*) chatgpt_model="${chatgpt_kv#model=}" ;;
    replaces=*) chatgpt_replaces="${chatgpt_kv#replaces=}" ;;
    reasoning=*) chatgpt_reasoning="${chatgpt_kv#reasoning=}" ;;
  esac
done
[ -n "${chatgpt_model}" ] && [ -n "${chatgpt_replaces}" ] || chatgpt_slot=false
# No account is fetched for a slot whose replaced slug is not on the panel.
if [ "${chatgpt_slot}" = "true" ] && ! printf '%s\n' "${REVIEWER_MODELS:-}" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -Fx -- "${chatgpt_replaces}" >/dev/null; then
  chatgpt_slot=false
  chatgpt_line="${chatgpt_line} panel=replaces_not_in_panel"
fi
# Logged when the slot is on, an operator set the switch, or a configured slot was forced off;
# a plain switch-off or a missing file stays silent, so off-path logs stay unchanged.
chatgpt_forced_off=false
case "${chatgpt_line}" in *panel=replaces_not_in_panel*|*reason=var_invalid*|*reason=config_invalid*) chatgpt_forced_off=true ;; esac
if [ "${chatgpt_slot}" = "true" ] || [ -n "${REVIEWER_CHATGPT_SLOT_ENABLED:-}" ] || [ "${chatgpt_forced_off}" = "true" ]; then
  echo "REVIEWER_CHATGPT: resolve slot=${chatgpt_slot} ${chatgpt_line:-reason=helper_unavailable}"
fi
{
  echo "chatgpt_slot=${chatgpt_slot}"
  echo "chatgpt_model=${chatgpt_model}"
  echo "chatgpt_replaces=${chatgpt_replaces}"
  echo "chatgpt_reasoning=${chatgpt_reasoning}"
} >> "$GITHUB_OUTPUT"
