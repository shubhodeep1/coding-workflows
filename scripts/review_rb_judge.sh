#!/usr/bin/env bash
# Pre-flight syntax check: detect transient file corruption (e.g. CI
# runner filesystem issues) before executing.  On failure, print the
# exact parse error, dump an md5 fingerprint for post-mortem, and exit
# with a clear diagnostic instead of a cryptic "unexpected token" deep
# in the script.
if ! bash -n "${BASH_SOURCE[0]}" 2>/tmp/_rb_judge_syntax_err; then
	echo "::error::review_rb_judge.sh failed pre-flight syntax check — file may be corrupt on this runner."
	echo "--- syntax error detail ---"
	cat /tmp/_rb_judge_syntax_err
	echo "--- file fingerprint ---"
	md5sum "${BASH_SOURCE[0]}" 2>/dev/null || wc -c < "${BASH_SOURCE[0]}"
	echo "---"
	rm -f /tmp/_rb_judge_syntax_err
	exit 2
fi
rm -f /tmp/_rb_judge_syntax_err

set -euo pipefail
SUPPORT_SCRIPTS_DIR="${SUPPORT_SCRIPTS_DIR:-/tmp/codex-support}"
SUPPORT_ROOT_DIR="${SUPPORT_ROOT_DIR:-$(pwd)}"
SUPPORT_PROMPTS_DIR="${SUPPORT_PROMPTS_DIR:-${SUPPORT_ROOT_DIR}/prompts}"
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
# Fallback: if gh_helpers.sh was not sourced (missing file), define a
# pass-through so subsequent `gh_retry gh ...` calls still execute —
# without the rate-limit retry/alert behaviour, but without hard-failing
# under `set -e`.
if ! command -v gh_retry >/dev/null 2>&1; then
  gh_retry() { "$@"; }
fi
REVIEW_RB_SEMBLE_HELPERS_AVAILABLE="false"
REVIEW_RB_SEMBLE_MAX_CHUNKS="4"
REVIEW_RB_SEMBLE_QUERY_MAX_BYTES="12000"
REVIEW_RB_SEMBLE_CONTEXT_MAX_BYTES="12000"
if [ -f "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh" ]; then
  # shellcheck disable=SC1090
  if source "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh"; then
    if type semble_query_block >/dev/null 2>&1; then
      REVIEW_RB_SEMBLE_HELPERS_AVAILABLE="true"
    else
      echo "::warning::${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh did not provide semble_query_block; continuing without Semble review-blocked judge context." >&2
    fi
  else
    echo "::warning::Failed to source ${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh; continuing without Semble review-blocked judge context." >&2
  fi
fi

append_review_rb_semble_query_section() {
  local label="$1"
  local text="${2:-}"
  local max_bytes="${3:-2048}"
  local truncated_text=""

  [ -n "${text}" ] || return 0

  truncated_text="${text:0:${max_bytes}}"
  printf '%s\n' "${label}"
  printf '%s\n' "${truncated_text}"
}

render_review_rb_semble_prefetch() {
  local query_file="$1"
  local header_label="${2:-Review-Blocked Judge Context}"
  local query_text=""
  local prefetch_text=""

  if [ "${REVIEW_RB_SEMBLE_HELPERS_AVAILABLE}" != "true" ] \
    || [ "${SEMBLE_INDEX_AVAILABLE:-false}" != "true" ] \
    || [ ! -s "${query_file}" ]; then
    return 0
  fi

  query_text="$(head -c "${REVIEW_RB_SEMBLE_QUERY_MAX_BYTES}" "${query_file}" 2>/dev/null || true)"
  [ -n "${query_text}" ] || return 0

  prefetch_text="$(semble_query_block "${query_text}" "${REVIEW_RB_SEMBLE_MAX_CHUNKS}" "${header_label}" || true)"
  [ -n "${prefetch_text}" ] || return 0

  printf '%s\n' "${prefetch_text:0:${REVIEW_RB_SEMBLE_CONTEXT_MAX_BYTES}}"
}
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
  if { gh_retry gh api -H 'Accept: application/vnd.github.raw+json' \
    "repos/${wf_source}/contents/scripts/label_helpers.sh?ref=${script_ref}" > "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null || \
     gh_retry gh api -H 'Accept: application/vnd.github.raw+json' \
      "repos/${wf_source}/contents/scripts/label_helpers.sh?ref=main" > "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null; } && \
    [ -s "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" ] && source "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh" 2>/dev/null; then
    chmod +x "${SUPPORT_SCRIPTS_DIR}/label_helpers.sh"
  else
    # Last-resort inline fallback if fetch fails.
    #
    # NOTE: `gh label create` is intentionally NOT wrapped with
    # `gh_retry` here — a "label already exists" (422) is a
    # non-transient error, and `gh_retry` would add ~31 s of
    # exponential backoff before the trailing `|| true` takes
    # effect. Same rationale as `ensure_label_exists` in
    # `scripts/orchestrate_poll_process.sh`. Rate-limit alerts
    # still fire through every other `gh_retry`-wrapped call
    # elsewhere in this script.
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

