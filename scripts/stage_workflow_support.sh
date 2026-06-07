#!/usr/bin/env bash

set -euo pipefail

WORKFLOW_SUPPORT_SOURCE_REPO_DEFAULT="shubhodeep1/coding-workflows"

usage()
{
	cat <<'EOF' >&2
Usage: scripts/stage_workflow_support.sh validate --manifest <path>
EOF
	exit 64
}

require_command()
{
	local cmd="$1"
	if ! command -v "${cmd}" >/dev/null 2>&1; then
		echo "::error::Required command '${cmd}' is unavailable." >&2
		exit 1
	fi
}

cleanup_support_stage()
{
	if [ -n "${SUPPORT_STAGE_ROOT:-}" ] && [ -d "${SUPPORT_STAGE_ROOT}" ]; then
		rm -rf "${SUPPORT_STAGE_ROOT}"
	fi
}

parse_args()
{
	TARGET_NAME="${1:-}"
	shift || true
	MANIFEST_PATH=""
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--manifest)
				if [ "$#" -lt 2 ]; then
					usage
				fi
				MANIFEST_PATH="$2"
				shift 2
				;;
			*)
				usage
				;;
		esac
	done

	if [ -z "${TARGET_NAME}" ] || [ -z "${MANIFEST_PATH}" ]; then
		usage
	fi
	if [ "${TARGET_NAME}" != "validate" ]; then
		echo "::error::Unsupported workflow support target '${TARGET_NAME}'." >&2
		exit 1
	fi
	if [ ! -f "${MANIFEST_PATH}" ]; then
		echo "::error::Workflow support manifest '${MANIFEST_PATH}' does not exist." >&2
		exit 1
	fi
}

setup_context()
{
	require_command git
	require_command jq
	require_command python3

	REPO_ROOT="${GITHUB_WORKSPACE:-$(pwd)}"
	cd "${REPO_ROOT}"

	WORKFLOW_SOURCE_REPO="${WORKFLOW_SUPPORT_SOURCE_REPO:-${WORKFLOW_SUPPORT_SOURCE_REPO_DEFAULT}}"
	ORIGINAL_SCRIPT_REF="${WORKFLOW_SUPPORT_REF:-}"
	if [ -z "${ORIGINAL_SCRIPT_REF}" ]; then
		if [ "${GITHUB_REPOSITORY:-}" = "${WORKFLOW_SOURCE_REPO}" ]; then
			ORIGINAL_SCRIPT_REF="${GITHUB_SHA:-}"
		else
			ORIGINAL_SCRIPT_REF="stable"
		fi
	fi
	RESOLVED_SCRIPT_REF="${ORIGINAL_SCRIPT_REF}"

	IS_SELF_REPO="false"
	if [ "${GITHUB_REPOSITORY:-}" = "${WORKFLOW_SOURCE_REPO}" ]; then
		IS_SELF_REPO="true"
	fi

	declare -ga FETCHED_SCRIPT_PATHS=()
	SUPPORT_STAGE_ROOT="$(mktemp -d "${RUNNER_TEMP:-/tmp}/stage-workflow-support-XXXXXX")"
	trap cleanup_support_stage EXIT
}

checkout_support_ref()
{
	local ref="$1"
	local dest="$2"
	local server_host remote_url

	if [ -z "${GH_TOKEN:-}" ]; then
		echo "::error::GH_TOKEN is required to stage workflow support files." >&2
		return 1
	fi

	server_host="${GITHUB_SERVER_URL:-https://github.com}"
	server_host="${server_host#https://}"
	server_host="${server_host#http://}"
	server_host="${server_host%/}"
	remote_url="https://x-access-token:${GH_TOKEN}@${server_host}/${WORKFLOW_SOURCE_REPO}"

	rm -rf "${dest}"
	mkdir -p "$(dirname "${dest}")"
	if git clone --quiet --no-tags --depth 1 --branch "${ref}" "${remote_url}" "${dest}" 2>/dev/null; then
		return 0
	fi
	rm -rf "${dest}"
	return 1
}

