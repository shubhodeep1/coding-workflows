#!/usr/bin/env bash
# stage_support_for_phase.sh — stage one phase's support scripts from the
# verified workflow support checkout into the out-of-tree runtime bundle.
#
# Usage: stage_support_for_phase.sh <phase> [--host-python-dir DIR]
#   phase              only `clarify` today; plan, clarify_respond and
#                      implement are added by the later P2a/P2b issues
#                      (docs/plans/remove-unattended-pipeline-stuck-states-plan.md)
#   --host-python-dir  absolute directory that also receives the phase's host
#                      Python helpers (clarify: CLARIFY_HOST_PYTHON_DIR)
#
# The phase's workflow used to copy a hand-kept list of scripts into the
# issue checkout's scripts/, so every helper missing from the list ran from
# the target branch. Heal issues target `stable`, so main's YAML ran stable's
# helpers and every heal plan crashed (`codex_stall_guard.sh: unknown option:
# --engine`, run 37406961564). This helper instead:
#   1. verifies that the support checkout's HEAD is the commit SCRIPT_REF
#      names (a 40-hex SHA must match; a branch or tag that resolves locally
#      must match; a ref that does not resolve, as after the checkout's
#      fallback to main, keeps the HEAD the workflow checked out);
#   2. runs scripts/stage_workflow_support.sh (the review_autofix.yml bundle
#      stager) from a scratch directory, so nothing is written into the issue
#      checkout; it exports SUPPORT_ROOT_DIR, SUPPORT_SCRIPTS_DIR,
#      SUPPORT_PROMPTS_DIR and related values to GITHUB_ENV;
#   3. adds the phase's own scripts and data files from the same commit, with
#      no fallback to another ref, so the bundle comes from one commit.
#
# Environment (defaults in brackets):
#   SUPPORT_SOURCE_DIR    [${GITHUB_WORKSPACE:-$PWD}/.codex-workflow-src]
#   WORKFLOW_SOURCE_REPO  [shubhodeep1/coding-workflows]
#   CURRENT_REPOSITORY    [${GITHUB_REPOSITORY}]
#   SCRIPT_REF, RUNNER_TEMP, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_ENV
#                         required, as for stage_workflow_support.sh
#
# Exit status: 0 staged; 1 verification or staging failure; 2 usage error.

set -euo pipefail

stage_support_usage()
{
	echo "Usage: stage_support_for_phase.sh <clarify> [--host-python-dir DIR]" >&2
	exit 2
}

stage_support_fail()
{
	echo "::error::stage_support_for_phase: $*" >&2
	exit 1
}

stage_support_phase="${1:-}"
[ "$#" -gt 0 ] && shift
stage_support_host_python_dir=""
while [ "$#" -gt 0 ]; do
	case "$1" in
		--host-python-dir)
			[ "$#" -ge 2 ] || stage_support_usage
			stage_support_host_python_dir="$2"
			shift 2
			;;
		*)
			stage_support_usage
			;;
	esac
done

# Per-phase lists. Required scripts are the names the phase's legacy copy
# loop installed (kept between the support-scripts-legacy markers of its
# workflow), so the kill switch and the bundle stage the same set.
case "${stage_support_phase}" in
	clarify)
		stage_support_required_scripts=(
			gh_helpers.sh pr_dependency.py git_ref_health_check.sh render_prompt.sh
			assemble_prompt.sh render_prompt.py load_workflow_overlay.py tg_helpers.sh
			label_helpers.sh memory_helpers.sh ai_memory.py ai_memory_lib.py
			memory_injection_patterns.py openrouter_prompt_cache.py semantic_cache.py
			build_static_context.sh assemble_agents.py install_semble.sh
			build_semble_wrapper.sh write_codex_config.sh codex_stall_guard.sh
			check_triage_resolution.py codex_helpers.sh clarify_isolated_run.sh
			clarify_base_image_preseed.sh sandbox_image.sh clarify_openrouter_broker.py
			security_dependency.py auto_decisions.py orchestrate_parse_and_post_answer.sh
			ai_engine.sh claude_engine.py claude_anthropic_relay.py
			claude_settings.json.tmpl workflow_failure_heal.py
			workflow_failure_heal_evidence.sh
		)
		# gh_helpers.sh sources emit_event.sh from its own directory first.
		stage_support_optional_scripts=(emit_event.sh)
		stage_support_data_files=(clarify_sandbox/Dockerfile)
		stage_support_host_python_scripts=(
			pr_dependency.py load_workflow_overlay.py security_dependency.py
			check_triage_resolution.py workflow_failure_heal.py
		)
		# clarify_isolated_run.sh takes its sandbox Dockerfile, broker, config
		# writer and catalog from this directory instead of the checkout.
		stage_support_export_isolation_dir="true"
		;;
	*)
		stage_support_usage
		;;
