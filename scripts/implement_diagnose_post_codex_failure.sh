#!/usr/bin/env bash
# implement_diagnose_post_codex_failure.sh — diagnose post-Codex
# validation failures in implement.yml and file fix-up issues.
#
# Extracted from the "Diagnose post-Codex failure and create fix-up issues"
# step of implement.yml because a single `run:` block exceeded GitHub Actions'
# 21,000-char template-expression limit (the limit applies per-`run:` block
# because each block is compiled as an expression template). Keeping the logic
# in a file lets it grow without re-approaching the limit.
#
# Inputs (environment):
#   GH_TOKEN                          GitHub token used by `gh` CLI.
#   OPENROUTER_API_KEY                OpenRouter API key consumed by codex.
#   GITHUB_REPOSITORY                 owner/repo slug (auto-set by Actions).
#   GITHUB_RUN_ID                     Current run id (auto-set by Actions).
#   GITHUB_SERVER_URL                 Server URL (auto-set by Actions).
#   JOB_STATUS                        Passed in from ${{ job.status }}.
#   DEFAULT_BRANCH                    Passed in from ${{ github.event.repository.default_branch }}.
#   ISSUE_NUMBER                      Source issue number (job-level env).
#   RUNTIME_DIR                       Per-run scratch directory.
#   MODEL_EDITOR                      codex model used for diagnosis.
#   ISSUE_META_FILE                   Optional cached issue metadata JSON.
#   ISSUE_BODY_FILE                   Optional cached issue body text.
#   PR_BASE_BRANCH                    Target base branch (falls back to DEFAULT_BRANCH).
#   IMPLEMENT_DIAGNOSE_PROMPT_FILE    Path for assembled diagnose prompt.
#   IMPLEMENT_DIAGNOSE_OUTPUT_FILE    Path for codex raw output.
#   IMPLEMENT_DIAGNOSE_LOG_FILE       Path for codex stderr log.
#   IMPLEMENT_DIAGNOSE_RESULT_FILE    Path for extracted JSON result.
#
# Outputs:
#   $GITHUB_OUTPUT:  handled=true|false.
#
# Failure modes:
#   - No-ops (exit 0) when RUNTIME_DIR unset, diagnostics capture missing,
#     or the source issue already carries ai:implementation-failed.
#   - Falls through to a deterministic fallback fix-up issue when codex
#     diagnose fails or returns unparseable JSON.

set -euo pipefail
source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry &>/dev/null || gh_retry() { "$@"; }
type _safe_gh_jq &>/dev/null || _safe_gh_jq() {
  local _tmpf
  _tmpf="$(mktemp "${TMPDIR:-/tmp}/_safe_gh_jq.XXXXXX" 2>/dev/null)" || return 1
  if gh api "$@" > "${_tmpf}" 2>/dev/null; then
    cat "${_tmpf}"
    rm -f "${_tmpf}"
    return 0
  fi
  rm -f "${_tmpf}"
  return 1
}

IMPLEMENT_DIAGNOSE_TIMEOUT_SEC=300
TOOL_CALL_BUDGET_IMPLEMENT_DIAGNOSE=20

echo "handled=false" >> "$GITHUB_OUTPUT"

# Reuse the cached issue snapshot when available — see the
# "Fetch issue metadata" step.  Falls back to a fresh API
# call when the file is missing OR jq fails to parse it
# (partial write, truncated download, etc.); the API path
# itself falls back to '[]' so the failure path still
# degrades safely.
ISSUE_LABELS_JSON=""
if [ -s "${ISSUE_META_FILE:-}" ]; then
  ISSUE_LABELS_JSON="$(jq -c '[.labels[].name]' "${ISSUE_META_FILE}" 2>/dev/null || true)"
fi
if [ -z "${ISSUE_LABELS_JSON}" ]; then
  ISSUE_LABELS_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}" --jq '[.labels[].name]' || echo '[]')"
fi
if printf '%s' "${ISSUE_LABELS_JSON}" | jq -e 'index("ai:implementation-failed") != null' >/dev/null 2>&1; then
  echo "Issue #${ISSUE_NUMBER} already has ai:implementation-failed label; skipping post-Codex diagnosis."
  echo "handled=true" >> "$GITHUB_OUTPUT"
  exit 0
