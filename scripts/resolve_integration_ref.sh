#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FIXTURE_DIR="${REPO_ROOT}/tests/fixtures/integration_ref_resolver"

# Rate-limit-aware reads (issue #5873 / #6634): use gh_api_retry from the
# sibling gh_helpers.sh (the same trusted staged copy as this script) when it
# is present; otherwise fall back to a plain `gh api` call. A read that stays
# rate-limited exits 75 so callers fail loudly instead of resolving to the
# default branch.
if [ -f "${SCRIPT_DIR}/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${SCRIPT_DIR}/gh_helpers.sh" || true
fi

_resolver_rate_limited() {
	echo "::error::INTEGRATION_REF_RESOLVE outcome=rate_limited what=$1" >&2
	exit 75
}

extract_integration_branch() {
	local body="${1:-}"
	printf '%s\n' "${body}" | python3 -c '
import re
import sys

body = sys.stdin.read()
pattern = re.compile(r"^\s*(?:-\s*)?(?:\*\*Integration branch:\*\*|Integration branch:)\s*`?\s*([^`\n]+?)\s*`?\s*$", re.MULTILINE)
match = pattern.search(body)
if match:
	print(match.group(1).strip())
	sys.exit(0)
# "Target branch:" is an accepted alias for human-authored issues that name
# the branch they must be planned and implemented against (issue #4075 used
# `**Target branch:** `orchestrator/project-3965` (integration branch ...)`,
# which the canonical parser above does not recognise, so planning checked
# out main and blocked). The backticked form may carry trailing prose; the
# plain form is a single whitespace-free token. The canonical
# "Integration branch:" line always wins when both are present. Keep in
# sync with TARGET_BRANCH_LINE_RE in scripts/orchestrate_lib.py.
alias_pattern = re.compile(r"^\s*(?:-\s*)?(?:\*\*Target branch:\*\*|Target branch:)\s*(?:`\s*([^`\n]+?)\s*`(?:\s.*)?|([^`\s]+))\s*$", re.MULTILINE)
alias_match = alias_pattern.search(body)
if not alias_match:
	sys.exit(0)
print((alias_match.group(1) or alias_match.group(2) or "").strip())
'
}

extract_tracking_issue() {
	local body="${1:-}"
	printf '%s\n' "${body}" | python3 -c '
import re
import sys

body = sys.stdin.read()
pattern = re.compile(r"^\s*(?:-\s*)?(?:\*\*Tracking issue:\*\*|Tracking issue:)\s*#(\d+)\s*$", re.MULTILINE)
match = pattern.search(body)
if not match:
	sys.exit(0)
print(match.group(1))
'
}

get_issue_body() {
	local issue_num="$1" body rc=0
	if ! type gh_api_retry >/dev/null 2>&1; then
		gh api "repos/${REPO}/issues/${issue_num}" --jq '.body // ""'
		return $?
	fi
	body="$(gh_api_retry "repos/${REPO}/issues/${issue_num}" --jq '.body // ""')" || rc=$?
	if [ "${rc}" -eq 75 ]; then
		_resolver_rate_limited "issue-${issue_num}"
	fi
	[ "${rc}" -eq 0 ] || return "${rc}"
	printf '%s\n' "${body}"
}

branch_exists() {
	local ref_name="$1"
	local encoded_ref
	local err
	encoded_ref="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${ref_name}")"
	if type gh_api_retry >/dev/null 2>&1; then
		# Called outside a subshell so GH_API_RETRY_LAST_STATUS is visible.
		local err_file rc=0
		err_file="$(mktemp)"
		gh_api_retry "repos/${REPO}/git/ref/heads/${encoded_ref}" >/dev/null 2>"${err_file}" || rc=$?
		err="$(cat "${err_file}" 2>/dev/null || true)"
		rm -f "${err_file}"
		if [ "${rc}" -eq 0 ]; then
			return 0
		fi
		if [ "${GH_API_RETRY_LAST_STATUS:-}" = "404" ]; then
			return 1
		fi
		if [ "${rc}" -eq 75 ]; then
			_resolver_rate_limited "branch"
		fi
		echo "::error::Failed to verify integration branch '${ref_name}': ${err}" >&2
		exit 2
	fi
	if err="$(gh api "repos/${REPO}/git/ref/heads/${encoded_ref}" 2>&1)"; then
		return 0
	fi
	if printf '%s' "${err}" | grep -Eq '404|Not Found'; then
		return 1
	fi

	echo "::error::Failed to verify integration branch '${ref_name}': ${err}" >&2
	exit 2
}

resolve_ref() {
	local child_body child_branch tracking_issue tracking_body tracking_branch
	child_body="$(get_issue_body "${ISSUE}")"
	child_branch="$(extract_integration_branch "${child_body}")"
	if [ -n "${child_branch}" ]; then
		if ! branch_exists "${child_branch}"; then
			echo "::error::Integration branch '${child_branch}' declared in child issue #${ISSUE} does not exist." >&2
			return 1
		fi
		printf '%s\n' "${child_branch}"
		return 0
	fi

	tracking_issue="$(extract_tracking_issue "${child_body}")"
	if [ -z "${tracking_issue}" ]; then
		printf '\n'
		return 0
	fi

	tracking_body="$(get_issue_body "${tracking_issue}")"
	tracking_branch="$(extract_integration_branch "${tracking_body}")"
	if [ -z "${tracking_branch}" ]; then
		printf '\n'
		return 0
	fi

	if ! branch_exists "${tracking_branch}"; then
		echo "::error::Integration branch '${tracking_branch}' declared in tracking issue #${tracking_issue} does not exist." >&2
		return 1
	fi

	printf '%s\n' "${tracking_branch}"
	return 0
}

