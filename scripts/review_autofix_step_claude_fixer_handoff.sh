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
#   * pre-review conflict in a protected `.claude/**` path
#     (CLAUDE_FIXER_PROTECTED_CONFLICT=true, set by the merge-topology gate;
#     plan D14) -> no comment here: the GPT resolver tail runs instead. The
#     step "Hand unresolved protected conflict to Claude session" sources
#     this file again with CLAUDE_FIXER_PROTECTED_CONFLICT_FALLBACK=true when
#     the resolver failed or escalated, and the conflict hand-off then names
#     the protected paths so the Claude session stops at once;
#   * reviewer findings or failing check runs
#       -> the consensus ledger through scripts/post_review_comment.sh
#          (chunked PR comments), then one comment carrying `kind=findings`;
#   * nothing at all (no finding in any ledger block, fresh ready check snapshot)
#       -> no comment; CLAUDE_FIXER_ZERO_FINDINGS=true is exported so the
#          workflow's own auto-merge step runs, as it does when the editor
#          finds nothing to commit;
#   * nothing found, no check failed, but checks still running after the
#     refresh wait (CLAUDE_FIXER_CHECKS_PENDING_ENABLED, default true)
#       -> no hand-off: one "clean, waiting for checks" comment per head
#          (updated in place) carrying
#          `<!-- ai:claude-fixer-checks-pending:v1 head=<sha> round=<n> run=<run_id> -->`.
#          The review sweep's next dispatch takes the gate's merge-check path
#          (scripts/review_autofix_step_claude_fixer_merge_check.sh) instead
#          of re-running the reviewers.
#
# Sticky judge rulings (CLAUDE_FIXER_JUDGE_ENABLED, default true): before the
# ledger is counted, every finding that matches an `invalid` ruling of an
# earlier GPT judge run on this PR (same file, start line within +/-3; rulings
# read back from verified judge evidence, newest 3 runs) moves into a
# `=== NON-BLOCKING FINDINGS ===` block that is not counted, so a round with
# only such findings is clean (scripts/review_claude_fixer_judge.py sticky).
#
# Every outcome also writes the round's evidence (outcome, head, round,
# ledger digest, finding count, failing checks) through
# scripts/review_claude_fixer_evidence.py into CLAUDE_FIXER_EVIDENCE_DIR
# (default ${RUNTIME_DIR}/claude_fixer_evidence); the workflow uploads it as
# the `claude-fixer-evidence-<run_id>-<run_attempt>` artifact, and later
# decisions trust only that artifact, never a comment marker.
#
# The hand-off marker is
#   <!-- ai:claude-fixer-handoff:v1 kind=<findings|conflict> head=<sha> round=<n> -->
# and .claude/scripts/check_in_status.py reads it (plus the session's
# `ai:claude-fixer-verdict:v1` reply) to start the next stage session or to
# hand the round back to the session that pushed the PR (--hand-back).
#
# Reviewer-slot infrastructure failures (failed, skipped_budget, skipped_unmapped,
# skipped_open) do not block a clean round. The summariser drops every slot whose
# status file is not `success`, and the round is clean when at least half of the
# ACTIVE panel (rounded up) returned `success` and the ledger holds no finding
# and no task gap. Below that floor the round is handed off as before. The
# active panel size is the larger of the `status_review_*.txt` count in
# PREVIOUS_REVIEWS_DIR and the line count of ${RUNTIME_DIR}/reviewer_active_models.txt;
# when neither is available the floor is not applied (today's behaviour).
# A pass whose only non-success slot is skipped_budget writes no ledger:
# run_reviewer_pass requests a partial finalize before the summariser, so this
# step sees ledger=missing and fails closed (kind=findings), as before. The
# partial-finalize continuation does not run in Claude-fixer mode (operator
# Q47: A keeps that path unchanged).
#
# Inputs (environment): PR_NUMBER, GH_TOKEN, GITHUB_REPOSITORY, HEAD_SHA,
# HEAD_REF, CLAUDE_FIXER_ROUND_INDEX (consecutive [ai-autofix] /
# [claude-autofix] commits on the head; round = index + 1),
# AUTOFIX_PRE_REVIEW_RESOLVE,
# AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED, REVIEWER_CONSENSUS_FILE,
# PR_CHECK_RUNS_CONTEXT_FILE, SUPPORT_SCRIPTS_DIR, GITHUB_RUN_ID,
# GITHUB_SERVER_URL, RUNTIME_DIR, REVIEWERS_SUCCESSFUL, and optionally
# PREVIOUS_REVIEWS_DIR (panel floor), CLAUDE_FIXER_CHECKS_PENDING_ENABLED
# (default true), CLAUDE_FIXER_EVIDENCE_DIR (optional),
# CLAUDE_FIXER_JUDGE_ENABLED (default true), PR_ISSUE_COMMENTS_FILE (the
# comments "Collect PR metadata" fetched), DEFAULT_BRANCH.
# API calls: on a clean candidate, the existing check-run collector refreshes
# its paginated check-runs GET; on findings, the ledger chunks from
# post_review_comment.sh and one hand-off comment are posted; on checks
# pending, one GET /user, one paginated GET of the PR's comments (to update
# this head's checks-pending comment in place) and one POST or PATCH; sticky
# rulings add 3 REST reads per prior judge run (at most 3 runs, none when the
# PR has no judge verdict).
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

