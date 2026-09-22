#!/usr/bin/env bash
# Trusted, hook-free commit and exact-ref push boundary for model-produced edits.
set -euo pipefail

die()
{
	echo "trusted_git_write: $*" >&2
	exit 2
}

is_sha()
{
	[[ "${1:-}" =~ ^[0-9a-f]{40,64}$ ]]
}

git_bin=""
for candidate_git_bin in /usr/bin/git /bin/git; do
	if [ -x "${candidate_git_bin}" ]; then
		git_bin="${candidate_git_bin}"
		break
	fi
done
[ -n "${git_bin}" ] || die "trusted Git executable is unavailable"

operation="${1:-}"
[ -n "${operation}" ] || die "operation is required"
shift
repo=""
message=""
message_file=""
branch=""
expected_local_head=""
expected_remote_head=""
remote_url=""
allow_empty=false
while [ "$#" -gt 0 ]; do
	case "$1" in
		--repo) repo="${2:-}"; shift 2 ;;
		--message) message="${2:-}"; shift 2 ;;
		--message-file) message_file="${2:-}"; shift 2 ;;
		--branch) branch="${2:-}"; shift 2 ;;
		--expected-local-head) expected_local_head="${2:-}"; shift 2 ;;
		--expected-remote-head) expected_remote_head="${2:-}"; shift 2 ;;
		--remote-url) remote_url="${2:-}"; shift 2 ;;
		--allow-empty) allow_empty=true; shift ;;
		*) die "unknown argument: $1" ;;
	esac
done
[ -n "${repo}" ] || die "--repo is required"
repo="$(cd "${repo}" && pwd -P)"
repo_git_dir="$(${git_bin} -C "${repo}" rev-parse --absolute-git-dir 2>/dev/null)" \
	|| die "repository is invalid"