# _resilient_phase_swap <issue_number> <target_label>
#
# Atomically swap AI phase labels on an issue via REST API GET+PUT,
# avoiding the `gh issue edit --remove-label` failure mode where a
# label that does not exist as a repo label definition aborts the
# entire command.  Falls back to POST (add-only) on PUT failure.
# API calls: 2 (GET + PUT) happy path, 3 on fallback.
_resilient_phase_swap()
{
	local _rps_issue="$1" _rps_target="$2"
	local _rps_phases='["ai:done","ai:implementing","ai:awaiting-approval","ai:planning","ai:clarification","ai:ready-to-merge","ai:review-blocked","ai:implementation-failed","ai:merged","ai:closed"]'
	local _rps_cur _rps_new
	if ! _rps_cur="$(gh_retry gh api --paginate "repos/${REPOSITORY}/issues/${_rps_issue}/labels" \
		--jq '[.[].name]' 2>/dev/null | jq -cs 'add // []')"; then
		echo "::warning::_resilient_phase_swap: GET labels failed for #${_rps_issue} — falling back to POST add." >&2
		gh_retry gh api -X POST "repos/${REPOSITORY}/issues/${_rps_issue}/labels" \
			-f "labels[]=${_rps_target}" >/dev/null 2>&1 \
			|| echo "::warning::_resilient_phase_swap: POST fallback also failed for #${_rps_issue}." >&2
		return 1
	fi
	_rps_cur="${_rps_cur:-[]}"
	_rps_new="$(echo "${_rps_cur}" | jq -c --argjson p "${_rps_phases}" --arg t "${_rps_target}" \
		'(. - $p) + [$t] | unique')"
	if printf '{"labels":%s}' "${_rps_new}" | \
		gh_retry gh api -X PUT "repos/${REPOSITORY}/issues/${_rps_issue}/labels" \
			--input - >/dev/null 2>&1; then
		return 0
	fi
	echo "::warning::_resilient_phase_swap: PUT failed for #${_rps_issue} — falling back to POST add." >&2
	gh_retry gh api -X POST "repos/${REPOSITORY}/issues/${_rps_issue}/labels" \
		-f "labels[]=${_rps_target}" >/dev/null 2>&1 \
		|| echo "::warning::_resilient_phase_swap: POST fallback also failed for #${_rps_issue}." >&2
}

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

# Early guard: skip judge if the PR is no longer open (e.g. already merged
# by a prior run). This prevents wasting AI compute and posting duplicate
# decisions on closed/merged PRs even if upstream gate/env guards fail.
_pr_state="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.state' 2>/dev/null || echo "")"
if [ -n "${_pr_state}" ] && [ "${_pr_state}" != "open" ]; then
  echo "PR #${PR_NUMBER} is ${_pr_state} — skipping review-blocked judge (PR not open)."
  echo "judge_handled=true" >> "$GITHUB_OUTPUT"
  echo "judge_action=skip" >> "$GITHUB_OUTPUT"
  echo "judge_skip_reason=pr_not_open" >> "$GITHUB_OUTPUT"
  if [ -n "${GITHUB_ENV:-}" ]; then
    echo "PR_CLOSED=true" >> "$GITHUB_ENV"
  fi
  exit 0
