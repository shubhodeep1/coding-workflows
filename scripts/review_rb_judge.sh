#!/usr/bin/env bash
set -euo pipefail
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
if [ -f "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" ] && source "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null; then
  :
else
  # Try to re-fetch the helper script if it was removed during cleanup.
  wf_source="${REPOSITORY%/*}/coding-workflows"
  if [ "${REPOSITORY}" = "${wf_source}" ]; then
    script_ref="${GITHUB_SHA}"
  else
    script_ref="stable"
  fi
  mkdir -p "${SUPPORT_SCRIPTS_DIR}"
  if { gh api -H 'Accept: application/vnd.github.raw+json' \
    "repos/${wf_source}/contents/scripts/label_helpers.sh?ref=${script_ref}" > "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null || \
     gh api -H 'Accept: application/vnd.github.raw+json' \
      "repos/${wf_source}/contents/scripts/label_helpers.sh?ref=main" > "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null; } && \
    [ -s "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" ] && source "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null; then
    chmod +x "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh"
  else
    # Last-resort inline fallback if fetch fails.
    ensure_label_exists() {
      local label_name="$1"
      local repo="$2"
      case "${label_name}" in
        ai:ready-to-merge)
          gh label create "${label_name}" --repo "${repo}" --color "0e8a16" --description "PR review complete and ready to merge" 2>/dev/null || true
          ;;
        ai:closed)
          gh label create "${label_name}" --repo "${repo}" --color "6a737d" --description "Linked PR closed without merge" 2>/dev/null || true
          ;;
        *)
          gh label create "${label_name}" --repo "${repo}" --color "1d76db" --description "AI workflow label" 2>/dev/null || true
          ;;
      esac
    }
  fi
fi

echo "judge_handled=false" >> "$GITHUB_OUTPUT"
echo "judge_skip_reason=" >> "$GITHUB_OUTPUT"

if [ "${ENABLE_REVIEW_BLOCKED_JUDGE}" != "true" ]; then
  echo "Review-blocked judge disabled (set ENABLE_REVIEW_BLOCKED_JUDGE=true to enable)."
  echo "judge_skip_reason=disabled" >> "$GITHUB_OUTPUT"
  exit 0
fi

if [ "${CAN_PUSH:-false}" != "true" ]; then
  echo "Branch not writable — skipping judge."
  echo "judge_skip_reason=not_writable" >> "$GITHUB_OUTPUT"
  exit 0
fi

ensure_label_exists "ai:ready-to-merge" "${REPOSITORY}"
ensure_label_exists "ai:closed" "${REPOSITORY}"

# -----------------------------------------------------------
# Find linked issues for judge context
# -----------------------------------------------------------
ISSUE_NUMBERS="$(gh api graphql \
  -f owner="${REPOSITORY%/*}" \
  -f name="${REPOSITORY#*/}" \
  -F number="${PR_NUMBER}" \
  -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
  --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"

if [ -z "${ISSUE_NUMBERS}" ]; then
  PR_DATA="$(_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.title + " " + (.body // "")' || echo "")"
  REPOSITORY_ESCAPED="${REPOSITORY//./\\.}"
  ISSUE_NUMBERS="$(echo "${PR_DATA}" | grep -oiE "(github\\.com/${REPOSITORY_ESCAPED}/issues/[0-9]+|${REPOSITORY_ESCAPED}/issues/[0-9]+|(^|[^[:alnum:]_/-])issues/[0-9]+|issue[[:space:]]*#[[:space:]]*[0-9]+|(closes|fixes|resolves)[[:space:]]*:?[[:space:]]*#[[:space:]]*[0-9]+)" | grep -oE '[0-9]+$' | sort -un || true)"
fi

FIRST_ISSUE=""
FIRST_ISSUE_BODY=""
while IFS= read -r issue_number; do
  [ -n "${issue_number}" ] || continue
  if [ -z "${FIRST_ISSUE}" ]; then
    FIRST_ISSUE="${issue_number}"
  fi
  BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
  if [ -z "${FIRST_ISSUE_BODY}" ]; then
    FIRST_ISSUE_BODY="${BODY}"
  fi
done <<< "${ISSUE_NUMBERS}"

if [ -z "${FIRST_ISSUE}" ]; then
  echo "No linked issues found — judge will use PR title/body as requirement context."
fi

# -----------------------------------------------------------
# Check retry budget
# -----------------------------------------------------------
RETRY_COUNT="${JUDGE_FIX_COUNT:-0}"
IS_FINAL="false"
if [ "${RETRY_COUNT}" -ge "${MAX_REVIEW_BLOCKED_RETRIES}" ]; then
  IS_FINAL="true"
  echo "Judge retries exhausted (${RETRY_COUNT}/${MAX_REVIEW_BLOCKED_RETRIES}) — final decision."
else
  echo "Judge retry ${RETRY_COUNT}/${MAX_REVIEW_BLOCKED_RETRIES}."
fi

# -----------------------------------------------------------
# Collect PR context for judge
# -----------------------------------------------------------
PR_DIFF="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
  -H 'Accept: application/vnd.github.diff' 2>/dev/null || echo "(diff unavailable)")"
