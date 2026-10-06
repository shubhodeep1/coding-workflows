#!/usr/bin/env bash
# Temporarily hide checkout credentials from plan/implement editor processes.
#
# hide:    before an editor runs, strip the credentials from each checkout's
#          origin URL and remove its http.<url>.extraheader entries. The
#          marker records what was removed (never a secret).
# restore: after the editor exits, put the trusted step's GH_TOKEN back.
# check:   before staging, refuse editor-writable Git command drivers again.
#
# Fail-closed: the editor can write each checkout's git config, the global
# git config and the marker, so restore checks every checkout against its
# trusted repository identity before injecting any token, and injects
# nothing when one check fails. hide refuses before changing anything when a
# checkout is not in a known state. Callers run under `set -e`, so a refusal
# stops the step.
#
# Exit codes: 0 done (or nothing to restore), 1 refused, 2 usage.
set -u
marker="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/editor_git_credentials_hidden.txt"
action="${1:-}"
shift || true
case "${action}" in hide|restore|check) ;; *) echo 'usage: editor_git_credentials.sh hide|restore|check [repo...]' >&2; exit 2 ;; esac
# The workflows check the support source out from this repository.
support_repository="shubhodeep1/coding-workflows"
# Settings that could send a restored token elsewhere or run a command (Git
# filters, diff/merge drivers, LFS, signing, pack helpers) that receives it.
# Only system scope (root-owned) and command scope (this trusted
# step's GIT_CONFIG_* environment) may set them. Include directives are
# refused too: actions/checkout@v5 writes its header into .git/config, and a
# credential file pulled in by include would stay visible to the editor.
hazard_re='^(remote\.origin\.(pushurl|proxy)|url\..*\.(insteadof|pushinsteadof)|http\.(.*\.)?(proxy|sslverify|sslcainfo|sslcapath|sslcert|sslkey|curloptresolve|followredirects)|credential\.(.*\.)?helper|core\.(askpass|sshcommand|gitproxy|fsmonitor|attributesfile|alternaterefscommand)|include\.path|includeif\..*\.path|filter\..*\.(clean|smudge|process)|diff\.external|diff\..*\.(command|textconv)|merge\..*\.driver|gpg\.(.*\.)?program|remote\..*\.(uploadpack|receivepack)|uploadpack\.packobjectshook|lfs\..*)$'

# Implement exports GIT_DIR/GIT_WORK_TREE; git -C alone still uses that repo.
repo_git()
{
	env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES git -C "$@"
}

log_event()
{
	echo "EDITOR_GIT_CREDENTIALS action=${action} repo=${1} removed=${2} outcome=${3}${4:+ reason=${4}}" >&2
}

canonical_dir()
{
	(cd "$1" 2>/dev/null && pwd -P)
}

# True only for the top of a checkout, not a directory inside a parent repo.
is_checkout_root()
{
	local top canon
	[ -d "$1" ] || return 1
	top="$(repo_git "$1" rev-parse --show-toplevel 2>/dev/null)" || return 1
	canon="$(canonical_dir "$1")" || return 1
	[ -n "${top}" ] && [ "${top}" = "${canon}" ]
}

# The workspace holds the triggering repository; the others hold support source.
trusted_slug()
{
	local workspace
	workspace="$(canonical_dir "${GITHUB_WORKSPACE:-$PWD}")"
	if [ -n "${workspace}" ] && [ "$(canonical_dir "$1")" = "${workspace}" ]; then
		printf '%s' "${GITHUB_REPOSITORY}"
	else
		printf '%s' "${support_repository}"
	fi
}

# The origin URL without its user-info, or the URL unchanged when it has none.
without_auth()
{
	if [[ "$1" =~ ^https://[^/@]+@github\.com/ ]]; then
		printf 'https://%s' "${1#*@}"
	else
		printf '%s' "$1"
	fi
}

is_trusted_origin()
{
	local url="${1,,}" slug="${2,,}"
	[ "${url}" = "https://github.com/${slug}" ] || [ "${url}" = "https://github.com/${slug}.git" ]
}

# Prints the single origin URL; fails when there is none or more than one.
single_origin_url()
{
	local urls count
	urls="$(repo_git "$1" config --get-all remote.origin.url 2>/dev/null)" || return 1
	count="$(printf '%s\n' "${urls}" | grep -c .)"
	[ "${count}" = 1 ] || return 1
	printf '%s' "${urls}"
}

# True when a hazardous setting comes from an editor-writable scope (local,
# worktree, global), or when the lookup itself fails (fail-closed).
has_config_hazard()
{
	local out rc scope rest
	out="$(repo_git "$1" config --show-scope --get-regexp "${hazard_re}" 2>/dev/null)"
	rc=$?
	[ "${rc}" -eq 1 ] && return 1
	[ "${rc}" -eq 0 ] || return 0
	while IFS=$'\t' read -r scope rest; do
		[ -n "${rest}" ] || continue
		case "${scope}" in system|command) ;; *) return 0 ;; esac
	done <<< "${out}"
	return 1
}

