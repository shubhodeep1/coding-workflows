#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# shellcheck source=/dev/null
source "${REPO_ROOT}/scripts/codex_helpers.sh"

assert_contains()
{
	local needle="$1"
	local haystack_file="$2"
	if ! grep -Fqx -- "${needle}" "${haystack_file}"; then
		echo "expected line not found: ${needle}" >&2
		echo "--- ${haystack_file} ---" >&2
		cat "${haystack_file}" >&2 || true
		exit 1
	fi
}

test_relative_support_dir_resolution()
{
	local tmpdir workspace scripts_dir env_file writer_log helper_home
	tmpdir="$(mktemp -d)"
	trap 'rm -rf "${tmpdir}"' RETURN
	workspace="${tmpdir}/workspace"
	scripts_dir="${workspace}/support/scripts"
	env_file="${tmpdir}/github.env"
	writer_log="${tmpdir}/writer.log"
	helper_home="${tmpdir}/home"
	mkdir -p "${scripts_dir}" "${helper_home}"
	touch "${scripts_dir}/codex_model_catalog.json"
	cat > "${scripts_dir}/write_codex_config.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "\$@" > "${writer_log}"
EOF
	chmod +x "${scripts_dir}/write_codex_config.sh"

	GITHUB_WORKSPACE="${workspace}" \
	GITHUB_ENV="${env_file}" \
	HOME="${helper_home}" \
	CODEX_HELPERS_SCRIPTS_DIR="support/scripts" \
		codex_config_assemble "openai/gpt-5.4" "xhigh" "low" --web-search disabled

	assert_contains '--model' "${writer_log}"
	assert_contains 'openai/gpt-5.4' "${writer_log}"
	assert_contains '--reasoning' "${writer_log}"
	assert_contains 'xhigh' "${writer_log}"
	assert_contains '--web-search' "${writer_log}"
	assert_contains 'disabled' "${writer_log}"
	assert_contains '--catalog-path' "${writer_log}"
	assert_contains "${scripts_dir}/codex_model_catalog.json" "${writer_log}"
	assert_contains "CODEX_HOME=${helper_home}/.codex" "${env_file}"
	trap - RETURN
	rm -rf "${tmpdir}"
}

test_absolute_support_dir_resolution()
{
	local tmpdir scripts_dir env_file writer_log helper_home
	tmpdir="$(mktemp -d)"
	trap 'rm -rf "${tmpdir}"' RETURN
	scripts_dir="${tmpdir}/runtime/scripts"
	env_file="${tmpdir}/github.env"
	writer_log="${tmpdir}/writer.log"
	helper_home="${tmpdir}/home"
	mkdir -p "${scripts_dir}" "${helper_home}"
	touch "${scripts_dir}/codex_model_catalog.json"
	cat > "${scripts_dir}/write_codex_config.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "\$@" > "${writer_log}"
EOF
	chmod +x "${scripts_dir}/write_codex_config.sh"

	GITHUB_ENV="${env_file}" \
	HOME="${helper_home}" \
	CODEX_HELPERS_SCRIPTS_DIR="${scripts_dir}" \
		codex_config_assemble "openai/gpt-5.4" "medium" "low"

	assert_contains '--web-search' "${writer_log}"
	assert_contains 'live' "${writer_log}"
	assert_contains "${scripts_dir}/codex_model_catalog.json" "${writer_log}"
	assert_contains "CODEX_HOME=${helper_home}/.codex" "${env_file}"
	trap - RETURN
	rm -rf "${tmpdir}"
}

test_default_support_dir_resolution_preserves_pwd()
{
	local tmpdir caller_dir resolved_file starting_dir current_dir
	tmpdir="$(mktemp -d)"
	trap 'cd "${starting_dir}" 2>/dev/null || true; rm -rf "${tmpdir}"' RETURN
	caller_dir="${tmpdir}/caller"
	resolved_file="${tmpdir}/resolved.txt"
	starting_dir="$(pwd)"
	mkdir -p "${caller_dir}"
	cd "${caller_dir}"
	CODEX_HELPERS_SCRIPTS_DIR="" _codex_helpers_resolve_scripts_dir > "${resolved_file}"
	assert_contains "${REPO_ROOT}/scripts" "${resolved_file}"
	current_dir="$(pwd)"
	if [ "${current_dir}" != "${caller_dir}" ]; then
		echo "default scripts-dir resolution mutated the caller working directory: ${current_dir}" >&2
		exit 1
	fi
	trap - RETURN
	cd "${starting_dir}"
	rm -rf "${tmpdir}"
}

test_missing_writer_helper_fails_fast()
{
	local tmpdir scripts_dir env_file err_file helper_home
	tmpdir="$(mktemp -d)"
	trap 'rm -rf "${tmpdir}"' RETURN
	scripts_dir="${tmpdir}/runtime/scripts"
	env_file="${tmpdir}/github.env"
	err_file="${tmpdir}/stderr.log"
	helper_home="${tmpdir}/home"
	mkdir -p "${scripts_dir}" "${helper_home}"
	touch "${scripts_dir}/codex_model_catalog.json"

	if GITHUB_ENV="${env_file}" \
		HOME="${helper_home}" \
		CODEX_HELPERS_SCRIPTS_DIR="${scripts_dir}" \
		codex_config_assemble "openai/gpt-5.4" "medium" "low" 2>"${err_file}"; then
		echo 'expected codex_config_assemble to fail when write_codex_config.sh is missing' >&2
		exit 1
	fi
	assert_contains "::error::codex_config_assemble: missing writer helper ${scripts_dir}/write_codex_config.sh" "${err_file}"
	trap - RETURN
	rm -rf "${tmpdir}"
}

test_missing_option_value_fails_fast()
{
	local tmpdir err_file
	tmpdir="$(mktemp -d)"
	trap 'rm -rf "${tmpdir}"' RETURN
	err_file="${tmpdir}/stderr.log"

	if codex_config_assemble "openai/gpt-5.4" "medium" "low" --web-search 2>"${err_file}"; then
		echo 'expected codex_config_assemble to fail when an option value is missing' >&2
		exit 1
	fi
	assert_contains '::error::codex_config_assemble: option --web-search requires an argument' "${err_file}"
	trap - RETURN
	rm -rf "${tmpdir}"
}

test_relative_support_dir_resolution
test_absolute_support_dir_resolution
test_default_support_dir_resolution_preserves_pwd
test_missing_writer_helper_fails_fast
test_missing_option_value_fails_fast
echo "test_codex_helpers.sh: PASS"