PR_COMMENTS="$(gh api --paginate "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" \
  | jq -s 'add // [] | [.[] | {author: .user.login, body: .body, created_at: .created_at}]' 2>/dev/null || echo "[]")"
PR_REVIEW_COMMENTS="$(gh api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/comments" \
  | jq -s 'add // [] | [.[] | {author: .user.login, path: .path, line: .line, body: .body}]' 2>/dev/null || echo "[]")"
PR_META_JSON="$(jq '.' "${PR_META_FILE}" 2>/dev/null || echo "{}")"

compute_rb_token_budget_hint() {
  local prompt_file="$1"
  local model_slug="$2"
  local catalog_path model_meta prompt_bytes prompt_tokens context_window
  local effective_percent_raw effective_context remaining_tokens

  prompt_bytes="$(wc -c < "${prompt_file}" 2>/dev/null | tr -d '[:space:]' || true)"
  if ! [[ "${prompt_bytes}" =~ ^[0-9]+$ ]]; then
    echo "advisory unavailable: unable to estimate prompt bytes"
    return 0
  fi
  prompt_tokens=$(( (prompt_bytes + 3) / 4 ))

  catalog_path="${SUPPORT_ROOT_DIR}/scripts/codex_model_catalog.json"
  if [ ! -f "${catalog_path}" ]; then
    echo "estimated_tokens=${prompt_tokens}; advisory only: model context metadata unavailable"
    return 0
  fi

  model_meta="$(jq -c --arg slug "${model_slug}" '.models[]? | select(.slug == $slug)' "${catalog_path}" 2>/dev/null | head -n1 || true)"
  if [ -z "${model_meta}" ]; then
    echo "estimated_tokens=${prompt_tokens}; advisory only: model metadata not found for ${model_slug}"
    return 0
  fi

  context_window="$(jq -r '.context_window // empty' <<< "${model_meta}" 2>/dev/null || true)"
  if ! [[ "${context_window}" =~ ^[0-9]+$ ]] || [ "${context_window}" -le 0 ]; then
    echo "estimated_tokens=${prompt_tokens}; advisory only: invalid context_window metadata"
    return 0
  fi

  effective_context="${context_window}"
  effective_percent_raw="$(jq -r 'if has("effective_context_window_percent") and .effective_context_window_percent != null then (.effective_context_window_percent|tostring) else "" end' <<< "${model_meta}" 2>/dev/null || true)"
  if [ -n "${effective_percent_raw}" ]; then
    if [[ "${effective_percent_raw}" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v p="${effective_percent_raw}" 'BEGIN { exit !(p > 0 && p <= 100) }'; then
      effective_context="$(awk -v c="${context_window}" -v p="${effective_percent_raw}" 'BEGIN { printf "%d", (c * p / 100.0) + 0.5 }')"
      if ! [[ "${effective_context}" =~ ^[0-9]+$ ]] || [ "${effective_context}" -le 0 ]; then
        echo "estimated_tokens=${prompt_tokens}; advisory only: invalid effective context metadata"
        return 0
      fi
    else
      echo "estimated_tokens=${prompt_tokens}; advisory only: invalid effective_context_window_percent metadata"
      return 0
    fi
  fi

  remaining_tokens=$(( effective_context - prompt_tokens ))
  if [ "${remaining_tokens}" -lt 0 ]; then
    remaining_tokens=0
  fi

  echo "${remaining_tokens} (approx remaining context tokens; advisory only)"
}

# -----------------------------------------------------------
# Build judge prompt
# -----------------------------------------------------------
RB_JUDGE_PROMPT="${RUNTIME_DIR}/rb_judge_prompt.txt"
RB_JUDGE_OUTPUT="${RUNTIME_DIR}/rb_judge_output.txt"

{
  if [ -f ./pre_assembled_static.txt ]; then
    cat ./pre_assembled_static.txt
  fi
  echo
  echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE}"
  echo
  echo "=== REVIEW-BLOCKED JUDGE TASK ==="
  echo
  if [ -f "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt" ]; then
    (
      cd "${SUPPORT_ROOT_DIR}"
      bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"
    )
  else
    echo "Evaluate the review-blocked PR and decide: merge, fix, or close_and_reissue."
  fi
  echo
  if [ -n "${FIRST_ISSUE}" ]; then
    echo "=== ISSUE #${FIRST_ISSUE} (original requirement) ==="
    echo
    echo "${FIRST_ISSUE_BODY}"
  else
    echo "=== PR DESCRIPTION (no linked issue) ==="
    echo
    echo "Title: $(jq -r '.title // ""' "${PR_META_FILE}")"
    echo "Body: $(jq -r '.body // ""' "${PR_PAYLOAD_FILE}")"
  fi
  echo
  echo "=== PR #${PR_NUMBER} METADATA ==="
  echo
  echo "${PR_META_JSON}" | jq '.'
  echo
  echo "=== PR #${PR_NUMBER} DIFF ==="
  echo
  head -1000 <<< "${PR_DIFF}"
  echo
  echo "=== PR #${PR_NUMBER} COMMENTS (editor summaries, reviewer findings) ==="
  echo
  echo "${PR_COMMENTS}" | jq '.'
  echo
  echo "=== PR #${PR_NUMBER} INLINE REVIEW COMMENTS ==="
  echo
  echo "${PR_REVIEW_COMMENTS}" | jq '.'
  echo
  echo "=== REVIEW-BLOCKED CONTEXT ==="
  echo "Review-blocked judge retry: $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}"
  echo "Retries exhausted: ${IS_FINAL}"
  if [ "${IS_FINAL}" = "true" ]; then
    echo
    echo "IMPORTANT: This is the FINAL attempt. You MUST choose either 'merge' or"
    echo "'close_and_reissue'. The 'fix' option is NOT available because previous"
    echo "fix attempts did not resolve the issues. Pick the action that best serves"
    echo "the project: merge if the PR is good enough, or close and reissue if the"
    echo "approach is fundamentally wrong."
  fi
} > "${RB_JUDGE_PROMPT}"

RB_TOKEN_BUDGET_HINT="$(compute_rb_token_budget_hint "${RB_JUDGE_PROMPT}" "${MODEL_EDITOR}")"
RB_JUDGE_PROMPT_TMP="${RB_JUDGE_PROMPT}.tmp"
if awk -v hint="${RB_TOKEN_BUDGET_HINT}" '
  {
    print
    if (!inserted && $0 ~ /^TOOL_CALL_BUDGET:/) {
      print "TOKEN_BUDGET_HINT: " hint
      inserted=1
    }
  }
' "${RB_JUDGE_PROMPT}" > "${RB_JUDGE_PROMPT_TMP}" && mv "${RB_JUDGE_PROMPT_TMP}" "${RB_JUDGE_PROMPT}"; then
  :
else
  rm -f "${RB_JUDGE_PROMPT_TMP}"
fi

# -----------------------------------------------------------
# Temporarily set judge reasoning effort in codex config
# -----------------------------------------------------------
if [ -f "${HOME}/.codex/config.toml" ]; then
  sed -i "s/model_reasoning_effort = \".*\"/model_reasoning_effort = \"${JUDGE_REASONING_EFFORT}\"/" "${HOME}/.codex/config.toml"
fi

# -----------------------------------------------------------
# Run the judge
# -----------------------------------------------------------
JUDGE_SUCCESS=false
for attempt in 1 2; do
  echo "Review-blocked judge attempt ${attempt}/2..."
  if codex exec --model "${MODEL_EDITOR}" --full-auto < "${RB_JUDGE_PROMPT}" > "${RB_JUDGE_OUTPUT}" 2>/dev/null; then
    if grep -q '[^[:space:]]' "${RB_JUDGE_OUTPUT}"; then
      JUDGE_SUCCESS=true
      break
    fi
  fi
  if [ "${attempt}" -lt 2 ]; then
    sleep 10
  fi
done

# Restore editor reasoning effort
if [ -f "${HOME}/.codex/config.toml" ]; then
  sed -i "s/model_reasoning_effort = \".*\"/model_reasoning_effort = \"${EDITOR_REASONING_EFFORT}\"/" "${HOME}/.codex/config.toml"
fi

if [ "${JUDGE_SUCCESS}" != "true" ]; then
  echo "::warning::Review-blocked judge failed — falling back to manual intervention."
  exit 0
fi

# -----------------------------------------------------------
# Parse judge output
# -----------------------------------------------------------
JUDGE_JSON="$(PYTHONDONTWRITEBYTECODE=1 python3 -c "
import json, re, sys

raw = open('${RB_JUDGE_OUTPUT}', 'r').read()

try:
    data = json.loads(raw.strip())
    json.dump(data, sys.stdout)
    sys.exit(0)
except json.JSONDecodeError:
    pass

cleaned = re.sub(r'\`\`\`(?:json)?\s*', '', raw)
cleaned = re.sub(r'\`\`\`\s*$', '', cleaned, flags=re.MULTILINE)

brace_depth = 0
start = None
for i, ch in enumerate(cleaned):
    if ch == '{':
        if brace_depth == 0:
            start = i
        brace_depth += 1
    elif ch == '}':
        brace_depth -= 1
        if brace_depth == 0 and start is not None:
            candidate = cleaned[start:i+1]
            try:
                data = json.loads(candidate)
                json.dump(data, sys.stdout)
                sys.exit(0)
            except json.JSONDecodeError:
                start = None

print('Could not parse review-blocked judge JSON', file=sys.stderr)
sys.exit(1)
" 2>/dev/null || echo "")"

if [ -z "${JUDGE_JSON}" ]; then
  echo "::warning::Could not parse review-blocked judge output — falling back to manual intervention."
  exit 0
fi

RB_ACTION="$(echo "${JUDGE_JSON}" | jq -r '.action')"
RB_JUSTIFICATION="$(echo "${JUDGE_JSON}" | jq -r '.justification // "no justification"')"
RB_FIX_DESC="$(echo "${JUDGE_JSON}" | jq -r '.fix_description // ""')"
RB_REMAINING="$(echo "${JUDGE_JSON}" | jq -r '.remaining_issues_summary // ""')"

echo "Judge decision: ${RB_ACTION}"
echo "Justification: ${RB_JUSTIFICATION}"

# Post judge assessment to PR
JUDGE_COMMENT="## Review-Blocked Judge Decision

**Decision:** ${RB_ACTION}
**Retry:** $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}
**Justification:** ${RB_JUSTIFICATION}

