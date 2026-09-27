#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FIXTURE_DIR="${REPO_ROOT}/tests/fixtures/integration_ref_resolver"

extract_integration_branch() {
	local body="${1:-}"
	printf '%s\n' "${body}" | python3 -c '
import re
import sys

body = sys.stdin.read()
lines = body.splitlines()
# Only the preamble and the explicit orchestrator metadata footer are
# branch declarations. A quoted check log or fenced diagnostic is not.
if lines and lines[0].startswith("## Project:"):
	boundary = next((i for i, line in enumerate(lines[1:], 1) if re.match(r"^(?:## |### Wave|```|~~~)", line)), len(lines))
else:
	boundary = next((i for i, line in enumerate(lines) if re.match(r"^(?:## |```|~~~|---$)", line)), len(lines))
metadata = lines[:boundary]

footer = next((i for i, line in enumerate(lines) if line.startswith("**Orchestrator metadata**")), None)
if footer is not None:
	start = footer + 1
	end = next((i for i in range(start, len(lines)) if re.match(r"^(?:## |```|~~~|---$)", lines[i])), len(lines))
	metadata += lines[start:end]
body = "\n".join(metadata)
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
	local issue_num="$1"
	gh api "repos/${REPO}/issues/${issue_num}" --jq '.body // ""'
}

branch_exists() {
	local ref_name="$1"
	local encoded_ref
	local err
	encoded_ref="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${ref_name}")"
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
	local child_body child_branch tracking_issue tracking_body tracking_branch child_json origin_pr origin_base origin_json
	child_json="$(gh api "repos/${REPO}/issues/${ISSUE}")"
	child_body="$(printf '%s' "${child_json}" | jq -r '.body // ""')"
	if printf '%s' "${child_json}" | jq -e '(.labels // []) | any(.[]; .name == "ai:check-triage")' >/dev/null; then
		if ! jq -e '(.author_association | IN("OWNER", "MEMBER", "COLLABORATOR")) or
			(.user.login == "github-actions[bot]" and .user.type == "Bot")' <<< "${child_json}" >/dev/null; then
			echo "::error::Untrusted triage issue author." >&2
			return 1
		fi
		# Triage prose is a model/log product. Only the machine header and an
		# independent PR read may select a non-default base.
		if ! origin_pr="$(printf '%s' "${child_body}" | sed -n '2s/^<!-- check-failure-triage:origin-pr=\([1-9][0-9]*\) base=[A-Za-z0-9][A-Za-z0-9._/-]* -->$/\1/p')" ||
		   [ -z "${origin_pr}" ]; then
			if [[ "${child_body}" == '<!-- check-failure-triage:fp='* ]] &&
			   printf '%s' "${child_body}" | grep -q 'check-failure-triage:origin-pr='; then
				echo "::error::Malformed triage origin header." >&2
				return 1
			fi
			printf '\n' # Legacy triage issues have no independently bound base.
			return 0
		fi
		origin_base="$(printf '%s' "${child_body}" | sed -n '2s/^<!-- check-failure-triage:origin-pr=[1-9][0-9]* base=\([A-Za-z0-9][A-Za-z0-9._/-]*\) -->$/\1/p')"
		if ! [[ "${child_body}" =~ ^'<!-- check-failure-triage:fp='[0-9a-f]{64}' -->' ]] ||
		   [ "$(printf '%s\n' "${child_body}" | grep -c 'check-failure-triage:fp=' || true)" != 1 ] ||
		   [ "$(printf '%s\n' "${child_body}" | grep -c 'check-failure-triage:origin-pr=' || true)" != 1 ] ||
		   ! git check-ref-format --branch "${origin_base}" >/dev/null 2>&1 ||
		   { child_branch="$(extract_integration_branch "${child_body}")"; [ -n "${child_branch}" ] && [ "${child_branch}" != "${origin_base}" ]; }; then
			echo "::error::Invalid triage provenance." >&2
			return 1
		fi
		origin_json="$(gh api "repos/${REPO}/pulls/${origin_pr}")" || return 1
		if ! jq -e --argjson number "${origin_pr}" --arg repo "${REPO}" --arg base "${origin_base}" '
			.number == $number and .head.repo.full_name == $repo and
			.base.repo.full_name == $repo and .base.ref == $base' <<< "${origin_json}" >/dev/null; then
			echo "::error::Triage origin PR does not match its header." >&2
			return 1
		fi
		if ! branch_exists "${origin_base}"; then
			echo "::error::Triage origin branch does not exist." >&2
			return 1
		fi
		printf '%s\n' "${origin_base}"
		return 0
	fi
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
	args = sys.argv[1:]
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
		print(json.dumps({"number": issue_num, **fixture.get("issues", {}).get(str(issue_num), {}), "body": body}))
		return 0
	if endpoint.startswith("repos/") and "/pulls/" in endpoint:
		print(json.dumps(fixture.get("pulls", {}).get(endpoint.rsplit("/", 1)[-1], {})))
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