fi
unset _pr_state

ensure_label_exists "ai:ready-to-merge" "${REPOSITORY}"
ensure_label_exists "ai:closed" "${REPOSITORY}"

# -----------------------------------------------------------
# Find linked issues for judge context
# -----------------------------------------------------------
ISSUE_NUMBERS="$(gh_retry gh api graphql \
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
# Labels of the parent (FIRST_ISSUE) issue.  Captured from the same
# REST GET that already fetches the body so the close_and_reissue
# branch below can propagate orchestrator-lineage labels without
# issuing an extra API call (see CLAUDE.md §15 — extend an existing
# call rather than adding a new one).
FIRST_ISSUE_LABELS_JSON="[]"
while IFS= read -r issue_number; do
  [ -n "${issue_number}" ] || continue
  ISSUE_META_JSON="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" || echo '{}')"
  BODY="$(printf '%s' "${ISSUE_META_JSON}" | jq -r '.body // ""' 2>/dev/null || echo "")"
  if [ -z "${FIRST_ISSUE}" ]; then
    FIRST_ISSUE="${issue_number}"
    FIRST_ISSUE_LABELS_JSON="$(printf '%s' "${ISSUE_META_JSON}" | jq -c '[(.labels // [])[]?.name]' 2>/dev/null || echo '[]')"
  fi
  if [ -z "${FIRST_ISSUE_BODY}" ]; then
    FIRST_ISSUE_BODY="${BODY}"
  fi
  # Stop once we have the first issue number and a non-empty body.
  # Subsequent linked issues are not used by review_rb_judge.sh; the
  # FIRST_ISSUE_LABELS_JSON capture above is already pinned to
  # FIRST_ISSUE on the first iteration, so the break preserves
  # parent-label propagation semantics.
  [ -n "${FIRST_ISSUE}" ] && [ -n "${FIRST_ISSUE_BODY}" ] && break
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
PR_DIFF="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
  -H 'Accept: application/vnd.github.diff' 2>/dev/null || echo "(diff unavailable)")"