esac

if [ -n "${stage_support_host_python_dir}" ] && [[ "${stage_support_host_python_dir}" != /* ]]; then
	stage_support_usage
fi

for stage_support_required_env in SCRIPT_REF RUNNER_TEMP GITHUB_RUN_ID GITHUB_RUN_ATTEMPT GITHUB_ENV; do
	if [ -z "${!stage_support_required_env:-}" ]; then
		stage_support_fail "${stage_support_required_env} is required."
	fi
done

STAGE_SUPPORT_SOURCE_DIR="${SUPPORT_SOURCE_DIR:-${GITHUB_WORKSPACE:-$PWD}/.codex-workflow-src}"
STAGE_SUPPORT_WORKFLOW_SOURCE_REPO="${WORKFLOW_SOURCE_REPO:-shubhodeep1/coding-workflows}"
STAGE_SUPPORT_CURRENT_REPOSITORY="${CURRENT_REPOSITORY:-${GITHUB_REPOSITORY:-}}"

if [ -L "${STAGE_SUPPORT_SOURCE_DIR}" ] || [ ! -d "${STAGE_SUPPORT_SOURCE_DIR}" ]; then
	stage_support_fail "support checkout ${STAGE_SUPPORT_SOURCE_DIR} is missing or a symlink."
fi
STAGE_SUPPORT_SOURCE_DIR="$(cd -- "${STAGE_SUPPORT_SOURCE_DIR}" && pwd -P)"

# --- 1. verify the support checkout -----------------------------------------
stage_support_sha="$(git -C "${STAGE_SUPPORT_SOURCE_DIR}" rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null || true)"
if [[ ! "${stage_support_sha}" =~ ^[0-9a-f]{40}$ ]]; then
	stage_support_fail "cannot read the support checkout HEAD."
fi
if [[ "${SCRIPT_REF}" =~ ^[0-9a-f]{40}$ ]]; then
	if [ "${SCRIPT_REF}" != "${stage_support_sha}" ]; then
		stage_support_fail "support checkout HEAD ${stage_support_sha} is not the pinned commit ${SCRIPT_REF}."
	fi
	stage_support_verify="exact"
else
	# Never hand an unchecked value to git as a ref.
	if [[ ! "${SCRIPT_REF}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${SCRIPT_REF}" == *..* ]] \
		|| [[ "${SCRIPT_REF}" == -* ]] || [[ "${SCRIPT_REF}" == /* ]]; then
		stage_support_fail "SCRIPT_REF has an invalid format."
	fi
	stage_support_verify="head_only"
	for stage_support_candidate in "refs/tags/${SCRIPT_REF}" "refs/remotes/origin/${SCRIPT_REF}" "refs/heads/${SCRIPT_REF}"; do
		stage_support_resolved="$(git -C "${STAGE_SUPPORT_SOURCE_DIR}" rev-parse --verify --quiet "${stage_support_candidate}^{commit}" 2>/dev/null || true)"
		[ -n "${stage_support_resolved}" ] || continue
		if [ "${stage_support_resolved}" != "${stage_support_sha}" ]; then
			stage_support_fail "${stage_support_candidate} is ${stage_support_resolved}, not the support checkout HEAD ${stage_support_sha}."
		fi
		stage_support_verify="ref"
	done
	# Visible degraded path: e.g. the checkout fell back to main while
	# SCRIPT_REF stays `stable` (clarify.yml support source fallback).
	if [ "${stage_support_verify}" = "head_only" ]; then
		echo "::warning::stage_support_for_phase: SCRIPT_REF ${SCRIPT_REF} does not resolve in the support checkout; staging its HEAD ${stage_support_sha} unverified."
	fi
fi

# --- 2. run the review bundle stager from a scratch directory ---------------
stage_support_stager="${STAGE_SUPPORT_SOURCE_DIR}/scripts/stage_workflow_support.sh"
if [ -L "${stage_support_stager}" ] || [ ! -f "${stage_support_stager}" ]; then
	stage_support_fail "scripts/stage_workflow_support.sh is missing from support commit ${stage_support_sha}."
fi
stage_support_scratch="$(mktemp -d "${RUNNER_TEMP}/stage-support-phase-XXXXXX")"
trap 'rm -rf -- "${stage_support_scratch}"' EXIT
# The stager reads .codex-workflow-src relative to its working directory and
# writes probably_unnecessary_but_read_if_stuck.md there; the scratch
# directory keeps both away from the issue checkout.
ln -s -- "${STAGE_SUPPORT_SOURCE_DIR}" "${stage_support_scratch}/.codex-workflow-src"
if ! (
	cd -- "${stage_support_scratch}"
	SCRIPT_REF="${stage_support_sha}" \
		CURRENT_REPOSITORY="${STAGE_SUPPORT_CURRENT_REPOSITORY}" \
		WORKFLOW_SOURCE_REPO="${STAGE_SUPPORT_WORKFLOW_SOURCE_REPO}" \
		bash "${stage_support_stager}"
); then
	stage_support_fail "stage_workflow_support.sh failed for support commit ${stage_support_sha}."
fi

stage_support_root="${RUNNER_TEMP}/coding-workflows-runtime-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
stage_support_scripts="${stage_support_root}/scripts"
if [ -L "${stage_support_scripts}" ] || [ ! -d "${stage_support_scripts}" ]; then
	stage_support_fail "the runtime bundle ${stage_support_scripts} was not created."
fi

# --- 3. add the phase's own scripts from the same commit --------------------
for stage_support_file in "${stage_support_required_scripts[@]}"; do
	stage_support_src="${STAGE_SUPPORT_SOURCE_DIR}/scripts/${stage_support_file}"
	if [ -L "${stage_support_src}" ] || [ ! -f "${stage_support_src}" ]; then
		stage_support_fail "Missing required support script ${stage_support_file} in ${STAGE_SUPPORT_WORKFLOW_SOURCE_REPO}@${stage_support_sha}"
	fi
	if [ -f "${stage_support_scripts}/${stage_support_file}" ]; then
		continue
	fi
	install -m 0755 "${stage_support_src}" "${stage_support_scripts}/${stage_support_file}"
done
for stage_support_file in "${stage_support_optional_scripts[@]}"; do
	stage_support_src="${STAGE_SUPPORT_SOURCE_DIR}/scripts/${stage_support_file}"
	[ -f "${stage_support_scripts}/${stage_support_file}" ] && continue
	if [ -L "${stage_support_src}" ] || [ ! -f "${stage_support_src}" ]; then
		echo "::warning::Optional support script ${stage_support_file} is unavailable at ${stage_support_sha}."
		continue
	fi
	install -m 0755 "${stage_support_src}" "${stage_support_scripts}/${stage_support_file}"
done
for stage_support_file in "${stage_support_data_files[@]}"; do
	stage_support_src="${STAGE_SUPPORT_SOURCE_DIR}/scripts/${stage_support_file}"
	if [ -L "${stage_support_src}" ] || [ ! -f "${stage_support_src}" ]; then
		stage_support_fail "Missing required support file scripts/${stage_support_file} in ${STAGE_SUPPORT_WORKFLOW_SOURCE_REPO}@${stage_support_sha}"
	fi
	install -D -m 0644 "${stage_support_src}" "${stage_support_scripts}/${stage_support_file}"
done
if [ -n "${stage_support_host_python_dir}" ]; then
	mkdir -p -- "${stage_support_host_python_dir}"
	for stage_support_file in "${stage_support_host_python_scripts[@]}"; do
		install -m 0755 "${stage_support_scripts}/${stage_support_file}" "${stage_support_host_python_dir}/${stage_support_file}"
	done
fi
if [ "${stage_support_export_isolation_dir}" = "true" ]; then
	echo "CLARIFY_ISOLATION_SUPPORT_DIR=${stage_support_scripts}" >> "${GITHUB_ENV}"
fi
# Machine-readable verify level (exact | ref | head_only) so later steps can
# gate on the degraded head_only path, not only read its ::warning::.
echo "SUPPORT_SCRIPTS_VERIFY=${stage_support_verify}" >> "${GITHUB_ENV}"

echo "::notice::Support scripts staged phase=${stage_support_phase} sha=${stage_support_sha} verify=${stage_support_verify}"
