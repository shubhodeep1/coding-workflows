#!/usr/bin/env bash
# scripts/orchestrate_stall_retired_conflict.sh
#
# Issue #6680 helpers for orchestrator stall recovery, sourced by
# scripts/orchestrate_poll_process.sh (never executed on its own):
#   _stall_retired_conflict_branch_ok <branch>
#   _stall_pr_json_may_conflict <pr_json>
#   _stall_retired_host_only_conflict_check <issue_num> <pr_num> [pr_json]
#
# They live in their own file because ShellCheck's CI run on the
# ~27,000-line poller did not finish once they were inlined there (the
# static-checks runner shut down mid-file). The poller declares the
# STALL_RETIRED_CONFLICT_* / STALL_REISSUE_EXTRA_GUIDANCE* globals and
# provides _fetch_pr_json, _pr_json_is_issue_implementation_pr and
# _list_integration_conflict_files. When this file is missing the poller
# defines fail-closed stubs, so the resolver dispatch is kept.
#
# See the comment above the source block in orchestrate_poll_process.sh for
# the full contract, API budget, kill switch
# (STALL_RETIRED_CONFLICT_REISSUE_ENABLED, default true) and log prefix
# (STALL_RETIRED_CONFLICT_REISSUE).

_stall_retired_conflict_branch_ok() {
  local branch="$1"
  [[ "${branch}" =~ ^[A-Za-z0-9._/-]{1,200}$ ]] || return 1
  case "${branch}" in
    -*|*..*|*//*|/*|*/) return 1 ;;
  esac
  git check-ref-format "refs/heads/${branch}" >/dev/null 2>&1
}

# Keep a retired-conflict replacement on the PR's base, not on an old issue
# target (which may be the closed PR's head). Main needs no routing header.
_stall_retired_conflict_reissue_body() {
  local original_body="$1" base_ref="$2"
  printf '%s\n' "${original_body}" | sed -E '/^[[:space:]]*(-[[:space:]]*)?(\*\*Target branch:\*\*|Target branch:)[[:space:]]*/d'
  if [ "${base_ref}" != "${DEFAULT_BRANCH:-main}" ]; then
    printf -- '- **Target branch:** `%s`\n' "${base_ref}"
  fi
}

# _stall_pr_json_may_conflict <pr_json>: returns 1 only when the REST PR
# payload says the PR merges cleanly (mergeable true and not dirty), so a
# clean PR skips the local merge-tree probe. Unknown mergeability returns 0
# and the probe decides. No API calls.
_stall_pr_json_may_conflict() {
  local _spmc_json="${1:-}" _spmc_mergeable _spmc_state
  _spmc_mergeable="$(printf '%s' "${_spmc_json}" | jq -r 'if .mergeable == null then "" else (.mergeable | tostring) end' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")"
  _spmc_state="$(printf '%s' "${_spmc_json}" | jq -r '(.mergeable_state // empty)' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")"
  if [ "${_spmc_mergeable}" = "true" ] && [ "${_spmc_state}" != "dirty" ]; then
    return 1
  fi
  return 0
}