bootstrap_support_roots()
{
	SUPPORT_PRIMARY_ROOT=""
	SUPPORT_MAIN_ROOT=""

	if [ "${IS_SELF_REPO}" = "true" ]; then
		SUPPORT_PRIMARY_ROOT="${REPO_ROOT}"
	elif checkout_support_ref "${ORIGINAL_SCRIPT_REF}" "${SUPPORT_STAGE_ROOT}/primary"; then
		SUPPORT_PRIMARY_ROOT="${SUPPORT_STAGE_ROOT}/primary"
	elif checkout_support_ref "main" "${SUPPORT_STAGE_ROOT}/primary"; then
		echo "::warning::Support checkout ref ${ORIGINAL_SCRIPT_REF} is unavailable; using main."
		SUPPORT_PRIMARY_ROOT="${SUPPORT_STAGE_ROOT}/primary"
		RESOLVED_SCRIPT_REF="main"
	else
		echo "::error::Failed to stage workflow support files from ${WORKFLOW_SOURCE_REPO} (${ORIGINAL_SCRIPT_REF} and main fallback)." >&2
		exit 1
	fi

	if [ "${RESOLVED_SCRIPT_REF}" != "main" ] && [ -n "${GH_TOKEN:-}" ] && checkout_support_ref "main" "${SUPPORT_STAGE_ROOT}/main"; then
		SUPPORT_MAIN_ROOT="${SUPPORT_STAGE_ROOT}/main"
	fi
}

copy_from_ref_or_local()
{
	local repo_path="$1"
	local target_path="$2"
	local require_remote="${3:-false}"
	local allow_main_fallback="${4:-true}"
	local source_path=""

	mkdir -p "$(dirname "${target_path}")"

	if [ -n "${SUPPORT_PRIMARY_ROOT}" ] && [ -f "${SUPPORT_PRIMARY_ROOT}/${repo_path}" ]; then
		source_path="${SUPPORT_PRIMARY_ROOT}/${repo_path}"
	elif [ "${allow_main_fallback}" = "true" ] && [ -n "${SUPPORT_MAIN_ROOT}" ] && [ -f "${SUPPORT_MAIN_ROOT}/${repo_path}" ]; then
		echo "::warning::${repo_path} not found on ${RESOLVED_SCRIPT_REF}; falling back to main"
		source_path="${SUPPORT_MAIN_ROOT}/${repo_path}"
	fi

	if [ -n "${source_path}" ]; then
		if [ -e "${target_path}" ] && [ "${source_path}" -ef "${target_path}" ]; then
			return 0
		fi
		cp "${source_path}" "${target_path}" || {
			echo "Failed to copy ${repo_path} from ${source_path} to ${target_path}" >&2
			return 1
		}
		return 0
	fi

	if [ "${require_remote}" != "true" ] && [ -f "${target_path}" ]; then
		echo "Using local fallback for ${target_path} (not found on ${RESOLVED_SCRIPT_REF}: ${repo_path})"
		return 0
	fi

	if [ "${require_remote}" = "true" ]; then
		echo "Missing required remote file: ${repo_path} (${RESOLVED_SCRIPT_REF}); local fallback is disabled for this file." >&2
	else
		echo "Missing required file: ${repo_path} (${RESOLVED_SCRIPT_REF}) and local fallback ${target_path}" >&2
	fi
	return 1
}

json_array_lines()
{
	local key="$1"
	jq -r --arg key "${key}" '.[$key][]? // empty' "${MANIFEST_PATH}"
}

json_string_value()
{
	local key="$1"
	jq -r --arg key "${key}" '.[$key] // empty' "${MANIFEST_PATH}"
}

record_fetched_script()
{
	local relative_path="$1"
	if [ -n "${relative_path}" ]; then
		FETCHED_SCRIPT_PATHS+=("${relative_path}")
	fi
}

