#!/usr/bin/env bash
# Release tag credential preflight (analysis/workflow-optimization-2026-09-22.md,
# Speed #4 / Reliability #2).
#
# Release run 35639756385 spent about 94 minutes in test jobs before the tag
# push was refused because the classic GH_PAT lacked the `workflow` scope and
# the release range changed `.github/workflows/`. This check runs in the
# `resolve-version` job of test-and-mark-stable.yml and mark-stable.yml, before
# the expensive jobs, and fails the run early for that one case.
#
# Why credential introspection and not a push: `git push --dry-run` never
# reaches GitHub's server-side permission checks, and pushing a temporary tag
# would be a mutation. A classic PAT reports its scopes in the `X-OAuth-Scopes`
# response header, which `GET /user` returns.
#
# Inputs (environment, all optional):
#   GH_TOKEN                        the release credential (secrets.GH_PAT only)
#   RELEASE_TAG_PREFLIGHT_ENABLED   unset, empty or `true` (any case) = on;
#                                   `false` = off; anything else = off + warning
#   RELEASE_TAG_PREFLIGHT_BASE_SHA  commit of the current `stable` tag (40-hex)
#   RELEASE_TAG_PREFLIGHT_HEAD_SHA  commit being released (40-hex)
#   RELEASE_TAG_PREFLIGHT_GIT_DIR   repository to diff in (default `.`)
#   STABLE_RELEASE_ENABLED / RELEASE_TAGGING_ENABLED
#                                   publishing switches; when either is off no
#                                   tag is pushed, so the check is skipped
#
# Output: one line
#   RELEASE_TAG_PREFLIGHT result=<pass|fail|unknown|skipped> error_class=<workflow_scope|none> reason=<token>
#
# Fails (exit 1) ONLY when the scopes header is present (classic PAT), lacks
# `workflow`, and the release range changes a path under `.github/workflows/`.
# Every uncertain case (no GH_PAT, no header, API, git or input error) prints
# result=unknown and exits 0. The token, the response body and every header
# other than the scope header are never printed; the scope value is not
# printed either.

set -uo pipefail

release_tag_preflight_emit() {
	printf 'RELEASE_TAG_PREFLIGHT result=%s error_class=%s reason=%s\n' "$1" "$2" "$3"
}

# Returns 0 when on, 1 when off. Prints a warning (without the value) for an
# unrecognised value, which counts as off.
release_tag_preflight_switch_on() {
	local raw="${1-}"
	local name="${2:-switch}"
	local value
	value="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
	case "${value}" in
		''|true) return 0 ;;
		false) return 1 ;;
		*)
			echo "::warning::${name} has an unrecognised value; treating it as off. Use true or false." >&2
			return 2
			;;
	esac
}

# Prints the X-OAuth-Scopes value from a `gh api -i` header block. Returns 2
# when the header is absent.
release_tag_preflight_header_scopes() {
	local headers_file="$1"
	awk '
		BEGIN { found = 0 }
		{ sub(/\r$/, "") }
		/^$/ { exit }
		{
			line = $0
			if (tolower(substr(line, 1, 15)) == "x-oauth-scopes:") {
				print substr(line, 16)
				found = 1
				exit
			}
		}
		END { exit (found ? 0 : 2) }
	' "${headers_file}"
}

