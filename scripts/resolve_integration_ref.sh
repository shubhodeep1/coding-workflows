#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FIXTURE_DIR="${REPO_ROOT}/tests/fixtures/integration_ref_resolver"
CI_TRIAGE_FIXTURE_DIR="${REPO_ROOT}/tests/fixtures/integration_ref_resolver_ci_triage"

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

# --- CI-triage PR-branch routing (CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED) ------
# Opt-in (default off). When on, an `ai:check-triage` issue filed by
# scripts/check_failure_triage.sh resolves to the head branch of the PR whose
# check failed, after verifying the label, the canonical marker header, a
# trusted author, the body's PR line, the live PR (open, same repository,
# matching head ref) and the branch itself. Any verification failure returns
# 3: callers must fail closed instead of falling back to the default branch.
# Log prefix: CI_TRIAGE_PR_BRANCH_ROUTING.
CI_TRIAGE_REFUSED_RC=3

ci_triage_routing_enabled() {
	local raw="${CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED:-false}"
	case "${raw,,}" in
		true|1|yes|on) return 0 ;;
		*) return 1 ;;
	esac
}

get_issue_json() {
	local issue_num="$1"
	gh api "repos/${REPO}/issues/${issue_num}"
}

ci_triage_refuse() {
	local reason="$1"
	local pr="${2:-none}"
	echo "::error::CI_TRIAGE_PR_BRANCH_ROUTING issue=${ISSUE} pr=${pr} outcome=refused reason=${reason}" >&2
}

# Reads the issue JSON on stdin; argv[1] is REPO. Prints one line:
# `skip` (not a triage issue), `refuse <reason> <pr|none>` or
# `verify <pr> <ref>`. Mirrors the header check in check_failure_triage.sh.
CI_TRIAGE_PARSE_PY='
import json
import re
import sys

repo = sys.argv[1]
try:
	data = json.load(sys.stdin)
except Exception:
	data = None
if not isinstance(data, dict):
	print("refuse issue_json_invalid none")
	sys.exit(0)
body = data.get("body")
body = body if isinstance(body, str) else ""
body = body.replace("\r\n", "\n")
names = set()
labels = data.get("labels")
for label in labels if isinstance(labels, list) else []:
	name = label.get("name") if isinstance(label, dict) else label
	if isinstance(name, str):
		names.add(name)
has_label = "ai:check-triage" in names
has_marker = re.search(r"<!--\s*check-failure-triage:", body) is not None
if not has_label and not has_marker:
	print("skip")
	sys.exit(0)
pr_markers = re.findall(r"check-failure-triage:pr=([^\s>]*)", body)
pr = pr_markers[0] if len(pr_markers) == 1 and re.fullmatch(r"[1-9][0-9]{0,9}", pr_markers[0]) else "none"

def refuse(reason):
	print(f"refuse {reason} {pr}")
	sys.exit(0)

if not has_label:
	refuse("label_missing")
lines = body.split("\n")
header = (
	r"<!-- check-failure-triage:fp=[0-9a-f]{64} -->",
	r"<!-- check-failure-triage:gen=[0-9]+ -->",
	r"<!-- check-failure-triage:root=[0-9a-f]{64} -->",
	r"<!-- check-failure-triage:pr=[1-9][0-9]{0,9} -->",
)
if (len(lines) < 4 or len(pr_markers) != 1 or pr == "none"
		or not all(re.fullmatch(pattern, lines[i]) for i, pattern in enumerate(header))):
	refuse("marker_invalid")
if data.get("pull_request") is not None:
	refuse("not_an_issue")
if data.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}:
	refuse("author_untrusted")
pr_line = next((line for line in lines if line.startswith("- **Pull request:**")), None)
match = re.fullmatch(r"- \*\*Pull request:\*\* (\S+) \(`([^`\n]+)`\)\s*", pr_line or "")
if not match:
	refuse("pr_association_mismatch")
url = re.fullmatch(r"https://[^/\s]+/([^/\s]+/[^/\s]+)/pull/([1-9][0-9]*)", match.group(1))
if not url or url.group(1).lower() != repo.lower() or url.group(2) != pr:
	refuse("pr_association_mismatch")
ref = match.group(2)
if (not re.fullmatch(r"[A-Za-z0-9._/-]+", ref) or ".." in ref or "//" in ref
		or ref.startswith(("-", "/")) or ref.endswith(("/", ".lock")) or ref == "HEAD"):
	refuse("head_ref_invalid")
print(f"verify {pr} {ref}")
'

# Reads the pull request JSON on stdin; argv: REPO, PR number, expected ref.
# Prints nothing when the PR matches, else one refusal reason token.
CI_TRIAGE_VERIFY_PR_PY='
import json
import sys

