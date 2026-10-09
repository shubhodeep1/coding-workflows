#!/usr/bin/env bash
# Body of the "Evaluate review gate" step (id: evaluate) in the gate job of
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# id:, env: and outputs stay in the workflow; edit those there.
# The gate job runs before support staging: this script is loaded only from
# the SHA-verified .codex-head-gate-src sparse checkout (pinned to
# review_support_sha), never from a PR-head copy or SUPPORT_SCRIPTS_DIR.
set -euo pipefail

SHOULD_RUN="true"
POST_MERGE_DISPATCH="false"
SKIP_REASON=""
GATE_SKIP_RECORDED="false"
# The skip-AI marker counts only when intentional (issue #4985): in
# the PR title, or on a body line that holds nothing but the marker
# (up to 3 leading spaces) outside a ``` / ~~~ fenced block. A
# quoted, backticked or mid-sentence mention is reviewed. A fence
# opens on any line starting with 3+ backticks or tildes and closes
# only on a line of up to 3 spaces, then a run of the same
# character at least as long as the opener, then only blanks, so
# a ``` line inside a ```` fence stays inside it (issue #5377); an
# unclosed fence runs to the end of the body. Keep this
# program identical to review_autofix_sweep.yml's copy
# (tests/test_skip_ai_marker_rule.py runs both). It reads its
# whole input, so pipefail never sees a SIGPIPE from printf.
SKIP_AI_BODY_AWK='{ sub(/\r$/, "") } fl > 0 { if ($0 ~ /^ ? ? ?(```+|~~~+)[ \t]*$/) { r = $0; sub(/^ */, "", r); sub(/[ \t]*$/, "", r); if (substr(r, 1, 1) == fc && length(r) >= fl) fl = 0 } next } match($0, /^[ \t]*(```+|~~~+)/) { r = substr($0, RSTART, RLENGTH); sub(/^[ \t]*/, "", r); fc = substr(r, 1, 1); fl = length(r); next } /^ ? ? ?\[skip ai\][ \t]*$/ { found = 1 } END { exit found ? 0 : 1 }'
POST_MERGE_PR_TEXT_JSON='{"title":"","body":""}'
POST_MERGE_LINKED_ISSUES_JSON='[]'
POST_MERGE_VALIDATE_CONTEXT_DEFINITELY_EMPTY="false"
FORCE_FULL_REVIEW_TIER="false"

pr_state=""
pr_merged="false"
pr_head_ref=""
pr_base_ref=""
pr_head_repo=""
pr_auto_merge_enabled=""
pr_labels=""
pr_additions=""
pr_deletions=""
pr_changed_files=""
pr_mergeable=""
pr_mergeable_state=""
pr_head_sha_gate=""
pr_head_repo_gate=""
review_checkout_sha=""
pr_base_ref_gate=""
pr_base_retargeted="false"
pr_updated_at_gate=""
pr_title_cached="${PR_TITLE:-}"
pr_body_cached="${PR_BODY:-}"
if [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
  : # fall through to existing PR-API fetch below
elif [ "${FORCE_CLAUDE_BRANCH_REVIEW:-false}" = "true" ] && [ -n "${HEAD_REF_OVERRIDE:-}" ]; then
  # No-PR claude-branch review path: caller (push to claude/** with
  # no open PR — see internal-review.yml resolve job) supplied the
  # head ref directly. Treat as an open synthetic PR for gating
  # purposes and leave PR-shaped fields empty; downstream editor /
  # commit / auto-merge steps still require a real PR number and are
  # skipped by the no-PR review-only mode below.
  pr_head_ref="${HEAD_REF_OVERRIDE}"
  pr_state="open"
  echo "AUTOFIX_GATE_NO_PR_FALLBACK head_ref=${pr_head_ref} reason=force_claude_branch_review_push"
else
  SHOULD_RUN="false"
  SKIP_REASON="invalid_pr_number"
  echo "::warning::PR_NUMBER '${PR_NUMBER}' is not numeric; skipping review run."
fi
if [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
  # CLAUDE.md §15: extend the existing PR fetch to also return
  # labels (force-review override check below), per-PR
  # additions/deletions totals (deterministic-skip size check), and
  # mergeable/mergeable_state (deterministic-skip conflict
  # suppression below), and changed_files (to verify the paginated
  # path list before either skip branch can run).
  if _pr_gate="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
    --jq '{state: (.state // ""), merged: (.merged // false), head_ref: (.head.ref // ""), base_ref: (.base.ref // ""), head_repo: (.head.repo.full_name // ""), head_sha: (.head.sha // ""), auto_merge_enabled: (if has("auto_merge") then (.auto_merge != null) else null end), updated_at: (.updated_at // ""), labels: ((.labels // []) | map(.name)), additions: (.additions // 0), deletions: (.deletions // 0), changed_files: .changed_files, mergeable: (if (.mergeable == null) then "" else (.mergeable | tostring) end), mergeable_state: (.mergeable_state // ""), title: (.title // ""), body: (.body // "")}' \
    2>/dev/null)"; then
    pr_state="$(printf '%s' "${_pr_gate}" | jq -r '.state // ""' 2>/dev/null || echo "")"
    pr_merged="$(printf '%s' "${_pr_gate}" | jq -r 'if (.merged // false) then "true" else "false" end' 2>/dev/null || echo "false")"
    pr_head_ref="$(printf '%s' "${_pr_gate}" | jq -r '.head_ref // ""' 2>/dev/null || echo "")"
    pr_base_ref="$(printf '%s' "${_pr_gate}" | jq -r '.base_ref // ""' 2>/dev/null || echo "")"
    pr_head_repo="$(printf '%s' "${_pr_gate}" | jq -r '.head_repo // ""' 2>/dev/null || echo "")"
    pr_head_sha_gate="$(printf '%s' "${_pr_gate}" | jq -r '.head_sha // ""' 2>/dev/null || echo "")"
    pr_auto_merge_enabled="$(printf '%s' "${_pr_gate}" | jq -r 'if .auto_merge_enabled == null then "" else (.auto_merge_enabled | tostring) end' 2>/dev/null || echo "")"
    pr_head_repo_gate="${pr_head_repo}"
    pr_base_ref_gate="$(printf '%s' "${_pr_gate}" | jq -r '.base_ref // ""' 2>/dev/null || echo "")"
    pr_updated_at_gate="$(printf '%s' "${_pr_gate}" | jq -r '.updated_at // ""' 2>/dev/null || echo "")"
    pr_labels="$(printf '%s' "${_pr_gate}" | jq -r '(.labels // []) | join(",")' 2>/dev/null || echo "")"
    pr_additions="$(printf '%s' "${_pr_gate}" | jq -r '.additions // 0' 2>/dev/null || echo "")"
    pr_deletions="$(printf '%s' "${_pr_gate}" | jq -r '.deletions // 0' 2>/dev/null || echo "")"
    pr_changed_files="$(printf '%s' "${_pr_gate}" | jq -r '.changed_files | if type == "number" and . == floor then tostring else "" end' 2>/dev/null || echo "")"
    pr_mergeable="$(printf '%s' "${_pr_gate}" | jq -r '.mergeable // ""' 2>/dev/null || echo "")"
    pr_mergeable_state="$(printf '%s' "${_pr_gate}" | jq -r '.mergeable_state // ""' 2>/dev/null || echo "")"
    if [ -z "$(printf '%s' "${pr_title_cached}" | tr -d '[:space:]')" ]; then
      pr_title_cached="$(printf '%s' "${_pr_gate}" | jq -r '.title // ""' 2>/dev/null || echo "")"
    fi
    if [ -z "$(printf '%s' "${pr_body_cached}" | tr -d '[:space:]')" ]; then
      pr_body_cached="$(printf '%s' "${_pr_gate}" | jq -r '.body // ""' 2>/dev/null || echo "")"
    fi
    # Withdraw an enrolled head before the rest of gate evaluation,
    # reusing this PR read rather than fetching the PR a second time.
    if [ "${HEAD_GATE_HELPER_VERIFIED:-false}" = "true" ] && [ "${EVENT_NAME}" = "pull_request" ] \
      && { [ "${EVENT_ACTION}" = "opened" ] || [ "${EVENT_ACTION}" = "synchronize" ]; }; then
      PR_EVENT_HEAD_SHA="${PR_HEAD_SHA:-}" GATE_HEAD_SHA="${pr_head_sha_gate}" \
        GATE_AUTO_MERGE_ENABLED="${pr_auto_merge_enabled}" DETERMINISTIC_SKIP=false \
        bash .codex-head-gate-src/scripts/review_head_gate.sh gate
      echo "HEAD_GATE_EARLY_DONE=true" >> "${GITHUB_ENV}"
    fi
  fi
fi

# Port P6: retarget an open stacked PR whose base already merged
# (scripts/retarget_merged_base.sh; no API call when the base is the
# default branch, fail-open otherwise).
if [ "${RETARGET_HELPER_VERIFIED:-false}" = "true" ] && [ "${pr_state}" = "open" ] \
  && [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]] && [ -n "${pr_base_ref_gate}" ] && [ -n "${RETARGET_DEFAULT_BRANCH:-}" ]; then
  retargeted_base="$(bash .codex-retarget-src/scripts/retarget_merged_base.sh pr "${REPOSITORY}" "${PR_NUMBER}" "${pr_base_ref_gate}" "${RETARGET_DEFAULT_BRANCH}")" || retargeted_base=""
  if [ -n "${retargeted_base}" ] && [ "${retargeted_base}" != "${pr_base_ref_gate}" ]; then
    pr_base_ref_gate="${retargeted_base}"
    pr_base_retargeted="true"
    # The head is unchanged, but the old-base mergeability and diff
    # no longer authorize any same-head or deterministic skip.
    pr_mergeable=""
    pr_mergeable_state=""
  fi