claude_fixer_evidence_dir="${CLAUDE_FIXER_EVIDENCE_DIR:-${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_evidence}"

# Write this round's evidence.json (and a copy of the ledger) for the
# evidence artifact. Returns non-zero when the helper is missing or fails;
# callers that depend on the evidence (checks pending) fall back to the
# hand-off, the others only warn.
claude_fixer_write_evidence()
{
  local evidence_outcome="$1"
  local evidence_ledger_args=()
  if [ ! -f "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_evidence.py" ]; then
    echo "::warning::review_claude_fixer_evidence.py is not staged; no Claude-fixer evidence for outcome ${evidence_outcome}."
    return 1
  fi
  if [ -s "${REVIEWER_CONSENSUS_FILE:-}" ]; then
    evidence_ledger_args=(--ledger-file "${REVIEWER_CONSENSUS_FILE}")
  fi
  if ! PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_evidence.py" write \
    --out-dir "${claude_fixer_evidence_dir}" \
    --pr "${PR_NUMBER}" \
    --head "${HEAD_SHA}" \
    --round "${claude_fixer_round}" \
    --outcome "${evidence_outcome}" \
    --ledger-sha256 "${claude_fixer_ledger_digest:-}" \
    --finding-count "${claude_fixer_finding_count:-0}" \
    --failed-checks "${claude_fixer_failed_checks:-}" \
    "${evidence_ledger_args[@]}" >/dev/null; then
    echo "::warning::Could not write Claude-fixer evidence for outcome ${evidence_outcome}."
    return 1
  fi
  return 0
}

