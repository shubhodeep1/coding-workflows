#!/usr/bin/env bash
# Body of the "Telegram review-blocked judge decision" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# well under the 430,000-byte contract-test ceiling and GitHub's 512,000-byte
# workflow file limit (#10166). The step sources this file in its own shell,
# so the step's if:, env: and continue-on-error: stay in the workflow; edit
# those there.
# GitHub does not substitute workflow expressions in a sourced script, so the
# step passes github.server_url, github.repository and github.run_id in as
# AUTOFIX_STEP_SERVER_URL, AUTOFIX_STEP_REPOSITORY and AUTOFIX_STEP_RUN_ID;
# they default to the runner's GITHUB_* values when unset.
# The inline body ran under the runner's default `bash -e` (no -u, no
# pipefail). The step-script contract requires the body to open with
# `set -euo pipefail`; the next line restores the original options so the
# body behaves exactly as it did inline.
set -euo pipefail
set +u +o pipefail
AUTOFIX_STEP_SERVER_URL="${AUTOFIX_STEP_SERVER_URL:-${GITHUB_SERVER_URL:-https://github.com}}"
AUTOFIX_STEP_REPOSITORY="${AUTOFIX_STEP_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
AUTOFIX_STEP_RUN_ID="${AUTOFIX_STEP_RUN_ID:-${GITHUB_RUN_ID:-}}"
source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true

PR_URL="${AUTOFIX_STEP_SERVER_URL}/${AUTOFIX_STEP_REPOSITORY}/pull/${PR_NUMBER}"
RUN_URL="${AUTOFIX_STEP_SERVER_URL}/${AUTOFIX_STEP_REPOSITORY}/actions/runs/${AUTOFIX_STEP_RUN_ID}"
ISSUE_NUM="$(echo "${LINKED_ISSUES_JSON:-[]}" | jq -r '.[0].number // empty' 2>/dev/null || true)"
ISSUE_LINE=""
if [ -n "${ISSUE_NUM}" ]; then
  ISSUE_LINE=$'\n'"Issue: ${AUTOFIX_STEP_SERVER_URL}/${AUTOFIX_STEP_REPOSITORY}/issues/${ISSUE_NUM}"
fi
# CRITICAL pages a human. Outcomes the pipeline finishes or retries
# on its own (judge merged/fixed/re-issued, stall recovery retries,
# conflict resolver re-dispatch) are DEBUG.
JUDGE_ALERT_LEVEL="CRITICAL"
# A draft PR is judged once it is ready for review
# (scripts/review_rb_judge.sh, judge_skip_reason=draft_pr); the
# ready_for_review event brings the judge back. Nothing for a human.
if [ "${JUDGE_HANDLED}" = "true" ] && [ "${JUDGE_ACTION}" = "skip" ] && [ "${JUDGE_SKIP_REASON}" = "draft_pr" ]; then
  echo "Review-blocked judge skipped draft PR #${PR_NUMBER}; it runs once the PR is ready for review. Suppressing alert."
  exit 0
