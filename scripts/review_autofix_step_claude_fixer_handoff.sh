#!/usr/bin/env bash
# Body of the "Hand review round to Claude session (Claude-fixer mode)" step
# in .github/workflows/review_autofix.yml. The step sources this file in its
# own shell, so the step's if:, env: and continue-on-error: stay in the
# workflow; edit those there.
#
# Claude-fixer mode (gate output `claude_fixer`, every PR-backed `claude/*`
# head): the reviewer panel runs exactly as for every PR, but the GPT editor,
# the conflict resolver and the push / re-trigger tail do not. The Claude
# session that owns the PR fixes the findings instead (the /implement-plan-claude
# stage session, or under CLAUDE.md §26 the session that pushed the PR, a
# `/fix-claude-pr` session, or the catch-all sweep's session), and this step
# is the hand-off:
#
#   * pre-review content conflict (AUTOFIX_PRE_REVIEW_RESOLVE=true)
#       -> one comment carrying `kind=conflict` and the unmerged paths;
#   * reviewer findings or failing check runs
#       -> the consensus ledger through scripts/post_review_comment.sh
#          (chunked PR comments), then one comment carrying `kind=findings`;
#   * nothing at all (no finding in any ledger block, fresh ready check snapshot)
#       -> no comment; CLAUDE_FIXER_ZERO_FINDINGS=true is exported so the
#          workflow's own auto-merge step runs, as it does when the editor
#          finds nothing to commit. A reviewer slot that failed (retry limit
#          reached, killed, timed out) is a missing vote, not a finding: the
#          ledger is still clean when at least CLAUDE_FIXER_MIN_CLEAN_REVIEWERS
#          (default 5) reviewers completed clean, each proven by its own
#          runner output (NONE, no finding or task gap; issue #5114), and
#          the ledger has a block for every reviewer slot the runner ran
#          (issue #5297), logged as CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS (see
#          the clean-ledger check).
#
# The hand-off marker is
#   <!-- ai:claude-fixer-handoff:v1 kind=<findings|conflict> head=<sha> round=<n> -->
# and .claude/scripts/check_in_status.py reads it (plus the session's
# `ai:claude-fixer-verdict:v1` reply) to start the next stage session or to
# hand the round back to the session that pushed the PR (--hand-back).
#
# Inputs (environment): PR_NUMBER, GH_TOKEN, GITHUB_REPOSITORY, HEAD_SHA,
# HEAD_REF, CLAUDE_FIXER_ROUND_INDEX (consecutive [ai-autofix] /
# [claude-autofix] commits on the head; round = index + 1),
# AUTOFIX_PRE_REVIEW_RESOLVE,
# AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED, REVIEWER_CONSENSUS_FILE,
# PR_CHECK_RUNS_CONTEXT_FILE, SUPPORT_SCRIPTS_DIR, GITHUB_RUN_ID,
# GITHUB_SERVER_URL, RUNTIME_DIR, PREVIOUS_REVIEWS_DIR (the reviewer
# runner's status_review_<slug>.txt and review_<slug>.txt files, read for
# both failed and clean blocks when a slot failed, and listed as the reviewer
# roster the ledger must cover),
# CLAUDE_FIXER_MIN_CLEAN_REVIEWERS (default 5).
# API calls: on a clean candidate, the existing check-run collector refreshes
# its paginated check-runs GET; on findings, the ledger chunks from
# post_review_comment.sh and one hand-off comment are posted.
set -euo pipefail