# Post this head's "clean, waiting for checks" comment, or update it in
# place when it is already the newest Claude-fixer marker comment for the
# head (a newer hand-off on the same head gets a fresh comment instead, so
# the gate's newest-marker rule sees the current state).
# §15 audit: PR_ISSUE_COMMENTS_FILE ("Collect PR metadata") is read before
# the reviewers run, so it can miss a marker posted since, and no earlier
# call in this step resolves the comment account; hence one `GET /user` and
# one paginated comments read, issued only on the checks-pending path.
claude_fixer_upsert_checks_pending_comment()
{
  local marker_login="" existing_id="" payload_file
  marker_login="$(gh_retry gh api user --jq '.login // ""' 2>/dev/null || true)"
  if [ -n "${marker_login}" ]; then
    # $login and $head are jq variables, not shell expansions.
    # shellcheck disable=SC2016
    existing_id="$(gh_retry gh api --paginate -X GET "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" -f per_page=100 \
      --jq '.[] | {id: .id, login: (.user.login // ""), body: (.body // "")}' 2>/dev/null \
      | jq -rs --arg login "${marker_login}" --arg head "${HEAD_SHA}" '
          [.[] | select(.login == $login and ((.id | type) == "number")) |
            select(.body | split("\n") | any(.[]; test("^<!-- ai:claude-fixer-(handoff:v1 kind=(findings|conflict)|checks-pending:v1) head=" + $head + " ")))]
          | max_by(.id) // empty
          | select(.body | split("\n") | any(.[]; test("^<!-- ai:claude-fixer-checks-pending:v1 head=" + $head + " ")))
          | .id
        ' 2>/dev/null || true)"
  fi
  payload_file="$(mktemp "${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_checks_pending_payload.XXXXXX")"
  jq -n --rawfile body "${claude_fixer_body_file}" '{body: $body}' > "${payload_file}"
  if [[ "${existing_id}" =~ ^[0-9]+$ ]]; then
    gh_retry gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${existing_id}" --input "${payload_file}" >/dev/null
  else
    gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" --input "${payload_file}" >/dev/null
  fi
  rm -f "${payload_file}"
}

if [ "${AUTOFIX_PRE_REVIEW_RESOLVE:-false}" = "true" ] \
  && [ "${CLAUDE_FIXER_PROTECTED_CONFLICT:-false}" = "true" ] \
  && [ "${CLAUDE_FIXER_PROTECTED_CONFLICT_FALLBACK:-false}" != "true" ]; then
  echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=none reason=protected_conflict_to_resolver"
  exit 0
fi

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
    if [ "${CLAUDE_FIXER_PROTECTED_CONFLICT_FALLBACK:-false}" = "true" ]; then
      echo
      echo "Protected paths: \`${CLAUDE_FIXER_PROTECTED_CONFLICT_PATHS//,/\`, \`}\`. The GPT resolver could not resolve this conflict (it failed or escalated), and an unattended Claude session cannot edit \`.claude/**\`: hold the head and ask a human to resolve it in a watched session."
    fi
    echo
    echo "<!-- ai:claude-fixer-handoff:v1 kind=conflict head=${HEAD_SHA} round=${claude_fixer_round} -->"
    if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
      echo "<!-- ai:claude-fixer-verification:v1 head=${HEAD_SHA} result=unresolved -->"
    fi
  } > "${claude_fixer_body_file}"
  claude_fixer_write_evidence conflict || true
  claude_fixer_post_marker_comment
  echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=conflict"
  exit 0
fi

if [ "${CLAUDE_FIXER_JUDGE_ENABLED:-true}" != "false" ] \
  && [ -s "${REVIEWER_CONSENSUS_FILE:-}" ] \
  && [ -s "${PR_ISSUE_COMMENTS_FILE:-}" ] \
  && [ -f "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_judge.py" ] \
  && [ -f "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_evidence.py" ]; then
  claude_fixer_prior_rulings_file="$(mktemp "${RUNTIME_DIR:-${TMPDIR:-/tmp}}/claude_fixer_prior_rulings.XXXXXX")"
  claude_fixer_sticky_moved="0"
  if PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_judge.py" prior-rulings \
      --comments "${PR_ISSUE_COMMENTS_FILE}" \
      --repo "${GITHUB_REPOSITORY}" \
      --pr "${PR_NUMBER}" \
      --default-branch "${DEFAULT_BRANCH:-}" \
      --out "${claude_fixer_prior_rulings_file}" >/dev/null 2>&1; then
    claude_fixer_sticky_moved="$(PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/review_claude_fixer_judge.py" sticky \
      --ledger "${REVIEWER_CONSENSUS_FILE}" \
      --rulings "${claude_fixer_prior_rulings_file}" 2>/dev/null || echo 0)"
  fi
  if [ "${claude_fixer_sticky_moved:-0}" != "0" ]; then
    echo "CLAUDE_FIXER_JUDGE pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} action=sticky_demoted findings=${claude_fixer_sticky_moved}"
  fi
  rm -f "${claude_fixer_prior_rulings_file}"
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

# Panel floor: infrastructure failures of individual reviewer slots do not block
# a clean round as long as at least half of the active panel (rounded up)
# returned `success`. States: true / false, or unknown when the active panel
# size cannot be determined (the floor is then not applied).
claude_fixer_panel_active=0
claude_fixer_panel_floor_met="unknown"
if [ -n "${PREVIOUS_REVIEWS_DIR:-}" ] && [ -d "${PREVIOUS_REVIEWS_DIR}" ]; then
  # `|| true` keeps a failing find from aborting the step under pipefail; an
  # unreadable directory then counts 0 and falls through to the models file.
  claude_fixer_panel_active="$({ find "${PREVIOUS_REVIEWS_DIR}" -maxdepth 1 -type f -name 'status_review_*.txt' 2>/dev/null || true; } | wc -l | tr -d '[:space:]')"
