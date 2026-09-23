#!/usr/bin/env bash
set -euo pipefail

PATH=/usr/bin:/bin
TMPDIR=/tmp
export PATH TMPDIR
unset BASH_ENV ENV GH_TOKEN GH_PAT GITHUB_TOKEN OPENROUTER_API_KEY TG_BOT_SECRET \
	ORCHESTRATOR_STATE_AUTH_KEYRING GITHUB_ENV GITHUB_OUTPUT GITHUB_PATH GITHUB_STEP_SUMMARY

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_PATH="${1:?behavioural smoke bundle path required}"
REPO_ROOT="${2:-$(pwd)}"
EVALUATOR_PATH="${SCRIPT_DIR}/evaluate_behavioural_smoke.py"

for required_command in sudo unshare mount setpriv python3; do
	if ! command -v "${required_command}" >/dev/null 2>&1; then
		echo "behavioural smoke isolation unavailable: missing ${required_command}" >&2
		exit 2
	fi
done
if [ ! -f "${EVALUATOR_PATH}" ] || [ -L "${EVALUATOR_PATH}" ]; then
	echo "behavioural smoke isolation unavailable: trusted evaluator is missing or symlinked" >&2
	exit 2
fi

REPO_ROOT="$(cd "${REPO_ROOT}" && pwd -P)"
case "${BUNDLE_PATH}" in
	/*) ;;
	*) BUNDLE_PATH="${REPO_ROOT}/${BUNDLE_PATH}" ;;
esac
if [ -L "${BUNDLE_PATH}" ] || [ ! -f "${BUNDLE_PATH}" ]; then
	echo "behavioural smoke bundle path is missing, non-regular, or symlinked" >&2
	exit 2
fi
BUNDLE_PATH="$(readlink -f -- "${BUNDLE_PATH}")"
case "${BUNDLE_PATH}" in
	"${REPO_ROOT}"/validation/tests/synth_round_*_assertions.json) ;;
	*) echo "behavioural smoke bundle path is outside the allowed validation directory" >&2; exit 2 ;;
esac

sandbox_root="$(mktemp -d "${TMPDIR:-/tmp}/behavioural-smoke-sandbox.XXXXXX")"
cleanup_behavioural_smoke_sandbox()
{
	rm -rf "${sandbox_root}" >/dev/null 2>&1 || true
}
trap cleanup_behavioural_smoke_sandbox EXIT INT TERM
mkdir -p "${sandbox_root}/repo"
install -m 0555 "${EVALUATOR_PATH}" "${sandbox_root}/evaluate_behavioural_smoke.py"
chmod 0755 "${sandbox_root}"

bundle_relative="${BUNDLE_PATH#"${REPO_ROOT}"/}"
sudo -n unshare --mount --net --pid --fork --mount-proc \
	bash -c '
		set -euo pipefail
		source_root="$1"
		sandbox_repo="$2"
		evaluator="$3"
		bundle_relative="$4"
		mount --bind "${source_root}" "${sandbox_repo}"
		mount -o remount,bind,ro "${sandbox_repo}"
		if [ -d "${sandbox_repo}/.git" ]; then
			mount -t tmpfs -o ro,nosuid,nodev,noexec,size=4096 tmpfs "${sandbox_repo}/.git"
		elif [ -e "${sandbox_repo}/.git" ]; then
			mount --bind /dev/null "${sandbox_repo}/.git"
			mount -o remount,bind,ro "${sandbox_repo}/.git"
		fi
		cd "${sandbox_repo}"
		exec setpriv --reuid=65534 --regid=65534 --clear-groups --no-new-privs \
			env -i HOME=/tmp PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
			python3 -I -B "${evaluator}" "${sandbox_repo}" "${sandbox_repo}/${bundle_relative}"
	' behavioural-smoke-isolation "${REPO_ROOT}" "${sandbox_root}/repo" "${sandbox_root}/evaluate_behavioural_smoke.py" "${bundle_relative}"