fi
if [ "${JUDGE_HANDLED}" = "true" ]; then
  case "${JUDGE_ACTION}" in
    approval_pending)
      if [ "${JUDGE_SKIP_REASON}" = "approval_request_created" ]; then
        MSG="Review-blocked terminal recommendation needs trusted human approval"
      else
        echo "Review-blocked terminal recommendation is awaiting trusted human approval; suppressing duplicate alert."
        exit 0
      fi ;;
    security_blocked)
      MSG="Review-blocked judge held PR: open high/critical security findings after the security pass and judge fix retries were exhausted. Needs a clean audit of the head or a human decision." ;;
    security_blocked_pending)
      echo "Security findings already blocked this head; suppressing duplicate alert."
      exit 0 ;;
    security_hold)
      # The single-issue security gate held the judge's merge;
      # judge_skip_reason carries its hold_reason. A dispatched or
      # pending audit, or open follow-ups, re-run the review and
      # bring the judge back without a human: no alert. So does
      # head_unpushed: this run's own deferred commit (the conflict
      # resolver's) is pushed after the judge, and that push starts
      # the review that audits the new head (PRs #9338, #9283).
      case "${JUDGE_SKIP_REASON}" in
        security_hold_audit_dispatched|security_hold_audit_pending|security_hold_audit_throttled|security_hold_awaiting_followups|security_hold_head_unpushed)
          echo "Judge merge held while the single-issue security pass runs (${JUDGE_SKIP_REASON}); suppressing alert."
          exit 0 ;;
      esac
      HOLD_REASON="${JUDGE_SKIP_REASON#security_hold_}"
      MSG="$(printf 'Review-blocked judge approved a merge, but the single-issue security pass holds it and will not clear on its own (%s)\nNeeds a human: check the security-pass comments on the PR, then re-run the audit or decide the merge' "${HOLD_REASON:-unknown}")"
      # Issue #9942: an unaudited exhausted head pages once per head.
      # A trusted comment ending with this marker (posted below after
      # the first page) drops later rounds on the same head to DEBUG.
      # API audit (§14): this step has no comment snapshot, so the
      # newest page is read through tg_helpers.sh (as #9249), only
      # for this hold reason. Any read failure pages (unchecked).
      # Accepted bound: a marker pushed off the newest page of 100
      # comments pages once more and is then re-posted at the end.
      if [ "${JUDGE_SKIP_REASON}" = "security_hold_exhausted_without_completed_audit" ] && [[ "${AUTOFIX_FAILURE_HEAD_SHA:-}" =~ ^[0-9a-f]{40}$ ]]; then
        RB_SECURITY_HOLD_MARKER="<!-- rb-judge-security-hold:v1 head=${AUTOFIX_FAILURE_HEAD_SHA} reason=exhausted_without_completed_audit -->"
        RB_SECURITY_HOLD_REPEAT="unchecked"
        RB_SECURITY_HOLD_TG_UNAVAILABLE=""
        # Telegram cannot page (no token/chat id, SILENT): no marker is
        # ever posted, so skip the reads that could never find one (§14).
        if type tg_alert_configured >/dev/null 2>&1 && ! tg_alert_configured "${JUDGE_ALERT_LEVEL}"; then
          RB_SECURITY_HOLD_TG_UNAVAILABLE="true"
          echo "rb-judge-security-hold dedupe skipped for PR #${PR_NUMBER}: Telegram cannot send ${JUDGE_ALERT_LEVEL}"
        elif type _tg_newest_comments_json >/dev/null 2>&1 && type _tg_tracking_author_login >/dev/null 2>&1; then
          _tg_tracking_author_login
          rb_security_hold_login="${_TG_TRACKING_AUTHOR_CURRENT:-github-actions[bot]}"
          if rb_security_hold_comments="$(_tg_newest_comments_json "${PR_NUMBER}")"; then
            rb_security_hold_count="$(printf '%s' "${rb_security_hold_comments}" | jq -r --arg login "${rb_security_hold_login}" --arg marker "${RB_SECURITY_HOLD_MARKER}" \
              "${_TG_TRACKING_JQ_DEFS}"'[.[]? | select(tg_trusted($login) and (.body | sub("\\s+\\z"; "") | endswith("\n" + $marker)))] | length' 2>/dev/null)" || rb_security_hold_count=""
            if [[ "${rb_security_hold_count}" =~ ^[0-9]+$ ]]; then
              RB_SECURITY_HOLD_REPEAT="false"
              if [ "${rb_security_hold_count}" -ge 1 ]; then
                RB_SECURITY_HOLD_REPEAT="true"
                JUDGE_ALERT_LEVEL="DEBUG"
              fi
            else
              echo "::warning::rb-judge-security-hold dedupe could not parse the comments of PR #${PR_NUMBER}; paging"
            fi
          else
            echo "::warning::rb-judge-security-hold dedupe read failed for PR #${PR_NUMBER}; paging"
          fi
        else
          echo "::warning::rb-judge-security-hold dedupe unavailable (tg_helpers.sh lacks the comment helpers) for PR #${PR_NUMBER}; paging"
        fi
        echo "RB_JUDGE_SECURITY_HOLD_ALERT pr=${PR_NUMBER} head=${AUTOFIX_FAILURE_HEAD_SHA} reason=exhausted_without_completed_audit repeat=${RB_SECURITY_HOLD_REPEAT} level=${JUDGE_ALERT_LEVEL}"
      fi ;;
    merge)
      # judge_merge_status (scripts/review_rb_judge.sh) says whether
      # the merge went through. Only finished or self-retrying
      # outcomes are DEBUG; a failed merge, failing required checks,
      # ENABLE_AUTO_MERGE=false or an unknown status still pages.
      MSG="$(printf 'Review-blocked judge approved PR as-is, but the merge did not go through (status: %s)\nNeeds merge investigation' "${JUDGE_MERGE_STATUS:-unknown}")"
      if [ "${JUDGE_MERGE_STATUS}" = "merge_requested" ] || [ "${JUDGE_MERGE_STATUS}" = "already_merged" ]; then
        MSG="Review-blocked judge merged PR as-is (or enabled auto-merge)"
        JUDGE_ALERT_LEVEL="DEBUG"
      elif [ "${JUDGE_MERGE_STATUS}" = "ready_to_merge" ]; then
        MSG="Review-blocked judge approved PR as-is; linked issues moved to ai:ready-to-merge"
        JUDGE_ALERT_LEVEL="DEBUG"
      elif [ "${JUDGE_MERGE_STATUS}" = "deferred_base_moved" ]; then
        MSG="$(printf 'Review-blocked judge approved PR as-is; base moved under its files, branch update requested\nThe synchronize run re-validates automatically')"
        JUDGE_ALERT_LEVEL="DEBUG"
      elif [ "${JUDGE_MERGE_STATUS}" = "checks_not_ready" ] || [ "${JUDGE_MERGE_STATUS}" = "mergeability_pending" ]; then
        MSG="$(printf 'Review-blocked judge approved PR as-is; merge waits for checks or mergeability (%s)\nStall recovery will retry automatically' "${JUDGE_MERGE_STATUS}")"
        JUDGE_ALERT_LEVEL="DEBUG"
      elif [ "${JUDGE_MERGE_STATUS}" = "merge_conflict" ]; then
        MSG="$(printf 'Review-blocked judge approved PR as-is, but it has merge conflicts\nThe conflict resolver is re-dispatched automatically')"
        JUDGE_ALERT_LEVEL="DEBUG"
      elif [ "${JUDGE_MERGE_STATUS}" = "checks_failed" ]; then
        MSG="$(printf 'Review-blocked judge approved PR as-is, but required check-runs failed\nNeeds a CI fix before the merge')"
      elif [ "${JUDGE_MERGE_STATUS}" = "manual_merge_required" ]; then
        MSG="$(printf 'Review-blocked judge approved PR as-is; ENABLE_AUTO_MERGE=false\nManual merge required')"
      fi ;;
    fix)
      MSG="Review-blocked judge pushed fix commit"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    close_and_reissue)
      # A closed PR without a replacement issue leaves the work
      # untracked: that pages.
      MSG="$(printf 'Review-blocked judge closed PR but could not create the replacement issue\nNeeds a replacement issue for the remaining work')"
      if [ "${JUDGE_REISSUE_CREATED}" = "true" ]; then
        MSG="Review-blocked judge closed PR and created replacement issue"
        JUDGE_ALERT_LEVEL="DEBUG"
      fi ;;
    merge_with_followup)
      # Merged, and the follow-up issue was created for the
      # pipeline to implement: nothing left for a person.
      MSG="Review-blocked judge merged PR and created a follow-up issue"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    orchestrator_integration_hold)
      # #8232: the orchestrator merges its integration PR after
      # runtime validation of the current head; no person needed.
      MSG="Review-blocked judge held action on orchestrator integration PR; the orchestrator merges it after runtime validation of the current head"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    *)
      MSG="Review-blocked judge action: ${JUDGE_ACTION}" ;;
  esac