fi
[[ "${claude_fixer_panel_active}" =~ ^[0-9]+$ ]] || claude_fixer_panel_active=0
if [ -s "${RUNTIME_DIR:-/nonexistent}/reviewer_active_models.txt" ]; then
  claude_fixer_panel_models_count="$(grep -c . "${RUNTIME_DIR}/reviewer_active_models.txt" 2>/dev/null || true)"
  if [[ "${claude_fixer_panel_models_count}" =~ ^[0-9]+$ ]] && [ "${claude_fixer_panel_models_count}" -gt "${claude_fixer_panel_active}" ]; then
    claude_fixer_panel_active="${claude_fixer_panel_models_count}"
  fi
fi
if [ "${claude_fixer_panel_active}" -gt 0 ]; then
  if [[ "${REVIEWERS_SUCCESSFUL:-}" =~ ^[0-9]+$ ]] && [ "$((REVIEWERS_SUCCESSFUL * 2))" -ge "${claude_fixer_panel_active}" ]; then
    claude_fixer_panel_floor_met="true"
  else
    claude_fixer_panel_floor_met="false"
  fi
fi
echo "CLAUDE_FIXER_PANEL_FLOOR pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} successful=${REVIEWERS_SUCCESSFUL:-unset} active=${claude_fixer_panel_active} floor_met=${claude_fixer_panel_floor_met}"

