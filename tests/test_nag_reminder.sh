#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

assert_equals()
{
	local expected="$1"
	local actual="$2"
	if [ "${expected}" != "${actual}" ]; then
		echo "expected: ${expected}" >&2
		echo "actual:   ${actual}" >&2
		exit 1
	fi
}

assert_empty()
{
	local actual="$1"
	if [ -n "${actual}" ]; then
		echo "expected empty output" >&2
		echo "actual: ${actual}" >&2
		exit 1
	fi
}

assert_file_empty()
{
	local path="$1"
	if [ -s "${path}" ]; then
		echo "expected empty file: ${path}" >&2
		cat "${path}" >&2
		exit 1
	fi
}

with_temp_workspace()
{
	local callback="$1"
	local tmpdir=""
	tmpdir="$(mktemp -d)"
	trap 'rm -rf "${tmpdir}"' RETURN
	"${callback}" "${tmpdir}"
	trap - RETURN
	rm -rf "${tmpdir}"
}

test_flag_off_emits_nothing()
{
	local tmpdir="$1"
	local stderr_file="${tmpdir}/stderr.txt"
	local actual=""

	actual="$({
		(
			export UNATTENDED_NAG_REMINDER_ENABLED=false
			export SUPPORT_PROMPTS_DIR="${REPO_ROOT}/prompts"
			source "${REPO_ROOT}/scripts/nag_reminder.sh"
			maybe_inject_nag "review-editor" "99" "Reminder body"
		) 2>"${stderr_file}"
	})"

	assert_empty "${actual}"
	assert_file_empty "${stderr_file}"
}

test_counter_below_threshold_emits_nothing()
{
	local tmpdir="$1"
	local stderr_file="${tmpdir}/stderr.txt"
	local actual=""

	actual="$({
		(
			export UNATTENDED_NAG_REMINDER_ENABLED=true
			export UNATTENDED_NAG_SILENT_ROUNDS=3
			source "${REPO_ROOT}/scripts/nag_reminder.sh"
			maybe_inject_nag "review-editor" "2" "Reminder body"
		) 2>"${stderr_file}"
	})"

	assert_empty "${actual}"
	assert_file_empty "${stderr_file}"
}

test_threshold_emits_reminder_and_reset_path_is_empty()
{
	local tmpdir="$1"
	local stderr_file="${tmpdir}/stderr.txt"
	local actual=""
	local reset_output=""
	local expected=$'<reminder>\nReminder body\n</reminder>'

	actual="$({
		(
			export UNATTENDED_NAG_REMINDER_ENABLED=true
			export UNATTENDED_NAG_SILENT_ROUNDS=3
			source "${REPO_ROOT}/scripts/nag_reminder.sh"
			maybe_inject_nag "review-editor" "3" "Reminder body"
		) 2>"${stderr_file}"
	})"
	assert_equals "${expected}" "${actual}"
	assert_file_empty "${stderr_file}"

	reset_output="$({
		(
			export UNATTENDED_NAG_REMINDER_ENABLED=true
			export UNATTENDED_NAG_SILENT_ROUNDS=3
			source "${REPO_ROOT}/scripts/nag_reminder.sh"
			maybe_inject_nag "review-editor" "0" "Reminder body"
		) 2>"${stderr_file}"
	})"
	assert_empty "${reset_output}"
	assert_file_empty "${stderr_file}"
}

test_invalid_thresholds_clamp_to_three()
{
	local _tmpdir="$1"
	local raw=""
	local actual=""

	for raw in 0 11 abc ''; do
		actual="$({
			(
				export UNATTENDED_NAG_SILENT_ROUNDS="${raw}"
				source "${REPO_ROOT}/scripts/nag_reminder.sh"
				nag_silent_round_threshold
			)
		})"
		assert_equals "3" "${actual}"
	done

	actual="$({
		(
			export UNATTENDED_NAG_SILENT_ROUNDS=5
			source "${REPO_ROOT}/scripts/nag_reminder.sh"
			nag_silent_round_threshold
		)
	})"
	assert_equals "5" "${actual}"
}

test_missing_prompt_fragment_fails_open()
{
	local tmpdir="$1"
	local helper_root="${tmpdir}/isolated"
	local helper_copy="${helper_root}/scripts/nag_reminder.sh"
	local stderr_file="${tmpdir}/stderr.txt"
	local actual=""

	mkdir -p "${helper_root}/scripts"
	install -m 0755 "${REPO_ROOT}/scripts/nag_reminder.sh" "${helper_copy}"

	actual="$({
		(
			cd "${helper_root}"
			export UNATTENDED_NAG_REMINDER_ENABLED=true
			export SUPPORT_PROMPTS_DIR="${helper_root}/missing-prompts"
			export SUPPORT_ROOT_DIR="${helper_root}/missing-support-root"
			source "${helper_copy}"
			maybe_inject_nag "review-editor" "3"
		) 2>"${stderr_file}"
	})"

	assert_empty "${actual}"
	grep -q '^NAG_REMINDER_LOAD_FAIL: prompts/_nag_reminders.txt unavailable for phase review-editor$' "${stderr_file}"
}

with_temp_workspace test_flag_off_emits_nothing
with_temp_workspace test_counter_below_threshold_emits_nothing
with_temp_workspace test_threshold_emits_reminder_and_reset_path_is_empty
with_temp_workspace test_invalid_thresholds_clamp_to_three
with_temp_workspace test_missing_prompt_fragment_fails_open

echo "test_nag_reminder.sh: PASS"