fi

if [ -z "${RUNTIME_DIR:-}" ]; then
  echo "RUNTIME_DIR is unavailable; leaving generic failure handling unchanged."
  exit 0
fi

CAPTURE_FILE="${RUNTIME_DIR}/post_codex_validation_errors.txt"
if [ ! -s "${CAPTURE_FILE}" ]; then
  echo "No post-Codex validation diagnostics found at ${CAPTURE_FILE}; leaving generic failure handling unchanged."
  exit 0
fi

echo "handled=true" >> "$GITHUB_OUTPUT"

ensure_implementation_failed_label() {
  if [ -f scripts/label_helpers.sh ]; then
    source scripts/label_helpers.sh
    ensure_label_exists "ai:implementation-failed" "${GITHUB_REPOSITORY}" || true
  else
    gh_retry gh label create "ai:implementation-failed" --repo "${GITHUB_REPOSITORY}" \
      --color "e11d48" --description "Implementation produced no changes or failed post-Codex validation" \
      2>/dev/null || true
  fi

  gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
    --add-label 'ai:implementation-failed' \
    --remove-label 'ai:implementing' \
    --remove-label 'ai:awaiting-approval' >/dev/null 2>&1 || \
  gh_retry gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" \
    --add-label 'ai:implementation-failed' >/dev/null 2>&1 || true
}

ensure_implement_fixup_labels() {
  if [ -f scripts/label_helpers.sh ]; then
    source scripts/label_helpers.sh
    ensure_label_exists "ai:clarification" "${GITHUB_REPOSITORY}" || true
    ensure_label_exists "ai:implement-fix-up" "${GITHUB_REPOSITORY}" || true
  else
    gh_retry gh label create "ai:clarification" --repo "${GITHUB_REPOSITORY}" \
      --color "f9d0c4" --description "AI clarification required before planning" \
      2>/dev/null || true
    gh_retry gh label create "ai:implement-fix-up" --repo "${GITHUB_REPOSITORY}" \
      --color "d4c5f9" --description "Implement-phase post-Codex fix-up issue" \
      2>/dev/null || true
  fi
}

FAILED_STEP_JOBS_JSON="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/jobs?per_page=100" || true)"
FAILED_STEP_NAME="$(printf '%s' "${FAILED_STEP_JOBS_JSON}" | jq -r '
  [.jobs[].steps[]
    | select(
        .conclusion == "failure"
        or .conclusion == "cancelled"
        or .conclusion == "timed_out"
        or .conclusion == "action_required"
        or .status == "in_progress"
      )
  ]
  | last
  | .name // ""' 2>/dev/null || true)"
if [ -z "${FAILED_STEP_NAME}" ]; then
  FAILED_STEP_NAME="unknown-step"
fi

if [ -z "${PR_BASE_BRANCH:-}" ]; then
  PR_BASE_BRANCH="${DEFAULT_BRANCH}"
fi

if [ -z "${ISSUE_BODY_FILE:-}" ] || [ ! -f "${ISSUE_BODY_FILE:-}" ]; then
  ISSUE_BODY_FILE="${RUNTIME_DIR}/issue_body_from_api.txt"
  # Prefer the cached issue snapshot from "Fetch issue
  # metadata" before re-hitting the API.  jq failure (e.g.
  # truncated/partial file) falls through to the API path
  # rather than killing the step under set -euo pipefail.
  : > "${ISSUE_BODY_FILE}"
  if [ -s "${ISSUE_META_FILE:-}" ]; then
    jq -er '.body // ""' "${ISSUE_META_FILE}" > "${ISSUE_BODY_FILE}" 2>/dev/null || : > "${ISSUE_BODY_FILE}"
  fi
  if [ ! -s "${ISSUE_BODY_FILE}" ]; then
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}" --jq '.body // ""' > "${ISSUE_BODY_FILE}" || printf '' > "${ISSUE_BODY_FILE}"
  fi
fi

