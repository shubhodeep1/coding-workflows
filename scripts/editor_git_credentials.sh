#!/usr/bin/env bash
# Temporarily hide checkout credentials from plan/implement editor processes.
set -u
marker="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/editor_git_credentials_hidden.txt"
action="${1:-}"
shift || true
case "${action}" in hide|restore) ;; *) echo 'usage: editor_git_credentials.sh hide|restore [repo...]' >&2; exit 2 ;; esac
# Implement exports GIT_DIR/GIT_WORK_TREE; git -C alone still uses that repo.
repo_git()
{
	env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES git -C "$@"
}
if [ "$#" -gt 0 ]; then
	repos=("$@")
else
	repos=("${GITHUB_WORKSPACE:-$PWD}" "${GITHUB_WORKSPACE:-$PWD}/.codex-workflow-src" "${GITHUB_WORKSPACE:-$PWD}/.codex-workflow-src-main")
fi

if [ "${action}" = hide ]; then
	# Do not discard a previous marker when a restore failed.
	if [ -s "${marker}" ]; then
		echo 'EDITOR_GIT_CREDENTIALS action=hide repo=all removed=none outcome=warn' >&2
		exit 0
	fi
	if ! : > "${marker}"; then
		echo 'EDITOR_GIT_CREDENTIALS action=hide repo=all removed=none outcome=warn' >&2
		exit 0
	fi
	for repo in "${repos[@]}"; do
		if ! repo_git "${repo}" rev-parse --git-dir >/dev/null 2>&1; then
			echo 'EDITOR_GIT_CREDENTIALS action=hide repo=non-git removed=none outcome=skip' >&2
			continue
		fi
		label="${repo##*/}"
		url="$(repo_git "${repo}" remote get-url origin 2>/dev/null || true)"
		if [[ "${url}" =~ ^https://[^/@]+@github\.com/ ]]; then
			clean="https://${url#*@}"
			if repo_git "${repo}" remote set-url origin "${clean}" >/dev/null 2>&1; then
				printf '%s\torigin\n' "${repo}" >> "${marker}"
				echo "EDITOR_GIT_CREDENTIALS action=hide repo=${label} removed=origin_url outcome=ok" >&2
			else
				echo "EDITOR_GIT_CREDENTIALS action=hide repo=${label} removed=none outcome=warn" >&2
			fi
		fi
		while IFS= read -r key; do
			[ -n "${key}" ] || continue
			if repo_git "${repo}" config --local --unset-all "${key}" >/dev/null 2>&1; then
				printf '%s\textraheader:%s\n' "${repo}" "${key}" >> "${marker}"
				echo "EDITOR_GIT_CREDENTIALS action=hide repo=${label} removed=extraheader outcome=ok" >&2
			else
				echo "EDITOR_GIT_CREDENTIALS action=hide repo=${label} removed=none outcome=warn" >&2
			fi
		done < <(repo_git "${repo}" config --local --name-only --get-regexp '^http\..*\.extraheader$' 2>/dev/null | sort -u)
	done
else
	[ -s "${marker}" ] || exit 0
	if [ -z "${GH_TOKEN:-}" ]; then
		echo 'EDITOR_GIT_CREDENTIALS action=restore repo=all removed=none outcome=warn' >&2
		exit 0
	fi
	failed=false
	while IFS=$'\t' read -r repo item; do
		if [ -z "${repo}" ] || [ -z "${item}" ]; then
			continue
		fi
		label="${repo##*/}"
		allowed=false
		for expected_repo in "${repos[@]}"; do
			if [ "${repo}" = "${expected_repo}" ]; then allowed=true; break; fi
		done
		if [ "${allowed}" != true ]; then
			echo 'EDITOR_GIT_CREDENTIALS action=restore repo=unexpected removed=none outcome=warn' >&2
			failed=true
			continue
		fi
		case "${item}" in
			origin)
				url="$(repo_git "${repo}" remote get-url origin 2>/dev/null || true)"
				# The marker lives in an editor-visible directory. Never inject a
				# token into an origin the editor changed to a different host.
				if [[ "${url}" =~ ^https://github\.com/ ]]; then
					repo_git "${repo}" remote set-url origin "https://x-access-token:${GH_TOKEN}@${url#https://}" >/dev/null 2>&1 || failed=true
				else failed=true; fi
				;;
			extraheader:http.https://github.com/*.extraheader)
				key="${item#extraheader:}"
				if [[ ! "${key}" =~ ^http\.https://github\.com/[^[:space:]]*\.extraheader$ ]]; then
					failed=true
					continue
				fi
				header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')"
				repo_git "${repo}" config --local "${key}" "${header}" >/dev/null 2>&1 || failed=true
				;;
			*) failed=true ;;
		esac
		echo "EDITOR_GIT_CREDENTIALS action=restore repo=${label} removed=${item%%:*} outcome=$([ "${failed}" = true ] && echo warn || echo ok)" >&2
	done < "${marker}"
	if [ "${failed}" = false ]; then
		: > "${marker}"
	fi
fi
exit 0