case "${repo_git_dir}" in
	"${repo}"/*|"${repo}"/.git) ;;
	*)
		# Linked worktrees legitimately keep metadata in the common repository.
		${git_bin} -C "${repo}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
			|| die "repository metadata is outside the validated worktree"
		;;
esac

trusted_git=(
	env -u GH_TOKEN -u GH_PAT -u GITHUB_TOKEN -u SSH_AUTH_SOCK
	GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
	"${git_bin}" -C "${repo}"
	-c core.hooksPath=/dev/null
	-c core.fsmonitor=false
	-c commit.gpgSign=false
	-c tag.gpgSign=false
	-c user.name=codex-bot
	-c user.email=codex@users.noreply.github.com
)

case "${operation}" in
	commit)
		if [ -n "${expected_local_head}" ]; then
			is_sha "${expected_local_head}" || die "invalid expected local head"
			actual_head="$(${git_bin} -C "${repo}" rev-parse HEAD)"
			[ "${actual_head}" = "${expected_local_head}" ] || die "local head changed before commit"
		fi
		[ -z "${message}" ] || [ -z "${message_file}" ] || die "choose one commit-message source"
		if [ -n "${message_file}" ]; then
			[ -f "${message_file}" ] || die "commit message file is unreadable"
		elif [ -z "${message}" ]; then
			die "commit message is required"
		fi
		if [ "${allow_empty}" != true ] && ${git_bin} -C "${repo}" diff --cached --quiet --exit-code; then
			die "refusing an empty commit"
		fi
		while IFS= read -r staged_path; do
			[ -n "${staged_path}" ] || continue
			case "${staged_path}" in
				/*|../*|*/../*|*$'\n'*|*$'\r'*) die "unsafe staged path" ;;
			esac
		done < <(${git_bin} -C "${repo}" diff --cached --name-only)
		commit_args=(commit --no-verify)
		[ "${allow_empty}" = true ] && commit_args+=(--allow-empty)
		if [ -n "${message_file}" ]; then
			commit_args+=(-F "${message_file}")
		else
			commit_args+=(-m "${message}")
		fi
		"${trusted_git[@]}" "${commit_args[@]}"
		;;
	push)
		[ -n "${branch}" ] || die "--branch is required"
		branch="${branch#refs/heads/}"
		${git_bin} check-ref-format "refs/heads/${branch}" >/dev/null 2>&1 \
			|| die "invalid destination branch"
		is_sha "${expected_local_head}" || die "invalid expected local head"
		if [ "${expected_remote_head}" != "absent" ]; then
			is_sha "${expected_remote_head}" || die "invalid expected remote head"
		fi
		actual_head="$(${git_bin} -C "${repo}" rev-parse HEAD)"
		[ "${actual_head}" = "${expected_local_head}" ] || die "local head changed before push"
		if [ -z "${remote_url}" ]; then
			remote_url="$(${git_bin} -C "${repo}" remote get-url origin 2>/dev/null)" \
				|| die "origin URL is unavailable"
		fi
		expected_repository="${GITHUB_REPOSITORY:-}"
		expected_server_url="${GITHUB_SERVER_URL:-https://github.com}"
		[[ "${expected_repository}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
			|| die "GITHUB_REPOSITORY is invalid"
		remote_url="$(python3 - "${remote_url}" "${expected_repository}" "${expected_server_url}" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

value, expected_repository, expected_server_url = sys.argv[1:]
parts = urlsplit(value)
expected_server = urlsplit(expected_server_url)
if parts.scheme != "https" or expected_server.scheme != "https" or not parts.hostname:
	raise SystemExit(2)
host = parts.hostname
if parts.port is not None:
	host = f"{host}:{parts.port}"
expected_host = expected_server.hostname
if expected_server.port is not None:
	expected_host = f"{expected_host}:{expected_server.port}"
repository_path = parts.path.strip("/")
if repository_path.endswith(".git"):
	repository_path = repository_path[:-4]
if host.lower() != str(expected_host).lower() or repository_path != expected_repository:
	raise SystemExit(2)
print(urlunsplit((parts.scheme, host, parts.path, "", "")))
PY
		)" || die "remote URL does not match the validated GitHub repository"
		token="${GH_TOKEN:-${GH_PAT:-${GITHUB_TOKEN:-}}}"
		[ -n "${token}" ] || die "GitHub credential is unavailable"
		credential_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/trusted-git.XXXXXX")"
		credential_file="${credential_dir}/token"
		askpass_file="${credential_dir}/askpass.sh"
		cleanup_credentials()
		{
			rm -rf -- "${credential_dir}"
		}
		trap cleanup_credentials EXIT HUP INT TERM
		chmod 700 "${credential_dir}"
		printf '%s' "${token}" > "${credential_file}"
		chmod 600 "${credential_file}"
		cat > "${askpass_file}" <<'ASKPASS'
#!/usr/bin/env bash
case "${1:-}" in
	*Username*) printf '%s\n' x-access-token ;;
	*) cat "${TRUSTED_GIT_CREDENTIAL_FILE}" ;;
esac
ASKPASS
		chmod 700 "${askpass_file}"
		push_env=(
			env -u GH_TOKEN -u GH_PAT -u GITHUB_TOKEN -u SSH_AUTH_SOCK
			GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
			GIT_ASKPASS="${askpass_file}" TRUSTED_GIT_CREDENTIAL_FILE="${credential_file}"
		)
		remote_head="$(${push_env[@]} "${git_bin}" ls-remote --heads "${remote_url}" "refs/heads/${branch}" | awk 'NR == 1 {print $1}')"
		if [ "${expected_remote_head}" = absent ]; then
			[ -z "${remote_head}" ] || die "destination branch was created concurrently"
			lease_value=""
		else
			[ "${remote_head}" = "${expected_remote_head}" ] || die "remote head changed before push"
			lease_value="${expected_remote_head}"
		fi
		"${push_env[@]}" "${git_bin}" -C "${repo}" \
			-c credential.helper= -c core.hooksPath=/dev/null \
			push --porcelain --force-with-lease="refs/heads/${branch}:${lease_value}" \
			"${remote_url}" "${expected_local_head}:refs/heads/${branch}"
		cleanup_credentials
		trap - EXIT HUP INT TERM
		;;
	*) die "unsupported operation: ${operation}" ;;
esac