fi

if [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]] && [ -z "${pr_state}" ]; then
  SHOULD_RUN="false"
  SKIP_REASON="pr_state_unknown"
  echo "::warning::Unable to determine PR state for #${PR_NUMBER}; skipping review run."
elif [ "${pr_state}" = "closed" ]; then
  SHOULD_RUN="false"
  SKIP_REASON="pr_closed"
  if [ "${pr_merged}" = "true" ]; then
    POST_MERGE_DISPATCH="true"
  fi
elif [ "${PR_IS_DRAFT}" = "true" ] || [ "${PR_SKIP_AI}" = "true" ]; then
  SHOULD_RUN="false"
  SKIP_REASON="draft_or_skip_ai"
elif [[ "${PR_TITLE}" == *"[skip ai]"* ]] \
  || printf '%s\n' "${PR_BODY}" | awk "${SKIP_AI_BODY_AWK}"; then
  SHOULD_RUN="false"
  SKIP_REASON="skip_ai_marker"
fi

# A PR checkout must never fall back to the dispatching commit. The
# authenticated PR response is also the only source of the SHA for
# workflow_dispatch / workflow_call; fork heads are not safe to run
# beside review-job credentials, including in deterministic skip.
if [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]] && [ "${pr_state}" = "open" ]; then
  if [ "${pr_head_repo_gate}" = "${REPOSITORY}" ] && [[ "${pr_head_sha_gate}" =~ ^[0-9a-f]{40}$ ]]; then
    review_checkout_sha="${pr_head_sha_gate}"
  else
    SHOULD_RUN="false"
    SKIP_REASON="review_checkout_unverified"
    echo "::warning::PR #${PR_NUMBER} head repo or SHA is unverified; skipping review and deterministic merge."
  fi
fi

