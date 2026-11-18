#!/usr/bin/env bash
# review_workflow_sync_skip.sh — review-gate check for consumer sync PRs (#7128).
#
# The consumer updater (update_workflows.yml) delivers verified upstream
# changes through a pull request from the ai-sync/stable branch. The review
# gate of review_autofix.yml skips the reviewer panel for such a PR only when
# every changed path (rename sources included) is in the signed sync
# allowlist that scripts/build_sync_manifest.py derives from this support
# checkout. A skip grants no authority: the gate never labels, comments,
# posts a success status or merges the PR.
#
# Usage (sourced): review_workflow_sync_skip_applies <head_ref> <paths_file> <support_root>
#   Returns 0 to skip review, 1 for normal review; sets
#   REVIEW_WORKFLOW_SYNC_SKIP_REASON. Any error means normal review.
# Usage (CLI): review_workflow_sync_skip.sh check <head_ref> <paths_file> <support_root>
#   Prints "skip" and exits 0, or prints "review reason=<token>" and exits 1.
# No GitHub API calls; the caller supplies the PR's file list.

REVIEW_WORKFLOW_SYNC_BRANCH="ai-sync/stable"
REVIEW_WORKFLOW_SYNC_SKIP_REASON=""

review_workflow_sync_skip_applies()
{
	local head_ref="${1:-}" paths_file="${2:-}" support_root="${3:-}"
	local builder allowed_file rc=0
	REVIEW_WORKFLOW_SYNC_SKIP_REASON="head_ref_not_sync"
	[ "${head_ref}" = "${REVIEW_WORKFLOW_SYNC_BRANCH}" ] || return 1
	REVIEW_WORKFLOW_SYNC_SKIP_REASON="paths_unavailable"
	if [ -z "${paths_file}" ] || [ ! -f "${paths_file}" ] || [ ! -r "${paths_file}" ] || [ ! -s "${paths_file}" ]; then
		return 1
	fi
	REVIEW_WORKFLOW_SYNC_SKIP_REASON="builder_missing"
	builder="${support_root}/scripts/build_sync_manifest.py"
	[ -n "${support_root}" ] && [ -f "${builder}" ] || return 1
	REVIEW_WORKFLOW_SYNC_SKIP_REASON="allowlist_unavailable"
	allowed_file="$(mktemp)" || return 1
	PYTHONDONTWRITEBYTECODE=1 python3 -I "${builder}" allowed-paths --repo-root "${support_root}" > "${allowed_file}" 2>/dev/null || rc=$?
	if [ "${rc}" -ne 0 ] || [ ! -s "${allowed_file}" ]; then
		rm -f "${allowed_file}"
		return 1
	fi
	PYTHONDONTWRITEBYTECODE=1 python3 -I "${builder}" check-paths --allowed-file "${allowed_file}" --paths-file "${paths_file}" >/dev/null 2>&1 || rc=$?
	rm -f "${allowed_file}"
	case "${rc}" in
		0)
			REVIEW_WORKFLOW_SYNC_SKIP_REASON="managed_paths_only"
			return 0
			;;
		3) REVIEW_WORKFLOW_SYNC_SKIP_REASON="unmanaged_path" ;;
		*) REVIEW_WORKFLOW_SYNC_SKIP_REASON="path_check_failed" ;;
	esac
	return 1
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
	case "${1:-}" in
		check)
			shift
			if review_workflow_sync_skip_applies "$@"; then
				echo "skip"
				exit 0
			fi
			echo "review reason=${REVIEW_WORKFLOW_SYNC_SKIP_REASON}"
			exit 1
			;;
		*)
			echo "usage: review_workflow_sync_skip.sh check <head_ref> <paths_file> <support_root>" >&2
			exit 2
			;;
	esac
fi