claude_fixer_clean_ledger="false"
if [ "${claude_fixer_ledger_state}" = "ok" ] && [ "${claude_fixer_finding_count}" -eq 0 ] \
  && [ "${claude_fixer_panel_floor_met}" != "false" ] \
  && awk '
    /^=== CONSENSUS FINDINGS ===$/ { expected = "(No findings reported.)"; block = 1; entries = 0; next }
    /^=== CONSENSUS TASK GAPS ===$/ { expected = "(No task gaps reported.)"; block = 1; entries = 0; next }
    /^=== FINDINGS FROM .+ ===$/ { expected = "(No findings reported.)"; block = 1; entries = 0; next }
    /^=== END / { if (block) { if (entries != 1) invalid = 1; blocks++; block = 0 }; next }
    block && NF { entries++; if ($0 != expected) invalid = 1 }
    END { if (invalid || block || blocks < 3) exit 1 }
  ' "${REVIEWER_CONSENSUS_FILE}"; then
  claude_fixer_clean_ledger="true"
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
    claude_fixer_write_evidence clean || true
    echo "CLAUDE_FIXER_ZERO_FINDINGS=true" >> "$GITHUB_ENV"
    exit 0
  fi
  # A check that failed while the reviewers ran is handed off as findings
  # (the fixer treats it as a `ci` round), naming it in the hand-off.
  if [ "${claude_fixer_checks_refreshed}" = "true" ] && [ -s "${PR_CHECK_RUNS_CONTEXT_FILE:-}" ]; then
    claude_fixer_failed_checks="$(sed -n 's/^failed\[[0-9]*\]\.name: //p' "${PR_CHECK_RUNS_CONTEXT_FILE}" | paste -sd, - || true)"
  fi
  # Checks pending is not a Claude hand-off (the review was clean). The
  # snapshot must be for this head and name no failure; a disabled or broken
  # collector keeps today's hand-off so the round can never wait forever.
  if [ "${CLAUDE_FIXER_CHECKS_PENDING_ENABLED:-true}" != "false" ] \
    && [ -z "${claude_fixer_failed_checks}" ] \
    && [ "${claude_fixer_checks_refreshed}" = "true" ] \
    && [[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]] \
    && [ -s "${PR_CHECK_RUNS_CONTEXT_FILE:-}" ] \
    && [ "$(sed -n '1p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "PR_CHECK_RUNS_CONTEXT" ] \
    && [ "$(sed -n '2p' "${PR_CHECK_RUNS_CONTEXT_FILE}")" = "head_sha: ${HEAD_SHA}" ] \
    && grep -Eqx 'collection_status: (ready|timeout|api_error)' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && grep -Fxq 'failed_count: 0' "${PR_CHECK_RUNS_CONTEXT_FILE}" \
    && claude_fixer_write_evidence checks-pending; then
    {
      echo "## Review round ${claude_fixer_round}: clean, waiting for checks"
      echo
      echo "The reviewer panel found nothing on head \`${HEAD_SHA}\` ([workflow run](${claude_fixer_run_url})), but not every check run on it has completed, so auto-merge is not enabled yet and nothing is handed to the Claude session."
      echo "The review sweep re-dispatches this workflow about every 30 minutes, and each of those runs is a merge check without reviewers: green checks enable auto-merge bound to this head, a failing check is handed to the Claude session, and checks that are still running wait for the next sweep."
      echo
      echo "<!-- ai:claude-fixer-checks-pending:v1 head=${HEAD_SHA} round=${claude_fixer_round} run=${GITHUB_RUN_ID} -->"
    } > "${claude_fixer_body_file}"
    claude_fixer_upsert_checks_pending_comment
    echo "CLAUDE_FIXER_CHECKS_PENDING pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} run=${GITHUB_RUN_ID} action=wait"
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
  if [ -n "${claude_fixer_failed_checks}" ]; then
    echo "Failing check runs on this head: \`${claude_fixer_failed_checks//,/\`, \`}\`."
  fi
  if [ "${claude_fixer_panel_floor_met}" = "false" ]; then
    echo "Only ${REVIEWERS_SUCCESSFUL:-0} of ${claude_fixer_panel_active} reviewers returned a result, below the panel floor (half of the active panel), so this round is not clean whatever the ledger says. When the ledger has no finding there is nothing for the GPT judge to rule on (a \`claude_fixer_judge_head\` dispatch decides nothing and labels the PR \`ai:review-blocked\`): push a new commit, or use the verdict-bot path below, so the reviewers run again."
  fi
  if [ -n "${claude_fixer_ledger_digest}" ]; then
    echo "Ledger SHA-256: \`${claude_fixer_ledger_digest}\`."
  fi
  echo
  echo "Claude-fixer mode: the GPT editor did not run. The Claude session that owns this PR judges each finding against the code, then either"
  echo "- fixes the valid ones in **one** commit whose subject starts with \`[claude-autofix]\` and pushes it (the push starts the next review round; the commits count toward \`MAX_AUTOFIX_ITERATIONS\` like \`[ai-autofix]\` ones), or"
  echo "- when nothing valid is left, posts one comment giving each finding's rejection reason (citing \`file:line\` or a test) and ending with \`<!-- ai:claude-fixer-rejection:v1 head=${HEAD_SHA} round=${claude_fixer_round} -->\`, then dispatches this workflow with \`claude_fixer_judge_head=${HEAD_SHA}\`: the GPT judge rules on every finding and merges, fixes what it upholds in a \`[judge-fix]\` commit, or holds for a human (\`CLAUDE_FIXER_JUDGE_ENABLED\`), or"
  echo "- where a dedicated fixer bot is configured (\`CLAUDE_FIXER_VERDICT_BOT_LOGIN\`), has that bot (not GH_PAT or a human) post a separate comment listing rejections and ending with both \`ai:claude-fixer-verdict:v1\` and \`ai:claude-fixer-verdict:v2\` markers for this head, round and ledger SHA-256, then dispatches this workflow with \`claude_fixer_converged_head=${HEAD_SHA}\`. That dispatch re-runs the reviewers; only a clean review with a fresh ready check snapshot can enable auto-merge."
  echo
  echo "<!-- ai:claude-fixer-handoff:v1 kind=findings head=${HEAD_SHA} round=${claude_fixer_round} -->"
  if [ -n "${claude_fixer_ledger_digest}" ]; then
    echo "<!-- ai:claude-fixer-handoff:v2 head=${HEAD_SHA} round=${claude_fixer_round} ledger=${claude_fixer_ledger_digest} -->"
  fi
  if [ "${CLAUDE_FIXER_VERIFICATION:-false}" = "true" ]; then
    echo "<!-- ai:claude-fixer-verification:v1 head=${HEAD_SHA} result=unresolved -->"
  fi
} > "${claude_fixer_body_file}"
claude_fixer_write_evidence findings || true
claude_fixer_post_marker_comment
echo "CLAUDE_FIXER_HANDOFF pr=${PR_NUMBER} head=${HEAD_SHA} round=${claude_fixer_round} kind=findings findings=${claude_fixer_finding_count} ledger=${claude_fixer_ledger_state} failed_checks=${claude_fixer_failed_checks:-none}"