TRACKING_ISSUE_NUM="$(sed -nE 's/.*Tracking issue:[[:space:]]*#([0-9]+).*/\1/p' "${ISSUE_BODY_FILE}" | head -n1 | tr -d '\r')"
if ! [[ "${TRACKING_ISSUE_NUM}" =~ ^[0-9]+$ ]]; then
  TRACKING_ISSUE_NUM="${ISSUE_NUMBER}"
fi

DIFF_FILE="${RUNTIME_DIR}/implement_diagnose_git_diff.txt"
UNTRACKED_LIST_FILE="${RUNTIME_DIR}/implement_diagnose_untracked_files.txt"
UNTRACKED_CONTEXT_FILE="${RUNTIME_DIR}/implement_diagnose_untracked_context.txt"
git diff HEAD > "${DIFF_FILE}" || true
git ls-files --others --exclude-standard > "${UNTRACKED_LIST_FILE}" || true

{
  if [ ! -s "${UNTRACKED_LIST_FILE}" ]; then
    echo "(none)"
  else
    while IFS= read -r untracked_path; do
      [ -n "${untracked_path}" ] || continue
      lower_untracked_path="$(printf '%s' "${untracked_path}" | tr '[:upper:]' '[:lower:]')"
      case "${lower_untracked_path}" in
        */.env|*/.env.*|.env|.env.*|*secret*|*token*|*password*|*credential*|*.pem|*.key|*.p12|*.pfx)
          echo "--- ${untracked_path} ---"
          echo "[sensitive file omitted]"
          echo
          continue
          ;;
      esac
      echo "--- ${untracked_path} ---"
      if [ -f "${untracked_path}" ]; then
        if grep -Iq . "${untracked_path}" 2>/dev/null; then
          head -c 16000 "${untracked_path}" || true
          if [ "$(wc -c < "${untracked_path}" 2>/dev/null || echo 0)" -gt 16000 ]; then
            echo
            echo "[truncated to 16000 bytes]"
          fi
        else
          echo "[binary file omitted]"
        fi
      else
        echo "[file no longer exists]"
      fi
      echo
    done < "${UNTRACKED_LIST_FILE}"
  fi
} > "${UNTRACKED_CONTEXT_FILE}"

ensure_diagnose_asset() {
  local local_path="$1"
  local remote_path="$2"
  [ -s "${local_path}" ] && return 0

  if [ ! -d .codex-workflow-src ]; then
    return 1
  fi

  src=".codex-workflow-src/${remote_path}"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/${remote_path}" ]; then
    src=".codex-workflow-src-main/${remote_path}"
  fi
  if [ ! -f "${src}" ]; then
    return 1
  fi

  mkdir -p "$(dirname "${local_path}")"
  install -m 0644 "${src}" "${local_path}"

  case "${local_path}" in
    *.sh|*.py)
      chmod +x "${local_path}" || true
      ;;
  esac
  return 0
}

DIAGNOSE_MODE_PROMPT_TEMPLATE="prompts/mode-implement-diagnose.txt"
if ! ensure_diagnose_asset "${DIAGNOSE_MODE_PROMPT_TEMPLATE}" "prompts/mode-implement-diagnose.txt"; then
  DIAGNOSE_MODE_PROMPT_TEMPLATE="${RUNTIME_DIR}/mode-implement-diagnose.fallback.txt"
  cat > "${DIAGNOSE_MODE_PROMPT_TEMPLATE}" <<'EOF'
Diagnose this implementation failure and return a single JSON object with keys:
status (needs_fixes|harness_error|infeasible), diagnosis, fix_issues, harness_fixes.
EOF
fi