else
  case "${JUDGE_SKIP_REASON}" in
    disabled)
      MSG="$(printf 'PR review blocked — autofix exhausted (%s iterations), judge disabled\nNeeds human intervention' "${MAX_AUTOFIX_ITERATIONS}")" ;;
    not_writable)
      MSG="$(printf 'PR review blocked — autofix exhausted (%s iterations), branch not writable\nNeeds human intervention' "${MAX_AUTOFIX_ITERATIONS}")" ;;
    missing_followup_details)
      MSG="$(printf 'PR review blocked — judge selected merge_with_followup but omitted follow-up details\nNeeds judge retry or manual follow-up issue creation')" ;;
    unresolved_head_sha)
      MSG="$(printf 'PR review blocked — judge could not resolve PR head SHA\nStall recovery will retry automatically')"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    check_runs_query_failed)
      MSG="$(printf 'PR review blocked — judge could not query head check-runs\nStall recovery will retry automatically')"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    blocking_check_runs)
      MSG="$(printf 'PR review blocked — merge_with_followup approved but check-runs are still in flight\nStall recovery will retry automatically')"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    base_moved_overlap)
      # review_rb_judge.sh requested a branch update; its
      # synchronize run re-validates the PR without a person.
      MSG="$(printf 'PR review blocked — merge_with_followup approved but the base moved under its files; branch update requested\nThe synchronize run re-validates automatically')"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    base_freshness_unknown)
      # #9502: the freshness gate could not verify the base (merge
      # lease held, or the check failed). Not autofix exhaustion;
      # a stuck lease is real, so WARNING, once per head (the
      # comment step's rb-judge-base-freshness marker).
      if [ "${RB_BLOCKED_FRESHNESS_REPEAT:-}" = "true" ]; then
        echo "RB_JUDGE_FRESHNESS_ALERT pr=${PR_NUMBER} head=${AUTOFIX_FAILURE_HEAD_SHA:-unknown} repeat=true level=SUPPRESSED"
        exit 0
      fi
      # Lease guidance only when the judge exported a lease blocker
      # (merge_lease_* reasons); a queued_behind blocker is the PR
      # first in the queue, not the holder, hence "held by or
      # queued for". Other reasons (compare_failed, ...) have no lease.
      LEASE_HOLDER_LINE=""
      FRESHNESS_NEXT_LINE="The judge re-fires and retries the merge; if this persists, check the run log for the freshness reason"
      if [[ "${JUDGE_MERGE_LEASE_BLOCKER:-}" =~ ^[0-9]+$ ]]; then
        LEASE_HOLDER_LINE=" (lease held by or queued for PR #${JUDGE_MERGE_LEASE_BLOCKER})"
        FRESHNESS_NEXT_LINE="The merge retries when the lease frees; if this persists, check that PR"
      fi
      MSG="$(printf 'PR review blocked — merge_with_followup approved, but base freshness could not be verified (merge lease held or freshness check failed); merge deferred%s\n%s' "${LEASE_HOLDER_LINE}" "${FRESHNESS_NEXT_LINE}")"
      JUDGE_ALERT_LEVEL="WARNING" ;;
    stale_head)
      # A push landed during the judge call; the push re-runs review.
      MSG="$(printf 'PR review blocked — PR head moved during the judge call; judge took no action\nThe new push re-runs review automatically')"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    head_unverified)
      MSG="$(printf 'PR review blocked — judge could not read the live PR head; fix refused\nStall recovery will retry automatically')"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    sync_merge_failed)
      MSG="$(printf 'PR review blocked — merge_with_followup approved but gh pr merge failed\nNeeds retry or manual merge investigation')" ;;
    auto_merge_disabled)
      MSG="$(printf 'PR review blocked — merge_with_followup approved but ENABLE_AUTO_MERGE=false\nManual merge required before follow-up creation')" ;;
    merge_conflict)
      MSG="$(printf 'PR review blocked — PR has merge conflicts\nThe conflict resolver is re-dispatched automatically')"
      # The orchestrator's review-blocked loop and the standalone
      # conflict sweep re-dispatch the resolver; the sweep pages once
      # when a head stays conflicted (STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS).
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    mergeability_pending)
      MSG="$(printf 'PR review blocked — GitHub mergeability still computing\nStall recovery will retry automatically')"
      JUDGE_ALERT_LEVEL="DEBUG" ;;
    followup_issue_create_failed)
      MSG="$(printf 'PR review blocked — PR merged but follow-up issue creation failed\nNeeds follow-up issue creation or retry')" ;;
    llm_failed_critical_issues|json_parse_failed_critical_issues)
      MSG="$(printf 'PR review blocked — autofix exhausted (%s iterations), judge LLM failed, deterministic fallback found critical issues\nNeeds human intervention' "${MAX_AUTOFIX_ITERATIONS}")" ;;
    llm_failed*|json_parse_failed*)
      MSG="$(printf 'PR review blocked — autofix exhausted (%s iterations), judge LLM failed\nNeeds human intervention' "${MAX_AUTOFIX_ITERATIONS}")" ;;
    *)
      MSG="$(printf 'PR review blocked — autofix exhausted (%s iterations)\nNeeds human intervention' "${MAX_AUTOFIX_ITERATIONS}")" ;;
  esac