# shellcheck source=/dev/null
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || ! [[ "${HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::Claude-fixer hand-off needs a numeric PR_NUMBER and a 40-hex HEAD_SHA (PR_NUMBER=${PR_NUMBER:-} HEAD_SHA=${HEAD_SHA:-})."
  exit 1
fi
claude_fixer_round_index="${CLAUDE_FIXER_ROUND_INDEX:-0}"
[[ "${claude_fixer_round_index}" =~ ^[0-9]+$ ]] || claude_fixer_round_index=0
claude_fixer_round="$((claude_fixer_round_index + 1))"
claude_fixer_run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID:-0}"
claude_fixer_body_file="$(mktemp "${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_handoff.XXXXXX")"

claude_fixer_post_marker_comment()
{
  local payload_file
  payload_file="$(mktemp "${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_handoff_payload.XXXXXX")"
  jq -n --rawfile body "${claude_fixer_body_file}" '{body: $body}' > "${payload_file}"
  gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" --input "${payload_file}" >/dev/null
  rm -f "${payload_file}"
}

if [ "${AUTOFIX_PRE_REVIEW_RESOLVE:-false}" = "true" ]; then
  if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
    echo "CLAUDE_FIXER_VERIFICATION_FAILED=true" >> "$GITHUB_ENV"
  fi
  {
    echo "## Review round ${claude_fixer_round}: merge conflict, handed to the Claude session"
    echo
    echo "The head \`${HEAD_SHA}\` conflicts with its base branch, so the reviewer panel did not run ([workflow run](${claude_fixer_run_url}))."
    echo "Claude-fixer mode skips the GPT conflict resolver: the Claude session that owns this PR merges the base branch into \`${HEAD_REF:-the head branch}\`, resolves the conflict keeping both sides' intent, commits it as \`[claude-merge-resolve] <summary>\`, and pushes. The push starts the next review round."
    if [ -n "${AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED:-}" ]; then
      echo
      echo "Unmerged paths: \`${AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED//,/\`, \`}\`"
    fi
    echo
    echo "<!-- ai:claude-fixer-handoff:v1 kind=conflict head=${HEAD_SHA} round=${claude_fixer_round} -->"
    if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
      echo "<!-- ai:claude-fixer-verification:v1 head=${HEAD_SHA} result=unresolved -->"
    fi
  } > "${claude_fixer_body_file}"
  claude_fixer_post_marker_comment
  echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=conflict"
  exit 0
fi

# Count ledger entries: every "- " bullet inside a CONSENSUS FINDINGS,
# CONSENSUS TASK GAPS or FINDINGS FROM <slug> block. A missing or empty
# ledger fails closed (the round is handed off, never auto-merged).
claude_fixer_ledger_state="missing"
claude_fixer_finding_count=0
claude_fixer_ledger_digest=""
if [[ "${REVIEWERS_SUCCESSFUL:-}" =~ ^[1-9][0-9]*$ ]] \
  && [ -s "${REVIEWER_CONSENSUS_FILE:-}" ] \
  && grep -Fxq '=== CONSENSUS FINDINGS ===' "${REVIEWER_CONSENSUS_FILE}" \
  && grep -Fxq '=== END CONSENSUS FINDINGS ===' "${REVIEWER_CONSENSUS_FILE}" \
  && grep -Fxq '=== CONSENSUS TASK GAPS ===' "${REVIEWER_CONSENSUS_FILE}" \
  && grep -Fxq '=== END CONSENSUS TASK GAPS ===' "${REVIEWER_CONSENSUS_FILE}" \
  && grep -Eq '^=== FINDINGS FROM .+ ===$' "${REVIEWER_CONSENSUS_FILE}"; then
  claude_fixer_ledger_state="ok"
  claude_fixer_ledger_digest="$(sha256sum "${REVIEWER_CONSENSUS_FILE}" | cut -d ' ' -f 1)"
  claude_fixer_finding_count="$(awk '
    /^=== (CONSENSUS FINDINGS|CONSENSUS TASK GAPS|FINDINGS FROM .*) ===$/ { in_block = 1; next }
    /^=== END / { in_block = 0; next }
    in_block && /^- / { total++ }
    END { printf "%d\n", total + 0 }
  ' "${REVIEWER_CONSENSUS_FILE}")"
fi
claude_fixer_failed_checks=""
if [ -s "${PR_CHECK_RUNS_CONTEXT_FILE:-}" ]; then
  claude_fixer_failed_checks="$(sed -n 's/^failed\[[0-9]*\]\.name: //p' "${PR_CHECK_RUNS_CONTEXT_FILE}" | paste -sd, - || true)"
fi

# Clean-ledger check. Both consensus blocks must be exactly their empty line,
# and each "=== FINDINGS FROM <slug> ===" block is classified by its single
# line:
#   clean   - "(No findings reported.)";
#   failed  - the terminal retry-exhaustion line scripts/review_run_reviewers.sh
#             writes for a slot that failed (retry limit reached, killed, timed
#             out), naming the block's own model (slug = model with '/', '.'
#             and ':' mapped to '_', the runner's safe_name);
#   finding - anything else.
# A failed slot is a missing vote: never a finding and never a clean vote
# (issue #4835). Without a failed block the rule is unchanged (every block
# clean). With one, the ledger is clean only when no block is a finding, the
# runner's status file ${PREVIOUS_REVIEWS_DIR}/status_review_<slug>.txt reads
# "failed" for every failed block and "success" for every clean one, the
# runner's output file ${PREVIOUS_REVIEWS_DIR}/review_<slug>.txt is exactly the
# failed block's line (the status file also reads "failed" after a
# non-retryable error, whose output line differs; issue #4885), every clean
# block's review_<slug>.txt is an unambiguous no-findings result (below;
# "success" only means the reviewer finished, and the ledger can mislabel a
# reviewer that reported a finding; issue #5114), no slug repeats, every
# slot the runner wrote a status_review_<slug>.txt or review_<slug>.txt for
# has its own block (the ledger can leave a reviewer out entirely; issue
# #5297), and at least CLAUDE_FIXER_MIN_CLEAN_REVIEWERS (default 5) clean
# reviewers remain.
# The ledger is model output over reviewer output, so its text alone never
# proves a failure or a clean vote; the status and output files are written
# by the runner only.
#
# A runner output is an unambiguous no-findings result when it is readable
# and not empty, at least one line is exactly NONE (the reviewer contract's
# "found nothing", prompts/_nag_reminders.txt), no line is a finding or
# task-gap field (File:, Line or code reference:, Problem:, Why it fails at
# runtime:, Requirement:, Expected change site:, Evidence of absence:,
# SEVERITY:, ISSUE_CONFIDENCE:, markdown list and emphasis markers ignored),
# and, when any lens heading of prompts/review-reviewer-checklist.txt appears
# (the same markdown markers and list numbering ignored, so a numbered or
# bulleted heading still needs its NONE), all nine appear and the next
# non-blank line after each is exactly NONE. Prose around those verdicts is
# allowed.
# tests/test_review_autofix_claude_fixer_mode.py pins this list to the prompt.
claude_fixer_checklist_lens_headings="SECURITY & INPUT VALIDATION|CORRECTNESS & LOGIC|CONCURRENCY / RACES / IDEMPOTENCY|ERROR PATHS & EDGE CASES|PERFORMANCE & RESOURCE USE|INDEX-CONTRACT / DB RULES|NAMING / BACKWARD COMPATIBILITY|IMPLICIT-EXECUTION & TRUST-BOUNDARY RISKS|TASK COMPLETENESS / INTENT GAPS"

# Prints "clean" when the runner output file is an unambiguous no-findings
# result, else the reason it is not. Always returns 0. The text is fed to awk
# through a here-string, not a pipe: under pipefail an early reader exit can
# fail the writer, and set -e would then end the step.
claude_fixer_runner_output_state()
{
  local claude_fixer_output_file="$1"
  local claude_fixer_output_text=""
  if [ ! -f "${claude_fixer_output_file}" ]; then
    echo "missing"
    return 0
  fi
  if ! claude_fixer_output_text="$(cat "${claude_fixer_output_file}" 2>/dev/null)"; then
    echo "unreadable"
    return 0
  fi
  if [ -z "${claude_fixer_output_text//[[:space:]]/}" ]; then
    echo "empty"
    return 0
  fi
  awk -v headings="${claude_fixer_checklist_lens_headings}" '
    function trim(s)
    {
      sub(/^[ \t\r]+/, "", s)
      sub(/[ \t\r]+$/, "", s)
      return s
    }
    BEGIN {
      heading_count = split(headings, heading_list, "|")
      for (i = 1; i <= heading_count; i++) is_heading[heading_list[i]] = 1
    }
    {
      line = trim($0)
      if (line == "") next
      if (expect_none) {
        if (line != "NONE") lens_without_none = 1
        expect_none = 0
      }
      if (line == "NONE") none_seen = 1
      field = tolower(line)
      sub(/^[-*>#_` \t]+/, "", field)
      sub(/^[0-9]+[.)][ \t]*/, "", field)
      sub(/^[-*>#_` \t]+/, "", field)
      if (field ~ /^(file|line or code reference|problem|why it fails at runtime|requirement|expected change site|evidence of absence|severity|issue_confidence)[*_` \t]*:/) finding = 1
      heading = toupper(line)
      sub(/^[-*>#_` \t]+/, "", heading)
      sub(/^[0-9]+[.)][ \t]*/, "", heading)
      sub(/^[-*>#_` \t]+/, "", heading)
      sub(/[*_`: \t]+$/, "", heading)
      if (heading in is_heading) {
        if (!(heading in heading_seen)) {
          heading_seen[heading] = 1
          distinct_headings++
        }
        expect_none = 1
      }
    }
    END {
      if (expect_none) lens_without_none = 1
      if (finding) print "reports a finding or task gap"
      else if (!none_seen) print "has no NONE verdict"
      else if (distinct_headings > 0 && (distinct_headings < heading_count || lens_without_none)) print "leaves a checklist lens without a NONE verdict"
      else print "clean"
    }
  ' <<< "${claude_fixer_output_text}"
  return 0
}

claude_fixer_clean_ledger="false"
claude_fixer_failed_slots=""
claude_fixer_clean_reviewers=0
claude_fixer_min_clean_reviewers="${CLAUDE_FIXER_MIN_CLEAN_REVIEWERS:-5}"
if ! [[ "${claude_fixer_min_clean_reviewers}" =~ ^[1-9][0-9]*$ ]]; then
  echo "::warning::Invalid CLAUDE_FIXER_MIN_CLEAN_REVIEWERS='${claude_fixer_min_clean_reviewers}' (need an integer of at least 1); using 5."
  claude_fixer_min_clean_reviewers=5
fi
claude_fixer_ledger_blocks=""
if [ "${claude_fixer_ledger_state}" = "ok" ] && [ "${claude_fixer_finding_count}" -eq 0 ] \
  && claude_fixer_ledger_blocks="$(awk '
    function close_block(  model)
    {
      if (entries != 1) {
        invalid = 1
      } else if (kind == "consensus") {
        if (first != expected) invalid = 1
      } else if (first == "(No findings reported.)") {
        print "clean " slug
      } else if (first ~ /^Reviewer [^ ]+ failed after (reaching the slot retryable-failure limit \([0-9]+\)|retryable failure recovery was exhausted|[0-9]+ attempts)\.$/) {
        model = first
        sub(/^Reviewer /, "", model)
        sub(/ failed after .*$/, "", model)
        gsub(/[\/.:]/, "_", model)
        if (model == slug) print "failed " slug " " first
        else invalid = 1
      } else {
        invalid = 1
      }
      blocks++
      block = 0
    }
    /^=== CONSENSUS FINDINGS ===$/ { kind = "consensus"; expected = "(No findings reported.)"; block = 1; entries = 0; next }
    /^=== CONSENSUS TASK GAPS ===$/ { kind = "consensus"; expected = "(No task gaps reported.)"; block = 1; entries = 0; next }
    /^=== FINDINGS FROM .+ ===$/ {
      kind = "reviewer"
      slug = $0
      sub(/^=== FINDINGS FROM /, "", slug)
      sub(/ ===$/, "", slug)
      block = 1
      entries = 0
      next
    }
    /^=== END / { if (block) close_block(); next }
    block && NF { entries++; if (entries == 1) first = $0 }
    END { if (invalid || block || blocks < 3) exit 1 }
  ' "${REVIEWER_CONSENSUS_FILE}")"; then
  # Pattern matches, not `printf | grep -q`: under pipefail an early grep exit
  # can SIGPIPE the writer and read as "no failed block", which fails open.
  if [[ $'\n'"${claude_fixer_ledger_blocks}" != *$'\n'"failed "* ]]; then
    claude_fixer_clean_ledger="true"
  else
    claude_fixer_failed_slots_verified="true"
    # A failed record is "failed <slug> <line>" (the slug has no space); a
    # clean one is "clean <slug>" with the slug as written in the ledger.
    if [ -n "$(printf '%s\n' "${claude_fixer_ledger_blocks}" | awk '$1 == "failed" { print $2; next } { sub(/^[a-z]* /, ""); print }' | sort | uniq -d)" ]; then
      echo "::warning::Claude-fixer ledger repeats a reviewer block; the ledger is not clean."
      claude_fixer_failed_slots_verified="false"
    fi
    while read -r claude_fixer_block_kind claude_fixer_block_rest; do
      [ -n "${claude_fixer_block_kind}" ] || continue
      claude_fixer_block_slug="${claude_fixer_block_rest}"
      claude_fixer_block_line=""
      if [ "${claude_fixer_block_kind}" = "failed" ]; then
        claude_fixer_block_slug="${claude_fixer_block_rest%% *}"
        claude_fixer_block_line="${claude_fixer_block_rest#* }"
      fi
      claude_fixer_block_status=""
      if [[ "${claude_fixer_block_slug}" =~ ^[A-Za-z0-9_-]+$ ]] && [ -n "${PREVIOUS_REVIEWS_DIR:-}" ] \
        && [ -f "${PREVIOUS_REVIEWS_DIR}/status_review_${claude_fixer_block_slug}.txt" ]; then
        claude_fixer_block_status="$(cat "${PREVIOUS_REVIEWS_DIR}/status_review_${claude_fixer_block_slug}.txt" 2>/dev/null || true)"
      fi
      case "${claude_fixer_block_kind}:${claude_fixer_block_status}" in
        clean:success)
          # A success status only says the reviewer finished. The clean vote
          # comes from the runner's own review_<slug>.txt, never from the
          # ledger's text (issue #5114). The guards repeat the status read's.
          claude_fixer_block_output_state="missing"
          if [[ "${claude_fixer_block_slug}" =~ ^[A-Za-z0-9_-]+$ ]] && [ -n "${PREVIOUS_REVIEWS_DIR:-}" ]; then
            claude_fixer_block_output_state="$(claude_fixer_runner_output_state "${PREVIOUS_REVIEWS_DIR}/review_${claude_fixer_block_slug}.txt")"
          fi
          if [ "${claude_fixer_block_output_state}" = "clean" ]; then
            claude_fixer_clean_reviewers="$((claude_fixer_clean_reviewers + 1))"
          else
            echo "::warning::Claude-fixer ledger block '${claude_fixer_block_slug}' reads clean but the reviewer runner's review_${claude_fixer_block_slug}.txt is not an unambiguous no-findings result (${claude_fixer_block_output_state}); the ledger is not clean."
            claude_fixer_failed_slots_verified="false"
          fi
          ;;
        failed:failed)
          # The status file reads `failed` for non-retryable errors too, so it
          # does not prove the failure class (issue #4885). The runner writes
          # the slot's exact terminal line to review_<slug>.txt; the ledger
          # line must be that line, or the ledger is not clean.
          # The status read above already required these guards; they are
          # repeated so this read stays safe on its own.
          claude_fixer_block_runner_line=""
          claude_fixer_block_runner_state="missing"
          if [[ "${claude_fixer_block_slug}" =~ ^[A-Za-z0-9_-]+$ ]] && [ -n "${PREVIOUS_REVIEWS_DIR:-}" ] \
            && [ -f "${PREVIOUS_REVIEWS_DIR}/review_${claude_fixer_block_slug}.txt" ]; then
            if claude_fixer_block_runner_line="$(cat "${PREVIOUS_REVIEWS_DIR}/review_${claude_fixer_block_slug}.txt" 2>/dev/null)"; then
              if [ -n "${claude_fixer_block_runner_line}" ]; then
                claude_fixer_block_runner_state="different"
              else
                claude_fixer_block_runner_state="empty"
              fi
            else
              claude_fixer_block_runner_line=""
              claude_fixer_block_runner_state="unreadable"
            fi
          fi
          if [ -n "${claude_fixer_block_line}" ] && [ "${claude_fixer_block_runner_line}" = "${claude_fixer_block_line}" ]; then
            claude_fixer_failed_slots="${claude_fixer_failed_slots:+${claude_fixer_failed_slots},}${claude_fixer_block_slug}"
          else
            echo "::warning::Claude-fixer ledger block '${claude_fixer_block_slug}' reads failed but the reviewer runner's review_${claude_fixer_block_slug}.txt is ${claude_fixer_block_runner_state}, not that failure line; the ledger is not clean."
            claude_fixer_failed_slots_verified="false"
          fi
          ;;
        *)
          echo "::warning::Claude-fixer ledger block '${claude_fixer_block_slug}' reads ${claude_fixer_block_kind} but its reviewer status is '${claude_fixer_block_status:-missing}'; the ledger is not clean."
          claude_fixer_failed_slots_verified="false"
          ;;
      esac
    done <<< "${claude_fixer_ledger_blocks}"
    # Roster check (issue #5297). The ledger is model output and can leave a
    # reviewer out entirely, and the loop above only sees the blocks it
    # contains. Every slot the runner wrote a status_review_<slug>.txt or
    # review_<slug>.txt for (the files the summariser reads) must have its own
    # block, or the ledger is not clean. Pattern matches, not `printf | grep -q`,
    # for the pipefail reason above.
    claude_fixer_roster_ledger_slugs="$(printf '%s\n' "${claude_fixer_ledger_blocks}" | awk '$1 == "failed" { print $2; next } { sub(/^[a-z]* /, ""); print }')"
    claude_fixer_roster_slugs=""
    claude_fixer_roster_size=0
    if [ -n "${PREVIOUS_REVIEWS_DIR:-}" ] && [ -d "${PREVIOUS_REVIEWS_DIR}" ]; then
      for claude_fixer_roster_file in "${PREVIOUS_REVIEWS_DIR}"/status_review_*.txt "${PREVIOUS_REVIEWS_DIR}"/review_*.txt; do
        [ -f "${claude_fixer_roster_file}" ] || continue
        claude_fixer_roster_slug="${claude_fixer_roster_file##*/}"
        claude_fixer_roster_slug="${claude_fixer_roster_slug#status_}"
        claude_fixer_roster_slug="${claude_fixer_roster_slug#review_}"
        claude_fixer_roster_slug="${claude_fixer_roster_slug%.txt}"
        if ! [[ "${claude_fixer_roster_slug}" =~ ^[A-Za-z0-9_-]+$ ]]; then
          echo "::warning::Claude-fixer reviewer roster holds a file with an unexpected slot name (${claude_fixer_roster_file##*/}); the ledger is not clean."
          claude_fixer_failed_slots_verified="false"
          continue
        fi
        [[ $'\n'"${claude_fixer_roster_slugs}" == *$'\n'"${claude_fixer_roster_slug}"$'\n'* ]] && continue
        claude_fixer_roster_slugs="${claude_fixer_roster_slugs}${claude_fixer_roster_slug}"$'\n'
        claude_fixer_roster_size="$((claude_fixer_roster_size + 1))"
        if [[ $'\n'"${claude_fixer_roster_ledger_slugs}"$'\n' != *$'\n'"${claude_fixer_roster_slug}"$'\n'* ]]; then
          echo "::warning::Claude-fixer ledger omits reviewer '${claude_fixer_roster_slug}' that the reviewer runner ran; the ledger is not clean."
          claude_fixer_failed_slots_verified="false"
        fi
      done
    fi
    if [ "${claude_fixer_roster_size}" -eq 0 ]; then
      echo "::warning::Claude-fixer reviewer roster is empty (no status_review_<slug>.txt or review_<slug>.txt in PREVIOUS_REVIEWS_DIR); the ledger is not clean."
      claude_fixer_failed_slots_verified="false"
    fi
    if [ "${claude_fixer_failed_slots_verified}" = "true" ] \
      && [ "${claude_fixer_clean_reviewers}" -ge "${claude_fixer_min_clean_reviewers}" ]; then
      claude_fixer_clean_ledger="true"
      echo "CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} failed_slots=${claude_fixer_failed_slots} clean_reviewers=${claude_fixer_clean_reviewers} min=${claude_fixer_min_clean_reviewers}"
    fi
  fi
fi

if [ "${claude_fixer_clean_ledger}" = "true" ] && [ -z "${claude_fixer_failed_checks}" ]; then
  # Reviewers can run for hours after the first snapshot. Refresh using the
  # existing collector, not the earlier reviewer context, before authorizing
  # a merge; a disabled, timed-out, or malformed snapshot fails closed.
  claude_fixer_checks_refreshed="false"
  if [ -f "${SUPPORT_SCRIPTS_DIR}/collect_pr_check_runs_context.py" ]; then
    SELF_RUN_ID="${GITHUB_RUN_ID:-}" CHECK_RUNS_EXCLUDE_SELF_FROM_CONTEXT=true PYTHONDONTWRITEBYTECODE=1 \
      python3 "${SUPPORT_SCRIPTS_DIR}/collect_pr_check_runs_context.py"
    claude_fixer_checks_refreshed="true"
  fi
  if [ "${claude_fixer_checks_refreshed}" = "true" ] \
    && [ -s "${PR_CHECK_RUNS_CONTEXT_FILE:-}" ] \
    && [ "$(sed -n '1p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "PR_CHECK_RUNS_CONTEXT" ] \
    && [ "$(sed -n '2p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "head_sha: ${HEAD_SHA}" ] \
    && [ "$(sed -n '3p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "collection_status: ready" ] \
    && grep -Eq '^total_check_runs: [1-9][0-9]*$' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && grep -Fxq 'failed_count: 0' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && grep -Fxq 'incomplete_count: 0' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && ! grep -Eq '^(failed|incomplete)\[[0-9]+\]\.' "${PR_CHECK_RUNS_CONTEXT_FILE}"; then
    echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=none findings=0 failed_checks=0 action=auto_merge"
    echo "CLAUDE_FIXER_ZERO_FINDINGS=true" >> "$GITHUB_ENV"
    exit 0
  fi
  echo "::warning::Claude-fixer clean review has no fresh ready same-head check-run snapshot; auto-merge disabled."
fi

if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
  # A same-head verdict gets one independent review. Unresolved findings or
  # unavailable verification require intervention, not another verdict loop.
  echo "CLAUDE_FIXER_VERIFICATION_FAILED=true" >> "$GITHUB_ENV"
fi

if [ "${claude_fixer_ledger_state}" = "ok" ]; then
  REPOSITORY="${GITHUB_REPOSITORY}" bash "${SUPPORT_SCRIPTS_DIR}/post_review_comment.sh"
fi

{
  echo "## Review round ${claude_fixer_round}: findings handed to the Claude session"
  echo
  echo "Reviewed head: \`${HEAD_SHA}\` ([workflow run](${claude_fixer_run_url}))."
  if [ "${claude_fixer_ledger_state}" = "ok" ]; then
    echo "Reviewer ledger entries: ${claude_fixer_finding_count} (posted above)."
  else
    echo "The consensus ledger was not produced; the per-reviewer outputs are in the run's \`reviewer-logs-*\` artifact."
  fi
  if [ -n "${claude_fixer_failed_slots}" ]; then
    echo "Reviewer slots that failed (missing votes, not findings): \`${claude_fixer_failed_slots//,/\`, \`}\`. Completed clean reviewers: ${claude_fixer_clean_reviewers} (\`CLAUDE_FIXER_MIN_CLEAN_REVIEWERS\` requires ${claude_fixer_min_clean_reviewers})."
  fi
  if [ -n "${claude_fixer_failed_checks}" ]; then
    echo "Failing check runs on this head: \`${claude_fixer_failed_checks//,/\`, \`}\`."
  fi
  if [ -n "${claude_fixer_ledger_digest}" ]; then
    echo "Ledger SHA-256: \`${claude_fixer_ledger_digest}\`."
  fi
  echo
  echo "Claude-fixer mode: the GPT editor did not run. The Claude session that owns this PR judges each finding against the code, then either"
  echo "- fixes the valid ones in **one** commit whose subject starts with \`[claude-autofix]\` and pushes it (the push starts the next review round; the commits count toward \`MAX_AUTOFIX_ITERATIONS\` like \`[ai-autofix]\` ones), or"
  echo "- when nothing valid is left, has the dedicated fixer bot (not GH_PAT or a human) post a separate comment listing rejections and ending with both \`ai:claude-fixer-verdict:v1\` and \`ai:claude-fixer-verdict:v2\` markers for this head, round and ledger SHA-256, then dispatches this workflow with \`claude_fixer_converged_head=${HEAD_SHA}\`. That dispatch re-runs the reviewers; only a clean review with a fresh ready check snapshot can enable auto-merge. If the bot cannot post, leave the PR blocked."
  echo
  echo "<!-- ai:claude-fixer-handoff:v1 kind=findings head=${HEAD_SHA} round=${claude_fixer_round} -->"
  if [ -n "${claude_fixer_ledger_digest}" ]; then
    echo "<!-- ai:claude-fixer-handoff:v2 head=${HEAD_SHA} round=${claude_fixer_round} ledger=${claude_fixer_ledger_digest} -->"
  fi
  if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
    echo "<!-- ai:claude-fixer-verification:v1 head=${HEAD_SHA} result=unresolved -->"
  fi
} > "${claude_fixer_body_file}"
claude_fixer_post_marker_comment
echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=findings findings=${claude_fixer_finding_count} ledger=${claude_fixer_ledger_state} failed_checks=${claude_fixer_failed_checks:-none}"