DIAGNOSE_MODE_PROMPT="${DIAGNOSE_MODE_PROMPT_TEMPLATE}"
if ensure_diagnose_asset "scripts/render_prompt.sh" "scripts/render_prompt.sh" && \
  ensure_diagnose_asset "prompts/serena-efficiency-block.txt" "prompts/serena-efficiency-block.txt"; then
  DIAGNOSE_RENDERED_PROMPT="${RUNTIME_DIR}/mode-implement-diagnose.rendered.txt"
  if bash scripts/render_prompt.sh "${DIAGNOSE_MODE_PROMPT_TEMPLATE}" > "${DIAGNOSE_RENDERED_PROMPT}"; then
    DIAGNOSE_MODE_PROMPT="${DIAGNOSE_RENDERED_PROMPT}"
  else
    echo "::warning::Failed to render ${DIAGNOSE_MODE_PROMPT_TEMPLATE}; using raw prompt."
  fi
else
  echo "::warning::Diagnose prompt render assets unavailable; using raw prompt."
fi

DIAGNOSE_STATIC_PREFIX_FILE="${RUNTIME_DIR}/implement_diagnose_static_prefix.txt"
if [ -f ./pre_assembled_static.txt ]; then
  cp ./pre_assembled_static.txt "${DIAGNOSE_STATIC_PREFIX_FILE}" || true
fi
if [ ! -s "${DIAGNOSE_STATIC_PREFIX_FILE}" ]; then
  cat > "${DIAGNOSE_STATIC_PREFIX_FILE}" <<'EOF'
You are diagnosing an AI implementation workflow failure.
Return exactly one JSON object and no surrounding prose.
The JSON object must use these keys:
- status (needs_fixes|harness_error|infeasible)
- diagnosis
- fix_issues
- harness_fixes
Keep the response focused on actionable diagnosis grounded in the supplied evidence.
EOF
fi

{
  if [ -s "${DIAGNOSE_STATIC_PREFIX_FILE}" ]; then
    cat "${DIAGNOSE_STATIC_PREFIX_FILE}"
    echo
  fi
  echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_IMPLEMENT_DIAGNOSE}"
  echo
  echo "=== IMPLEMENT FAILURE DIAGNOSIS TASK ==="
  cat "${DIAGNOSE_MODE_PROMPT}"
  echo
  echo "=== SOURCE ISSUE BODY ==="
  cat "${ISSUE_BODY_FILE}"
  echo
  echo "=== FAILED STEP NAME ==="
  echo "${FAILED_STEP_NAME}"
  echo
  echo "=== WORKING TREE CHANGES (git diff HEAD) ==="
  if [ -s "${DIFF_FILE}" ]; then
    cat "${DIFF_FILE}"
  else
    echo "(no content)"
  fi
  echo
  echo "=== UNTRACKED FILE CONTEXT ==="
  if [ -s "${UNTRACKED_CONTEXT_FILE}" ]; then
    cat "${UNTRACKED_CONTEXT_FILE}"
  else
    echo "(no content)"
  fi
  echo
  echo "=== CAPTURED POST-CODEX VALIDATION ERRORS (FULL) ==="
  cat "${CAPTURE_FILE}"
} > "${IMPLEMENT_DIAGNOSE_PROMPT_FILE}"

extract_last_json_with_key() {
  local source_file="$1"
  local required_key="$2"
  local output_file="$3"

  python3 - "${source_file}" "${required_key}" "${output_file}" <<'PY'
import json
import sys

if len(sys.argv) < 4:
    print("Usage: extract_last_json_with_key <source_file> <required_key> <output_file>", file=sys.stderr)
    sys.exit(1)

source_file = sys.argv[1]
required_key = sys.argv[2]
output_file = sys.argv[3]

with open(source_file, "r", encoding="utf-8", errors="replace") as handle:
    raw = handle.read()

candidates = []
trimmed = raw.strip()
if trimmed:
    try:
        parsed = json.loads(trimmed)
        if isinstance(parsed, dict) and required_key in parsed:
            candidates.append(parsed)
    except json.JSONDecodeError:
        pass

lines = raw.splitlines()
if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip().startswith("```"):
    cleaned = "\n".join(lines[1:-1])
else:
    cleaned = raw

decoder = json.JSONDecoder()
pos = 0
while pos < len(cleaned):
    start = cleaned.find("{", pos)
    if start == -1:
        break
    try:
        parsed, end = decoder.raw_decode(cleaned[start:])
        if isinstance(parsed, dict) and required_key in parsed:
            candidates.append(parsed)
        pos = start + end
    except json.JSONDecodeError:
        pos = start + 1

if not candidates:
    print(f"No JSON object with key '{required_key}' found", file=sys.stderr)
    sys.exit(1)

with open(output_file, "w", encoding="utf-8") as handle:
    json.dump(candidates[-1], handle, ensure_ascii=True, indent=2)
    handle.write("\n")
PY
}

