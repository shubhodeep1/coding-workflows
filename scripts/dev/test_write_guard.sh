#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RUN_GUARD_STATUS=0
RUN_GUARD_OUTPUT=""

assert_eq()
{
	local expected="$1"
	local actual="$2"
	local message="$3"
	if [ "${expected}" != "${actual}" ]; then
		echo "${message}: expected '${expected}', got '${actual}'" >&2
		exit 1
	fi
}

assert_contains()
{
	local needle="$1"
	local haystack="$2"
	local message="$3"
	if ! grep -Fq -- "${needle}" <<< "${haystack}"; then
		echo "${message}: missing '${needle}'" >&2
		echo "--- output ---" >&2
		printf '%s\n' "${haystack}" >&2
		echo "--------------" >&2
		exit 1
	fi
}

assert_not_contains()
{
	local needle="$1"
	local haystack="$2"
	local message="$3"
	if grep -Fq -- "${needle}" <<< "${haystack}"; then
		echo "${message}: unexpected '${needle}'" >&2
		echo "--- output ---" >&2
		printf '%s\n' "${haystack}" >&2
		echo "--------------" >&2
		exit 1
	fi
}

unset_git_env()
{
	local write_guard_git_env
	while IFS= read -r write_guard_git_env; do
		[ -n "${write_guard_git_env}" ] || continue
		unset "${write_guard_git_env}"
	done < <(env | sed -n 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p')
}

make_temp_repo()
{
	local temp_repo
	temp_repo="$(mktemp -d)"
	mkdir -p "${temp_repo}/scripts" "${temp_repo}/.github/ai"
	cp "${REPO_ROOT}/scripts/write_guard.sh" "${temp_repo}/scripts/write_guard.sh"
	cp "${REPO_ROOT}/.github/ai/write_guards.v1.json" "${temp_repo}/.github/ai/write_guards.v1.json"
	(
		unset_git_env
		cd "${temp_repo}"
		git init -q
		git config user.name tester
		git config user.email tester@example.com
		git add .
		git commit -q -m init
	)
	printf '%s\n' "${temp_repo}"
}

run_guard_capture()
{
	local repo_dir="$1"
	local phase="$2"
	local paths_content="$3"
	shift 3
	local paths_file="${repo_dir}/paths.txt"
	local output_file="${repo_dir}/guard.out"
	local write_guard_env_assignment

	printf '%s' "${paths_content}" > "${paths_file}"
	set +e
	(
		unset_git_env
		cd "${repo_dir}"
		export PYTHONDONTWRITEBYTECODE=1
		for write_guard_env_assignment in "$@"; do
			export "${write_guard_env_assignment}"
			done
		# shellcheck source=/dev/null
		source scripts/write_guard.sh
		write_guard_check "${phase}" "${paths_file}"
	) > "${output_file}" 2>&1
	RUN_GUARD_STATUS=$?
	set -e
	RUN_GUARD_OUTPUT="$(cat "${output_file}")"
}

test_allowlist_pass()
{
	local repo_dir
	repo_dir="$(make_temp_repo)"
	run_guard_capture "${repo_dir}" validate_fix_harness $'validation/generated.sh\nscripts/helper.sh\n'
	assert_eq "0" "${RUN_GUARD_STATUS}" 'validate allowlist should pass'
	assert_not_contains 'WRITE_GUARD_BLOCK' "${RUN_GUARD_OUTPUT}" 'allowlist pass should not block'
	rm -rf "${repo_dir}"
}

test_blocklist_fail()
{
	local repo_dir
	repo_dir="$(make_temp_repo)"
	run_guard_capture "${repo_dir}" validate_fix_harness $'ai-memory/state.json\n'
	assert_eq "1" "${RUN_GUARD_STATUS}" 'validate blocklist should fail'
	assert_contains 'WRITE_GUARD_BLOCK: phase=validate_fix_harness path=ai-memory/state.json' "${RUN_GUARD_OUTPUT}" 'blocklist failure should be logged'
	rm -rf "${repo_dir}"
}

test_not_allowed_fail()
{
	local repo_dir
	repo_dir="$(make_temp_repo)"
	run_guard_capture "${repo_dir}" validate_fix_harness $'README.md\n'
	assert_eq "1" "${RUN_GUARD_STATUS}" 'validate allowlist misses should fail'
	assert_contains 'WRITE_GUARD_BLOCK: phase=validate_fix_harness path=README.md reason=not_allowed pattern=<no-match>' "${RUN_GUARD_OUTPUT}" 'not_allowed failure should use the sentinel pattern'
	rm -rf "${repo_dir}"
}

test_conditional_block_with_env_disabled()
{
	local repo_dir
	repo_dir="$(make_temp_repo)"
	run_guard_capture "${repo_dir}" review_editor $'.github/workflows/test.yml\n' 'ALLOW_WORKFLOW_EDITS=false'
	assert_eq "1" "${RUN_GUARD_STATUS}" 'workflow edits should block when ALLOW_WORKFLOW_EDITS=false'
	assert_contains 'WRITE_GUARD_BLOCK: phase=review_editor path=.github/workflows/test.yml reason=conditional_blocked_glob' "${RUN_GUARD_OUTPUT}" 'conditional block should be logged'
	rm -rf "${repo_dir}"
}

test_conditional_block_with_env_enabled()
{
	local repo_dir
	repo_dir="$(make_temp_repo)"
	run_guard_capture "${repo_dir}" review_editor $'.github/workflows/test.yml\n' 'ALLOW_WORKFLOW_EDITS=true'
	assert_eq "0" "${RUN_GUARD_STATUS}" 'workflow edits should pass when ALLOW_WORKFLOW_EDITS=true'
	assert_not_contains 'WRITE_GUARD_BLOCK' "${RUN_GUARD_OUTPUT}" 'conditional allow should not block'
	rm -rf "${repo_dir}"
}

test_config_parse_fail_open()
{
	local repo_dir
	repo_dir="$(make_temp_repo)"
	printf '{invalid json\n' > "${repo_dir}/.github/ai/write_guards.v1.json"
	run_guard_capture "${repo_dir}" review_editor $'.github/workflows/test.yml\n' 'ALLOW_WORKFLOW_EDITS=false'
	assert_eq "0" "${RUN_GUARD_STATUS}" 'config parse errors should fail open'
	assert_contains 'WRITE_GUARD_CONFIG_ERROR: phase=review_editor config=.github/ai/write_guards.v1.json' "${RUN_GUARD_OUTPUT}" 'config parse failure should be logged'
	rm -rf "${repo_dir}"
}

test_config_parse_fail_open_from_fallback()
{
	local repo_dir
	repo_dir="$(make_temp_repo)"
	mkdir -p "${repo_dir}/.codex-workflow-src/.github/ai"
	rm -f "${repo_dir}/.github/ai/write_guards.v1.json"
	printf '{invalid json\n' > "${repo_dir}/.codex-workflow-src/.github/ai/write_guards.v1.json"
	run_guard_capture "${repo_dir}" review_editor $'.github/workflows/test.yml\n' 'ALLOW_WORKFLOW_EDITS=false'
	assert_eq "0" "${RUN_GUARD_STATUS}" 'fallback config parse errors should fail open'
	assert_contains 'WRITE_GUARD_CONFIG_ERROR: phase=review_editor config=.codex-workflow-src/.github/ai/write_guards.v1.json' "${RUN_GUARD_OUTPUT}" 'fallback config parse failure should log the resolved path'
	rm -rf "${repo_dir}"
}

test_bypass_env_audit()
{
	local repo_dir
	repo_dir="$(make_temp_repo)"
	run_guard_capture "${repo_dir}" validate_fix_harness $'README.md\n' 'WRITE_GUARDS_ENABLED=false'
	assert_eq "0" "${RUN_GUARD_STATUS}" 'WRITE_GUARDS_ENABLED=false should bypass the guard'
	assert_contains 'WRITE_GUARD_BYPASS_ENV: phase=validate_fix_harness env=WRITE_GUARDS_ENABLED value=false' "${RUN_GUARD_OUTPUT}" 'bypass should be logged'
	rm -rf "${repo_dir}"
}

test_allowlist_pass
test_blocklist_fail
test_not_allowed_fail
test_conditional_block_with_env_disabled
test_conditional_block_with_env_enabled
test_config_parse_fail_open
test_config_parse_fail_open_from_fallback
test_bypass_env_audit
echo "test_write_guard.sh: PASS"