run_case() {
	local fixture_file="$1"
	local bin_dir="$2"
	local out_file err_file actual_stdout actual_rc

	out_file="$(mktemp)"
	err_file="$(mktemp)"
	(
		export PATH="${bin_dir}:${PATH}"
		export GH_FIXTURE_FILE="${fixture_file}"
		export GH_MOCK_MODE="assert"
		export REPO="owner/repo"
		export ISSUE="101"
		export GH_TOKEN="test-token"
		if resolve_ref >"${out_file}" 2>"${err_file}"; then
			echo 0 >"${out_file}.rc"
		else
			echo $? >"${out_file}.rc"
		fi
	)
	actual_stdout="$(cat "${out_file}")"
	actual_rc="$(cat "${out_file}.rc")"

	python3 - <<'PY' "${fixture_file}" "${actual_stdout}" "${actual_rc}" "${err_file}"
import json
import pathlib
import sys

fixture = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
actual_stdout = sys.argv[2]
actual_rc = int(sys.argv[3])
err_text = pathlib.Path(sys.argv[4]).read_text(encoding="utf-8")
expected_stdout = fixture["expected_stdout"]
expected_rc = int(fixture["expected_exit_code"])

if actual_stdout != expected_stdout:
	print(f"self-test mismatch stdout for {fixture['name']}: expected {expected_stdout!r}, got {actual_stdout!r}", file=sys.stderr)
	sys.exit(1)
if actual_rc != expected_rc:
	print(f"self-test mismatch exit for {fixture['name']}: expected {expected_rc}, got {actual_rc}", file=sys.stderr)
	sys.exit(1)
if expected_rc != 0 and "::error::" not in err_text:
	print(f"self-test expected ::error:: in stderr for {fixture['name']}", file=sys.stderr)
	sys.exit(1)
PY

	rm -f "${out_file}" "${err_file}" "${out_file}.rc"
}

run_self_test() {
	if [ ! -d "${FIXTURE_DIR}" ]; then
		echo "self-test fixture directory not found: ${FIXTURE_DIR}" >&2
		exit 1
	fi

	local bin_dir
	bin_dir="$(mktemp -d)"
	cat > "${bin_dir}/gh" <<'GHMOCK'
#!/usr/bin/env python3
import json
import os
import pathlib
import sys

def _load_fixture() -> dict:
	path = os.environ.get("GH_FIXTURE_FILE", "")
	if not path:
		print("GH_FIXTURE_FILE is required", file=sys.stderr)
		sys.exit(2)
	return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _parse_issue(endpoint: str):
	parts = endpoint.split("/")
	if len(parts) < 5:
		return None
	if parts[0] != "repos" or parts[3] != "issues":
		return None
	try:
		return int(parts[4])
	except ValueError:
		return None


def main() -> int:
	args = [a for a in sys.argv[1:] if a != "-i"]
	if len(args) < 2 or args[0] != "api":
		print("mock gh only supports: gh api ...", file=sys.stderr)
		return 2

	fixture = _load_fixture()
	endpoint = args[1]
	if endpoint.startswith("repos/") and "/issues/" in endpoint:
		issue_num = _parse_issue(endpoint)
		if issue_num is None:
			print("invalid issue endpoint", file=sys.stderr)
			return 2
		body = fixture.get("issues", {}).get(str(issue_num), {}).get("body", "")
		if "--jq" in args:
			jq_expr = args[args.index("--jq") + 1]
			if jq_expr == '.body // ""':
				print(body)
				return 0
		print(json.dumps({"number": issue_num, "body": body}))
		return 0

	if endpoint.startswith("repos/") and "/git/ref/heads/" in endpoint:
		ref = endpoint.split("/git/ref/heads/", 1)[1]
		from urllib.parse import unquote
		ref_name = unquote(ref)
		exists_map = fixture.get("branch_exists", {})
		exists = bool(exists_map.get(ref_name, False))
		if exists:
			print(json.dumps({"ref": f"refs/heads/{ref_name}"}))
			return 0
		print("gh: Not Found (HTTP 404)", file=sys.stderr)
		return 1

	print(f"unsupported endpoint: {endpoint}", file=sys.stderr)
	return 2


if __name__ == "__main__":
	raise SystemExit(main())
GHMOCK
	chmod +x "${bin_dir}/gh"

	local fixture
	for fixture in "${FIXTURE_DIR}"/*.json; do
		run_case "${fixture}" "${bin_dir}"
	done

	rm -rf "${bin_dir}"
	echo "self-test passed"
}

main() {
	if [ "${1:-}" = "--self-test" ]; then
		run_self_test
		exit 0
	fi

	: "${REPO:?REPO is required}"
	: "${ISSUE:?ISSUE is required}"
	: "${GH_TOKEN:?GH_TOKEN is required}"

	resolve_ref
}

main "$@"