fi
# Issue #9249: a judge crash (or llm_failed*/json_parse_failed*)
# before autofix ran out is not autofix exhaustion. Same condition
# as scripts/review_autofix_step_post_review_blocked_comment.sh; a
# repeat for the same head and reason (its marker) is logged only.
JUDGE_CRASH_REASON=""
if [ "${JUDGE_HANDLED}" != "true" ] && [[ "${AUTOFIX_ITERATION:-}" =~ ^[0-9]+$ ]] && [[ "${MAX_AUTOFIX_ITERATIONS:-}" =~ ^[0-9]+$ ]] && [ "${AUTOFIX_ITERATION}" -lt "${MAX_AUTOFIX_ITERATIONS}" ]; then
  if [ "${RB_JUDGE_STATUS:-}" = "failed" ]; then
    JUDGE_CRASH_REASON="judge_crashed"
  elif [[ "${JUDGE_SKIP_REASON:-}" == llm_failed* || "${JUDGE_SKIP_REASON:-}" == json_parse_failed* ]]; then
    JUDGE_CRASH_REASON="unknown"
    [[ ! "${JUDGE_SKIP_REASON}" =~ ^[a-z0-9_]+$ ]] || JUDGE_CRASH_REASON="${JUDGE_SKIP_REASON}"
  fi