emit_optional_missing_notice()
{
	local repo_path="$1"
	case "${repo_path}" in
		scripts/render_prompt.py)
			echo "render_prompt.py backend not on ${ORIGINAL_SCRIPT_REF} yet; render_prompt.sh shim (if present) resolves it elsewhere or the bundled bash renderer is used."
			;;
		scripts/render_validation_templates.py)
			echo "Template renderer not on ${ORIGINAL_SCRIPT_REF} yet; template mode may not be available."
			;;
		scripts/templates/slot_manifest.schema.json)
			echo "Validation slot manifest schema not on ${ORIGINAL_SCRIPT_REF} yet; template mode may not be available."
			;;
		scripts/codex_heartbeat.sh)
			echo "Codex heartbeat helper not on ${ORIGINAL_SCRIPT_REF} yet; validation will continue without live heartbeat logging."
			;;
		scripts/codex_stall_guard.sh)
			echo "Codex stall guard helper not on ${ORIGINAL_SCRIPT_REF} yet; validation will continue without idle-kill wrapping."
			;;
		scripts/codex_thread_reuse.sh)
			echo "Codex thread reuse helper not on ${ORIGINAL_SCRIPT_REF} yet; validation will continue without same-run thread reuse."
			;;
		scripts/install_semble.sh)
			echo "Semble installer not on ${ORIGINAL_SCRIPT_REF} yet; validation will continue without Semble bootstrap."
			;;
		scripts/semble_helpers.sh)
			echo "Semble helper library not on ${ORIGINAL_SCRIPT_REF} yet; validation prompts will continue without Semble context."
			;;
		scripts/build_semble_wrapper.sh)
			echo "Semble BM25 wrapper builder not on ${ORIGINAL_SCRIPT_REF} yet; validation prompts will continue without Semble context."
			;;
		scripts/self_heal_validation.sh)
			echo "Self-heal helper not on ${ORIGINAL_SCRIPT_REF} yet; self-heal will be a no-op."
			;;
		scripts/setup_serena.sh|scripts/serena_stats_emit.py|scripts/mcp_handshake_probe.py)
			echo "Optional Serena support asset ${repo_path} not on ${ORIGINAL_SCRIPT_REF} yet; validation will continue without that helper."
			;;
		scripts/templates/serena_project.yml.j2)
			echo "Optional Serena template scripts/templates/serena_project.yml.j2 not on ${ORIGINAL_SCRIPT_REF} yet; validation will continue without Serena project templating."
			;;
		prompts/mode-validate-self-heal.txt)
			echo "Self-heal prompt not on ${ORIGINAL_SCRIPT_REF} yet; self-heal will be a no-op."
			;;
		prompts/mode-validate-self-heal-continuation.txt)
			echo "Self-heal continuation prompt not on ${ORIGINAL_SCRIPT_REF} yet; validation will continue with the full prompt path."
			;;
		prompts/contracts/mode-validate-self-heal-continuation.yml)
			echo "Self-heal continuation contract not on ${ORIGINAL_SCRIPT_REF} yet; validation will continue with the full prompt path."
			;;
		*)
			return 1
			;;
		esac
	return 0
}

stage_required_entry()
{
	local repo_path="$1"
	local executable="$2"
	local track_path="$3"
	local require_remote="$4"

	copy_from_ref_or_local "${repo_path}" "${repo_path}" "${require_remote}" "true"
	if [ "${executable}" = "true" ]; then
		chmod +x "${repo_path}"
	fi
	record_fetched_script "${track_path}"
}

stage_optional_preserve_entry()
{
	local repo_path="$1"
	local executable="$2"
	local track_path="$3"
	local emit_notice="${4:-true}"
	local tmp_path

	if [ -f "${repo_path}" ]; then
		if [ "${executable}" = "true" ]; then
			chmod +x "${repo_path}"
		fi
		record_fetched_script "${track_path}"
		return 0
	fi

	tmp_path="${repo_path}.tmp"
	rm -f "${tmp_path}"
	if copy_from_ref_or_local "${repo_path}" "${tmp_path}" "false" "true"; then
		if [ -f "${tmp_path}" ]; then
			mv "${tmp_path}" "${repo_path}"
			if [ "${executable}" = "true" ]; then
				chmod +x "${repo_path}"
			fi
			record_fetched_script "${track_path}"
		else
			rm -f "${tmp_path}"
			if [ "${emit_notice}" = "true" ]; then
				emit_optional_missing_notice "${repo_path}" || true
			fi
		fi
	else
		rm -f "${tmp_path}"
		if [ "${emit_notice}" = "true" ]; then
			emit_optional_missing_notice "${repo_path}" || true
		fi
	fi
}

stage_optional_copy_entry()
{
	local repo_path="$1"
	copy_from_ref_or_local "${repo_path}" "${repo_path}" "false" "true" || true
}

stage_copy_if_missing_silent_entry()
{
	local repo_path="$1"
	local tmp_path

	if [ -f "${repo_path}" ]; then
		return 0
	fi

	tmp_path="${repo_path}.tmp"
	rm -f "${tmp_path}"
	if copy_from_ref_or_local "${repo_path}" "${tmp_path}" "false" "true"; then
		if [ -f "${tmp_path}" ]; then
			mv "${tmp_path}" "${repo_path}"
		else
			rm -f "${tmp_path}"
		fi
	else
		rm -f "${tmp_path}"
	fi
}

stage_required_if_missing_entry()
{
	local repo_path="$1"
	if [ ! -f "${repo_path}" ]; then
		copy_from_ref_or_local "${repo_path}" "${repo_path}" "false" "true"
	fi
}

stage_model_catalog_entry()
{
	local repo_path="$1"
	local tmp_path="${repo_path}.tmp"

	rm -f "${tmp_path}"
	if copy_from_ref_or_local "${repo_path}" "${tmp_path}" "false" "false"; then
		if [ -s "${tmp_path}" ]; then
			mv "${tmp_path}" "${repo_path}"
			record_fetched_script "${repo_path#scripts/}"
		else
			rm -f "${tmp_path}"
			echo "Model catalog not on ${ORIGINAL_SCRIPT_REF} yet; using local copy."
		fi
	else
		rm -f "${tmp_path}"
		echo "Model catalog not on ${ORIGINAL_SCRIPT_REF} yet; using local copy."
	fi
}