_stall_retired_host_only_conflict_check() {
  local issue_num="$1"
  local pr_num="$2"
  local pr_json="${3:-}"
  STALL_REISSUE_EXTRA_GUIDANCE=""
  STALL_RETIRED_CONFLICT_PATHS=""
  STALL_RETIRED_CONFLICT_BASE=""
  STALL_RETIRED_CONFLICT_CLOSE_MESSAGE=""
  STALL_REISSUE_EXTRA_GUIDANCE_ISSUE=""

  _srcc_skip() {
    echo "STALL_RETIRED_CONFLICT_REISSUE issue=${issue_num} pr=${pr_num} outcome=skip reason=$1 retired_paths=${2:-none}"
  }

  if [ "${STALL_RETIRED_CONFLICT_REISSUE_ENABLED:-true}" = "false" ]; then
    _srcc_skip disabled
    return 1
  fi
  [[ "${pr_num}" =~ ^[0-9]+$ ]] || { _srcc_skip invalid_pr; return 1; }

  local support_dir="" candidate
  for candidate in "${GITHUB_WORKSPACE:-$PWD}/.codex-workflow-src" "${GITHUB_WORKSPACE:-$PWD}/.codex-workflow-src-main"; do
    if [ -d "${candidate}" ] && [ ! -L "${candidate}" ] \
      && [ -f "${candidate}/workflow-templates/retired_files.txt" ] \
      && [ -f "${candidate}/scripts/orchestrate_lib.py" ] && [ ! -L "${candidate}/scripts/orchestrate_lib.py" ]; then
      support_dir="${candidate}"
      break
    fi
  done
  if [ -z "${support_dir}" ]; then
    _srcc_skip support_unavailable
    return 1
  fi

  if [ -z "$(printf '%s' "${pr_json}" | jq -r '(.base.ref // empty)' 2>/dev/null)" ] \
    || [ -z "$(printf '%s' "${pr_json}" | jq -r '(.head.repo.full_name // empty)' 2>/dev/null)" ]; then
    pr_json="$(_fetch_pr_json "${pr_num}")"
  fi
  local pr_state head_repo head_ref head_sha base_ref
  pr_state="$(printf '%s' "${pr_json}" | jq -r '(.state // empty)' 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo "")"
  head_repo="$(printf '%s' "${pr_json}" | jq -r '(.head.repo.full_name // empty)' 2>/dev/null || echo "")"
  head_ref="$(printf '%s' "${pr_json}" | jq -r '(.head.ref // empty)' 2>/dev/null || echo "")"
  head_sha="$(printf '%s' "${pr_json}" | jq -r '(.head.sha // empty)' 2>/dev/null || echo "")"
  base_ref="$(printf '%s' "${pr_json}" | jq -r '(.base.ref // empty)' 2>/dev/null || echo "")"
  if [ "${pr_state}" != "open" ]; then
    _srcc_skip pr_not_open
    return 1
  fi
  # Only the issue's own implementation PR (ai/issue-<n> head or a closing
  # keyword) may be closed and re-issued; a mention-only cross-reference
  # keeps the existing resolver path.
  if ! _pr_json_is_issue_implementation_pr "${issue_num}" "${pr_json}"; then
    _srcc_skip not_implementation_pr
    return 1
  fi
  if [ -z "${head_repo}" ] || [ "${head_repo,,}" != "${GITHUB_REPOSITORY,,}" ]; then
    _srcc_skip head_not_same_repo
    return 1
  fi
  if ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]] || ! _stall_retired_conflict_branch_ok "${head_ref}" || ! _stall_retired_conflict_branch_ok "${base_ref}"; then
    _srcc_skip invalid_pr_refs
    return 1
  fi

  # _list_integration_conflict_files ignores a failed fetch and would then
  # probe whatever refs are cached locally. A stale base could still show
  # the retired file as deleted after the base restored it, so refresh both
  # refs here and keep the resolver dispatch when that fails.
  if ! git fetch --no-tags --quiet origin \
    "+refs/heads/${base_ref}:refs/remotes/origin/${base_ref}" \
    "+refs/heads/${head_ref}:refs/remotes/origin/${head_ref}" 2>/dev/null; then
    _srcc_skip fetch_failed
    return 1
  fi

  local conflicts
  if ! conflicts="$(_list_integration_conflict_files "${head_ref}" "${base_ref}" 2>/dev/null)" || [ -z "${conflicts}" ]; then
    _srcc_skip probe_unavailable
    return 1
  fi
  local head_tip
  head_tip="$(git rev-parse --verify --quiet "refs/remotes/origin/${head_ref}^{commit}" 2>/dev/null || echo "")"
  if [ "${head_tip}" != "${head_sha}" ]; then
    _srcc_skip head_moved
    return 1
  fi

  local work_dir
  work_dir="$(mktemp -d "${RUNTIME_DIR:-/tmp}/retired-conflict.XXXXXX" 2>/dev/null)" || { _srcc_skip probe_unavailable; return 1; }
  # Drop the merge-tree tree OID line even where awk lacks interval support
  # (mawk), so it is never mistaken for a conflicted path.
  printf '%s\n' "${conflicts}" | sed '/^$/d' | sed '1{/^[0-9a-f]\{40\}$/d;/^[0-9a-f]\{64\}$/d;}' > "${work_dir}/conflicts.txt"
  # Classify only a complete conflict set: a truncated list could hide a
  # live host-only path, so an oversized set keeps the resolver dispatch.
  if [ "$(wc -l < "${work_dir}/conflicts.txt")" -gt 500 ]; then
    rm -rf "${work_dir}" 2>/dev/null || true
    _srcc_skip too_many_conflicts
    return 1
  fi
  : > "${work_dir}/base_present.txt"
  local conflict_path
  while IFS= read -r conflict_path; do
    [ -n "${conflict_path}" ] || continue
    [[ "${conflict_path}" =~ ^[A-Za-z0-9_.][A-Za-z0-9._/-]{0,199}$ ]] || continue
    if git cat-file -e "refs/remotes/origin/${base_ref}:${conflict_path}" 2>/dev/null; then
      printf '%s\n' "${conflict_path}" >> "${work_dir}/base_present.txt"
    fi
  done < "${work_dir}/conflicts.txt"

  local result decision reason retired_csv
  result="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${support_dir}/scripts/orchestrate_lib.py" retired-conflict-check \
    --support-dir "${support_dir}" \
    --conflict-paths-file "${work_dir}/conflicts.txt" \
    --base-present-file "${work_dir}/base_present.txt" 2>/dev/null || echo "")"
  decision="$(printf '%s' "${result}" | jq -r '.decision // empty' 2>/dev/null || echo "")"
  reason="$(printf '%s' "${result}" | jq -r '.reason // empty' 2>/dev/null || echo "")"
  [[ "${reason}" =~ ^[a-z_]{1,64}$ ]] || reason="unavailable"
  retired_csv="$(printf '%s' "${result}" | jq -r '(.retired_paths // []) | join(",")' 2>/dev/null || echo "")"
  if [ "${decision}" != "reissue" ] || [ -z "${retired_csv}" ]; then
    rm -rf "${work_dir}" 2>/dev/null || true
    _srcc_skip "${reason}" "${retired_csv}"
    return 1
  fi

  # Files the closed PR changed (local git, capped), so its still-relevant
  # work is listed in the re-issue instead of being dropped silently.
  local merge_base changed_list="" changed_count=0 odd_count=0 changed_path
  merge_base="$(git merge-base "refs/remotes/origin/${base_ref}" "refs/remotes/origin/${head_ref}" 2>/dev/null || echo "")"
  if [ -n "${merge_base}" ]; then
    while IFS= read -r changed_path; do
      [ -n "${changed_path}" ] || continue
      if ! [[ "${changed_path}" =~ ^[A-Za-z0-9_.][A-Za-z0-9._/-]{0,199}$ ]]; then
        odd_count=$((odd_count + 1))
        continue
      fi
      changed_count=$((changed_count + 1))
      [ "${changed_count}" -le 50 ] || continue
      changed_list="${changed_list:+${changed_list}, }\`${changed_path}\`"
    done < <(git diff --name-only "${merge_base}" "refs/remotes/origin/${head_ref}" 2>/dev/null)
  fi
  [ -n "${changed_list}" ] || changed_list="(unavailable)"
  [ "${changed_count}" -le 50 ] || changed_list="${changed_list} and $((changed_count - 50)) more"
  [ "${odd_count}" -eq 0 ] || changed_list="${changed_list} (plus ${odd_count} file(s) with unusual names)"
  rm -rf "${work_dir}" 2>/dev/null || true

  local retired_md
  retired_md="$(printf '%s' "${result}" | jq -r '(.retired_paths // []) | map("`" + . + "`") | join(", ")' 2>/dev/null || echo "")"
  STALL_RETIRED_CONFLICT_PATHS="${retired_csv}"
  STALL_RETIRED_CONFLICT_BASE="${base_ref}"
  STALL_REISSUE_EXTRA_GUIDANCE="**Closed PR #${pr_num} conflicts with \`${base_ref}\` on retired file(s):** ${retired_md}. These files are listed in \`workflow-templates/retired_files.txt\` and \`${base_ref}\` no longer has them, so the conflict resolver cannot merge them in its sandbox. Recreate this PR's still-relevant changes against the current \`${base_ref}\`. Files the closed PR changed: ${changed_list}. Its edits to the retired file(s) are not carried over automatically: move any intent that still matters into the file that replaced them, or say in the PR why it is obsolete. Closed PR for reference: #${pr_num} (branch \`${head_ref}\` kept)."
  STALL_RETIRED_CONFLICT_CLOSE_MESSAGE="Closed by orchestrator stall recovery — this PR conflicts with \`${base_ref}\` on retired host-only file(s) ${retired_md}, which the conflict resolver cannot merge in its sandbox. Issue #${issue_num} is re-issued against the current \`${base_ref}\`; the replacement lists this PR's changed files. The branch is kept for reference."
  STALL_REISSUE_EXTRA_GUIDANCE_ISSUE="${issue_num}"
  echo "STALL_RETIRED_CONFLICT_REISSUE issue=${issue_num} pr=${pr_num} outcome=reissue reason=${reason} retired_paths=${retired_csv}"
  return 0
}