**Remaining issues:** ${RB_REMAINING}"

gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" \
  -f body="${JUDGE_COMMENT}" >/dev/null 2>&1 || true

# -----------------------------------------------------------
# Execute judge action
# -----------------------------------------------------------
case "${RB_ACTION}" in
  merge)
    echo "Judge says merge PR #${PR_NUMBER} as-is."

    # Label linked issues ready-to-merge
    ensure_label_exists "ai:ready-to-merge" "${REPOSITORY}"
    while IFS= read -r issue_number; do
      [ -n "${issue_number}" ] || continue
      gh issue edit "${issue_number}" --repo "${REPOSITORY}" \
        --remove-label 'ai:done' --remove-label 'ai:implementing' --remove-label 'ai:awaiting-approval' \
        --remove-label 'ai:planning' --remove-label 'ai:clarification' --remove-label 'ai:ready-to-merge' \
        --remove-label 'ai:review-blocked' --remove-label 'ai:merged' --remove-label 'ai:closed' \
        --add-label 'ai:ready-to-merge' || true
    done <<< "${ISSUE_NUMBERS}"

    # Attempt merge.
    #
    # GitHub's REST `pulls` API returns `mergeable` as one of three values:
    #   - true   : merge is clean
    #   - false  : real merge conflicts
    #   - null   : GitHub has not finished computing mergeability yet
    #              (typical immediately after a push). Mergeability is
    #              computed asynchronously, so we must poll briefly before
    #              treating an empty value as a hard failure — otherwise a
    #              transient `null` is indistinguishable from a real conflict
    #              in the log.
    PR_STATE=""
    PR_MERGEABLE=""
    _mergeable_attempts="${PR_MERGEABLE_POLL_ATTEMPTS:-6}"
    _mergeable_sleep="${PR_MERGEABLE_POLL_SLEEP:-5}"
    _attempt=0
    while [ "${_attempt}" -lt "${_mergeable_attempts}" ]; do
      _pr_json="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo '{}')"
      PR_STATE="$(echo "${_pr_json}" | jq -r '.state // ""' | grep -xE 'open|closed|merged' || echo "")"
      PR_MERGEABLE="$(echo "${_pr_json}" | jq -r '.mergeable // ""' | grep -xE 'true|false' || echo "")"
      # Stop polling as soon as state is terminal or mergeability is known.
      if [ "${PR_STATE}" != "open" ] || [ -n "${PR_MERGEABLE}" ]; then
        break
      fi
      _attempt=$((_attempt + 1))
      if [ "${_attempt}" -lt "${_mergeable_attempts}" ]; then
        echo "PR #${PR_NUMBER} mergeable=null (GitHub still computing); retrying in ${_mergeable_sleep}s (${_attempt}/${_mergeable_attempts})."
        sleep "${_mergeable_sleep}"
      fi
    done

    if [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "true" ]; then
      if [ "${ENABLE_AUTO_MERGE}" = "true" ]; then
        gh pr merge "${PR_NUMBER}" --repo "${REPOSITORY}" --squash --auto 2>/dev/null \
          || gh pr merge "${PR_NUMBER}" --repo "${REPOSITORY}" --squash 2>/dev/null || true
      fi
    elif [ "${PR_STATE}" = "open" ] && [ "${PR_MERGEABLE}" = "false" ]; then
      echo "::warning::PR #${PR_NUMBER} has merge conflicts (mergeable=false); judge cannot merge as-is."
      echo "PR #${PR_NUMBER} state=${PR_STATE} mergeable=false, merge conflicts present."
    else
      echo "PR #${PR_NUMBER} state=${PR_STATE} mergeable=${PR_MERGEABLE:-null}, cannot merge yet (mergeability still computing or PR not open)."
    fi

    echo "judge_handled=true" >> "$GITHUB_OUTPUT"
    echo "judge_action=merge" >> "$GITHUB_OUTPUT"
    ;;

  fix)
    if [ "${IS_FINAL}" = "true" ]; then
      echo "Judge returned 'fix' but retries exhausted — treating as merge."

      ensure_label_exists "ai:ready-to-merge" "${REPOSITORY}"
      while IFS= read -r issue_number; do
        [ -n "${issue_number}" ] || continue
        gh issue edit "${issue_number}" --repo "${REPOSITORY}" \
          --remove-label 'ai:done' --remove-label 'ai:implementing' --remove-label 'ai:awaiting-approval' \
          --remove-label 'ai:planning' --remove-label 'ai:clarification' --remove-label 'ai:ready-to-merge' \
          --remove-label 'ai:review-blocked' --remove-label 'ai:merged' --remove-label 'ai:closed' \
          --add-label 'ai:ready-to-merge' || true
      done <<< "${ISSUE_NUMBERS}"

      PR_STATE="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.state' 2>/dev/null | grep -xE 'open|closed|merged' || echo "")"
      if [ "${PR_STATE}" = "open" ] && [ "${ENABLE_AUTO_MERGE}" = "true" ]; then
        gh pr merge "${PR_NUMBER}" --repo "${REPOSITORY}" --squash --auto 2>/dev/null \
          || gh pr merge "${PR_NUMBER}" --repo "${REPOSITORY}" --squash 2>/dev/null || true
      fi

      echo "judge_handled=true" >> "$GITHUB_OUTPUT"
      echo "judge_action=merge" >> "$GITHUB_OUTPUT"
    else
      echo "Judge is applying fixes to PR #${PR_NUMBER}..."

      # Re-run the judge in editing mode on the PR branch
      RB_FIX_PROMPT="${RUNTIME_DIR}/rb_fix_prompt.txt"
      RB_FIX_OUTPUT="${RUNTIME_DIR}/rb_fix_output.txt"
      {
        cat "${RB_JUDGE_PROMPT}"
        echo
        echo "=== APPLY FIXES NOW ==="
        echo "You are on the PR branch (${TARGET_BRANCH})."
        echo "Apply the fixes you identified directly to the repository files."
        echo "Focus only on the issues that blocked the review."
        echo "Do not create new files unless absolutely required."
        echo "After applying fixes, output the same JSON with action='fix' and"
        echo "fix_description describing what you changed."
      } > "${RB_FIX_PROMPT}"

      # Temporarily restore judge reasoning for fix application
      if [ -f "${HOME}/.codex/config.toml" ]; then
        sed -i "s/model_reasoning_effort = \".*\"/model_reasoning_effort = \"${JUDGE_REASONING_EFFORT}\"/" "${HOME}/.codex/config.toml"
      fi

      if codex exec --model "${MODEL_EDITOR}" --full-auto < "${RB_FIX_PROMPT}" > "${RB_FIX_OUTPUT}" 2>/dev/null; then
        echo "Fix codex completed."
      else
        echo "::warning::Fix codex failed for PR #${PR_NUMBER}."
      fi

      # Restore editor reasoning effort
      if [ -f "${HOME}/.codex/config.toml" ]; then
        sed -i "s/model_reasoning_effort = \".*\"/model_reasoning_effort = \"${EDITOR_REASONING_EFFORT}\"/" "${HOME}/.codex/config.toml"
      fi

      # Check for changes and commit
      if [ -n "$(git status --porcelain)" ]; then
        git config user.name "codex-bot"
        git config user.email "codex@users.noreply.github.com"

        # Clean up workflow-fetched artifacts before committing
        if [[ "${REPOSITORY}" != *"/coding-workflows" ]]; then
          rm -f ./pre_assembled_static.txt
          rm -f codex_system_instructions.md ai_pipeline.md unattended_llm_system_instructions.md agents.md
          rm -f scripts/setup_serena.sh scripts/git_ref_health_check.sh scripts/serena_efficiency_report.py scripts/generate_symbol_diff_summary.py scripts/label_helpers.sh scripts/codex_model_catalog.json
          rm -f scripts/memory_helpers.sh scripts/ai_memory.py scripts/ai_memory_lib.py
          rm -f scripts/review_run_reviewers.sh scripts/review_apply_fixes.sh scripts/review_rb_judge.sh
          rm -rf ai-memory
          rm -rf .serena
          rm -rf prompts
        fi

        if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then
          git add -u -- ':!node_modules' ':!scripts/memory_helpers.sh' ':!scripts/ai_memory.py' ':!scripts/ai_memory_lib.py' ':!scripts/review_run_reviewers.sh' ':!scripts/review_apply_fixes.sh' ':!scripts/review_rb_judge.sh' ':!ai-memory' ':!.github/prompts' ':!.github/scripts'
        else
          git add -u -- ':!node_modules' ':!scripts' ':!prompts' ':!.serena' ':!ai-memory' ':!.github/prompts' ':!.github/scripts'
        fi
        echo "Staged files before commit:"
        STAGED_FILES="$(git diff --cached --name-only || true)"
        printf '%s\n' "${STAGED_FILES}" | sed '/^$/d; s/^/ - /' || true
        if printf '%s\n' "${STAGED_FILES}" | grep -Eq '^\.github/(prompts|scripts)/'; then
          echo "Error: .github/prompts or .github/scripts is staged"
          exit 1
        fi
        if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ] && printf '%s\n' "${STAGED_FILES}" | grep -Eq '^(scripts/|prompts/|\.serena/|\.github/scripts/|\.github/prompts/|ai-memory/)'; then
          echo "Error: workflow runtime/helper artifacts are staged in consumer repo"
          exit 1
        fi
        if ! git diff --cached --quiet; then
          git commit -m "[judge-fix] address review-blocked issues