# Same scope rule, but retain the caller's Git environment (notably GIT_DIR).
has_config_hazard_effective()
{
	local out rc scope rest
	out="$(git config --show-scope --get-regexp "${hazard_re}" 2>/dev/null)"
	rc=$?
	[ "${rc}" -eq 1 ] && return 1
	[ "${rc}" -eq 0 ] || return 0
	while IFS=$'\t' read -r scope rest; do
		[ -n "${rest}" ] || continue
		case "${scope}" in system|command) ;; *) return 0 ;; esac
	done <<< "${out}"
	return 1
}

# Git may read an untracked attributes file outside the repository content.
# Do not follow editor-writable symlinks, including dangling ones.
has_attributes_hazard()
{
	local info_path global_path attrs_path grep_rc
	info_path="$(repo_git "$1" rev-parse --git-path info/attributes 2>/dev/null)" || return 0
	case "${info_path}" in /*) ;; *) info_path="$1/${info_path}" ;; esac
	global_path="${XDG_CONFIG_HOME:-${HOME:-}/.config}/git/attributes"
	for attrs_path in "${info_path}" "${global_path}"; do
		if [ -L "${attrs_path}" ]; then return 0; fi
		if [ -e "${attrs_path}" ]; then
			[ -f "${attrs_path}" ] && [ -r "${attrs_path}" ] || return 0
			grep -E '^[[:space:]]*[^#[:space:]].*[[:space:]](filter|diff|merge)=[^[:space:]]+' "${attrs_path}" >/dev/null 2>&1
			grep_rc=$?
			[ "${grep_rc}" -eq 1 ] || return 0
		fi
	done
	return 1
}

valid_slug()
{
	[[ "$1" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]
}

if [ "$#" -gt 0 ]; then
	repos=("$@")
else
	repos=("${GITHUB_WORKSPACE:-$PWD}" "${GITHUB_WORKSPACE:-$PWD}/.codex-workflow-src" "${GITHUB_WORKSPACE:-$PWD}/.codex-workflow-src-main")
fi

if [ "${action}" = check ]; then
	refused=false
	if has_config_hazard_effective; then
		log_event effective none refused config_hazard
		refused=true
	fi
	for repo in "${repos[@]}"; do
		is_checkout_root "${repo}" || continue
		label="${repo##*/}"
		if has_config_hazard "${repo}"; then
			log_event "${label}" none refused config_hazard
			refused=true
		fi
		if has_attributes_hazard "${repo}"; then
			log_event "${label}" none refused attributes_hazard
			refused=true
		fi
	done
	[ "${refused}" = false ] || exit 1
	exit 0
fi

if [ -L "${marker}" ]; then
	log_event all none refused marker_symlink
	exit 1
fi

if [ "${action}" = hide ]; then
	if ! valid_slug "${GITHUB_REPOSITORY:-}"; then
		log_event all none refused repository_identity_missing
		exit 1
	fi
	# Do not discard a previous marker when a restore failed.
	if [ -s "${marker}" ]; then
		log_event all none refused marker_present
		exit 1
	fi
	# Validate every checkout before changing any of them.
	plan_lines=()
	refused=false
	for repo in "${repos[@]}"; do
		label="${repo##*/}"
		if ! is_checkout_root "${repo}"; then
			log_event non-git none skip not_a_checkout
			continue
		fi
		if ! url="$(single_origin_url "${repo}")"; then
			log_event "${label}" none refused origin_not_single
			refused=true
			continue
		fi
		clean="$(without_auth "${url}")"
		if ! is_trusted_origin "${clean}" "$(trusted_slug "${repo}")"; then
			log_event "${label}" none refused origin_untrusted
			refused=true
			continue
		fi
		if has_config_hazard "${repo}"; then
			log_event "${label}" none refused config_hazard
			refused=true
			continue
		fi
		if has_attributes_hazard "${repo}"; then
			log_event "${label}" none refused attributes_hazard
			refused=true
			continue
		fi
		inject=0
		[ "${url}" != "${clean}" ] && inject=1
		plan_lines+=("${repo}"$'\t'origin$'\t'"${clean}"$'\t'"${inject}")
		keys="$(repo_git "${repo}" config --local --name-only --get-regexp '^http\..*\.extraheader$' 2>/dev/null | sort -u)"
		while IFS= read -r key; do
			[ -n "${key}" ] || continue
			if [[ ! "${key}" =~ ^http\.https://github\.com/[^@[:space:]]*\.extraheader$ ]]; then
				log_event "${label}" none refused extraheader_scope
				refused=true
				continue
			fi
			plan_lines+=("${repo}"$'\t'"extraheader:${key}")
		done <<< "${keys}"
		if repo_git "${repo}" config --local --get-regexp '^http\.extraheader$' >/dev/null 2>&1; then
			log_event "${label}" none refused extraheader_unscoped
			refused=true
		fi
	done
	if [ "${refused}" = true ]; then
		exit 1
	fi
	# Record before removing, so an interrupted hide can still be restored.
	if ! (umask 077 && : > "${marker}") || ! { [ "${#plan_lines[@]}" -eq 0 ] || printf '%s\n' "${plan_lines[@]}" > "${marker}"; }; then
		log_event all none refused marker_unwritable
		exit 1
	fi
	failed=false
	for line in "${plan_lines[@]}"; do
		IFS=$'\t' read -r repo item clean inject <<< "${line}"
		label="${repo##*/}"
		case "${item}" in
			origin)
				[ "${inject}" = 1 ] || continue
				if repo_git "${repo}" remote set-url origin "${clean}" >/dev/null 2>&1; then
					log_event "${label}" origin_url ok
				else
					log_event "${label}" none refused set_url_failed
					failed=true
				fi
				;;
			extraheader:*)
				if repo_git "${repo}" config --local --unset-all "${item#extraheader:}" >/dev/null 2>&1; then
					log_event "${label}" extraheader ok
				else
					log_event "${label}" none refused unset_failed
					failed=true
				fi
				;;
		esac
	done
	[ "${failed}" = false ] || exit 1
	exit 0
