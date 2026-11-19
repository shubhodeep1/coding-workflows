#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROMPT_FILE="${REPO_ROOT}/prompts/_nag_reminders.txt"

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

lookup_prompt_value()
{
	local prompt_file="$1"
	local phase="$2"

	awk -F= -v key="${phase}" '
		$1 == key {
			sub(/^[^=]*=/, "")
			print
			exit
		}
	' "${prompt_file}"
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

test_repo_prompt_fragment_has_expected_phase_text()
{
	local _tmpdir="$1"
	local actual=""

	actual="$(lookup_prompt_value "${PROMPT_FILE}" "review-editor")"
	assert_equals "Re-engage with the queued fixes now. If changes are needed, perform the repository write in this turn instead of only describing it, then finish with the required editor summary." "${actual}"

	actual="$(lookup_prompt_value "${PROMPT_FILE}" "review-reviewer")"
	assert_equals "Continue the review using the required plain-text issue format. If you found no qualifying issues, reply with the literal word NONE." "${actual}"

	actual="$(lookup_prompt_value "${PROMPT_FILE}" "orchestrate-poll-judge")"
	assert_equals "Return exactly one JSON object matching the judge output contract, including a non-empty status field." "${actual}"
}

test_helper_loads_phase_specific_text_from_prompt_fragment()
{
	local tmpdir="$1"
	local prompts_dir="${tmpdir}/prompts"
	local phase=""
	local expected_text=""
	local actual_text=""
	local actual_block=""
	local expected_block=""

	mkdir -p "${prompts_dir}"
	cat > "${prompts_dir}/_nag_reminders.txt" <<'EOF'
review-editor=editor sentinel reminder
review-reviewer=reviewer sentinel reminder
orchestrate-poll-judge=judge sentinel reminder
EOF

	for phase in review-editor review-reviewer orchestrate-poll-judge; do
		expected_text="$(lookup_prompt_value "${prompts_dir}/_nag_reminders.txt" "${phase}")"
		actual_text="$({
			(
				export SUPPORT_PROMPTS_DIR="${prompts_dir}"
				source "${REPO_ROOT}/scripts/nag_reminder.sh"
				load_nag_reminder_text "${phase}"
			)
		})"
		assert_equals "${expected_text}" "${actual_text}"

		expected_block=$(printf '<reminder>\n%s\n</reminder>' "${expected_text}")
		actual_block="$({
			(
				export UNATTENDED_NAG_REMINDER_ENABLED=true
				export UNATTENDED_NAG_SILENT_ROUNDS=3
				export SUPPORT_PROMPTS_DIR="${prompts_dir}"
				source "${REPO_ROOT}/scripts/nag_reminder.sh"
				maybe_inject_nag "${phase}" "3"
			)
		})"
		assert_equals "${expected_block}" "${actual_block}"
	done
}

with_temp_workspace test_repo_prompt_fragment_has_expected_phase_text
with_temp_workspace test_helper_loads_phase_specific_text_from_prompt_fragment

echo "test_nag_reminder_phases.sh: PASS"