fi
if [ -n "${JUDGE_CRASH_REASON}" ]; then
  # A skip reason written before the crash may have set DEBUG.
  JUDGE_ALERT_LEVEL="CRITICAL"
  JUDGE_CRASH_DETAIL="${JUDGE_CRASH_REASON}"
  if [ "${JUDGE_CRASH_REASON}" = "judge_crashed" ] && [[ "${RB_JUDGE_EXIT_CODE:-}" =~ ^[0-9]+$ ]]; then
    JUDGE_CRASH_DETAIL="exit ${RB_JUDGE_EXIT_CODE}"
  fi
  MSG="$(printf 'PR review blocked — review-blocked judge failed (%s; %s autofix commits, max %s)\nNo autofix exhaustion: the judge itself could not complete. Needs a look at the run log' "${JUDGE_CRASH_DETAIL}" "${AUTOFIX_ITERATION}" "${MAX_AUTOFIX_ITERATIONS}")"
  # unchecked: the comment step (and its marker dedupe) did not run,
  # e.g. a security-pass-only run; that path pages as before.
  JUDGE_CRASH_REPEAT="${RB_BLOCKED_REPEAT:-unchecked}"
  [ "${JUDGE_CRASH_REPEAT}" != "true" ] || JUDGE_ALERT_LEVEL="SUPPRESSED"
  echo "RB_JUDGE_CRASH_ALERT pr=${PR_NUMBER} head=${AUTOFIX_FAILURE_HEAD_SHA:-unknown} reason=${JUDGE_CRASH_REASON} repeat=${JUDGE_CRASH_REPEAT} level=${JUDGE_ALERT_LEVEL}"
  [ "${JUDGE_CRASH_REPEAT}" != "true" ] || exit 0
fi
MSG+=$'\n'
MSG+="PR: ${PR_URL}${ISSUE_LINE}"
MSG+=$'\n'
MSG+="Run: ${RUN_URL}"
# Issue #9942: record the page only after Telegram returned a message
# id, so a failed send or POST only means the next round pages again.
if [ -n "${RB_SECURITY_HOLD_MARKER:-}" ] && [ "${RB_SECURITY_HOLD_REPEAT:-}" != "true" ]; then
  rb_security_hold_msg_id="$(tg_send_msg "${MSG}" "${JUDGE_ALERT_LEVEL}")"
  tg_store_msg_id "${PR_NUMBER}" "${rb_security_hold_msg_id}"
  if [ -n "${rb_security_hold_msg_id}" ]; then
    RB_SECURITY_HOLD_BODY="$(printf 'Security-pass hold alerted: no completed audit exists for `%s` and its retry budget is used up. Later review rounds on this head do not page again; a new push starts over (/re-run rechecks this head without paging again).\n\n%s' "${AUTOFIX_FAILURE_HEAD_SHA}" "${RB_SECURITY_HOLD_MARKER}")"
    # Empty AI_COMMENT_SIGNING_SECRET makes the gh signing shim exec
    # the real gh, so the marker is an unsigned GH_TOKEN comment, as
    # the tg_trusted + endswith read above (and tracking) expects.
    AI_COMMENT_SIGNING_SECRET="" gh api "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" -f body="${RB_SECURITY_HOLD_BODY}" >/dev/null 2>&1 \
      || echo "::warning::Could not post the rb-judge-security-hold marker on PR #${PR_NUMBER}; the next round pages again."
  elif [ "${RB_SECURITY_HOLD_TG_UNAVAILABLE:-}" = "true" ]; then
    # Telegram cannot send at this level: nothing pages, so no warning.
    echo "rb-judge-security-hold page not sent for PR #${PR_NUMBER}: Telegram cannot send ${JUDGE_ALERT_LEVEL}; no marker."
  else
    echo "::warning::rb-judge-security-hold page not confirmed by Telegram for PR #${PR_NUMBER}; no marker, the next round pages again."
  fi
else
  tg_send_tracked "${PR_NUMBER}" "${MSG}" "${JUDGE_ALERT_LEVEL}"
fi
