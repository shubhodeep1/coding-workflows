#!/usr/bin/env bash
# Body of the "Post editor summary comment" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
# GitHub does not substitute workflow expressions in a sourced script, so the
# step passes github.server_url, github.repository and github.run_id in as
# AUTOFIX_STEP_SERVER_URL, AUTOFIX_STEP_REPOSITORY and AUTOFIX_STEP_RUN_ID;
# they default to the runner's GITHUB_* values when unset.
set -euo pipefail
AUTOFIX_STEP_SERVER_URL="${AUTOFIX_STEP_SERVER_URL:-${GITHUB_SERVER_URL:-https://github.com}}"
AUTOFIX_STEP_REPOSITORY="${AUTOFIX_STEP_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
AUTOFIX_STEP_RUN_ID="${AUTOFIX_STEP_RUN_ID:-${GITHUB_RUN_ID:-}}"
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

# Empty-editor-output short-circuit.
#
# If the editor produced no structured summary AND no file
# changes were committed, this run has nothing meaningful to
# report — the synthetic fallback summary (below) would be a
# false positive that masks a codex crash/timeout/empty-prompt.
# Post a distinct, retry-friendly comment, signal the
# failure handlers to NOT apply ai:review-blocked for this
# specific cause, and fail the step so the stall poller can
# retry the review later instead of poisoning the linked
# issue with an unrecoverable label.
if [ ! -s "${EDITOR_SUMMARY_FILE}" ] && [ ! -s "${COMMITTED_FILES_FILE:-/dev/null}" ]; then
  echo "::warning::Editor produced no summary and no committed changes — treating as retryable no-op."
  RUN_URL="${AUTOFIX_STEP_SERVER_URL}/${AUTOFIX_STEP_REPOSITORY}/actions/runs/${AUTOFIX_STEP_RUN_ID}"
  # A failed reviewer step skips the editor, so its empty output is
  # only the symptom (PR #4323: every reviewer slot and the
  # summariser exited 226 and the run was reported as
  # editor_empty_noop). Name the earliest failing phase and keep the
  # slot / summariser exit codes as evidence; the retry handling
  # below is unchanged.
  autofix_empty_failure_reason="editor_empty_noop"
  noop_explanation="The editor stage completed without a structured summary and without committing any file changes. This typically indicates a transient agent crash, timeout, or empty prompt rather than a reviewable defect in the PR."
  if [ "${REVIEWERS_STEP_OUTCOME:-}" = "failure" ]; then
    autofix_empty_failure_reason="reviewers_failed"
    noop_explanation="The reviewer stage failed, so the editor never ran and no file changes were committed. The reviewer and summariser exit codes are in the workflow run log."
    echo "AUTOFIX_REVIEWERS_FAILED=true" >> "$GITHUB_ENV"
    if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ] && [ -n "${RUNTIME_DIR:-}" ]; then
      reviewers_failure_log_args=()
      # reviewers_stage_stderr.txt (issue #9533) is the reviewer step's own
      # stderr, teed by the workflow, so a shell crash before any slot log
      # exists (e.g. `render_prompt.sh: line 306: ...`) is evidence too. It
      # goes in as --stage-stderr-file, the only source of a plain ERROR line
      # (slot logs hold raw model-CLI stderr and review prose).
      for reviewers_failure_log in "${PREVIOUS_REVIEWS_DIR:-/nonexistent}"/*.log "${RUNTIME_DIR}"/summariser_*.log; do
        [ -f "${reviewers_failure_log}" ] && reviewers_failure_log_args+=(--log-file "${reviewers_failure_log}")
      done
      [ -f "${RUNTIME_DIR}/reviewers_stage_stderr.txt" ] && reviewers_failure_log_args+=(--stage-stderr-file "${RUNTIME_DIR}/reviewers_stage_stderr.txt")
      PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" reviewer-failure-evidence "${reviewers_failure_log_args[@]}" \
        > "${RUNTIME_DIR}/reviewers_failure_evidence.txt" 2>/dev/null || : > "${RUNTIME_DIR}/reviewers_failure_evidence.txt"
      echo "AUTOFIX_REVIEWERS_FAILED reason=reviewers_failed $(grep -m1 '^dominant_rc=' "${RUNTIME_DIR}/reviewers_failure_evidence.txt" 2>/dev/null || echo dominant_rc=unknown)"
      reviewers_failed_headline="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" failure-headline \
        --evidence-file "${RUNTIME_DIR}/reviewers_failure_evidence.txt" 2>/dev/null | sed -n 's/^first_error=//p' | head -n 1 || true)"
    fi
  elif [ "${SANDBOX_PREPARE_STEP_OUTCOME:-}" = "failure" ]; then
    # The review sandbox could not be prepared (PR #6645: Docker
    # Hub answered 500/504 on the base image), so the editor never
    # ran. Name that phase and keep its last error line as evidence.
    autofix_empty_failure_reason="sandbox_prepare_failed"
    echo "AUTOFIX_SANDBOX_PREPARE_FAILED=true" >> "$GITHUB_ENV"
    if [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ]; then
      sandbox_prepare_last_error="$(grep -E '^(#[0-9]+ )?(::error::|ERROR:? )' "${RUNTIME_DIR}/sandbox_prepare_stderr.txt" 2>/dev/null | tail -n 1 | sed -E 's/^#[0-9]+ //; s/^::error:://; s/^ERROR:? //' | tr -d '\r' | cut -c1-300 || true)"
      {
        echo "sandbox_prepare_failed=true"
        grep -E '^REVIEW_SANDBOX_BUILD .*outcome=fail' "${RUNTIME_DIR}/sandbox_prepare_stderr.txt" 2>/dev/null | tail -n 1 || true
        if [ -n "${sandbox_prepare_last_error}" ]; then
          printf '::error::Review sandbox prepare failed: %s\n' "${sandbox_prepare_last_error}"
        else
          echo "::error::Review sandbox prepare failed"
        fi
      } > "${RUNTIME_DIR}/sandbox_prepare_failure_evidence.txt"
    fi
    sandbox_prepare_headline=""
    if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ]; then
      sandbox_prepare_headline="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" failure-headline \
        --evidence-file "${RUNTIME_DIR:-}/sandbox_prepare_failure_evidence.txt" 2>/dev/null | sed -n 's/^first_error=//p' | head -n 1 || true)"
    fi
    noop_explanation="The review sandbox could not be prepared (step \"Install project dependencies\"), so the editor never ran and no file changes were committed. ${sandbox_prepare_headline:-The prepare error is in the workflow run log.}"
    echo "AUTOFIX_SANDBOX_PREPARE_FAILED reason=sandbox_prepare_failed error_present=$([ -n "${sandbox_prepare_headline}" ] && echo 1 || echo 0)"
  fi
  # Model-provider outage (issue #6633): a live probe confirms the
  # log evidence, so the reason is provider_unavailable (no cap
  # count, no label, one repo-wide tracker). Fail-open.
  if [ -n "${RUNTIME_DIR:-}" ] && [ -f "${SUPPORT_SCRIPTS_DIR:-}/provider_outage.py" ]; then
    provider_outage_log_args=()
    for provider_outage_log in "${PREVIOUS_REVIEWS_DIR:-/nonexistent}"/*.log "${RUNTIME_DIR}"/summariser_*.log "${RUNTIME_DIR}/reviewers_failure_evidence.txt" "${RUNTIME_DIR}/editor_stage_stderr.txt" "${RUNTIME_DIR}/collect_metadata_stderr.txt" "${RUNTIME_DIR}/resolver_stage_stderr.txt"; do
      [ -f "${provider_outage_log}" ] && provider_outage_log_args+=(--log-file "${provider_outage_log}")
    done
    if [ "${#provider_outage_log_args[@]}" -gt 0 ]; then
      provider_outage_class="$(PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/provider_outage.py" classify --probe-cache "${RUNTIME_DIR}/provider_outage_probe.json" "${provider_outage_log_args[@]}" 2>/dev/null | head -n 1 || true)"
      if [[ "${provider_outage_class}" == reason=provider_unavailable* ]]; then
        provider_outage_status="$(printf '%s\n' "${provider_outage_class}" | sed -n 's/.* status=\([a-z0-9_]*\).*/\1/p' | head -n 1)"
        autofix_empty_failure_reason="provider_unavailable"
        noop_explanation="The model provider failed (status ${provider_outage_status:-unknown}, confirmed by a live probe). This is a repo-wide outage tracked on the ai:provider-outage issue, not a defect in this PR, and it does not count toward the identical-failure cap; review resumes automatically when the provider recovers."
        { echo "AUTOFIX_FAILURE_REASON=provider_unavailable"; echo "AUTOFIX_PROVIDER_STATUS=${provider_outage_status:-unknown}"; } >> "$GITHUB_ENV"
      fi
    fi
  fi
  # review-autofix-failure:v1 marker for the gate's identical-failure
  # fingerprint cap. Same evidence files and reason precedence as
  # "Assemble failure evidence", so every failure comment of one run
  # carries one fingerprint. Empty when the helper is unavailable.
  AUTOFIX_FAILURE_MARKER_SUFFIX=""
  autofix_failure_marker_line=""
  autofix_failure_fp=""
  if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ]; then
    # One helper run yields both marker= and fp= (issue #9533 compares fp=).
    autofix_failure_fingerprint_out="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" autofix-failure-fingerprint \
      --failure-reason "${autofix_empty_failure_reason}" \
      --summary-line-file "${RUNTIME_DIR:-}/review_autofix_run_summary_line.txt" \
      --evidence-file "${RUNTIME_DIR:-}/reviewers_failure_evidence.txt" \
      --evidence-file "${RUNTIME_DIR:-}/sandbox_prepare_failure_evidence.txt" \
      --evidence-file "${RUNTIME_DIR:-}/editor_stage_stderr.txt" \
      --evidence-file "${RUNTIME_DIR:-}/collect_metadata_stderr.txt" \
      --evidence-file "${RUNTIME_DIR:-}/resolver_stage_stderr.txt" \
      --evidence-file "${RUNTIME_DIR:-}/review_autofix_run_summary_line.txt" \
      --head-sha "${AUTOFIX_FAILURE_HEAD_SHA:-}" \
      --support-sha "${REVIEW_SUPPORT_SHA:-}" \
      --run-id "${GITHUB_RUN_ID:-}" 2>/dev/null || true)"
    autofix_failure_marker_line="$(printf '%s\n' "${autofix_failure_fingerprint_out}" | sed -n 's/^marker=//p' | head -n 1 || true)"
    autofix_failure_fp="$(printf '%s\n' "${autofix_failure_fingerprint_out}" | sed -n 's/^fp=//p' | head -n 1 || true)"
    if [ -n "${autofix_failure_marker_line}" ]; then
      AUTOFIX_FAILURE_MARKER_SUFFIX=$'\n\n'"${autofix_failure_marker_line}"
    fi
  fi
  # Repeated reviewer failure (issue #9533). The gate already counted the
  # trusted same-head review-autofix-failure:v1 markers (GATE_FINGERPRINT_CAP_*).
  # When this run's reviewers_failed fingerprint matches them and this marker
  # reaches REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL_REVIEWERS_FAILED (default
  # 2), the crash is deterministic: post no "will retry" comment and leave
  # AUTOFIX_EDITOR_EMPTY_NOOP unset, so the workflow-failure steps label
  # ai:review-blocked and post one comment naming the fingerprint and the
  # first error. Any missing or invalid value keeps the retry (fail open).
  reviewers_failed_repeat_max="${REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL_REVIEWERS_FAILED:-2}"
  case "${reviewers_failed_repeat_max}" in
    ''|*[!0-9]*|0) reviewers_failed_repeat_max=2 ;;
  esac
  reviewers_failed_prior_count="${GATE_FINGERPRINT_CAP_COUNT:-0}"
  [[ "${reviewers_failed_prior_count}" =~ ^[0-9]{1,6}$ ]] || reviewers_failed_prior_count=0
  # No fallback to this run's head: an unset gate head fails the same-head
  # check and keeps the retry.
  reviewers_failed_gate_head="$(printf '%s' "${GATE_HEAD_SHA_FOR_FINGERPRINT:-}" | tr 'A-F' 'a-f')"
  reviewers_failed_this_head="$(printf '%s' "${AUTOFIX_FAILURE_HEAD_SHA:-}" | tr 'A-F' 'a-f')"
  if [ "${autofix_empty_failure_reason}" = "reviewers_failed" ] \
    && [ "${REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED:-true}" != "false" ] \
    && [ "${GATE_FINGERPRINT_CAP_REASON:-}" = "reviewers_failed" ] \
    && [[ "${autofix_failure_fp}" =~ ^[0-9a-f]{64}$ ]] \
    && [ "${GATE_FINGERPRINT_CAP_FP:-}" = "${autofix_failure_fp}" ] \
    && [ "${reviewers_failed_prior_count}" -ge 1 ] \
    && [[ "${reviewers_failed_this_head}" =~ ^[0-9a-f]{40}$ ]] \
    && [ "${reviewers_failed_gate_head}" = "${reviewers_failed_this_head}" ] \
    && [ "$((reviewers_failed_prior_count + 1))" -ge "${reviewers_failed_repeat_max}" ]; then
    reviewers_failed_repeat_pr="${PR_NUMBER:-}"
    [[ "${reviewers_failed_repeat_pr}" =~ ^[0-9]+$ ]] || reviewers_failed_repeat_pr="unknown"
    echo "AUTOFIX_REVIEWERS_FAILED_REPEATED pr=${reviewers_failed_repeat_pr} head=${reviewers_failed_this_head} fp=${autofix_failure_fp} count=$((reviewers_failed_prior_count + 1)) max=${reviewers_failed_repeat_max}"
    {
      echo "AUTOFIX_REVIEWERS_FAILED_REPEATED=true"
      echo "AUTOFIX_REVIEWERS_FAILED_REPEATED_COUNT=$((reviewers_failed_prior_count + 1))"
      echo "AUTOFIX_REVIEWERS_FAILED_REPEATED_FP=${autofix_failure_fp}"
    } >> "$GITHUB_ENV"
    exit 1
  fi
  # Issue-less PRs (issue #9487): the standalone stall poller follows only
  # labelled issues, so for a PR whose LINKED_ISSUES_JSON (exported by the
  # "Collect PR metadata" step) is exactly an empty array the retry comes
  # from the poller's issue-less review retry pass. Unset, unparseable or
  # non-empty values keep the legacy sentence.
  issueless_retry_note="The stall poller will retry the review on a subsequent cycle."
  if [ -n "${LINKED_ISSUES_JSON:-}" ] \
    && printf '%s' "${LINKED_ISSUES_JSON}" | jq -e 'type == "array" and length == 0' >/dev/null 2>&1; then
    issueless_retry_note="This PR has no linked issue, so the orchestrator poller's issue-less review retry pass re-dispatches this review once, after a newer review-support (\`@stable\`) release is pinned."
  fi
  NOOP_BODY="$(cat <<EOF