PRELOADED_PR_META="$(jq -c '{
  title: (.title // ""),
  body: (.body // ""),
  head_ref: (.head_ref // .head.ref // .headRefName // ""),
  base_ref: (.base_ref // .base.ref // .baseRefName // ""),
  head_sha: (.head_sha // .head.sha // .headSha // "")
}' "${PR_META_FILE}" 2>/dev/null || echo '{}')"
if type gh_pr_with_all_comments >/dev/null 2>&1; then
  PR_CONTEXT_JSON="$(gh_pr_with_all_comments "${REPOSITORY%%/*}" "${REPOSITORY##*/}" "${PR_NUMBER}" "${PRELOADED_PR_META}" || echo '{}')"
elif type _gh_pr_with_all_comments_rest >/dev/null 2>&1; then
  PR_CONTEXT_JSON="$(_gh_pr_with_all_comments_rest "${REPOSITORY%%/*}" "${REPOSITORY##*/}" "${PR_NUMBER}" "${PRELOADED_PR_META}" || echo '{}')"
else
  printf '%s\n' "::warning::rate_limit_audit_fallback helper=gh_pr_with_all_comments mode=legacy_rest_hydration reason=helper_unavailable owner=${REPOSITORY%%/*} repo=${REPOSITORY##*/} pr=${PR_NUMBER}" >&2
  PR_ISSUE_COMMENTS="$(gh_retry gh api --paginate "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" 2>/dev/null | jq -cs 'add // [] | [.[] | {author: .user.login, body: .body, created_at: .created_at}] | sort_by((.created_at // ""), (.author // ""), (.body // ""))' 2>/dev/null || echo '[]')"
  PR_REVIEW_COMMENTS="$(gh_retry gh api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/comments" 2>/dev/null | jq -cs 'add // [] | [.[] | {author: .user.login, path: .path, line: .line, body: .body}] | sort_by((.path // ""), (.line // 0), (.author // ""), (.body // ""))' 2>/dev/null || echo '[]')"
  PR_CONTEXT_JSON="$(jq -cn --argjson meta "${PRELOADED_PR_META}" --argjson comments "${PR_ISSUE_COMMENTS}" --argjson review_comments "${PR_REVIEW_COMMENTS}" '{meta: $meta, comments: $comments, review_comments: $review_comments}' 2>/dev/null || echo '{}')"
fi
PR_COMMENTS="$(printf '%s' "${PR_CONTEXT_JSON}" | jq -c '.comments // []' 2>/dev/null || echo "[]")"
PR_REVIEW_COMMENTS="$(printf '%s' "${PR_CONTEXT_JSON}" | jq -c '.review_comments // []' 2>/dev/null || echo "[]")"
PR_META_JSON="$(printf '%s' "${PR_CONTEXT_JSON}" | jq -c '.meta // {}' 2>/dev/null || echo "{}")"
if [ "${PR_META_JSON}" = "{}" ]; then
  PR_META_JSON="$(jq '.' "${PR_META_FILE}" 2>/dev/null || echo "{}")"
fi

# -----------------------------------------------------------
# Build judge prompt
# -----------------------------------------------------------
RB_JUDGE_PROMPT="${RUNTIME_DIR}/rb_judge_prompt.txt"
RB_JUDGE_OUTPUT="${RUNTIME_DIR}/rb_judge_output.txt"
RB_JUDGE_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/rb_judge_semble_query.txt"
RB_JUDGE_SEMBLE_PREFETCH=""

{
  printf '%s\n' 'Review-blocked judge context.'
  if [ -n "${FIRST_ISSUE_BODY}" ]; then
    append_review_rb_semble_query_section "Issue body:" "${FIRST_ISSUE_BODY}" 2500
  else
    append_review_rb_semble_query_section \
      "PR title/body:" \
      "Title: $(jq -r '.title // ""' "${PR_META_FILE}")
Body: $(jq -r '.body // ""' "${PR_PAYLOAD_FILE}")" \
      2500
  fi
  append_review_rb_semble_query_section "PR metadata JSON:" "${PR_META_JSON}" 2000
  append_review_rb_semble_query_section "PR diff excerpt:" "${PR_DIFF}" 5000
  append_review_rb_semble_query_section "PR issue comments JSON:" "${PR_COMMENTS}" 2500
  append_review_rb_semble_query_section "PR review comments JSON:" "${PR_REVIEW_COMMENTS}" 2500
} > "${RB_JUDGE_SEMBLE_QUERY_FILE}"
RB_JUDGE_SEMBLE_PREFETCH="$(render_review_rb_semble_prefetch "${RB_JUDGE_SEMBLE_QUERY_FILE}" "Review-Blocked Judge Context")"

{
  if [ -f ./pre_assembled_static.txt ]; then
    cat ./pre_assembled_static.txt
  fi
  echo
  echo "=== REVIEW-BLOCKED JUDGE TASK ==="
  echo
  if [ -f "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt" ]; then
    (
      cd "${SUPPORT_ROOT_DIR}"
      SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"
    )
  else
    echo "Evaluate the review-blocked PR and decide: merge, fix, or close_and_reissue."
  fi
  echo
  echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_JUDGE}"
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
rm -f "${RB_JUDGE_SEMBLE_QUERY_FILE}"

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
JUDGE_STDERR_FILE="${RUNTIME_DIR}/rb_judge_stderr.txt"
for attempt in 1 2; do
  echo "Review-blocked judge attempt ${attempt}/2..."
  if codex --ask-for-approval never -c model_verbosity=high -c include_apply_patch_tool=true exec --model "${MODEL_EDITOR}" --sandbox read-only < "${RB_JUDGE_PROMPT}" > "${RB_JUDGE_OUTPUT}" 2>"${JUDGE_STDERR_FILE}"; then
    if grep -q '[^[:space:]]' "${RB_JUDGE_OUTPUT}"; then
      JUDGE_SUCCESS=true
      break
    else
      echo "::warning::Judge attempt ${attempt}/2 produced empty output."
    fi
  else
    echo "::warning::Judge attempt ${attempt}/2 codex exec failed (rc=$?)."
    if [ -s "${JUDGE_STDERR_FILE}" ]; then
      echo "--- judge stderr (attempt ${attempt}) ---"
      cat "${JUDGE_STDERR_FILE}"
      echo "---"
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
  echo "::warning::Review-blocked judge LLM execution failed after 2 attempts — needs human intervention."
  if [ -s "${JUDGE_STDERR_FILE}" ]; then
    echo "::group::Last judge stderr"
    cat "${JUDGE_STDERR_FILE}"
    echo "::endgroup::"
  fi
  echo "judge_skip_reason=llm_failed" >> "$GITHUB_OUTPUT"
  exit 0
fi

# -----------------------------------------------------------
# Parse judge output
# -----------------------------------------------------------
# Pre-initialize to guarantee JUDGE_JSON is bound under `set -u`. The
# complex multi-line command substitution below has been observed to
# leave JUDGE_JSON unbound in rare cases (e.g. when the python3 subshell
# is killed mid-run by an external signal, which prevents the `|| echo ""`
# fallback from completing). Without this default, the subsequent
# `[ -z "${JUDGE_JSON}" ]` check fires `JUDGE_JSON: unbound variable`
# under `set -u` and aborts the whole review_autofix job.
JUDGE_JSON=""
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

if [ -z "${JUDGE_JSON:-}" ]; then
  echo "::warning::Could not parse review-blocked judge output — needs human intervention."
  echo "::group::Raw judge output (first 200 lines)"
  head -200 "${RB_JUDGE_OUTPUT}" 2>/dev/null || true
  echo "::endgroup::"
  echo "judge_skip_reason=json_parse_failed" >> "$GITHUB_OUTPUT"
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

gh_retry gh api "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" \
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
      _resilient_phase_swap "${issue_number}" "ai:ready-to-merge" || true
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
      # Use _safe_gh_jq (via gh_retry) so a failed `gh api` response
      # emits no stdout — preventing the error JSON body from being
      # concatenated with the `|| echo '{}'` fallback, which would
      # yield invalid JSON and break the downstream `jq` parses below.
      _pr_json="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo '{}')"
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
        # NOTE: gh pr merge is intentionally NOT wrapped with gh_retry.
        # These calls are best-effort (trailing `|| true`); non-
        # transient failures (branch protection, permissions, merge
        # queue, 422 merge commit conflicts, etc.) would otherwise
        # incur ~31s of exponential backoff under gh_retry before
        # reaching the `|| true` fallthrough. Rate-limit alerts still
        # fire through every other gh_retry-wrapped call in this
        # script.
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
        _resilient_phase_swap "${issue_number}" "ai:ready-to-merge" || true
      done <<< "${ISSUE_NUMBERS}"

      PR_STATE="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.state' 2>/dev/null | grep -xE 'open|closed|merged' || echo "")"
      if [ "${PR_STATE}" = "open" ] && [ "${ENABLE_AUTO_MERGE}" = "true" ]; then
        # Best-effort merge — see note above re: gh_retry.
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

      if codex --ask-for-approval never -c model_verbosity=high -c include_apply_patch_tool=true exec --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${RB_FIX_PROMPT}" > "${RB_FIX_OUTPUT}" 2>/dev/null; then
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

        # Clean up workflow-fetched artifacts before committing.
        #
        # Gate on the git origin URL rather than ${REPOSITORY}: the env
        # var (and GITHUB_REPOSITORY) is user-controllable and any test
        # harness that sets e.g. REPOSITORY=owner/repo while running this
        # script as a subprocess from the real coding-workflows checkout
        # would trip this block and rm the tracked source files under
        # that checkout (see PRs #917/#931 for the incident in the sibling
        # orchestrate_poll_process.sh cleanup block). The remote URL
        # reflects the actual checkout on disk, not a user-overridable
        # env var. Unknown/empty URL is fail-closed: skip cleanup.
        _rb_origin_url="$(git config --get remote.origin.url 2>/dev/null || true)"
        case "${_rb_origin_url}" in
          ""|*/coding-workflows|*/coding-workflows.git|*/coding-workflows/|*/coding-workflows.git/)
            : # self-repo or unknown — keep files; consumer-repo-only cleanup
            ;;
          *)
            rm -f ./pre_assembled_static.txt
            rm -f unattended_system_instructions.md ai_pipeline.md agents.md probably_unnecessary_but_read_if_stuck.md
            rm -f scripts/git_ref_health_check.sh scripts/generate_symbol_diff_summary.py scripts/label_helpers.sh scripts/codex_model_catalog.json
            rm -f scripts/memory_helpers.sh scripts/ai_memory.py scripts/ai_memory_lib.py scripts/openrouter_prompt_cache.py
            rm -f scripts/review_run_reviewers.sh scripts/review_apply_fixes.sh
            rm -rf ai-memory
            ;;
        esac
        unset _rb_origin_url

        if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then
          git add -u -- ':!node_modules' ':!scripts/memory_helpers.sh' ':!scripts/ai_memory.py' ':!scripts/ai_memory_lib.py' ':!scripts/openrouter_prompt_cache.py' ':!scripts/review_run_reviewers.sh' ':!scripts/review_apply_fixes.sh' ':!scripts/review_rb_judge.sh' ':!ai-memory' ':!.github/prompts' ':!.github/scripts'
        else
          git add -u -- ':!node_modules' ':!scripts' ':!prompts' ':!ai-memory' ':!.github/prompts' ':!.github/scripts'
        fi
        echo "Staged files before commit:"
        STAGED_FILES="$(git diff --cached --name-only || true)"
        printf '%s\n' "${STAGED_FILES}" | sed '/^$/d; s/^/ - /' || true
        if printf '%s\n' "${STAGED_FILES}" | grep -Eq '^\.github/(prompts|scripts)/'; then
          echo "Error: .github/prompts or .github/scripts is staged"
          exit 1
        fi
        if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ] && printf '%s\n' "${STAGED_FILES}" | grep -Eq '^(scripts/|prompts/|\.github/scripts/|\.github/prompts/|ai-memory/)'; then
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
            gh_retry gh issue edit "${issue_number}" --repo "${REPOSITORY}" \
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
          gh_retry gh issue edit "${issue_number}" --repo "${REPOSITORY}" \
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
    gh_retry gh pr close "${PR_NUMBER}" --repo "${REPOSITORY}" \
      --comment "Closed by review-blocked judge — the approach needs rework. A new issue will be created with refined guidance." \
      2>/dev/null || true

    # Label linked issues as closed
    ensure_label_exists "ai:closed" "${REPOSITORY}"
    while IFS= read -r issue_number; do
      [ -n "${issue_number}" ] || continue
      _resilient_phase_swap "${issue_number}" "ai:closed" || true
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

      # Propagate ai:orchestrator-managed from the parent issue when it
      # carries that label.  Without this, an orchestrator-managed
      # parent's reissue lands with only ai:clarification (added later
      # by clarify.yml on issues.opened) and the orchestrator-managed
      # auto-answer fast path in clarify.yml never fires — the reissue
      # stalls in clarification while the orchestrator's parallel
      # judge-addition issue silently delivers the same work.  Standalone
      # (non-orchestrator) reissues do NOT inherit this label so their
      # human-driven clarify semantics are preserved.
      RB_PROPAGATE_LABELS=()
      if printf '%s' "${FIRST_ISSUE_LABELS_JSON}" | jq -e 'index("ai:orchestrator-managed")' >/dev/null 2>&1; then
        ensure_label_exists "ai:orchestrator-managed" "${REPOSITORY}"
        RB_PROPAGATE_LABELS+=("--label" "ai:orchestrator-managed")
        echo "Propagating ai:orchestrator-managed from parent issue #${FIRST_ISSUE} to review-blocked reissue."
      fi

      NEW_URL="$(gh_retry gh issue create \
        --repo "${REPOSITORY}" \
        --title "${NEW_ISSUE_TITLE}" \
        --body "${FULL_NEW_BODY}" \
        ${RB_PROPAGATE_LABELS[@]+"${RB_PROPAGATE_LABELS[@]}"})"
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