stage_serena_template_entry()
{
	local repo_path="$1"
	local tmp_path

	if [ "${IS_SELF_REPO}" != "true" ] && git ls-files --error-unmatch -- "${repo_path}" >/dev/null 2>&1; then
		echo "::notice::Consumer repo already tracks ${repo_path}; preserving caller-owned Serena template."
		return 0
	fi

	if [ -f "${repo_path}" ]; then
		record_fetched_script "${repo_path#scripts/}"
		return 0
	fi

	tmp_path="${repo_path}.tmp"
	rm -f "${tmp_path}"
	if copy_from_ref_or_local "${repo_path}" "${tmp_path}" "false" "true"; then
		if [ -f "${tmp_path}" ]; then
			mv "${tmp_path}" "${repo_path}"
			record_fetched_script "${repo_path#scripts/}"
		else
			rm -f "${tmp_path}"
			emit_optional_missing_notice "${repo_path}" || true
		fi
	else
		rm -f "${tmp_path}"
		emit_optional_missing_notice "${repo_path}" || true
	fi
}

emit_consumer_gitignore()
{
	if [ "${IS_SELF_REPO}" = "true" ]; then
		return 0
	fi

	mkdir -p scripts
	{
		echo "# Auto-generated by coding-workflows — DO NOT EDIT"
		echo "# Prevents runtime-fetched support scripts from being committed."
		if [ "${#FETCHED_SCRIPT_PATHS[@]}" -gt 0 ]; then
			printf '%s\n' "${FETCHED_SCRIPT_PATHS[@]}"
		fi
		echo ".gitignore"
	} > scripts/.gitignore
}

run_overlay_loader()
{
	: "${GITHUB_ENV:?GITHUB_ENV must be set}"
	# WORKFLOW.md overlay is opt-in by file presence; absent file must
	# stay a no-op while valid prompt overrides flow through render_prompt.py.
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/load_workflow_overlay.py \
		--repo-root "${REPO_ROOT}" \
		--schema-path "ai-memory/schemas/workflow_overlay.v1.json" \
		--github-env "${GITHUB_ENV}"
}

stage_validate_support()
{
	local repo_path require_remote_when_external model_catalog_path serena_template_path

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_required_entry "${repo_path}" "true" "${repo_path#scripts/}" "false"
	done < <(json_array_lines "required_scripts")

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_optional_preserve_entry "${repo_path}" "true" "${repo_path#scripts/}" "true"
	done < <(json_array_lines "optional_preserve_scripts_before_templates")

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_optional_preserve_entry "${repo_path}" "false" "" "true"
	done < <(json_array_lines "optional_preserve_files_before_templates")

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_optional_copy_entry "${repo_path}"
	done < <(json_array_lines "optional_copy_files")

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		require_remote_when_external="false"
		if [ "${IS_SELF_REPO}" != "true" ]; then
			require_remote_when_external="true"
		fi
		stage_required_entry "${repo_path}" "true" "${repo_path#scripts/}" "${require_remote_when_external}"
	done < <(json_array_lines "required_remote_when_external_scripts")

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_copy_if_missing_silent_entry "${repo_path}"
	done < <(json_array_lines "schema_files_if_missing")

	run_overlay_loader

	model_catalog_path="$(json_string_value "model_catalog_path")"
	if [ -n "${model_catalog_path}" ]; then
		stage_model_catalog_entry "${model_catalog_path}"
	fi

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_optional_preserve_entry "${repo_path}" "true" "${repo_path#scripts/}" "true"
	done < <(json_array_lines "optional_preserve_scripts_after_schemas")

	serena_template_path="$(json_string_value "serena_template_path")"
	if [ -n "${serena_template_path}" ]; then
		stage_serena_template_entry "${serena_template_path}"
	fi

	emit_consumer_gitignore

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_required_entry "${repo_path}" "false" "" "false"
	done < <(json_array_lines "required_prompts")

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_optional_preserve_entry "${repo_path}" "false" "" "true"
	done < <(json_array_lines "optional_preserve_files_after_prompts")

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_required_if_missing_entry "${repo_path}"
	done < <(json_array_lines "required_root_files_if_missing")

	while IFS= read -r repo_path; do
		[ -n "${repo_path}" ] || continue
		stage_copy_if_missing_silent_entry "${repo_path}"
	done < <(json_array_lines "optional_root_files_if_missing")
}

main()
{
	parse_args "$@"
	setup_context
	bootstrap_support_roots

	case "${TARGET_NAME}" in
		validate)
			stage_validate_support
			;;
		*)
			usage
			;;
		esac
}

main "$@"