**AI review/autofix produced no output — will retry**

${noop_explanation} No \`ai:review-blocked\` label is being applied for this run.

[View workflow run](${RUN_URL}) for details. ${issueless_retry_note}
EOF
)"
  if [ -n "${reviewers_failed_headline:-}" ]; then
    NOOP_BODY+=$'\n\n'"**First error:** \`${reviewers_failed_headline//\`/\'}\`"
  fi
  if [ "${autofix_empty_failure_reason}" = "provider_unavailable" ]; then
    NOOP_BODY="${NOOP_BODY/produced no output — will retry/paused — model provider unavailable}"
  fi
  NOOP_BODY+="${AUTOFIX_FAILURE_MARKER_SUFFIX}"
  gh_retry gh api "repos/${AUTOFIX_STEP_REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${NOOP_BODY}" >/dev/null 2>&1 || true
  echo "AUTOFIX_EDITOR_EMPTY_NOOP=true" >> "$GITHUB_ENV"
  exit 1
fi

# Records whether the editor wrote a real summary, before the synthetic
# fallback below fills the file in (#8321: the sandbox-excluded escalation
# reads only a real summary).
editor_summary_is_real=0
[ -s "${EDITOR_SUMMARY_FILE}" ] && editor_summary_is_real=1

if [ ! -s "${EDITOR_SUMMARY_FILE}" ]; then
  cat > "${EDITOR_SUMMARY_FILE}" <<'__EDITOR_SUMMARY__'
Changes made:
- none (no editor summary output was generated)

Already satisfied (suggested but already present):
- none (no editor summary output was generated)

Ignored suggestions (with short reason):
- no summary available from editor stage

Reviewer files processed:
- none (no editor summary output was generated)

Review file issue audit:
- none (no editor summary output was generated)

Regression fingerprint:
- unavailable (editor fallback)

Runtime failure path:
- unavailable (editor fallback)
__EDITOR_SUMMARY__
fi

{
  echo "AI autofix editor summary"
  echo
  cat "${EDITOR_SUMMARY_FILE}"
  echo
  echo "Files changed / commit status:"
  if [ -s "${COMMITTED_FILES_FILE}" ]; then
    cat "${COMMITTED_FILES_FILE}"
  else
    echo "- none"
  fi
  echo
} > "${PR_EDITOR_COMMENT_FILE}"

type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

# Sandbox-excluded escalation (#8321). PR #8250 was re-reviewed 29 times on
# unchanged heads because the editor declined its only fix as
# "sandbox-excluded path" and the run still ended as a success, so nothing
# counted it. When the editor's real summary reports "Change status:
# not-edited", nothing was committed, and at least one "Ignored suggestions"
# bullet ends with the fixed reason "sandbox-excluded path" (the wording the
# editor prompt in review_apply_fixes.sh pins), the posted summary ends with a
# review-autofix-failure:v1 marker for the non-retryable reason
# review_editor_sandbox_path_excluded. The gate's identical-failure cap then
# stops the head on the next dispatch; a new push resets it. The marker rides
# on this step's existing comment (no extra API call). Only the integer count
# is logged, never summary text. Fail-open: no helper or no marker leaves the
# run as it was, and the step's exit status is unchanged.
# "Nothing committed" needs positive evidence: the commit step writes "- none"
# or a "- commit skipped (...)" marker line when it committed nothing, and one
# "- <path>" line per committed file otherwise. An unset or missing file is
# unknown, so no marker is written (fail closed toward not capping).
# Only a top-level bullet (at most one leading space, no tab) is a declined
# finding; an indented sub-bullet is the editor's explanation under one. PRs
# #9453 and #9390 were capped and labelled ai:needs-human because a sub-bullet
# explaining a sandbox-only test failure ended with "a sandbox-excluded path"
# while the editor declined no fix for that reason. review_apply_fixes.sh's
# claimed-changes filter likewise treats indented bullets as nested.
sandbox_excluded_marker_suffix=""
sandbox_excluded_nothing_committed=0
if [ -n "${COMMITTED_FILES_FILE:-}" ] && [ -f "${COMMITTED_FILES_FILE}" ] && [ -r "${COMMITTED_FILES_FILE}" ] \
  && ! grep -q -v -E '^[[:space:]]*$|^- none$|^- none \(edits withheld for safety: .*\)$|^- commit skipped \(.*\)$' "${COMMITTED_FILES_FILE}"; then
  sandbox_excluded_nothing_committed=1
fi
# The commit step's own flag wins over the file text: a committed path that
# prints like a placeholder line never counts as nothing committed.
if [ "${DID_COMMIT:-}" = "true" ]; then
  sandbox_excluded_nothing_committed=0
fi
if [ "${editor_summary_is_real}" = "1" ] && [ "${sandbox_excluded_nothing_committed}" = "1" ]; then
  sandbox_excluded_findings="$(tr -d '\r' < "${EDITOR_SUMMARY_FILE}" | awk '
    BEGIN { section = ""; status = ""; count = 0; trail = "[] \t.\"\047`)*]+$" }
    /^(Changes made|Change status|Already satisfied \(suggested but already present\)|Ignored suggestions \(with short reason\)|Reviewer files processed|Review file issue audit|PR comment audit|Regression fingerprint|Runtime failure path):[ \t]*$/ {
      section = $0
      sub(/:[ \t]*$/, "", section)
      next
    }
    section == "Change status" && status == "" && /^[ \t]*-[ \t]+/ {
      value = $0
      sub(/^[ \t]*-[ \t]+/, "", value)
      sub(/[ \t]+$/, "", value)
      status = tolower(value)
      next
    }
    section == "Ignored suggestions (with short reason)" && /^ ?-[ \t]+/ {
      value = tolower($0)
      sub(trail, "", value)
      if (value ~ /sandbox-excluded path$/) count++
    }
    END { print (status == "not-edited" ? count : 0) }
  ' 2>/dev/null || echo 0)"
  case "${sandbox_excluded_findings}" in
    ''|*[!0-9]*) sandbox_excluded_findings=0 ;;
  esac
  if [ "${sandbox_excluded_findings}" -gt 0 ]; then
    sandbox_excluded_outcome="marker_unavailable"
    sandbox_excluded_reason=" reason=helper_missing"
    if [ -n "${AUTOFIX_FAILURE_HEAL_PY:-}" ] && [ -f "${AUTOFIX_FAILURE_HEAL_PY}" ]; then
      sandbox_excluded_reason=" reason=no_marker"
      sandbox_excluded_marker_line="$(PYTHONDONTWRITEBYTECODE=1 python3 "${AUTOFIX_FAILURE_HEAL_PY}" autofix-failure-fingerprint \
        --failure-reason review_editor_sandbox_path_excluded \
        --head-sha "${AUTOFIX_FAILURE_HEAD_SHA:-}" \
        --support-sha "${REVIEW_SUPPORT_SHA:-}" \
        --run-id "${GITHUB_RUN_ID:-}" 2>/dev/null | sed -n 's/^marker=//p' | head -n 1 || true)"
      if [ -n "${sandbox_excluded_marker_line}" ]; then
        sandbox_excluded_marker_suffix=$'\n\n'"${sandbox_excluded_marker_line}"
        sandbox_excluded_outcome="marker"
        sandbox_excluded_reason=""
      fi
    fi
    sandbox_excluded_head="$(printf '%s' "${AUTOFIX_FAILURE_HEAD_SHA:-}" | tr 'A-F' 'a-f')"
    [[ "${sandbox_excluded_head}" =~ ^[0-9a-f]{40}$ ]] || sandbox_excluded_head="unknown"
    sandbox_excluded_pr="${PR_NUMBER:-}"
    [[ "${sandbox_excluded_pr}" =~ ^[0-9]+$ ]] || sandbox_excluded_pr="unknown"
    echo "AUTOFIX_SANDBOX_EXCLUDED_ESCALATION pr=${sandbox_excluded_pr} head=${sandbox_excluded_head} findings=${sandbox_excluded_findings} outcome=${sandbox_excluded_outcome}${sandbox_excluded_reason}"

    # .gitleaksignore fingerprint suggestion (#9659). The editor never writes
    # .gitleaksignore (a sandbox-excluded root dotfile), so when a
    # sandbox-excluded finding names it, the gitleaks "Fingerprint:" lines of
    # the failed checks' log_tail blocks in PR_CHECK_RUNS_CONTEXT_FILE are
    # offered as a ready-to-paste block in this step's existing comment (no
    # extra API call), ahead of the trailing markers. log_tail is untrusted:
    # a line is used only when it fully matches the gitleaks fingerprint
    # shape, carries no backtick, angle bracket or control character, and
    # names a commit that exists and is an ancestor of the validated PR head.
    # At most 10 lines. Only the count is logged, never the lines. Fail-open:
    # any error logs outcome=none count=0 and leaves the step's status alone.
    # Log key: REVIEW_EDITOR_GITLEAKS_SUGGESTION pr=<n|unknown> outcome=suggested|none count=<k> missing=<m>
    gitleaks_suggest_named="$(tr -d '\r' < "${EDITOR_SUMMARY_FILE}" | awk '
      BEGIN { section = ""; found = 0; trail = "[] \t.\"\047`)*]+$" }
      /^(Changes made|Change status|Already satisfied \(suggested but already present\)|Ignored suggestions \(with short reason\)|Reviewer files processed|Review file issue audit|PR comment audit|Regression fingerprint|Runtime failure path):[ \t]*$/ {
        section = $0
        sub(/:[ \t]*$/, "", section)
        next
      }
      section == "Ignored suggestions (with short reason)" && /^[ \t]*-[ \t]+/ {
        value = tolower($0)
        sub(trail, "", value)
        if (value ~ /sandbox-excluded path$/ && index(value, ".gitleaksignore") > 0) found = 1
      }
      END { print found }
    ' 2>/dev/null || echo 0)"
    if [ "${gitleaks_suggest_named}" = "1" ]; then
      _review_editor_gitleaks_suggestion()
      {
        local gitleaks_suggest_head gitleaks_suggest_repo gitleaks_suggest_candidate gitleaks_suggest_sha
        # Candidates whose commit is absent from the shallow checkout
        # (fetch-depth 100) are dropped, never fetched; missing=<k> tells that
        # apart from "no gitleaks output" in the log line.
        local gitleaks_suggest_missing=0
        local -a gitleaks_suggest_lines=()
        gitleaks_suggest_head="$(printf '%s' "${AUTOFIX_FAILURE_HEAD_SHA:-}" | tr 'A-F' 'a-f')"
        gitleaks_suggest_repo="${GITHUB_WORKSPACE:-$PWD}"
        if [[ "${gitleaks_suggest_head}" =~ ^[0-9a-f]{40}$ ]] && [ -n "${PR_CHECK_RUNS_CONTEXT_FILE:-}" ] \
          && [ -f "${PR_CHECK_RUNS_CONTEXT_FILE}" ] && [ ! -L "${PR_CHECK_RUNS_CONTEXT_FILE}" ]; then
          while IFS= read -r gitleaks_suggest_candidate; do
            [[ "${gitleaks_suggest_candidate}" =~ ^[0-9a-f]{40}: ]] || continue
            gitleaks_suggest_sha="${gitleaks_suggest_candidate%%:*}"
            # The 10-line cap bounds only the suggestion: later candidates are
            # still counted toward missing= (the extractor emits at most 200).
            if ! git -C "${gitleaks_suggest_repo}" cat-file -e "${gitleaks_suggest_sha}^{commit}" >/dev/null 2>&1; then
              gitleaks_suggest_missing=$((gitleaks_suggest_missing + 1))
            elif [ "${#gitleaks_suggest_lines[@]}" -lt 10 ] && git -C "${gitleaks_suggest_repo}" merge-base --is-ancestor "${gitleaks_suggest_sha}" "${gitleaks_suggest_head}" >/dev/null 2>&1; then
              gitleaks_suggest_lines+=("${gitleaks_suggest_candidate}")
            fi
          done < <(PYTHONDONTWRITEBYTECODE=1 python3 -I -B -c '
import re, sys
FP = r"[0-9a-f]{40}:[^:\n]+:[A-Za-z0-9_.-]+:[0-9]+"
START = re.compile(r"^failed\[[0-9]+\]\.log_tail \([0-9]+ chars\):$")
HEADER = re.compile(r"^(failed|incomplete)\[[0-9]+\]\.")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
STAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z[ \t]+")
LINE = re.compile(r"Fingerprint:[ \t]+(" + FP + r")[ \t]*")
with open(sys.argv[1], "rb") as fh:
	text = fh.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
seen = []
in_tail = False
for raw in text.split("\n"):
	line = raw.replace("\r", "")
	if START.match(line):
		in_tail = True
		continue
	if HEADER.match(line):
		in_tail = False
		continue
	if not in_tail:
		continue
	line = STAMP.sub("", ANSI.sub("", line), count=1)
	match = LINE.fullmatch(line)
	if not match:
		continue
	fp = match.group(1)
	if len(fp) > 400 or any(ch in fp for ch in "`<>") or not fp.isprintable():
		continue
	if fp not in seen:
		seen.append(fp)
	if len(seen) >= 200:
		break
for fp in seen:
	print(fp)
' "${PR_CHECK_RUNS_CONTEXT_FILE}" 2>/dev/null || true)
        fi
        if [ "${#gitleaks_suggest_lines[@]}" -gt 0 ] && {
          printf '\n%s\n\n' "### Suggested \`.gitleaksignore\` lines (not applied)"
          printf '%s\n\n' "The editor declined a finding on \`.gitleaksignore\`, a sandbox-excluded path the autofix editor never writes. The failed checks' gitleaks output names the fingerprints below, each for a commit in this pull request's history. Adding them suppresses a gitleaks secret finding, so a human or the main-branch pipeline must decide whether each one is a false positive before pasting it into \`.gitleaksignore\`."
          printf '%s\n' '```text'
          printf '%s\n' "${gitleaks_suggest_lines[@]}"
          printf '%s\n' '```'
        } >> "${PR_EDITOR_COMMENT_FILE}"; then
          echo "REVIEW_EDITOR_GITLEAKS_SUGGESTION pr=${sandbox_excluded_pr} outcome=suggested count=${#gitleaks_suggest_lines[@]} missing=${gitleaks_suggest_missing}"
        else
          echo "REVIEW_EDITOR_GITLEAKS_SUGGESTION pr=${sandbox_excluded_pr} outcome=none count=0 missing=${gitleaks_suggest_missing}"
        fi
        return 0
      }
      _review_editor_gitleaks_suggestion || echo "REVIEW_EDITOR_GITLEAKS_SUGGESTION pr=${sandbox_excluded_pr} outcome=none count=0 missing=0"
    fi
  fi
fi

# Clean same-head marker (#8952). A full review whose real editor summary
# reports "Change status: not-edited", with positive evidence that nothing was
# committed and no sandbox-excluded finding, ends its summary comment with
# `<!-- review-autofix-clean:v1 head=<sha> base=<sha> support=<sha|none>
# run=<id> -->` as the last line. The review gate
# (review_autofix_step_evaluate_gate.sh) then skips another full review of the
# same head, base and review-support SHA. The fallback summary, a commit, a
# sandbox-excluded finding (a failure path even without its marker), or a
# missing head / base / run id writes no marker, so today's behaviour holds.
# AUTOFIX_CLEAN_MARKER_BASE_SHA defaults to empty (no marker): an older
# workflow that does not pass the gate's base_sha degrades safely.
clean_marker_suffix=""
clean_marker_reason="none"
clean_marker_head="$(printf '%s' "${AUTOFIX_FAILURE_HEAD_SHA:-}" | tr 'A-F' 'a-f')"
clean_marker_base="$(printf '%s' "${AUTOFIX_CLEAN_MARKER_BASE_SHA:-}" | tr 'A-F' 'a-f')"
clean_marker_status=""
if [ "${editor_summary_is_real}" = "1" ]; then
  clean_marker_status="$(tr -d '\r' < "${EDITOR_SUMMARY_FILE}" | awk '
    BEGIN { section = ""; status = "" }
    /^(Changes made|Change status|Already satisfied \(suggested but already present\)|Ignored suggestions \(with short reason\)|Reviewer files processed|Review file issue audit|PR comment audit|Regression fingerprint|Runtime failure path):[ \t]*$/ {
      section = $0
      sub(/:[ \t]*$/, "", section)
      next
    }
    section == "Change status" && status == "" && /^[ \t]*-[ \t]+/ {
      value = $0
      sub(/^[ \t]*-[ \t]+/, "", value)
      sub(/[ \t]+$/, "", value)
      status = tolower(value)
    }
    END { print status }
  ' 2>/dev/null || true)"
fi
if [ "${editor_summary_is_real}" != "1" ]; then
  clean_marker_reason="fallback"
elif [ "${clean_marker_status}" != "not-edited" ]; then
  clean_marker_reason="not_not_edited"
elif [ "${sandbox_excluded_nothing_committed}" != "1" ] || [ "${DID_COMMIT:-}" = "true" ]; then
  clean_marker_reason="committed"
elif [ -n "${sandbox_excluded_marker_suffix}" ] || [ "${sandbox_excluded_findings:-0}" != "0" ]; then
  clean_marker_reason="sandbox_excluded"
elif [ -n "${RUNTIME_DIR:-}" ] && [ -s "${RUNTIME_DIR}/review_unreachable_all.tsv" ] \
  && awk -F '\t' '$5 == "true" { found = 1 } END { exit !found }' "${RUNTIME_DIR}/review_unreachable_all.tsv" 2>/dev/null; then
  # Issue #10057: an unreachable security finding (ledger UNREACHABLE_SECURITY
  # true, kept across rounds even when no reviewer re-raises it) keeps the PR
  # blocked, so the head is never marked clean while it stands.
  clean_marker_reason="unreachable_security_hold"
elif ! [[ "${clean_marker_head}" =~ ^[0-9a-f]{40}$ ]]; then
  clean_marker_reason="head_unavailable"
elif ! [[ "${clean_marker_base}" =~ ^[0-9a-f]{40}$ ]]; then
  clean_marker_reason="base_unavailable"
elif ! [[ "${AUTOFIX_STEP_RUN_ID:-}" =~ ^[0-9]+$ ]]; then
  clean_marker_reason="run_unavailable"
else
  clean_marker_support="$(printf '%s' "${REVIEW_SUPPORT_SHA:-}" | tr 'A-F' 'a-f')"
  [[ "${clean_marker_support}" =~ ^[0-9a-f]{40}$ ]] || clean_marker_support="none"
  clean_marker_suffix=$'\n\n'"<!-- review-autofix-clean:v1 head=${clean_marker_head} base=${clean_marker_base} support=${clean_marker_support} run=${AUTOFIX_STEP_RUN_ID} -->"
fi
clean_marker_pr="${PR_NUMBER:-}"
[[ "${clean_marker_pr}" =~ ^[0-9]+$ ]] || clean_marker_pr="unknown"
[[ "${clean_marker_head}" =~ ^[0-9a-f]{40}$ ]] || clean_marker_head="unknown"
echo "AUTOFIX_CLEAN_SAME_HEAD_MARKER outcome=$([ -n "${clean_marker_suffix}" ] && echo written || echo not_written) reason=${clean_marker_reason} pr=${clean_marker_pr} head=${clean_marker_head}"

# Stale ai:review-blocked clear (#9369 / #9716). The identical-failure cap job
# in review_autofix.yml adds ai:review-blocked to a PR with no linked issue,
# and nothing removed it after a later clean review (PR #9218 and PR #9229
# kept it after the harness was fixed). Once this run has posted its clean
# marker, the label is removed when the newest own-line
# `<!-- review-autofix-failure-cap:v1 head=<sha> ... [support=<sha>] -->`
# marker on the PR is stale for the clean head / support, says it labelled
# "this pull request (no linked issue found)", and was posted by the GH_PAT
# login (the cap job's own trust rule). No cap marker, a matching head and
# support, a cap that labelled linked issues, or an untrusted author keeps the
# label. Labels and comments come from the run's metadata snapshot
# (PR_PAYLOAD_FILE, PR_ISSUE_COMMENTS_FILE); one fallback read runs only when
# a snapshot file is missing or unreadable. New calls (GET /user, one DELETE)
# happen only on the stale path. Linked issues are untouched. Every exit logs
# one REVIEW_BLOCKED_STALE_CLEAR line with validated SHAs and fixed tokens
# only; the function always returns 0 and never fails the step.
review_blocked_stale_clear_log()
{
  # Reads the caller's rb_stale_* locals (bash locals are dynamically scoped).
  echo "REVIEW_BLOCKED_STALE_CLEAR pr=${rb_stale_pr_log} outcome=$1 reason=$2 cap_head=${rb_stale_cap_head_log} cap_support=${rb_stale_cap_support_log} clean_head=${rb_stale_clean_head} clean_support=${rb_stale_clean_support}"
}

# Classifies one cap-marker row against the clean head / support. Prints
# cap_head_mismatch or cap_support_mismatch when the marker is stale and
# labelled the PR itself, else same_head_same_support or cap_not_pr_label.
# Reads the caller's rb_stale_clean_* locals. A cap with no support= counts as
# a different support only when the clean support is a SHA (the cap job's own
# matching rule).
review_blocked_stale_clear_row_reason()
{
  local rb_row_head="$1" rb_row_support="$2" rb_row_pr_label="$3" rb_row_stale=""
  if [ "${rb_row_head}" != "${rb_stale_clean_head}" ]; then
    rb_row_stale="cap_head_mismatch"
  elif [[ "${rb_stale_clean_support}" =~ ^[0-9a-f]{40}$ ]] && [ "${rb_row_support}" != "${rb_stale_clean_support}" ]; then
    rb_row_stale="cap_support_mismatch"
  else
    echo "same_head_same_support"
    return 0
  fi
  # With a linked issue the cap labels the issue, never the PR, so a PR label
  # next to such a marker came from another path: leave it.
  if [ "${rb_row_pr_label}" != "true" ]; then
    echo "cap_not_pr_label"
    return 0
  fi
  echo "${rb_row_stale}"
}

review_blocked_stale_clear_after_clean()
{
  local rb_stale_pr="${PR_NUMBER:-}" rb_stale_repo="${AUTOFIX_STEP_REPOSITORY:-}"
  local rb_stale_pr_log="unknown" rb_stale_cap_head_log="none" rb_stale_cap_support_log="none"
  local rb_stale_clean_head="${clean_marker_head:-unknown}" rb_stale_clean_support="${clean_marker_support:-none}"
  local rb_stale_labels="" rb_stale_comments_raw="" rb_stale_selected="" rb_stale_reason=""
  local rb_stale_cap_head="" rb_stale_cap_support="" rb_stale_cap_pr_label=""
  local rb_stale_login="" rb_stale_del_err="" rb_stale_del_first=""
  local rb_stale_row="" rb_stale_row_login="" rb_stale_row_head="" rb_stale_row_support="" rb_stale_row_pr_label=""
  local rb_stale_row_reason="" rb_stale_any_removable="" rb_stale_trusted_row=""
  # Lists every comment with an own-line cap marker, the same own-line shape
  # the cap job matches, newest (highest id) first; output is one row per
  # marker comment of unit-separator (0x1f) joined fields, or nothing. A
  # whitespace IFS such as tab would collapse an empty login or support field.
  # shellcheck disable=SC2016
  local rb_stale_select_jq='if type != "array" then error("not an array") else
    [ .[] | select(type == "object") | (.body // "" | tostring) as $b
      | [ $b | capture("(^|\n)<!-- review-autofix-failure-cap:v1 head=(?<h>[0-9a-f]{40})(?<f>( [^ >\n]+)*) -->[ \r]*(\n|$)"; "g") ] as $m
      | select($m | length > 0)
      | { id: (.id | if type == "number" then . else (tostring | tonumber? // 0) end),
          login: (.user.login // "" | tostring | gsub("[[:cntrl:]]"; "")),
          head: $m[-1].h,
          support: ($m[-1].f | split(" ") | map(select(startswith("support="))) | last // "" | ltrimstr("support=") | gsub("[[:cntrl:]]"; "")),
          pr_label: ($b | contains("this pull request (no linked issue found)")) } ]
    | sort_by(.id) | reverse | .[] | [.login, .head, .support, (.pr_label | tostring)] | join("\u001f") end'

  [[ "${rb_stale_pr}" =~ ^[0-9]+$ ]] && rb_stale_pr_log="${rb_stale_pr}"
  if ! [[ "${rb_stale_pr}" =~ ^[0-9]+$ ]]; then
    review_blocked_stale_clear_log kept pr_unavailable
    return 0
  fi
  if ! [[ "${rb_stale_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    review_blocked_stale_clear_log kept repo_unavailable
    return 0
  fi

  # 1. Is the label on the PR? Snapshot first, one fallback read otherwise.
  if [ -n "${PR_PAYLOAD_FILE:-}" ] && [ -r "${PR_PAYLOAD_FILE}" ] \
    && rb_stale_labels="$(jq -r 'if type == "object" then (.labels[]?.name // empty) else error("not an object") end' "${PR_PAYLOAD_FILE}" 2>/dev/null)"; then
    :
  elif rb_stale_labels="$(gh_retry gh api "repos/${rb_stale_repo}/pulls/${rb_stale_pr}" --jq '.labels[]?.name' 2>/dev/null)"; then
    :
  else
    review_blocked_stale_clear_log kept snapshot_unavailable
    return 0
  fi
  if ! printf '%s\n' "${rb_stale_labels}" | grep -qxF 'ai:review-blocked'; then
    review_blocked_stale_clear_log kept label_absent
    return 0
  fi

  # 2. Own-line cap markers, from the snapshot or one fallback read.
  if [ -n "${PR_ISSUE_COMMENTS_FILE:-}" ] && [ -r "${PR_ISSUE_COMMENTS_FILE}" ] \
    && rb_stale_selected="$(jq -r "${rb_stale_select_jq}" "${PR_ISSUE_COMMENTS_FILE}" 2>/dev/null)"; then
    :
  elif rb_stale_comments_raw="$(gh_retry gh api --paginate "repos/${rb_stale_repo}/issues/${rb_stale_pr}/comments?per_page=100" 2>/dev/null)" \
    && rb_stale_selected="$(printf '%s' "${rb_stale_comments_raw}" | jq -s 'add // []' 2>/dev/null | jq -r "${rb_stale_select_jq}" 2>/dev/null)"; then
    :
  else
    review_blocked_stale_clear_log kept snapshot_unavailable
    return 0
  fi
  rb_stale_selected="$(printf '%s\n' "${rb_stale_selected}" | grep -v '^$' || true)"
  if [ -z "${rb_stale_selected}" ]; then
    review_blocked_stale_clear_log kept no_cap_marker
    return 0
  fi
  # The log reports the newest marker until a trusted one is selected.
  IFS=$'\x1f' read -r _ rb_stale_cap_head rb_stale_cap_support rb_stale_cap_pr_label <<< "$(printf '%s\n' "${rb_stale_selected}" | head -n 1)" || true
  rb_stale_cap_support="$(printf '%s' "${rb_stale_cap_support}" | tr 'A-F' 'a-f')"
  [[ "${rb_stale_cap_head}" =~ ^[0-9a-f]{40}$ ]] && rb_stale_cap_head_log="${rb_stale_cap_head}"
  [[ "${rb_stale_cap_support}" =~ ^[0-9a-f]{40}$ ]] && rb_stale_cap_support_log="${rb_stale_cap_support}"

  # 3. When no marker from any author is stale and labelled the PR, nothing
  # can be removed whoever is trusted: keep the label with the newest marker's
  # reason and skip the trust lookup (no new call off the stale path).
  while IFS=$'\x1f' read -r rb_stale_row_login rb_stale_row_head rb_stale_row_support rb_stale_row_pr_label; do
    rb_stale_row_support="$(printf '%s' "${rb_stale_row_support}" | tr 'A-F' 'a-f')"
    rb_stale_row_reason="$(review_blocked_stale_clear_row_reason "${rb_stale_row_head}" "${rb_stale_row_support}" "${rb_stale_row_pr_label}")"
    case "${rb_stale_row_reason}" in
      cap_head_mismatch|cap_support_mismatch) rb_stale_any_removable="true"; break ;;
    esac
  done <<< "${rb_stale_selected}"
  if [ "${rb_stale_any_removable}" != "true" ]; then
    review_blocked_stale_clear_log kept "$(review_blocked_stale_clear_row_reason "${rb_stale_cap_head}" "${rb_stale_cap_support}" "${rb_stale_cap_pr_label}")"
    return 0
  fi

  # 4. Trusted author. The PR payload and comments in the metadata snapshot
  # (and their fallback reads above) do not expose the identity authenticated
  # by GH_PAT, so this lookup cannot be merged into an existing call (§14). It
  # runs once, only when some marker is stale (same lookup as the gate in
  # review_autofix_step_evaluate_gate.sh).
  if ! rb_stale_login="$(GH_RETRY_IDEMPOTENT=true gh_retry gh api user --jq '.login // ""' 2>/dev/null)"; then
    rb_stale_login=""
  fi
  rb_stale_login="$(printf '%s' "${rb_stale_login}" | head -n 1 | tr -d '\r')"
  if [ -z "${rb_stale_login}" ]; then
    review_blocked_stale_clear_log kept marker_author_unavailable
    return 0
  fi
  # The newest trusted marker decides (trust first, then newest, as the cap
  # job and the evaluate gate match markers), so a newer marker from another
  # author cannot shadow the trusted one.
  while IFS= read -r rb_stale_row; do
    IFS=$'\x1f' read -r rb_stale_row_login _ <<< "${rb_stale_row}" || true
    if [ -n "${rb_stale_row_login}" ] && [ "${rb_stale_row_login,,}" = "${rb_stale_login,,}" ]; then
      rb_stale_trusted_row="${rb_stale_row}"
      break
    fi
  done <<< "${rb_stale_selected}"
  if [ -z "${rb_stale_trusted_row}" ]; then
    review_blocked_stale_clear_log kept untrusted_cap_author
    return 0
  fi
  IFS=$'\x1f' read -r _ rb_stale_cap_head rb_stale_cap_support rb_stale_cap_pr_label <<< "${rb_stale_trusted_row}" || true
  rb_stale_cap_support="$(printf '%s' "${rb_stale_cap_support}" | tr 'A-F' 'a-f')"
  rb_stale_cap_head_log="none" rb_stale_cap_support_log="none"
  [[ "${rb_stale_cap_head}" =~ ^[0-9a-f]{40}$ ]] && rb_stale_cap_head_log="${rb_stale_cap_head}"
  [[ "${rb_stale_cap_support}" =~ ^[0-9a-f]{40}$ ]] && rb_stale_cap_support_log="${rb_stale_cap_support}"

  # 5. Is the newest trusted marker stale and did it label this PR?
  rb_stale_reason="$(review_blocked_stale_clear_row_reason "${rb_stale_cap_head}" "${rb_stale_cap_support}" "${rb_stale_cap_pr_label}")"
  case "${rb_stale_reason}" in
    cap_head_mismatch|cap_support_mismatch) ;;
    *)
      review_blocked_stale_clear_log kept "${rb_stale_reason}"
      return 0
      ;;
  esac

  # 6. Remove the label from the PR only. A 404 means it is already gone.
  if rb_stale_del_err="$(GH_RETRY_IDEMPOTENT=true gh_retry gh api -X DELETE \
    "repos/${rb_stale_repo}/issues/${rb_stale_pr}/labels/ai%3Areview-blocked" </dev/null 2>&1 >/dev/null)"; then
    review_blocked_stale_clear_log removed "${rb_stale_reason}"
    return 0
  fi
  if printf '%s' "${rb_stale_del_err}" | grep -qE 'HTTP 404'; then
    review_blocked_stale_clear_log removed "${rb_stale_reason}"
    return 0
  fi
  # gh_retry's last stderr line is its own `::warning::  stderr: <excerpt>`
  # wrapper around gh's error; keep only the excerpt.
  rb_stale_del_first="$(printf '%s\n' "${rb_stale_del_err}" | tr -d '\r' | awk 'NF { l = $0 } END { sub(/^::warning::[ \t]*stderr: /, "", l); print substr(l, 1, 200) }' || true)"
  echo "::warning::Could not remove stale ai:review-blocked from PR #${rb_stale_pr}: ${rb_stale_del_first//::/: :}"
  review_blocked_stale_clear_log error delete_failed
  return 0
}

# Issue #10057: list findings on paths the editor cannot change (written by
# review_apply_fixes.sh) under their own heading. Same comment, no extra API
# call; it goes before the trailing markers so the clean marker stays last.
unreachable_summary_section=""
if [ -n "${RUNTIME_DIR:-}" ] && [ -s "${RUNTIME_DIR}/review_unreachable_summary.md" ]; then
  unreachable_summary_section=$'\n\n'"$(head -c 20000 "${RUNTIME_DIR}/review_unreachable_summary.md")"
fi

if gh_retry gh api repos/${AUTOFIX_STEP_REPOSITORY}/issues/"${PR_NUMBER}"/comments -f body="$(cat "${PR_EDITOR_COMMENT_FILE}")${unreachable_summary_section}${sandbox_excluded_marker_suffix}${clean_marker_suffix}" >/dev/null; then
  echo "Posted editor summary comment."
  # Signal to the failure-comment step (line ~3269) that the
  # editor summary already landed on the PR thread. The generic
  # "AI review/autofix failed" body speculates about editor /
  # dependency / infra failure modes and would contradict the
  # success-looking summary above when the editor in fact ran
  # to completion and a later step (push, conflict resolver,
  # auto-merge config, telemetry) is what actually failed.
  # Set only on a successful post; on retry exhaustion the
  # editor summary is not visible to the reader, so the
  # generic body is the right thing to post. See PR #1476
  # follow-up for the contradictory-comment regression this
  # signal resolves.
  echo "EDITOR_SUMMARY_POSTED=true" >> "$GITHUB_ENV"
  # Only after the clean marker is actually on the PR (#9369 / #9716).
  if [ -n "${clean_marker_suffix}" ]; then
    review_blocked_stale_clear_after_clean || true
  fi
else
  echo "::warning::Unable to post editor summary comment after retries."
fi