release_tag_preflight_main() {
	local switch_rc=0
	release_tag_preflight_switch_on "${RELEASE_TAG_PREFLIGHT_ENABLED-}" RELEASE_TAG_PREFLIGHT_ENABLED || switch_rc=$?
	if [ "${switch_rc}" -eq 1 ]; then
		release_tag_preflight_emit skipped none disabled
		return 0
	fi
	if [ "${switch_rc}" -ne 0 ]; then
		release_tag_preflight_emit skipped none invalid_switch
		return 0
	fi

	# Publishing switches: same rules as the release job (unset/empty/true on).
	local publish_value
	for publish_value in "${STABLE_RELEASE_ENABLED-}" "${RELEASE_TAGGING_ENABLED-}"; do
		publish_value="$(printf '%s' "${publish_value}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
		if [ -n "${publish_value}" ] && [ "${publish_value}" != "true" ]; then
			release_tag_preflight_emit skipped none publishing_disabled
			return 0
		fi
	done

	if [ -z "${GH_TOKEN:-}" ]; then
		release_tag_preflight_emit unknown none gh_pat_missing
		return 0
	fi

	local base_sha="${RELEASE_TAG_PREFLIGHT_BASE_SHA:-}"
	local head_sha="${RELEASE_TAG_PREFLIGHT_HEAD_SHA:-}"
	if ! [[ "${base_sha}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
		release_tag_preflight_emit unknown none invalid_range
		return 0
	fi

	local work_dir
	if ! work_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/release_tag_preflight.XXXXXX" 2>/dev/null)"; then
		release_tag_preflight_emit unknown none tmpdir_unavailable
		return 0
	fi
	chmod 700 "${work_dir}" 2>/dev/null || true
	# shellcheck disable=SC2064
	trap "rm -rf '${work_dir}'" EXIT
	local headers_file="${work_dir}/headers"

	# GitHub API audit (unattended_system_instructions.md §14): one GET /user
	# call. No existing call in the `source` or `resolve-version` jobs returns
	# response headers, and the X-OAuth-Scopes header is the only place a
	# classic PAT reports its scopes, so no existing call can be reused.
	# stderr is discarded so no response text reaches the log.
	if ! (umask 077 && gh api -i user >"${headers_file}" 2>/dev/null); then
		release_tag_preflight_emit unknown none api_failed
		return 0
	fi

	local scopes=""
	local scopes_rc=0
	scopes="$(release_tag_preflight_header_scopes "${headers_file}")" || scopes_rc=$?
	if [ "${scopes_rc}" -ne 0 ]; then
		release_tag_preflight_emit unknown none no_scope_header
		return 0
	fi

	local scope
	local has_workflow=false
	local IFS_SAVE="${IFS}"
	IFS=','
	for scope in ${scopes}; do
		scope="$(printf '%s' "${scope}" | tr -d '[:space:]')"
		if [ "${scope}" = "workflow" ]; then
			has_workflow=true
		fi
	done
	IFS="${IFS_SAVE}"
	if [ "${has_workflow}" = "true" ]; then
		release_tag_preflight_emit pass none workflow_scope_present
		return 0
	fi

	local git_dir="${RELEASE_TAG_PREFLIGHT_GIT_DIR:-.}"
	local changed_file="${work_dir}/changed"
	if ! git -C "${git_dir}" diff --name-only "${base_sha}..${head_sha}" >"${changed_file}" 2>/dev/null; then
		release_tag_preflight_emit unknown none git_failed
		return 0
	fi
	local workflow_changes
	workflow_changes="$(grep -c '^\.github/workflows/' "${changed_file}" 2>/dev/null || true)"
	case "${workflow_changes}" in
		''|*[!0-9]*) workflow_changes=0 ;;
	esac
	if [ "${workflow_changes}" -eq 0 ]; then
		release_tag_preflight_emit pass none no_workflow_changes
		return 0
	fi

	release_tag_preflight_emit fail workflow_scope classic_pat_missing_workflow_scope
	echo "::error::RELEASE_TAG_PREFLIGHT: GH_PAT is a classic token without the \`workflow\` scope, and the release range changes ${workflow_changes} file(s) under .github/workflows/, so GitHub would refuse the tag push. Give GH_PAT the \`workflow\` scope (classic PAT), or switch to a fine-grained PAT / GitHub App with \`workflows: write\`, then re-run the release. Set RELEASE_TAG_PREFLIGHT_ENABLED=false to skip this check."
	return 1
}

release_tag_preflight_main