Review-blocked judge applied fixes to unblock the review pipeline.
Retry $((RETRY_COUNT + 1)) of ${MAX_REVIEW_BLOCKED_RETRIES}.

${RB_FIX_DESC}"
          git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/${REPOSITORY}"
          if git push origin "HEAD:${TARGET_BRANCH}"; then
            echo "Pushed [judge-fix] commit to ${TARGET_BRANCH}."
            echo "judge_handled=true" >> "$GITHUB_OUTPUT"
            echo "judge_action=fix" >> "$GITHUB_OUTPUT"
          else
            echo "::warning::Failed to push judge fix — falling back to manual intervention."
          fi
        else
          echo "Judge staged no effective changes. Treating as merge."
          ensure_label_exists "ai:ready-to-merge" "${REPOSITORY}"
          while IFS= read -r issue_number; do
            [ -n "${issue_number}" ] || continue
            gh issue edit "${issue_number}" --repo "${REPOSITORY}" \
              --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
          done <<< "${ISSUE_NUMBERS}"
          echo "judge_handled=true" >> "$GITHUB_OUTPUT"
          echo "judge_action=merge" >> "$GITHUB_OUTPUT"
        fi
      else
        echo "Judge produced no file changes. Treating as merge."
        ensure_label_exists "ai:ready-to-merge" "${REPOSITORY}"
        while IFS= read -r issue_number; do
          [ -n "${issue_number}" ] || continue
          gh issue edit "${issue_number}" --repo "${REPOSITORY}" \
            --remove-label 'ai:review-blocked' --add-label 'ai:ready-to-merge' 2>/dev/null || true
        done <<< "${ISSUE_NUMBERS}"
        echo "judge_handled=true" >> "$GITHUB_OUTPUT"
        echo "judge_action=merge" >> "$GITHUB_OUTPUT"
      fi
    fi
    ;;

  close_and_reissue)
    echo "Judge says close PR #${PR_NUMBER} and reissue."

    # Close the PR
    gh pr close "${PR_NUMBER}" --repo "${REPOSITORY}" \
      --comment "Closed by review-blocked judge — the approach needs rework. A new issue will be created with refined guidance." \
      2>/dev/null || true

    # Label linked issues as closed
    ensure_label_exists "ai:closed" "${REPOSITORY}"
    while IFS= read -r issue_number; do
      [ -n "${issue_number}" ] || continue
      gh issue edit "${issue_number}" --repo "${REPOSITORY}" \
        --remove-label 'ai:review-blocked' --remove-label 'ai:done' \
        --add-label 'ai:closed' 2>/dev/null || true
    done <<< "${ISSUE_NUMBERS}"

    # Create replacement issue
    NEW_ISSUE_TITLE="$(echo "${JUDGE_JSON}" | jq -r '.new_issue.title // empty')"
    NEW_ISSUE_BODY="$(echo "${JUDGE_JSON}" | jq -r '.new_issue.body // empty' | sed 's/\\n/\n/g')"
    if [ -n "${NEW_ISSUE_TITLE}" ] && [ -n "${NEW_ISSUE_BODY}" ]; then
      FULL_NEW_BODY="${NEW_ISSUE_BODY}

---
**Review-blocked reissue metadata**
- Replaces: ${FIRST_ISSUE:+#${FIRST_ISSUE} }(PR #${PR_NUMBER} closed — approach rework)
- Type: review-blocked-reissue"

      NEW_URL="$(gh_retry gh issue create \
        --repo "${REPOSITORY}" \
        --title "${NEW_ISSUE_TITLE}" \
        --body "${FULL_NEW_BODY}")"
      echo "Created replacement issue: ${NEW_URL}"
    else
      echo "::warning::Judge chose close_and_reissue but provided no new issue details."
    fi

    echo "judge_handled=true" >> "$GITHUB_OUTPUT"
    echo "judge_action=close_and_reissue" >> "$GITHUB_OUTPUT"
    ;;

  *)
    echo "::warning::Unknown review-blocked judge action: ${RB_ACTION} — falling back to manual intervention."
    ;;
esac