repo, pr, ref = sys.argv[1], sys.argv[2], sys.argv[3]
try:
	data = json.load(sys.stdin)
except Exception:
	data = None
if not isinstance(data, dict):
	print("pr_lookup_failed")
	sys.exit(0)

def full_name(side):
	part = data.get(side)
	repo_obj = part.get("repo") if isinstance(part, dict) else None
	name = repo_obj.get("full_name") if isinstance(repo_obj, dict) else None
	return name if isinstance(name, str) else ""

number = data.get("number")
head = data.get("head") if isinstance(data.get("head"), dict) else {}
if number is not None and str(number) != pr:
	print("pr_association_mismatch")
elif data.get("state") != "open" or data.get("merged") is True:
	print("pr_not_open")
elif full_name("head").lower() != repo.lower() or full_name("base").lower() != repo.lower():
	print("pr_cross_repository")
elif head.get("ref") != ref:
	print("pr_association_mismatch")
'

CI_TRIAGE_REF_MATCH_PY='
import json
import sys

try:
	data = json.load(sys.stdin)
except Exception:
	sys.exit(1)
sys.exit(0 if isinstance(data, dict) and data.get("ref") == "refs/heads/" + sys.argv[1] else 1)
'

# Prints the verified PR head branch (rc 0), prints nothing when the issue is
# not a CI-triage issue (rc 0), or logs exactly one refusal and returns 3.
# Every error is handled explicitly: callers run this inside `|| rc=$?`,
# where `set -e` does not apply.
resolve_ci_triage_branch() {
	local issue_json="$1"
	local parsed verdict pr ref reason pr_json encoded_ref ref_json ref_err
	if ! parsed="$(printf '%s' "${issue_json}" | python3 -I -c "${CI_TRIAGE_PARSE_PY}" "${REPO}")"; then
		ci_triage_refuse "issue_parse_failed" "none"
		return "${CI_TRIAGE_REFUSED_RC}"
	fi
	verdict=""
	pr=""
	ref=""
	read -r verdict pr ref <<< "${parsed}" || true
	case "${verdict}" in
		skip)
			return 0
			;;
		refuse)
			# `refuse <reason> <pr>`: the second and third words.
			ci_triage_refuse "${pr:-unknown}" "${ref:-none}"
			return "${CI_TRIAGE_REFUSED_RC}"
			;;
		verify)
			;;
		*)
			ci_triage_refuse "issue_parse_failed" "none"
			return "${CI_TRIAGE_REFUSED_RC}"
			;;
	esac

	if ! pr_json="$(gh api "repos/${REPO}/pulls/${pr}" 2>/dev/null)"; then
		ci_triage_refuse "pr_lookup_failed" "${pr}"
		return "${CI_TRIAGE_REFUSED_RC}"
	fi
	if ! reason="$(printf '%s' "${pr_json}" | python3 -I -c "${CI_TRIAGE_VERIFY_PR_PY}" "${REPO}" "${pr}" "${ref}")"; then
		reason="pr_lookup_failed"
	fi
	if [ -n "${reason}" ]; then
		ci_triage_refuse "${reason}" "${pr}"
		return "${CI_TRIAGE_REFUSED_RC}"
	fi

	# Unlike branch_exists(), never exit 2 here: callers map 2 to the
	# default-branch fallback this routing must not reach.
	if ! encoded_ref="$(python3 -I -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${ref}")"; then
		ci_triage_refuse "branch_lookup_failed" "${pr}"
		return "${CI_TRIAGE_REFUSED_RC}"
	fi
	# Same capture as branch_exists(); any stderr noise on success makes the
	# JSON check below fail, which refuses (fail closed).
	if ! ref_json="$(gh api "repos/${REPO}/git/ref/heads/${encoded_ref}" 2>&1)"; then
		ref_err="${ref_json}"
		if printf '%s' "${ref_err}" | grep -Eq '404|Not Found'; then
			ci_triage_refuse "branch_missing" "${pr}"
		else
			ci_triage_refuse "branch_lookup_failed" "${pr}"
		fi
		return "${CI_TRIAGE_REFUSED_RC}"
	fi
	if ! printf '%s' "${ref_json}" | python3 -I -c "${CI_TRIAGE_REF_MATCH_PY}" "${ref}"; then
		ci_triage_refuse "branch_missing" "${pr}"
		return "${CI_TRIAGE_REFUSED_RC}"
	fi

	echo "CI_TRIAGE_PR_BRANCH_ROUTING issue=${ISSUE} pr=${pr} outcome=routed branch=${ref}" >&2
	printf '%s\n' "${ref}"
	return 0
}