DIAGNOSE_SUCCESS=false
if timeout "${IMPLEMENT_DIAGNOSE_TIMEOUT_SEC}"s codex exec --model "${MODEL_EDITOR}" --full-auto \
  < "${IMPLEMENT_DIAGNOSE_PROMPT_FILE}" > "${IMPLEMENT_DIAGNOSE_OUTPUT_FILE}" \
  2> >(tee -a "${IMPLEMENT_DIAGNOSE_LOG_FILE}" >&2); then
  if extract_last_json_with_key "${IMPLEMENT_DIAGNOSE_OUTPUT_FILE}" "status" "${IMPLEMENT_DIAGNOSE_RESULT_FILE}"; then
    DIAGNOSE_SUCCESS=true
  fi
fi

if [ "${DIAGNOSE_SUCCESS}" != "true" ]; then
  RAW_CAPTURE_SNIPPET="$(head -c 50000 "${CAPTURE_FILE}" || true)"
  if [ "$(wc -c < "${CAPTURE_FILE}" 2>/dev/null || echo 0)" -gt 50000 ]; then
    RAW_CAPTURE_SNIPPET+=$'\n\n[truncated to first 50000 bytes]'
  fi

  FALLBACK_BODY="$(printf 'Investigate post-Codex validation failure for issue #%s.\n\nThe diagnose step could not produce a valid JSON contract, so this deterministic fallback issue was generated with raw captured diagnostics.\n\n### Captured diagnostics\n\n```text\n%s\n```' "${ISSUE_NUMBER}" "${RAW_CAPTURE_SNIPPET}")"

  jq -n \
    --arg diagnosis "Codex diagnose failed or returned invalid JSON. Fallback fix-up issue created with raw captured diagnostics." \
    --arg body "${FALLBACK_BODY}" \
    '{
      status: "needs_fixes",
      diagnosis: $diagnosis,
      fix_issues: [
        {
          id: "implement-post-codex-fallback",
          title: "Implement phase post-Codex validation failure fallback",
          body: $body,
          priority: 1,
          depends_on: []
        }
      ],
      harness_fixes: ""
    }' > "${IMPLEMENT_DIAGNOSE_RESULT_FILE}"
fi

DIAG_STATUS="$(jq -r '.status // "harness_error"' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")"
DIAG_TEXT="$(jq -r '.diagnosis // "Implementation failed after Codex execution."' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")"
RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