fi

# restore
if [ ! -s "${marker}" ]; then
	log_event all none skip marker_missing
	exit 0
fi
if [ -z "${GH_TOKEN:-}" ]; then
	log_event all none warn token_missing
	exit 1
fi
if ! valid_slug "${GITHUB_REPOSITORY:-}"; then
	log_event all none refused repository_identity_missing
	exit 1
fi
# First pass: check every marker entry and checkout; inject nothing yet.
declare -A origin_clean=() origin_inject=()
header_items=()
refused=false
while IFS=$'\t' read -r repo item clean inject; do
	[ -n "${repo}${item}" ] || continue
	label="${repo##*/}"
	allowed=false
	for expected_repo in "${repos[@]}"; do
		if [ "${repo}" = "${expected_repo}" ]; then allowed=true; break; fi
	done
	if [ "${allowed}" != true ]; then
		log_event unexpected none refused repo_unexpected
		refused=true
		continue
	fi
	case "${item}" in
		origin)
			if [ -z "${clean:-}" ] || [[ ! "${inject:-}" =~ ^[01]$ ]]; then
				log_event "${label}" none refused origin_unrecorded
				refused=true
				continue
			fi
			origin_clean["${repo}"]="${clean}"
			origin_inject["${repo}"]="${inject}"
			;;
		extraheader:*)
			header_items+=("${repo}"$'\t'"${item#extraheader:}")
			;;
		*)
			log_event "${label}" none refused item_unknown
			refused=true
			;;
	esac
done < "${marker}"
for repo in "${!origin_clean[@]}"; do
	label="${repo##*/}"
	clean="${origin_clean[${repo}]}"
	if ! is_checkout_root "${repo}"; then
		log_event "${label}" none refused not_a_checkout
		refused=true
		continue
	fi
	if ! is_trusted_origin "${clean}" "$(trusted_slug "${repo}")"; then
		log_event "${label}" none refused origin_untrusted
		refused=true
		continue
	fi
	# The editor must leave origin exactly as hide left it.
	if ! url="$(single_origin_url "${repo}")" || [ "${url}" != "${clean}" ]; then
		log_event "${label}" none refused origin_changed
		refused=true
		continue
	fi
	if has_config_hazard "${repo}"; then
		log_event "${label}" none refused config_hazard
		refused=true
	fi
	if has_attributes_hazard "${repo}"; then
		log_event "${label}" none refused attributes_hazard
		refused=true
	fi
done
for header in "${header_items[@]}"; do
	IFS=$'\t' read -r repo key <<< "${header}"
	label="${repo##*/}"
	if [ -z "${origin_clean[${repo}]+set}" ]; then
		log_event "${label}" none refused header_without_origin
		refused=true
		continue
	fi
	# Only the broad checkout header or one already scoped to this origin.
	if [ "${key}" != "http.https://github.com/.extraheader" ] && [ "${key}" != "http.${origin_clean[${repo}]}.extraheader" ]; then
		log_event "${label}" none refused extraheader_scope
		refused=true
	fi
done
if [ "${refused}" = true ]; then
	exit 1
fi
# Second pass: every checkout is valid, so put the token back.
failed=false
for repo in "${!origin_clean[@]}"; do
	label="${repo##*/}"
	clean="${origin_clean[${repo}]}"
	if [ "${origin_inject[${repo}]}" = 1 ]; then
		if repo_git "${repo}" remote set-url origin "https://x-access-token:${GH_TOKEN}@${clean#https://}" >/dev/null 2>&1; then
			log_event "${label}" origin ok
		else
			log_event "${label}" none warn set_url_failed
			failed=true
		fi
	fi
done
header_value="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')"
for header in "${header_items[@]}"; do
	IFS=$'\t' read -r repo key <<< "${header}"
	label="${repo##*/}"
	# Scope the header to this checkout's origin, never to all of github.com.
	if repo_git "${repo}" config --local "http.${origin_clean[${repo}]}.extraheader" "${header_value}" >/dev/null 2>&1; then
		log_event "${label}" extraheader ok
	else
		log_event "${label}" none warn header_failed
		failed=true
	fi
done
if [ "${failed}" = true ]; then
	exit 1
fi
: > "${marker}"
exit 0