resolve_ref() {
	local child_body child_branch tracking_issue tracking_body tracking_branch
	local child_json="" triage_branch triage_rc
	if ci_triage_routing_enabled; then
		# One full issue read supplies body, labels, author and PR flag. A
		# failed read keeps the legacy non-3 failure: whether the issue is a
		# CI-triage issue is not yet known.
		child_json="$(get_issue_json "${ISSUE}")" || return 1
		child_body="$(printf '%s' "${child_json}" | python3 -I -c '
import json, sys
data = json.load(sys.stdin)
body = data.get("body") if isinstance(data, dict) else ""
print(body if isinstance(body, str) else "")
')" || return 1
	else
		child_body="$(get_issue_body "${ISSUE}")"
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

	if ci_triage_routing_enabled; then
		triage_rc=0
		triage_branch="$(resolve_ci_triage_branch "${child_json}")" || triage_rc=$?
		if [ "${triage_rc}" -ne 0 ]; then
			return "${CI_TRIAGE_REFUSED_RC}"
		fi
		if [ -n "${triage_branch}" ]; then
			printf '%s\n' "${triage_branch}"
			return 0
		fi
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

	local routing_flag
	out_file="$(mktemp)"
	err_file="$(mktemp)"
	# Always set the routing flag explicitly so an outer value cannot leak
	# into a fixture; fixtures opt in through `env`.
	routing_flag="$(python3 -c '
import json, pathlib, sys
fixture = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
env = fixture.get("env") if isinstance(fixture.get("env"), dict) else {}
print(str(env.get("CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED", "false")))
' "${fixture_file}")"
	(
		export PATH="${bin_dir}:${PATH}"
		export GH_FIXTURE_FILE="${fixture_file}"
		export GH_MOCK_MODE="assert"
		export REPO="owner/repo"
		export ISSUE="101"
		export GH_TOKEN="test-token"
		export CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED="${routing_flag}"
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
expected_reason = fixture.get("expected_error_reason")
routing_errors = [line for line in err_text.splitlines() if "::error::CI_TRIAGE_PR_BRANCH_ROUTING" in line]
if expected_reason:
	if len(routing_errors) != 1 or f"outcome=refused reason={expected_reason}" not in routing_errors[0]:
		print(f"self-test expected one CI_TRIAGE_PR_BRANCH_ROUTING refusal reason={expected_reason} for {fixture['name']}, got {routing_errors!r}", file=sys.stderr)
		sys.exit(1)
elif routing_errors:
	print(f"self-test unexpected CI_TRIAGE_PR_BRANCH_ROUTING refusal for {fixture['name']}: {routing_errors!r}", file=sys.stderr)
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
		issue = fixture.get("issues", {}).get(str(issue_num), {})
		body = issue.get("body", "")
		if "--jq" in args:
			jq_expr = args[args.index("--jq") + 1]
			if jq_expr == '.body // ""':
				print(body)
				return 0
		payload = {"number": issue_num, "body": body}
		for key in ("labels", "author_association", "user", "pull_request"):
			if key in issue:
				payload[key] = issue[key]
		print(json.dumps(payload))
		return 0

	if endpoint.startswith("repos/") and "/pulls/" in endpoint:
		pr_num = endpoint.split("/pulls/", 1)[1]
		pull = fixture.get("pulls", {}).get(pr_num)
		if pull is None:
			print("gh: Not Found (HTTP 404)", file=sys.stderr)
			return 1
		print(json.dumps(pull))
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
	if [ "$#" -gt 0 ]; then
		# --self-test-case: run only the named fixture files.
		for fixture in "$@"; do
			run_case "${fixture}" "${bin_dir}"
		done
	else
		for fixture in "${FIXTURE_DIR}"/*.json; do
			run_case "${fixture}" "${bin_dir}"
		done
		if [ -d "${CI_TRIAGE_FIXTURE_DIR}" ]; then
			for fixture in "${CI_TRIAGE_FIXTURE_DIR}"/*.json; do
				run_case "${fixture}" "${bin_dir}"
			done
		fi
	fi

	rm -rf "${bin_dir}"
	echo "self-test passed"
}

main() {
	if [ "${1:-}" = "--self-test" ]; then
		run_self_test
		exit 0
	fi
	if [ "${1:-}" = "--self-test-case" ]; then
		if [ -z "${2:-}" ] || [ ! -f "${2}" ]; then
			echo "--self-test-case requires an existing fixture file" >&2
			exit 2
		fi
		run_self_test "${2}"
		exit 0
	fi

	: "${REPO:?REPO is required}"
	: "${ISSUE:?ISSUE is required}"
	: "${GH_TOKEN:?GH_TOKEN is required}"

	resolve_ref
}

main "$@"