case "${DIAG_STATUS}" in
  needs_fixes)
    FIX_COUNT="$(jq -r '(.fix_issues // []) | if type == "array" then length else 0 end' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")"
    if [ "${FIX_COUNT}" -le 0 ]; then
      FIX_COUNT=1
      jq -n \
        --arg diagnosis "Diagnosis returned needs_fixes with empty fix_issues. Generated fallback issue payload." \
        --arg body "No fix_issues were returned by diagnose output. Investigate implement workflow post-Codex failure handling and captured diagnostics in ${CAPTURE_FILE}." \
        '{
          status: "needs_fixes",
          diagnosis: $diagnosis,
          fix_issues: [
            {
              id: "implement-post-codex-empty-fix-issues",
              title: "Implement phase diagnose returned empty fix_issues",
              body: $body,
              priority: 1,
              depends_on: []
            }
          ],
          harness_fixes: ""
        }' > "${IMPLEMENT_DIAGNOSE_RESULT_FILE}"
    fi

    MAX_FIXUP_ISSUES=10
    if [ "${FIX_COUNT}" -gt "${MAX_FIXUP_ISSUES}" ]; then
      echo "::warning::Diagnose returned ${FIX_COUNT} fix_issues; capping to ${MAX_FIXUP_ISSUES}."
      FIX_COUNT="${MAX_FIXUP_ISSUES}"
    fi

    CREATED_FIX_ISSUES_JSON='[]'
    creation_failed=0
    local_to_issue_map='{}'
    ensure_implement_fixup_labels
    for idx in $(seq 0 $((FIX_COUNT - 1))); do
      FIX_ID="$(jq -r --argjson idx "${idx}" '.fix_issues[$idx].id // ("implement-fix-" + (($idx + 1) | tostring))' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")"
      FIX_TITLE="$(jq -r --argjson idx "${idx}" '.fix_issues[$idx].title // ("Implement post-Codex fix-up " + (($idx + 1) | tostring))' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")"
      FIX_BODY_BASE="$(jq -r --argjson idx "${idx}" '.fix_issues[$idx].body // "No body provided"' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}" | sed 's/\\n/\n/g')"
      FIX_PRIORITY="$(jq -r --argjson idx "${idx}" '.fix_issues[$idx].priority // 5' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")"

      FIX_BODY_FULL="${FIX_BODY_BASE}

      ---
      **Orchestrator metadata** (do not edit)
      - Tracking issue: #${TRACKING_ISSUE_NUM}
      - Integration branch: ${PR_BASE_BRANCH}
      - Local ID: \`${FIX_ID}\`
      - Type: implement-fix-up (post-codex-validation)
      - Source issue: #${ISSUE_NUMBER}
      - Failed step: ${FAILED_STEP_NAME}
      - Priority: ${FIX_PRIORITY}
      - Managed by: AI Orchestrator"

      CREATE_ERR_FILE="$(mktemp)"
      if FIX_URL="$(gh_retry gh issue create \
        --repo "${GITHUB_REPOSITORY}" \
        --title "${FIX_TITLE}" \
        --body "${FIX_BODY_FULL}" \
        --label "ai:clarification" \
        --label "ai:implement-fix-up" 2>"${CREATE_ERR_FILE}")"; then
        FIX_NUM="$(printf '%s' "${FIX_URL}" | sed -nE 's#.*\/([0-9]+)([/?#].*)?$#\1#p')"
        if [ -n "${FIX_NUM}" ]; then
          CREATED_FIX_ISSUES_JSON="$(echo "${CREATED_FIX_ISSUES_JSON}" | jq --argjson num "${FIX_NUM}" '. + [$num]')"
          local_to_issue_map="$(echo "${local_to_issue_map}" | jq --arg id "${FIX_ID}" --argjson num "${FIX_NUM}" '. + {($id): $num}')"
        else
          echo "::warning::Could not parse issue number from gh issue create output for local id ${FIX_ID}: ${FIX_URL}" >&2
          creation_failed=1
        fi
      else
        CREATE_ERR_MSG="$(cat "${CREATE_ERR_FILE}" 2>/dev/null || true)"
        echo "::warning::Failed to create fix-up issue for local id ${FIX_ID}. ${CREATE_ERR_MSG}"
        creation_failed=1
      fi
      rm -f "${CREATE_ERR_FILE}"
    done

    if [ "${creation_failed}" -ne 0 ]; then
      echo "::error::One or more fix-up issues failed to create; skipping dependency comments and summary comment."
      ensure_implementation_failed_label
      COMMENT_BODY="$(printf '## Post-Codex validation follow-up creation failed\n\nOne or more fix-up issues could not be created.\n\nFailed step: %s\n\nRun: %s' "${FAILED_STEP_NAME}" "${RUN_URL}")"
      gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}/comments" \
        -f body="${COMMENT_BODY}" >/dev/null || true
      exit 1
    fi


    for idx in $(seq 0 $((FIX_COUNT - 1))); do
      FIX_ID="$(jq -r --argjson idx "${idx}" '.fix_issues[$idx].id // ("implement-fix-" + (($idx + 1) | tostring))' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")"
      FIX_NUM="$(echo "${local_to_issue_map}" | jq -r --arg id "${FIX_ID}" '.[$id] // empty')"
      [ -n "${FIX_NUM}" ] || continue

      DEP_SUMMARY=""
      while IFS= read -r dep_id; do
        [ -n "${dep_id}" ] || continue
        DEP_NUM="$(echo "${local_to_issue_map}" | jq -r --arg dep_id "${dep_id}" '.[$dep_id] // empty')"
        if [ -n "${DEP_NUM}" ]; then
          DEP_SUMMARY+="- #${DEP_NUM} (from ${dep_id})"$'\n'
        fi
      done < <(jq -r --argjson idx "${idx}" '.fix_issues[$idx].depends_on[]?' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")

      if [ -n "${DEP_SUMMARY}" ]; then
        DEP_BODY="$(printf '## Dependency Notes\n\nThis fix-up should be applied after:\n%s' "${DEP_SUMMARY}")"
        gh_retry gh issue comment "${FIX_NUM}" \
          --repo "${GITHUB_REPOSITORY}" \
          --body "${DEP_BODY}" >/dev/null || \
          echo "::warning::Failed to post dependency notes to issue #${FIX_NUM}."
      fi
    done

    issue_list_md="$(echo "${CREATED_FIX_ISSUES_JSON}" | jq -r '.[] | "- #\(.)"')"
    if [ -z "${issue_list_md}" ]; then
      issue_list_md="- (no issue numbers captured; issue creation failed)"
    fi

    BLOCKER_METADATA_JSON="$(jq -cn \
      --argjson fixups "${CREATED_FIX_ISSUES_JSON}" \
      --arg source_issue "${ISSUE_NUMBER}" \
      '{
        fixup_issue_numbers: ($fixups | if type == "array" then map(select(type == "number")) | unique else [] end),
        blocks_source_issue: ($source_issue | tonumber?)
      }')"
    BLOCKER_METADATA_BLOCK="$(printf '<!-- IMPLEMENT_FIXUP_BLOCKERS_V1\n%s\nIMPLEMENT_FIXUP_BLOCKERS_V1 -->' "${BLOCKER_METADATA_JSON}")"

    SUMMARY_COMMENT_BODY="$(printf '## Post-Codex validation diagnosed follow-up fixes\n\n%s\n\nFailed step: %s\n\nCreated fix-up issues:\n%s\n\nRun: %s\n\n%s\n' \
      "${DIAG_TEXT}" \
      "${FAILED_STEP_NAME}" \
      "${issue_list_md}" \
      "${RUN_URL}" \
      "${BLOCKER_METADATA_BLOCK}")"

    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}/comments" \
      -f body="${SUMMARY_COMMENT_BODY}" >/dev/null || true

    ensure_implementation_failed_label
    ;;

  harness_error)
    HARNESS_FIXES="$(jq -r '.harness_fixes // "No harness fixes were provided."' "${IMPLEMENT_DIAGNOSE_RESULT_FILE}")"
    COMMENT_BODY="$(printf '## Post-Codex validation harness error\n\n%s\n\nFailed step: %s\n\nHarness fix guidance:\n\n%s\n\nRun: %s' "${DIAG_TEXT}" "${FAILED_STEP_NAME}" "${HARNESS_FIXES}" "${RUN_URL}")"
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}/comments" \
      -f body="${COMMENT_BODY}" >/dev/null || true

    ensure_implementation_failed_label
    ;;

  infeasible)
    COMMENT_BODY="$(printf '## Post-Codex validation marked infeasible\n\n%s\n\nFailed step: %s\n\nRun: %s' "${DIAG_TEXT}" "${FAILED_STEP_NAME}" "${RUN_URL}")"
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}/comments" \
      -f body="${COMMENT_BODY}" >/dev/null || true

    ensure_implementation_failed_label
    ;;

  *)
    COMMENT_BODY="$(printf '## Post-Codex validation diagnosis returned unknown status\n\nStatus: %s\n\n%s\n\nFailed step: %s\n\nRun: %s' "${DIAG_STATUS}" "${DIAG_TEXT}" "${FAILED_STEP_NAME}" "${RUN_URL}")"
    gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}/comments" \
      -f body="${COMMENT_BODY}" >/dev/null || true

    ensure_implementation_failed_label
    ;;
esac