# post-merge-validate-dispatch runs in a separate job and cannot
# read the codex-agent runtime files. Hand off compact caches via
# gate outputs so the merged-PR path can reuse the already fetched
# PR text and linked-issue labels before any live fallback.
if [ "${POST_MERGE_DISPATCH}" = "true" ] && [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
  post_merge_linked_issues_cache_known="false"
  POST_MERGE_PR_TEXT_JSON="$(jq -cn --arg title "${pr_title_cached}" --arg body "${pr_body_cached}" '{title: $title, body: $body}')"
  if _post_merge_linked="$(gh api graphql \
    -f owner="${REPOSITORY%/*}" \
    -f name="${REPOSITORY#*/}" \
    -F number="${PR_NUMBER}" \
    -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number labels(first: 100) { nodes { name } } } } } } }' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes // [] | map({number: .number, labels: ((.labels.nodes // []) | map(.name))})' \
    2>/dev/null)"; then
    POST_MERGE_LINKED_ISSUES_JSON="$(printf '%s' "${_post_merge_linked}" | jq -c '.' 2>/dev/null || echo '[]')"
    post_merge_linked_issues_cache_known="true"
  else
    echo "::warning::Failed to cache linked issue labels for merged PR #${PR_NUMBER}; post-merge validation will fall back to live lookups."
  fi
  if [ "${post_merge_linked_issues_cache_known}" = "true" ] \
    && [ "${POST_MERGE_LINKED_ISSUES_JSON}" = "[]" ] \
    && [ -z "$(printf '%s%s' "${pr_title_cached}" "${pr_body_cached}" | tr -d '[:space:]')" ]; then
    POST_MERGE_VALIDATE_CONTEXT_DEFINITELY_EMPTY="true"
  fi
fi

# No-PR claude-branch reviewer-comment mode:
# Pushes to claude/** with no open PR have no PR thread to update
# and no PR number for the editor / commit / auto-merge loop. Keep
# that synthetic no-PR route as reviewer-comment-only, but do NOT
# special-case PR-backed claude/** branches here. Once a claude/**
# branch has a PR, it takes the normal PR review path.
CLAUDE_BRANCH_REVIEW="false"
if [ "${SHOULD_RUN}" = "true" ] \
  && [ "${FORCE_CLAUDE_BRANCH_REVIEW:-false}" = "true" ] \
  && [ -z "${PR_NUMBER:-}" ] \
  && [ -n "${pr_head_ref}" ]; then
  case "${pr_head_ref}" in
    claude/*)
      CLAUDE_BRANCH_REVIEW="true"
      echo "AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW_NO_PR pr=${PR_NUMBER:-none} head_ref=${pr_head_ref} — running reviewer panel + commit-comment path because no PR exists."
      ;;
  esac
fi

# Self-triggered autofix skip:
# When a pull_request.synchronize event was fired by the autofix's
# own [ai-autofix] commit push (GitHub-attributed .author.login or
# .committer.login equals AUTOFIX_BOT_LOGIN, default "codex"), the
# follow-up "verification" run re-executes the full reviewer panel + editor
# pass on the tree that was just committed. In the common case
# every reviewer finding has already been applied or classified in
# the preceding fix run, so this pass returns Change status:
# not-edited and contributes no new work — it is pure cost
# (~7 LLM calls per follow-up run). Skipping it cuts ~50% of
# autofix LLM spend per fix cycle.
#
# Safety boundary:
#   * Only synchronize events are eligible. workflow_dispatch (the
#     post-commit fallback, EDITOR_CHANGES_LOST re-dispatch,
#     orchestrator conflict-dispatch in _dispatch_review_for_conflicts,
#     and manual re-runs) always runs.
#   * pull_request.opened / reopened / ready_for_review always run.
#   * Only the [ai-autofix] prefix is skipped; [ai-merge-resolve]
#     commits still run the verification pass so post-resolution
#     reviewer coverage is preserved (conflict-resolved code is
#     higher-risk than a vanilla autofix edit).
#   * Identity guard is based on GitHub-attributed identity:
#     .author.login or .committer.login on the commit API response
#     must equal AUTOFIX_BOT_LOGIN (default "codex"). These fields
#     are resolved server-side by GitHub from the push credentials
#     and are NOT user-controlled — unlike .commit.author.email,
#     which is spoofable. If both logins are empty (e.g. an
#     unauthenticated mirror push or a commit whose author email
#     is not linked to any GitHub account), the gate fails open
#     and runs review normally.
#
# Safety net:
#   If a [ai-autofix] commit is incomplete or regresses the tree,
#   the orchestrator stall cron (internal-orchestrate-poll.yml
#   every 30 min) re-kicks autofix via workflow_dispatch, which
#   bypasses this skip. Worst-case window: ~30 min.
#
# Opt-IN to the synchronize-self-skip: set repository variable
# AUTOFIX_SKIP_SELF_TRIGGERED=true. Default is now `false` so that
# autofix runs continuously on every push (including its own
# [ai-autofix] verification reruns), matching the new
# `internal-review-autofix-sweep` cron behaviour. Flip the var to
# `true` to restore the prior LLM-cost-saving skip.
# Override bot login: set repository variable AUTOFIX_BOT_LOGIN.
if [ "${SHOULD_RUN}" = "true" ] \
  && [ "${EVENT_NAME}" = "pull_request" ] \
  && [ "${EVENT_ACTION}" = "synchronize" ] \
  && [ "${AUTOFIX_SKIP_SELF_TRIGGERED:-false}" = "true" ] \
  && [ -n "${PR_HEAD_SHA}" ]; then
  head_meta=""
  if ! head_meta="$(gh api "repos/${REPOSITORY}/commits/${PR_HEAD_SHA}" \
    --jq '[(.commit.message // "" | split("\n")[0]), (.author.login // ""), (.committer.login // "")] | @tsv' \
    2>/dev/null)"; then
    # Fail open — API error falls through to normal run so the
    # cycle is never silently broken.
    echo "AUTOFIX_GATE_SKIP_QUERY_FAILED pr=${PR_NUMBER:-?} head_sha=${PR_HEAD_SHA} reason=api_error"
    head_meta=""
  fi
  if [ -n "${head_meta}" ]; then
    head_msg_line="$(printf '%s' "${head_meta}" | cut -f1)"
    head_author_login="$(printf '%s' "${head_meta}" | cut -f2)"
    head_committer_login="$(printf '%s' "${head_meta}" | cut -f3)"
    bot_login="${AUTOFIX_BOT_LOGIN:-codex}"
    case "${head_msg_line}" in
      "[ai-autofix]"*)
        if [ -n "${bot_login}" ] \
          && { [ "${head_author_login,,}" = "${bot_login,,}" ] \
            || [ "${head_committer_login,,}" = "${bot_login,,}" ]; }; then
          SHOULD_RUN="false"
          SKIP_REASON="self_triggered_autofix"
          GATE_SKIP_RECORDED="true"
          echo "AUTOFIX_GATE_SKIP reason=self_triggered_autofix pr=${PR_NUMBER} head_sha=${PR_HEAD_SHA} head_prefix=[ai-autofix] author_login=${head_author_login} committer_login=${head_committer_login} bot_login=${bot_login}"
        else
          echo "AUTOFIX_GATE_NO_SKIP_IDENTITY pr=${PR_NUMBER} head_sha=${PR_HEAD_SHA} head_prefix=[ai-autofix] author_login=${head_author_login} committer_login=${head_committer_login} bot_login=${bot_login}"
        fi
        ;;
    esac
  fi
fi

# ----- PR comments shared by the terminal same-head skip and the
# identical-failure fingerprint cap (CLAUDE.md §15) -----
# Both checks authenticate workflow-owned PR-comment markers against
# the login GH_PAT posts as, and both read the PR's issue comments.
# gate_fetch_marker_comments issues the `gh api user` lookup and the
# paginated comments call at most once per gate evaluation, on first
# use, so enabling both checks still costs one comments call. The
# file keeps only the comments either parser reads: the
# REVIEW_AUTOFIX_PARTIAL_V1 markers (verbatim), and the failure /
# cap markers, failure comment texts and editor summaries the cap
# scan reads (other bodies clipped to their first and last 2000
# characters; both markers sit at the ends of their comments).
# gate_marker_fetch_state: pending | ok | marker_author_unavailable
# | api_error.
gate_marker_fetch_state="pending"
gate_marker_author_login=""
gate_marker_comments_file="${RUNNER_TEMP:-/tmp}/review_gate_marker_comments.json"
gate_fetch_marker_comments()
{
  if [ "${gate_marker_fetch_state}" != "pending" ]; then
    return 0
  fi
  # The existing PR and comments calls do not expose the identity
  # authenticated by GH_PAT, so this lookup cannot be merged into
  # either response. Resolve it once for this gate evaluation.
  if ! gate_marker_author_login="$(gh api user --jq '.login // ""' 2>/dev/null)"; then
    gate_marker_author_login=""
  fi
  if [ -z "${gate_marker_author_login}" ]; then
    gate_marker_fetch_state="marker_author_unavailable"
    return 0
  fi
  # $b is a jq variable, not a shell expansion.
  # shellcheck disable=SC2016
  if gh api --paginate -X GET "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" \
    -f per_page=100 \
    --jq '[.[] | (.body // "") as $b | select(($b | contains("<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->")) or ($b | contains("review-autofix-failure")) or ($b | contains("AI review/autofix")) or ($b | contains("AI autofix editor summary")) or ($b | contains("Editor changes lost")) or ($b | contains("Editor no-op suspicious")) or ($b | contains("<!-- ai:claude-fixer-review-skipped:"))) | {id: .id, author_login: (.user.login // ""), author_type: (.user.type // ""), author_association: (.author_association // ""), created_at: (.created_at // ""), body: (if (($b | contains("<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->")) or (($b | length) <= 4000)) then $b else ($b[0:2000] + "\n[... clipped ...]\n" + $b[-2000:]) end)}]' \
    2>/dev/null | jq -c -s 'add // []' > "${gate_marker_comments_file}" 2>/dev/null \
    && [ -s "${gate_marker_comments_file}" ]; then
    gate_marker_fetch_state="ok"
  else
    gate_marker_fetch_state="api_error"
  fi
}

# ----- Terminal same-head skip (workflow_dispatch only) -----
# The partial-finalize path caps same-head resume rounds at
# REVIEW_MAX_RESUME_ROUNDS and marks the cycle terminal
# (`resume_state=no_progress` or `round_budget_exhausted`,
# `resume_should_continue=false`) in the machine-readable block of
# its `<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->` PR comment, keyed on
# `head_sha=`. Nothing else in the run can make progress on that
# head, yet the review_autofix_sweep cron re-dispatches every open
# PR every 30 minutes: after PR #4077 went terminal on 0d30bc69
# (2026-09-12T15:55Z) the sweep dispatched ~25 more same-head runs
# over the next 12.5 hours, each restoring the cached terminal
# state and exiting neutral after ~6 minutes of runner setup.
# Skip those dispatches here, before the codex-agent job starts.
#
# Scope and fail-open rules:
#   * workflow_dispatch only. pull_request events either carry a
#     new head (the marker no longer matches) or are the
#     opened/reopened/ready_for_review events that are never
#     skipped; leaving them untouched keeps this guard narrow. A
#     reusable workflow retains its caller's `github` context, so
#     the sweep's internal-review.yml dispatch reaches this block
#     with EVENT_NAME=workflow_dispatch.
#   * force_rb_judge dispatches (stall poller) bypass the skip.
#   * `[force-review]` in the title or the `force-review` label
#     bypasses the skip, mirroring the deterministic-skip override.
#   * Only a PR GitHub reports as mergeable=true is skipped: a
#     conflicted (or not-yet-computed) PR stays on codex-agent so
#     the "Detect merge conflicts" step + resolver can still run
#     on the same head.
#   * A comments API failure or an unparseable marker logs
#     AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED and runs normally.
#   * vars.AUTOFIX_SKIP_TERMINAL_SAME_HEAD=false disables the check.
# Cost (CLAUDE.md §15): head_sha rides on the existing /pulls fetch
# above. The authenticated marker author cannot be derived from that
# response or the comments response, so eligible dispatches issue one
# GET /user plus one paginated GET /issues/{n}/comments, shared with
# the fingerprint cap below through gate_fetch_marker_comments.
if [ "${SHOULD_RUN}" = "true" ] \
  && [ "${EVENT_NAME}" = "workflow_dispatch" ] \
  && [ "${pr_base_retargeted}" != "true" ] \
  && [ "${AUTOFIX_SKIP_TERMINAL_SAME_HEAD:-true}" != "false" ] \
  && [ "${FORCE_RB_JUDGE:-false}" != "true" ] \
  && [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
  terminal_force_review="false"
  if printf '%s' "${pr_title_cached}" | grep -Fq "[force-review]"; then
    terminal_force_review="true"
  fi
  case ",${pr_labels}," in
    *,force-review,*) terminal_force_review="true" ;;
  esac
  if [ "${terminal_force_review}" = "true" ]; then
    echo "AUTOFIX_GATE_TERMINAL_SAME_HEAD_OVERRIDE pr=${PR_NUMBER} head_sha=${pr_head_sha_gate:-unknown} reason=force_review_marker"
  elif [ "${pr_mergeable}" != "true" ]; then
    echo "AUTOFIX_GATE_TERMINAL_SAME_HEAD_UNCHECKED pr=${PR_NUMBER} head_sha=${pr_head_sha_gate:-unknown} reason=mergeable_not_true mergeable=${pr_mergeable:-unknown} mergeable_state=${pr_mergeable_state:-unknown}"
  elif [ -z "${pr_head_sha_gate}" ]; then
    echo "AUTOFIX_GATE_TERMINAL_SAME_HEAD_UNCHECKED pr=${PR_NUMBER} head_sha=unknown reason=head_sha_unavailable"
  else
    gate_fetch_marker_comments
    terminal_marker_author_login="${gate_marker_author_login}"
    if [ -z "${terminal_marker_author_login}" ]; then
      echo "AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=marker_author_unavailable"
    else
      # Keep only the partial-finalize marker comments (id, author,
      # created_at, body) so the parser below can authenticate the
      # workflow-owned state.
      partial_marker_comments_json=""
      if [ "${gate_marker_fetch_state}" = "ok" ]; then
        if ! partial_marker_comments_json="$(jq -c '[.[] | select((.body // "") | contains("<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->"))]' "${gate_marker_comments_file}" 2>/dev/null)"; then
          partial_marker_comments_json=""
        fi
      fi
      if [ -z "${partial_marker_comments_json}" ]; then
        echo "AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=api_error"
      else
        terminal_decision=""
        if ! terminal_decision="$(PARTIAL_MARKER_COMMENTS_JSON="${partial_marker_comments_json}" GATE_HEAD_SHA="${pr_head_sha_gate}" GATE_BASE_REF="${pr_base_ref_gate}" GATE_BASE_UPDATED_AT="${pr_updated_at_gate}" GATE_BOT_LOGIN="${AUTOFIX_BOT_LOGIN:-codex}" GATE_MARKER_AUTHOR_LOGIN="${terminal_marker_author_login}" python3 - <<'PY'
import json
import os
import re

# Decide whether the newest REVIEW_AUTOFIX_PARTIAL_V1 marker for the
# current head SHA says the same-head resume cycle is terminal.
# Output is KEY=VALUE lines consumed by the shell below; every value
# is reduced to [A-Za-z0-9_.-] so it is safe inside a log line.
head_sha = (os.environ.get("GATE_HEAD_SHA") or "").strip().lower()
base_ref = (os.environ.get("GATE_BASE_REF") or "").strip()
base_updated_at = (os.environ.get("GATE_BASE_UPDATED_AT") or "").strip()
bot_login = (os.environ.get("GATE_BOT_LOGIN") or "").strip().lower()
marker_author_login = (os.environ.get("GATE_MARKER_AUTHOR_LOGIN") or "").strip().lower()
try:
    comments = json.loads(os.environ.get("PARTIAL_MARKER_COMMENTS_JSON") or "[]")
except (TypeError, ValueError):
    raise SystemExit(2)
if not isinstance(comments, list):
    raise SystemExit(2)

field_re = re.compile(r"^\s*([a-z_]+)=(.*?)\s*$")

def marker_fields(body: object) -> dict[str, str]:
    fields: dict[str, str] = {}
    in_block = False
    for line in str(body or "").splitlines():
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        if not in_block:
            continue
        match = field_re.match(line)
        if match:
            fields.setdefault(match.group(1), match.group(2).strip())
    return fields

def clean(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(value or "")) or "unknown"

latest_key = None
latest_fields: dict[str, str] = {}
latest_id = ""
matching = 0
untrusted_markers = 0
commit_bot_markers = 0
for comment in comments:
    if not isinstance(comment, dict):
        continue
    fields = marker_fields(comment.get("body"))
    if fields.get("partial_finalize") != "true":
        continue
    if not head_sha or fields.get("head_sha", "").strip().lower() != head_sha:
        continue
    marker_base = fields.get("base_ref", "")
    if marker_base and marker_base != base_ref:
        continue
    # A legacy marker has no base identity; a PR update timestamp
    # cannot prove whether it was posted before a base change.
    if not marker_base:
        continue
    author_login = str(comment.get("author_login") or "").strip().lower()
    if not marker_author_login or author_login != marker_author_login:
        untrusted_markers += 1
        if bot_login and author_login == bot_login:
            commit_bot_markers += 1
        continue
    matching += 1
    try:
        comment_id = int(comment.get("id") or 0)
    except (TypeError, ValueError):
        comment_id = 0
    key = (str(comment.get("created_at") or ""), comment_id)
    if latest_key is None or key > latest_key:
        latest_key = key
        latest_fields = fields
        latest_id = str(comment_id)

terminal = "false"
if matching and latest_fields.get("resume_should_continue", "").strip().lower() == "false":
    terminal = "true"
print(f"terminal={terminal}")
print(f"matching_markers={matching}")
print(f"untrusted_markers={untrusted_markers}")
print(f"commit_bot_markers={commit_bot_markers}")
print(f"resume_state={clean(latest_fields.get('resume_state'))}")
print(f"resume_round={clean(latest_fields.get('resume_round'))}")
print(f"resume_round_limit={clean(latest_fields.get('resume_round_limit'))}")
print(f"marker_comment_id={clean(latest_id)}")
PY
        )"; then
          terminal_decision=""
        fi
        if [ -z "${terminal_decision}" ]; then
          echo "AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=parse_error"
        else
          terminal_same_head="$(printf '%s\n' "${terminal_decision}" | awk -F= '$1 == "terminal" { print $2 }' | tail -n 1)"
          terminal_matching_markers="$(printf '%s\n' "${terminal_decision}" | awk -F= '$1 == "matching_markers" { print $2 }' | tail -n 1)"
          terminal_untrusted_markers="$(printf '%s\n' "${terminal_decision}" | awk -F= '$1 == "untrusted_markers" { print $2 }' | tail -n 1)"
          terminal_commit_bot_markers="$(printf '%s\n' "${terminal_decision}" | awk -F= '$1 == "commit_bot_markers" { print $2 }' | tail -n 1)"
          terminal_resume_state="$(printf '%s\n' "${terminal_decision}" | awk -F= '$1 == "resume_state" { print $2 }' | tail -n 1)"
          terminal_resume_round="$(printf '%s\n' "${terminal_decision}" | awk -F= '$1 == "resume_round" { print $2 }' | tail -n 1)"
          terminal_resume_round_limit="$(printf '%s\n' "${terminal_decision}" | awk -F= '$1 == "resume_round_limit" { print $2 }' | tail -n 1)"
          terminal_marker_comment_id="$(printf '%s\n' "${terminal_decision}" | awk -F= '$1 == "marker_comment_id" { print $2 }' | tail -n 1)"
          if [ "${terminal_same_head}" = "true" ]; then
            SHOULD_RUN="false"
            SKIP_REASON="terminal_same_head"
            GATE_SKIP_RECORDED="true"
            echo "AUTOFIX_GATE_SKIP reason=terminal_same_head pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} resume_state=${terminal_resume_state} resume_round=${terminal_resume_round} resume_round_limit=${terminal_resume_round_limit} marker_comment_id=${terminal_marker_comment_id} mergeable=${pr_mergeable}"
          elif [ "${terminal_matching_markers:-0}" != "0" ] || [ "${terminal_untrusted_markers:-0}" != "0" ]; then
            echo "AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} resume_state=${terminal_resume_state} resume_round=${terminal_resume_round} resume_round_limit=${terminal_resume_round_limit} matching_markers=${terminal_matching_markers:-0} untrusted_markers=${terminal_untrusted_markers:-0} commit_bot_markers=${terminal_commit_bot_markers:-0} marker_author_login=${terminal_marker_author_login}"
          fi
        fi
      fi
    fi
  fi
fi

# ----- Identical-failure fingerprint cap -----
# Every failure comment the codex-agent job posts ends with a
# `<!-- review-autofix-failure:v1 head=<sha> reason=<r> fp=<sha256>
# degraded=<0|1> run=<id> -->` marker (fp = the failure reason plus
# the normalised error signature of the failing stage's stderr).
# When the trailing markers for the current head written by the
# GH_PAT identity share one fingerprint REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL
# (default 3) times, the failure is deterministic: another run on
# this head spends reviewer and editor models to fail the same way
# (PR #4259: seven identical editor failures on one head, about 75
# minutes each). Stop here instead and hand the PR to the sibling
# fingerprint-cap-block job, which labels the linked issues (or the
# PR) ai:review-blocked, posts one cap comment and reports the
# failure to workflow failure heal. A push (new head) resets the
# count by construction. A reason the helper reports as
# non_retryable=true (NON_RETRYABLE_FAILURE_REASONS) trips the cap on
# its first marker (PR #6438: ~28 identical resolver runs).
#
# Scope and fail-open rules:
#   * Every event (pull_request and workflow_dispatch) of a PR with
#     a known head SHA, once every earlier gate passed.
#   * force_rb_judge dispatches (stall poller) bypass the cap: the
#     review-blocked judge is the cap's intended consumer.
#   * A missing helper, a comments / identity lookup failure, or a
#     parse failure logs AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED and
#     keeps the normal gate result.
#   * vars.REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED=false disables it.
# Cost (CLAUDE.md §15): the comments are the ones
# gate_fetch_marker_comments already fetched for the terminal
# same-head skip, or one GET /user plus one paginated GET
# /issues/{n}/comments when that skip did not run.
FINGERPRINT_CAP="false"
FINGERPRINT_CAP_FP=""
FINGERPRINT_CAP_REASON=""
FINGERPRINT_CAP_COUNT="0"
FINGERPRINT_CAP_ALREADY_APPLIED="false"
FINGERPRINT_CAP_NON_RETRYABLE="false"
FINGERPRINT_CAP_MAX="${REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL:-3}"
case "${FINGERPRINT_CAP_MAX}" in
  ''|*[!0-9]*|0) FINGERPRINT_CAP_MAX=3 ;;
esac
if [ "${SHOULD_RUN}" = "true" ] \
  && [ "${pr_base_retargeted}" != "true" ] \
  && [ "${REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED:-true}" != "false" ] \
  && [ "${FINGERPRINT_CAP_SUPPORT_VERIFIED:-false}" = "true" ] \
  && [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
  fingerprint_cap_helper=""
  for fingerprint_cap_candidate in .codex-workflow-src/scripts/workflow_failure_heal.py; do
    if [ -f "${fingerprint_cap_candidate}" ] && grep -q 'autofix-identical-failure-count' "${fingerprint_cap_candidate}"; then
      fingerprint_cap_helper="${fingerprint_cap_candidate}"
      break
    fi
  done
  if [ "${FORCE_RB_JUDGE:-false}" = "true" ]; then
    echo "AUTOFIX_FINGERPRINT cap=bypassed pr=${PR_NUMBER} head=${pr_head_sha_gate:-unknown} reason=force_rb_judge"
  elif ! [[ "${pr_head_sha_gate}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED pr=${PR_NUMBER} head=unknown reason=head_sha_unavailable"
  elif [ -z "${fingerprint_cap_helper}" ]; then
    echo "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED pr=${PR_NUMBER} head=${pr_head_sha_gate} reason=helper_missing"
  else
    gate_fetch_marker_comments
    if [ "${gate_marker_fetch_state}" != "ok" ]; then
      echo "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED pr=${PR_NUMBER} head=${pr_head_sha_gate} reason=${gate_marker_fetch_state}"
    else
      fingerprint_cap_decision=""
      if ! fingerprint_cap_decision="$(PYTHONDONTWRITEBYTECODE=1 python3 "${fingerprint_cap_helper}" autofix-identical-failure-count \
        --comments-json "${gate_marker_comments_file}" \
        --head-sha "${pr_head_sha_gate}" \
        --author-login "${gate_marker_author_login}" \
        --support-sha "${REVIEW_SUPPORT_SHA:-}" 2>/dev/null)"; then
        fingerprint_cap_decision=""
      fi
      fingerprint_cap_count="$(printf '%s\n' "${fingerprint_cap_decision}" | awk -F= '$1 == "count" { print $2 }' | tail -n 1)"
      if ! [[ "${fingerprint_cap_count}" =~ ^[0-9]+$ ]]; then
        echo "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED pr=${PR_NUMBER} head=${pr_head_sha_gate} reason=parse_error"
      else
        FINGERPRINT_CAP_COUNT="${fingerprint_cap_count}"
        FINGERPRINT_CAP_FP="$(printf '%s\n' "${fingerprint_cap_decision}" | awk -F= '$1 == "fp" { print $2 }' | tail -n 1)"
        FINGERPRINT_CAP_REASON="$(printf '%s\n' "${fingerprint_cap_decision}" | awk -F= '$1 == "reason" { print $2 }' | tail -n 1)"
        if [ "$(printf '%s\n' "${fingerprint_cap_decision}" | awk -F= '$1 == "cap_applied" { print $2 }' | tail -n 1)" = "true" ]; then
          FINGERPRINT_CAP_ALREADY_APPLIED="true"
          echo "AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED pr=${PR_NUMBER} head=${pr_head_sha_gate} fp=${FINGERPRINT_CAP_FP} count=${FINGERPRINT_CAP_COUNT}"
        fi
        if [ "${FINGERPRINT_CAP_COUNT}" -ge 1 ] && [ "$(printf '%s\n' "${fingerprint_cap_decision}" | awk -F= '$1 == "non_retryable" { print $2 }' | tail -n 1)" = "true" ]; then
          FINGERPRINT_CAP_NON_RETRYABLE="true"
        fi
        if [ "${FINGERPRINT_CAP_COUNT}" -ge "${FINGERPRINT_CAP_MAX}" ] || [ "${FINGERPRINT_CAP_NON_RETRYABLE}" = "true" ]; then
          FINGERPRINT_CAP="true"
          SHOULD_RUN="false"
          SKIP_REASON="fingerprint_cap"
          echo "AUTOFIX_FINGERPRINT_CAP_TRIPPED pr=${PR_NUMBER} head=${pr_head_sha_gate} fp=${FINGERPRINT_CAP_FP} reason=${FINGERPRINT_CAP_REASON} count=${FINGERPRINT_CAP_COUNT} max=${FINGERPRINT_CAP_MAX} already_applied=${FINGERPRINT_CAP_ALREADY_APPLIED} non_retryable=${FINGERPRINT_CAP_NON_RETRYABLE} support=${REVIEW_SUPPORT_SHA:-none}"
        else
          echo "AUTOFIX_FINGERPRINT cap=not_tripped pr=${PR_NUMBER} head=${pr_head_sha_gate} fp=${FINGERPRINT_CAP_FP:-none} reason=${FINGERPRINT_CAP_REASON:-none} count=${FINGERPRINT_CAP_COUNT} max=${FINGERPRINT_CAP_MAX} support=${REVIEW_SUPPORT_SHA:-none}"
        fi
      fi
    fi
  fi
fi

# Deterministic pre-review skip (last gate check):
# If all earlier gates have passed (SHOULD_RUN still true) and the
# PR qualifies as doc-only or under the size threshold, flip
# SHOULD_RUN to false and route the PR to the
# `deterministic-skip-merge` sibling job, which preserves the
# ai:ready-to-merge + auto-merge tail of the normal review path.
# See header env-block comment for full semantics.
DETERMINISTIC_SKIP="false"
DET_SKIP_REASON=""
if [ "${SHOULD_RUN}" = "true" ] && [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
  FORCE_REVIEW="false"
  if [ "${pr_base_retargeted}" = "true" ]; then
    FORCE_REVIEW="true"
    echo "AUTOFIX_GATE_DET_SKIP_OVERRIDE pr=${PR_NUMBER} reason=base_retargeted"
  fi
  if printf "%s" "${PR_TITLE}" | grep -Fq "[force-review]"; then
    FORCE_REVIEW="true"
  fi
  if [ -n "${pr_labels}" ]; then
    case ",${pr_labels}," in
      *,force-review,*) FORCE_REVIEW="true" ;;
    esac
  fi
  FORCE_FULL_REVIEW_TIER="${FORCE_REVIEW}"

  if [ "${FORCE_REVIEW}" = "true" ]; then
    echo "AUTOFIX_GATE_DET_SKIP_OVERRIDE pr=${PR_NUMBER} reason=force_review_marker"
  else
    # Size check uses the .additions/.deletions totals already
    # returned by the PR-state fetch above (no extra API call).
    # Bash arithmetic test fails open ("not small") if either
    # value is non-numeric.
    SMALL_DIFF="false"
    max_add="${AUTOFIX_SKIP_MAX_ADDITIONS:-10}"
    max_del="${AUTOFIX_SKIP_MAX_DELETIONS:-10}"
    if [ -n "${pr_additions}" ] && [ -n "${pr_deletions}" ] \
      && [ "${pr_additions}" -le "${max_add}" ] 2>/dev/null \
      && [ "${pr_deletions}" -le "${max_del}" ] 2>/dev/null; then
      SMALL_DIFF="true"
    fi

    # Even a size-qualified PR needs path evidence: a two-line
    # instruction change must never bypass review. Fetch once for
    # both branches and reuse the same list for materiality.
    DOC_ONLY="false"
    file_count=""
    pr_files_json=""
    FILES_SKIP_SUPPRESSED="false"
    PROTECTED_SKIP_SUPPRESSED="false"
    if [ "${SMALL_DIFF}" = "true" ] || [ "${AUTOFIX_SKIP_DOC_ONLY:-true}" != "false" ]; then
      # `gh api --paginate --jq '[…]'` emits one JSON array per
      # page. Without `jq -s 'add // []'` the concatenated output
      # is not one document. pipefail propagates fetch failures;
      # the length check rejects partial pagination results.
      if ! pr_files_json="$(gh api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/files" --jq '[.[] | {filename: .filename, previous_filename: .previous_filename, status: .status}]' 2>/dev/null | jq -s 'add // []' 2>/dev/null)"; then
        pr_files_json=""
      fi
      # Missing, malformed, partial, or capped API evidence never
      # authorizes a skip. Validate rename sources and destinations.
      if [[ "${pr_changed_files}" =~ ^[1-9][0-9]*$ ]] \
        && [ -n "${pr_files_json}" ] \
        && printf '%s' "${pr_files_json}" | jq -e --argjson expected "${pr_changed_files}" '
          def valid_path:
            type == "string" and length > 0 and
            (startswith("/") | not) and (contains("\\") | not) and
            (test("[[:cntrl:]]") | not) and
            (split("/") | all(. != "" and . != "." and . != ".."));
          type == "array" and length > 0 and length < 3000 and
          length == $expected and length == ([.[].filename] | unique | length) and
          all(.[]; type == "object" and (.filename | valid_path) and
            (.previous_filename == null or (.previous_filename | valid_path)) and
            (.status | type == "string" and length > 0) and
            (.status != "renamed" or (.previous_filename | valid_path)))
        ' >/dev/null 2>&1; then
        file_count="${pr_changed_files}"
        if [ "${AUTOFIX_SKIP_DOC_ONLY:-true}" != "false" ]; then
          DOC_ONLY="true"
          while IFS= read -r fname; do
            base="${fname##*/}"
            lc_fname="${fname,,}"
            lc_base="${base,,}"
            case "${lc_fname}" in
              docs/*) continue ;;
            esac
            case "${lc_base}" in
              *.md|*.txt|*.rst) continue ;;
            esac
            # LICENSE / CHANGELOG: case-insensitive prefix on
            # basename — matches GitHub's own license/changelog
            # detection (recognises license.txt, Changelog.md,
            # LICENCE, etc.).
            case "${lc_base}" in
              license*|changelog*) continue ;;
            esac
            DOC_ONLY="false"
            break
          done < <(printf '%s' "${pr_files_json}" | jq -r '.[].filename' 2>/dev/null || true)
        fi
        while IFS= read -r fname; do
          lc_fname="${fname,,}"
          lc_base="${lc_fname##*/}"
          case "${lc_base}" in
            agents.md|claude.md|unattended_system_instructions.md)
              PROTECTED_SKIP_SUPPRESSED="true" ;;
          esac
          case "${lc_fname}" in
            .github/*|.claude/*|scripts/*|prompts/*|workflow-templates/*|validation/*|ai-memory/*|db/contracts/*)
              PROTECTED_SKIP_SUPPRESSED="true" ;;
          esac
          # Filenames are PR-controlled: executable build/test
          # entry points and configuration must not take either
          # deterministic skip route, even under docs/ or after
          # a rename away from a protected name.
          case "${lc_base}" in
            dockerfile|dockerfile.*|dockerfile-*|*.dockerfile|*.dockerfile.*|*.dockerfile-*|containerfile|containerfile.*|containerfile-*|*.containerfile|*.containerfile.*|*.containerfile-*|.dockerignore|.containerignore|compose.yml|compose.yaml|compose.*.yml|compose.*.yaml|compose-*.yml|compose-*.yaml|docker-compose.yml|docker-compose.yaml|docker-compose.*.yml|docker-compose.*.yaml|docker-compose-*.yml|docker-compose-*.yaml|makefile|makefile.*|gnumakefile|gnumakefile.*|justfile|justfile.*|taskfile|taskfile.*|rakefile|rakefile.*|jenkinsfile|jenkinsfile.*|cmakelists.txt|meson.build|meson_options.txt|pom.xml|build.xml|build.gradle*|settings.gradle*|gradlew|gradlew.bat|gulpfile.*|gruntfile.*|package.json|build|build.bazel|workspace|workspace.bazel|module.bazel|*.bazel|*.bzl|*.mk|*.cmake|*.gradle|*.gradle.kts|requirements*.txt|constraints*.txt|go.mod|go.sum|pipfile|pipfile.lock|*.lock|*.lockb|config|*.config|*.config.*|*.conf|*.ini|*.toml|*.yaml|*.yml|*.json|*.jsonc|*.properties|*.xml|*.tf|*.hcl|.*rc|.*rc.*|.env|.env.*|*.sh|*.bash|*.zsh|*.ps1|*.cmd|*.bat)
              PROTECTED_SKIP_SUPPRESSED="true" ;;
          esac
          # Root build, dependency and lint configuration already
          # recognized by the materiality classifier.
          if [ "${lc_fname}" = "${lc_base}" ]; then
            case "${lc_base}" in
              package.json|pyproject.toml|cargo.toml|go.mod|go.work|makefile|.editorconfig|turbo.json|pytest.ini|tox.ini|noxfile.py|*.config.js|*.config.cjs|*.config.mjs|*.config.ts|package-lock.json|bun.lock|bun.lockb|yarn.lock|pnpm-lock.yaml|cargo.lock|poetry.lock|uv.lock|go.sum|pipfile|pipfile.lock|requirements*.txt|constraints*.txt|.eslintrc*|eslint.config.*|.prettierrc*|.stylelintrc*|stylelint.config.*|ruff.toml|.ruff.toml|.flake8|pylintrc|biome.json|biome.jsonc)
                PROTECTED_SKIP_SUPPRESSED="true" ;;
            esac
          fi
        done < <(printf '%s' "${pr_files_json}" | jq -r '.[] | .filename, (.previous_filename // empty)' 2>/dev/null)
      else
        FILES_SKIP_SUPPRESSED="true"
        echo "AUTOFIX_GATE_DET_SKIP_FILES_UNAVAILABLE pr=${PR_NUMBER} — /files missing, invalid, truncated, or inconsistent with changed_files; running review."
      fi
    fi

    candidate_skip="false"
    candidate_skip_reason=""
    if [ "${DOC_ONLY}" = "true" ]; then
      candidate_skip="true"
      candidate_skip_reason="docs_only"
    elif [ "${SMALL_DIFF}" = "true" ]; then
      candidate_skip="true"
      candidate_skip_reason="small_diff"
    fi

    if [ "${candidate_skip}" = "true" ] && [ "${PROTECTED_SKIP_SUPPRESSED}" = "true" ]; then
      echo "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=protected_path pr=${PR_NUMBER} candidate=${candidate_skip_reason}"
    fi

    MATERIALITY_SKIP_SUPPRESSED="false"
    case "$(printf '%s' "${AGENTS_MD_MATERIALITY_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
      1|true|yes|on)
        # Keep medium/high material PRs on the codex-agent path so
        # the advisory comment is still reachable when the diff
        # would otherwise short-circuit through deterministic skip.
        # This lightweight gate only has /pulls/{n}/files path data,
        # so it suppresses skip on the path-glob signals that are
        # visible here and fails open for helper-only signals such as
        # repo-verified new top-level directories.
        if [ "${candidate_skip}" = "true" ]; then
          if [ "${FILES_SKIP_SUPPRESSED}" != "true" ]; then
            # Mirror the helper's visible path rules here so the gate
            # only suppresses deterministic skip when the helper would
            # later classify the diff medium/high from PR file paths
            # alone. Repo-verified signals such as new top-level
            # directories remain helper-only and intentionally fail
            # open on the gate path.
            if gate_materiality_json="$(PR_FILES_JSON="${pr_files_json}" python3 - <<'PY'
import json
import os
import sys
from pathlib import PurePosixPath


ROOT_HIGH_PATHS = {"package.json", "pyproject.toml", "Cargo.toml", "go.mod"}
HIGH_BUILD_TEST_BASENAMES = {
    "pytest.ini",
    "tox.ini",
    "noxfile.py",
    "jest.config.js",
    "jest.config.cjs",
    "jest.config.mjs",
    "jest.config.ts",
    "vitest.config.js",
    "vitest.config.cjs",
    "vitest.config.mjs",
    "vitest.config.ts",
    "playwright.config.js",
    "playwright.config.cjs",
    "playwright.config.mjs",
    "playwright.config.ts",
    "cypress.config.js",
    "cypress.config.cjs",
    "cypress.config.mjs",
    "cypress.config.ts",
    "webpack.config.js",
    "webpack.config.cjs",
    "webpack.config.mjs",
    "webpack.config.ts",
    "vite.config.js",
    "vite.config.cjs",
    "vite.config.mjs",
    "vite.config.ts",
    "turbo.json",
    "go.work",
}
MEDIUM_DEPENDENCY_BASENAMES = {
    "package-lock.json",
    "bun.lock",
    "bun.lockb",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    "Pipfile",
    "Pipfile.lock",
}
MEDIUM_LINT_BASENAMES = {
    ".eslintrc",
    ".eslintrc.json",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.yaml",
    ".eslintrc.yml",
    "eslint.config.js",
    "eslint.config.cjs",
    "eslint.config.mjs",
    "eslint.config.ts",
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".stylelintrc",
    ".stylelintrc.json",
    ".stylelintrc.js",
    "stylelint.config.js",
    "stylelint.config.cjs",
    "stylelint.config.mjs",
    "stylelint.config.ts",
    "ruff.toml",
    ".ruff.toml",
    ".flake8",
    "pylintrc",
    "biome.json",
    "biome.jsonc",
}
CLIENT_WRAPPER_BASENAMES = {
    "client.py",
    "client.ts",
    "client.js",
    "client.tsx",
    "client.jsx",
    "client.go",
    "client.rb",
    "client.java",
}
CLIENT_WRAPPER_TOKENS = (
    "api_client",
    "api-client",
    "http_client",
    "http-client",
    "github_client",
    "github-client",
    "rest_client",
    "rest-client",
    "sdk_client",
    "sdk-client",
)


def looks_like_client_wrapper(path: str) -> bool:
    pure = PurePosixPath(path)
    basename = pure.name.lower()
    if basename in CLIENT_WRAPPER_BASENAMES:
        parent_parts = [part.lower() for part in pure.parts[:-1]]
        if any(part in {"api", "apis", "client", "clients", "http", "sdk"} for part in parent_parts):
            return True
    if any(token in basename for token in CLIENT_WRAPPER_TOKENS):
        return True
    return False


def classify_path(path: str) -> str:
    pure = PurePosixPath(path)
    basename = pure.name
    if path in ROOT_HIGH_PATHS:
        return "high"
    if path.startswith(".github/workflows/"):
        return "high"
    if "/" not in path and basename in HIGH_BUILD_TEST_BASENAMES:
        return "high"
    if basename in MEDIUM_DEPENDENCY_BASENAMES or (basename.startswith("requirements") and basename.endswith(".txt")) or (basename.startswith("constraints") and basename.endswith(".txt")):
        return "medium"
    if basename in MEDIUM_LINT_BASENAMES:
        return "medium"
    if looks_like_client_wrapper(path):
        return "medium"
    return "low"


payload = os.environ.get("PR_FILES_JSON", "")
entries = json.loads(payload) if payload else json.load(sys.stdin)
paths = []
seen = set()
for entry in entries:
    path = str((entry or {}).get("filename") or "").strip()
    if not path or path in seen:
        continue
    seen.add(path)
    paths.append(path)

materiality = "low"
for path in paths:
    severity = classify_path(path)
    if severity == "high":
        materiality = "high"
        break
    if severity == "medium":
        materiality = "medium"

print(json.dumps({
    "agents_md_changed": "agents.md" in seen,
    "materiality": materiality,
}))
PY
            )"; then
              gate_materiality="$(printf '%s' "${gate_materiality_json}" | jq -r '.materiality // "low"' 2>/dev/null || echo low)"
              gate_agents_changed="$(printf '%s' "${gate_materiality_json}" | jq -r 'if (.agents_md_changed // false) then "true" else "false" end' 2>/dev/null || echo false)"
              if [ "${gate_agents_changed}" != "true" ] && { [ "${gate_materiality}" = "high" ] || [ "${gate_materiality}" = "medium" ]; }; then
                MATERIALITY_SKIP_SUPPRESSED="true"
                echo "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=agents_md_materiality pr=${PR_NUMBER} materiality=${gate_materiality} candidate=${candidate_skip_reason}"
              fi
            else
              echo "AUTOFIX_GATE_MATERIALITY_FILES_UNAVAILABLE pr=${PR_NUMBER} candidate=${candidate_skip_reason} — classifier unavailable, keeping deterministic skip fail-open."
            fi
          else
            echo "AUTOFIX_GATE_MATERIALITY_FILES_UNAVAILABLE pr=${PR_NUMBER} candidate=${candidate_skip_reason} — /files fetch failed or empty, keeping deterministic skip fail-open."
          fi
        fi
        ;;
    esac

    # Conflict suppression. The skip path runs no merge-conflict
    # resolution — the codex-agent job's "Detect merge conflicts"
    # step and the resolver behind it are the only entry point, and
    # deterministic skip bypasses that job entirely. So the cheapest
    # PRs, whose only conflict is typically a shared doc, used to
    # stall until the orchestrator stall-recovery cron re-dispatched
    # them with a force-review override. Keeping a conflicted PR on
    # the codex-agent path resolves it in the same run instead.
    #
    # Reviewer spend stays at its floor: review_run_reviewers.sh
    # already drops doc-only and <=lite-LOC diffs to the lite tier,
    # which is every PR that could have qualified for this skip.
    #
    # Fails open in both directions that matter: `mergeable` is
    # computed asynchronously by GitHub and is null on a freshly
    # pushed PR, and an API failure leaves both fields empty — in
    # either case the skip stands, which is exactly today's
    # behaviour. Only an explicit `mergeable=false` (or the
    # unambiguous `mergeable_state=dirty`) suppresses it.
    CONFLICT_SKIP_SUPPRESSED="false"
    if [ "${candidate_skip}" = "true" ] \
      && [ "${AUTOFIX_SKIP_SUPPRESS_ON_CONFLICT:-true}" != "false" ]; then
      if [ "${pr_mergeable}" = "false" ] || [ "${pr_mergeable_state}" = "dirty" ]; then
        CONFLICT_SKIP_SUPPRESSED="true"
        echo "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=merge_conflict pr=${PR_NUMBER} mergeable=${pr_mergeable:-unknown} mergeable_state=${pr_mergeable_state:-unknown} candidate=${candidate_skip_reason}"
      fi
    fi

    # The sibling skip job enables auto-merge without running the
    # single-issue pass. Keep eligible default-branch PRs on the
    # codex-agent path, including tiny and documentation-only diffs.
    SECURITY_PASS_SKIP_SUPPRESSED="false"
    if [ "${candidate_skip}" = "true" ] && [ "${SINGLE_ISSUE_SECURITY_PASS_ENABLED:-false}" != "false" ] \
      && [ -n "${DEFAULT_BRANCH:-}" ] && [ "${pr_base_ref}" = "${DEFAULT_BRANCH}" ] \
      && [ "${pr_head_repo}" = "${REPOSITORY}" ] \
      && ! [[ "${pr_head_ref}" =~ ${ORCH_INTEGRATION_BRANCH_PATTERN:-^orchestrator/project-} ]] \
      && [[ ",${pr_labels}," != *,e2e-smoke-test,* ]]; then
      SECURITY_PASS_SKIP_SUPPRESSED="true"
      echo "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=single_issue_security_pass pr=${PR_NUMBER} candidate=${candidate_skip_reason}"
    fi

    if [ "${candidate_skip}" = "true" ] && [ "${MATERIALITY_SKIP_SUPPRESSED}" != "true" ] \
      && [ "${CONFLICT_SKIP_SUPPRESSED}" != "true" ] \
      && [ "${SECURITY_PASS_SKIP_SUPPRESSED}" != "true" ] \
      && [ "${FILES_SKIP_SUPPRESSED}" != "true" ] \
      && [ "${PROTECTED_SKIP_SUPPRESSED}" != "true" ]; then
      DETERMINISTIC_SKIP="true"
      DET_SKIP_REASON="${candidate_skip_reason}"
      SHOULD_RUN="false"
      SKIP_REASON="deterministic_skip_${candidate_skip_reason}"
    fi

    echo "AUTOFIX_GATE_DET_SKIP_EVAL pr=${PR_NUMBER} files=${file_count:-unavailable} additions=${pr_additions:-?} deletions=${pr_deletions:-?} max_add=${max_add} max_del=${max_del} doc_only=${DOC_ONLY} small_diff=${SMALL_DIFF} skip=${DETERMINISTIC_SKIP} reason=${DET_SKIP_REASON} materiality_suppressed=${MATERIALITY_SKIP_SUPPRESSED} conflict_suppressed=${CONFLICT_SKIP_SUPPRESSED} files_suppressed=${FILES_SKIP_SUPPRESSED} protected_suppressed=${PROTECTED_SKIP_SUPPRESSED}"
  fi
fi

# The no-PR claude-branch reviewer-comment route and the deterministic
# doc-only / small-diff skip path are mutually exclusive: without a
# PR, there is nowhere to apply deterministic skip labeling /
# auto-merge. PR-backed claude/** branches are not in this mode and
# follow the same deterministic-skip behavior as other PRs.
if [ "${CLAUDE_BRANCH_REVIEW}" = "true" ] && [ "${DETERMINISTIC_SKIP}" = "true" ]; then
  echo "AUTOFIX_GATE_DET_SKIP_SUPPRESSED reason=claude_branch_review pr=${PR_NUMBER} prior_det_skip_reason=${DET_SKIP_REASON}"
  DETERMINISTIC_SKIP="false"
  DET_SKIP_REASON=""
fi

# Say why (issue #4985, CLAUDE.md §8): exactly one searchable line
# per skip, retaining earlier more detailed lines unchanged.
if [ "${SHOULD_RUN}" != "true" ] && [ "${GATE_SKIP_RECORDED}" != "true" ]; then
  echo "AUTOFIX_GATE_SKIP reason=${SKIP_REASON:-unknown} pr=${PR_NUMBER:-none} head_sha=${pr_head_sha_gate:-${PR_HEAD_SHA:-unknown}}"
fi
# A PR-backed claude/* head skipped on purpose gets one PR comment
# per head, so whoever waits on that PR sees the skip instead of
# waiting for a review that never comes. Only
# skip_ai_marker, and draft_or_skip_ai when the PR is not a draft
# (pr_skip_ai=true): GitHub already shows a draft, and
# ready_for_review starts its review. No label: ai:review-skipped
# means the deterministic doc-only / size skip
# (.github/ai/label_contract.v1.json). Deduped against the workflow
# account's own marker through gate_fetch_marker_comments (shared
# with the marker checks above, CLAUDE.md §15), plus one POST.
# Every failure only warns; the gate outcome never changes.
if [ "${SHOULD_RUN}" != "true" ] && [ "${pr_state}" = "open" ] \
  && [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]] && [[ "${pr_head_sha_gate}" =~ ^[0-9a-f]{40}$ ]] \
  && [[ "${pr_head_ref}" == claude/* ]] \
  && { [ "${SKIP_REASON}" = "skip_ai_marker" ] \
    || { [ "${SKIP_REASON}" = "draft_or_skip_ai" ] && [ "${PR_IS_DRAFT}" != "true" ]; }; }; then
  gate_fetch_marker_comments
  if [ "${gate_marker_fetch_state}" != "ok" ]; then
    echo "::warning::AUTOFIX_GATE_SKIP_NOTICE pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=${SKIP_REASON} posted=false detail=comments_${gate_marker_fetch_state}"
  elif jq -e --arg head "${pr_head_sha_gate}" --arg author "${gate_marker_author_login}" '
      any(.[]; .author_login == $author and
        ((.body // "") | split("\n") | map(select(length > 0)) |
          (.[-1] // "") as $last |
          ($last | test("^<!-- ai:claude-fixer-review-skipped:v1 reason=(skip_ai_marker|draft_or_skip_ai) head=" + $head + " -->$")) and
          (.[0] == ("## Review skipped: `" + ($last | sub("^.* reason="; "") | sub(" head=.*$"; "")) + "`"))))
    ' "${gate_marker_comments_file}" >/dev/null 2>&1; then
    echo "AUTOFIX_GATE_SKIP_NOTICE pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=${SKIP_REASON} posted=false detail=already_posted"
  else
    skip_notice_body_file="${RUNNER_TEMP:-/tmp}/review_gate_skip_notice.md"
    skip_notice_payload_file="${RUNNER_TEMP:-/tmp}/review_gate_skip_notice.json"
    {
      echo "## Review skipped: \`${SKIP_REASON}\`"
      echo
      echo "The review gate skipped head \`${pr_head_sha_gate}\`, so the reviewer panel did not run and no hand-off or auto-merge follows for this head ([workflow run](${GITHUB_SERVER_URL:-https://github.com}/${REPOSITORY}/actions/runs/${GITHUB_RUN_ID:-0}))."
      echo
      if [ "${SKIP_REASON}" = "skip_ai_marker" ]; then
        echo "Why: the PR title contains the skip-AI marker \`[skip ai]\`, or a line of the description holds only that marker. A mention in backticks, in a code block, or mid-sentence does not count."
      else
        echo "Why: the calling workflow passed \`pr_skip_ai=true\`."
      fi
      echo
      echo "To review this head anyway: remove the marker (or the \`pr_skip_ai\` input) and push a new commit, or dispatch the review workflow for this PR (\`internal-review.yml\` here, \`ai-review.yml\` in a consumer repo, with \`pr_number=${PR_NUMBER}\`). A dispatched run does not read the PR text."
      echo
      echo "<!-- ai:claude-fixer-review-skipped:v1 reason=${SKIP_REASON} head=${pr_head_sha_gate} -->"
    } > "${skip_notice_body_file}"
    # The earlier PR fetch established this head for the gate, not
    # for the comment POST: a push may have moved it in between.
    # This read cannot reuse the earlier snapshot; on failure or
    # a moved head, do not publish stale review-skip evidence.
    if ! skip_notice_live_head="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq 'if .state == "open" then .head.sha // "" else "" end' 2>/dev/null)" \
      || [ "${skip_notice_live_head}" != "${pr_head_sha_gate}" ]; then
      echo "::warning::AUTOFIX_GATE_SKIP_NOTICE pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=${SKIP_REASON} posted=false detail=head_moved_or_unavailable"
    elif jq -n --rawfile body "${skip_notice_body_file}" '{body: $body}' > "${skip_notice_payload_file}" 2>/dev/null \
      && gh api -X POST "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments" --input "${skip_notice_payload_file}" >/dev/null 2>&1; then
      echo "AUTOFIX_GATE_SKIP_NOTICE pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=${SKIP_REASON} posted=true"
    else
      echo "::warning::AUTOFIX_GATE_SKIP_NOTICE pr=${PR_NUMBER} head_sha=${pr_head_sha_gate} reason=${SKIP_REASON} posted=false detail=post_failed"
    fi
  fi
fi

echo "should_run=${SHOULD_RUN}" >> "${GITHUB_OUTPUT}"
echo "post_merge_dispatch=${POST_MERGE_DISPATCH}" >> "${GITHUB_OUTPUT}"
echo "skip_reason=${SKIP_REASON}" >> "${GITHUB_OUTPUT}"
echo "deterministic_skip=${DETERMINISTIC_SKIP}" >> "${GITHUB_OUTPUT}"
echo "det_skip_reason=${DET_SKIP_REASON}" >> "${GITHUB_OUTPUT}"
echo "claude_branch_review=${CLAUDE_BRANCH_REVIEW}" >> "${GITHUB_OUTPUT}"
echo "force_full_review_tier=${FORCE_FULL_REVIEW_TIER}" >> "${GITHUB_OUTPUT}"
# Propagate head ref so downstream jobs (deterministic-skip-merge)
# can apply the same head-ref-keyed auto-merge suppressors without
# repeating the /pulls/{n} fetch above (§15 API hygiene). May be
# empty on the invalid-PR-number branch; deterministic-skip-merge
# refuses merge authorization when the head ref is unavailable.
echo "head_ref=${pr_head_ref}" >> "${GITHUB_OUTPUT}"
# Bind downstream deterministic-skip merge authorization to the
# exact head evaluated by this gate. Consumers fail closed when the
# authenticated PR metadata did not provide a head SHA.
echo "head_sha=${pr_head_sha_gate}" >> "${GITHUB_OUTPUT}"
echo "auto_merge_enabled=${pr_auto_merge_enabled}" >> "${GITHUB_OUTPUT}"
echo "review_checkout_sha=${review_checkout_sha}" >> "${GITHUB_OUTPUT}"
echo "retargeted_base_ref=${pr_base_ref_gate}" >> "${GITHUB_OUTPUT}"
echo "retargeted_base_updated_at=${pr_updated_at_gate}" >> "${GITHUB_OUTPUT}"
echo "base_retargeted=${pr_base_retargeted}" >> "${GITHUB_OUTPUT}"
printf 'post_merge_pr_text_json=%s\n' "${POST_MERGE_PR_TEXT_JSON}" >> "${GITHUB_OUTPUT}"
printf 'post_merge_linked_issues_json=%s\n' "${POST_MERGE_LINKED_ISSUES_JSON}" >> "${GITHUB_OUTPUT}"
echo "post_merge_validate_context_definitely_empty=${POST_MERGE_VALIDATE_CONTEXT_DEFINITELY_EMPTY}" >> "${GITHUB_OUTPUT}"
echo "fingerprint_cap=${FINGERPRINT_CAP}" >> "${GITHUB_OUTPUT}"
echo "fingerprint_cap_fp=${FINGERPRINT_CAP_FP}" >> "${GITHUB_OUTPUT}"
echo "fingerprint_cap_reason=${FINGERPRINT_CAP_REASON}" >> "${GITHUB_OUTPUT}"
echo "fingerprint_cap_count=${FINGERPRINT_CAP_COUNT}" >> "${GITHUB_OUTPUT}"
echo "fingerprint_cap_max=${FINGERPRINT_CAP_MAX}" >> "${GITHUB_OUTPUT}"
echo "fingerprint_cap_already_applied=${FINGERPRINT_CAP_ALREADY_APPLIED}" >> "${GITHUB_OUTPUT}"
echo "fingerprint_cap_non_retryable=${FINGERPRINT_CAP_NON_RETRYABLE}" >> "${GITHUB_OUTPUT}"
echo "fingerprint_cap_marker_author_login=${gate_marker_author_login}" >> "${GITHUB_OUTPUT}"
