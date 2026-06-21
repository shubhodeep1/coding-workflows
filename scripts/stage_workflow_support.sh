#!/usr/bin/env bash
set -euo pipefail

stage_review_runtime_support()
{

WORKFLOW_SOURCE_REPO="${WORKFLOW_SOURCE_REPO:-shubhodeep1/coding-workflows}"
CURRENT_REPOSITORY="${CURRENT_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
SCRIPT_REF="${SCRIPT_REF:-unknown}"

if [ -z "${RUNNER_TEMP:-}" ] || [ -z "${GITHUB_RUN_ID:-}" ] || [ -z "${GITHUB_RUN_ATTEMPT:-}" ] || [ -z "${GITHUB_ENV:-}" ]; then
  echo "::error::stage_workflow_support.sh requires RUNNER_TEMP, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, and GITHUB_ENV."
  exit 1
fi

wf_source="${WORKFLOW_SOURCE_REPO}"
workspace_root="${GITHUB_WORKSPACE:-$PWD}"

# Always stage runtime support files out of tree so main-primary
# bootstrap helpers never overwrite the source-repo PR worktree
# before reviewer/editor snapshots are taken.
SUPPORT_ROOT_DIR="${RUNNER_TEMP}/coding-workflows-runtime-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
SUPPORT_SCRIPTS_DIR="${SUPPORT_ROOT_DIR}/scripts"
SUPPORT_PROMPTS_DIR="${SUPPORT_ROOT_DIR}/prompts"
SUPPORT_AI_MEMORY_DIR="${SUPPORT_ROOT_DIR}/ai-memory"
if [ "${CURRENT_REPOSITORY}" = "${wf_source}" ]; then
  IS_WORKFLOW_SOURCE_REPO="true"
else
  IS_WORKFLOW_SOURCE_REPO="false"
fi
mkdir -p "${SUPPORT_SCRIPTS_DIR}" "${SUPPORT_PROMPTS_DIR}" "${SUPPORT_AI_MEMORY_DIR}/schemas"
{
  echo "SUPPORT_ROOT_DIR=${SUPPORT_ROOT_DIR}"
  echo "SUPPORT_SCRIPTS_DIR=${SUPPORT_SCRIPTS_DIR}"
  echo "SUPPORT_PROMPTS_DIR=${SUPPORT_PROMPTS_DIR}"
  echo "SUPPORT_AI_MEMORY_DIR=${SUPPORT_AI_MEMORY_DIR}"
  echo "IS_WORKFLOW_SOURCE_REPO=${IS_WORKFLOW_SOURCE_REPO}"
  echo "SUPPORT_INSTRUCTIONS_FILE=${SUPPORT_ROOT_DIR}/unattended_system_instructions.md"
  echo "SUPPORT_CODEX_INSTRUCTIONS_FILE=${SUPPORT_ROOT_DIR}/unattended_system_instructions.md"
  echo "SUPPORT_AGENTS_FILE=${SUPPORT_ROOT_DIR}/agents.md"
} >> "$GITHUB_ENV"

REQUIRED_BOOTSTRAP_SCRIPTS="gh_helpers.sh git_ref_health_check.sh generate_symbol_diff_summary.py render_prompt.sh load_workflow_overlay.py tg_helpers.sh label_helpers.sh memory_helpers.sh ai_memory.py ai_memory_lib.py memory_injection_patterns.py openrouter_prompt_cache.py cost_audit.py codex_helpers.sh codex_heartbeat.sh codex_stall_guard.sh watchdog_helpers.sh review_run_reviewers.sh review_apply_fixes.sh review_reject_verify.sh review_rb_judge.sh review_run_judge_interim.sh review_synthesise_smoke.sh review_commit_changes.sh write_guard.sh review_collect_pr_metadata.sh collect_pr_check_runs_context.py review_enable_auto_merge.sh review_conflict_prepare.sh review_conflict_resolve.sh orchestrate_force_tick.sh check_workflow_script_refs.py check_resolver_diff.sh summarize_reviewer_consensus.sh check_external_branch_advance.sh post_review_comment.sh targeted_file_context.py write_codex_config.sh detect_editor_changes_lost.sh validate_editor_audit.sh workspace_init.sh workspace_safety_check.sh"
# Main-primary bootstrap scripts: prefer the fresh main snapshot so
# wedged integration branches still pick up resolver safety fixes
# shipped on main. Entries staged only via this list fail open when
# missing from both refs so older consumer script_refs still bootstrap cleanly.
MAIN_PRIMARY_BOOTSTRAP_SCRIPTS="verify_integration_fingerprints.py review_conflict_resolve.sh review_conflict_prepare.sh"
# Optional bootstrap scripts: allowed to be missing from both
# refs.  The bootstrap emits a warning and continues — callers
# that depend on these must themselves tolerate absence.  Keep
# this list empty unless a genuinely optional helper is added;
# the default should always be "required".
#
# render_prompt.py is the Python backend for shim-adopting support
# refs. Some refs still ship a self-contained render_prompt.sh, so
# render_prompt.py MUST stay optional: when the checked-out support ref
# lacks the backend, bootstrap should preserve that ref's bundled bash
# renderer instead of hard-failing.
OPTIONAL_BOOTSTRAP_SCRIPTS="install_semble.sh build_semble_wrapper.sh semble_helpers.sh render_prompt.py"
for f in ${REQUIRED_BOOTSTRAP_SCRIPTS}; do
  src=".codex-workflow-src/scripts/${f}"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/scripts/${f}" ]; then
    src=".codex-workflow-src-main/scripts/${f}"
  fi
  if [ ! -f "${src}" ]; then
    echo "::error::Required bootstrap script '${f}' is missing from checked-out support sources (${SCRIPT_REF} and optional main snapshot) in ${wf_source}. This usually means review_autofix.yml references a file that was never committed (common cause: a hallucinated [ai-merge-resolve] commit added a new bootstrap entry without its corresponding script). Verify REQUIRED_BOOTSTRAP_SCRIPTS matches the current contents of scripts/."
    exit 1
  fi
  install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/${f}"
done
for f in ${MAIN_PRIMARY_BOOTSTRAP_SCRIPTS}; do
  src=".codex-workflow-src-main/scripts/${f}"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src/scripts/${f}" ]; then
    src=".codex-workflow-src/scripts/${f}"
  fi
  if [ ! -f "${src}" ]; then
    echo "::warning::Main-primary bootstrap script '${f}' not available in checked-out support sources; downstream features relying on this helper will be unavailable."
    continue
  fi
  if [ "${src}" = ".codex-workflow-src-main/scripts/${f}" ] && [ -f ".codex-workflow-src/scripts/${f}" ]; then
    echo "::notice::Bootstrapped ${f} from main snapshot (branch copy ignored)."
  fi
  install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/${f}"
done
for f in ${OPTIONAL_BOOTSTRAP_SCRIPTS}; do
  src=".codex-workflow-src/scripts/${f}"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/scripts/${f}" ]; then
    src=".codex-workflow-src-main/scripts/${f}"
  fi
  if [ ! -f "${src}" ]; then
    echo "::warning::Optional bootstrap script '${f}' not available in checked-out support sources; downstream features relying on this helper will be unavailable."
    continue
  fi
  install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/${f}"
done

for f in setup_serena.sh serena_stats_emit.py mcp_handshake_probe.py; do
  src=".codex-workflow-src/scripts/${f}"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/scripts/${f}" ]; then
    src=".codex-workflow-src-main/scripts/${f}"
  fi
  if [ ! -f "${src}" ]; then
    echo "::warning::Optional Serena support asset ${f} is unavailable in checked-out support sources; Serena bootstrap remains disabled."
    continue
  fi
  install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/${f}"
done

mkdir -p "${SUPPORT_SCRIPTS_DIR}/templates"
serena_template_src=".codex-workflow-src/scripts/templates/serena_project.yml.j2"
if [ ! -f "${serena_template_src}" ] && [ -f ".codex-workflow-src-main/scripts/templates/serena_project.yml.j2" ]; then
  serena_template_src=".codex-workflow-src-main/scripts/templates/serena_project.yml.j2"
fi
if [ ! -f "${serena_template_src}" ]; then
  echo "::warning::Optional Serena template scripts/templates/serena_project.yml.j2 is unavailable in checked-out support sources; Serena bootstrap remains disabled."
else
  install -m 0644 "${serena_template_src}" "${SUPPORT_SCRIPTS_DIR}/templates/serena_project.yml.j2"
fi

for sf in memory_record.v1.json processed_command_entry.v1.json run_ledger_entry.v1.json task_lineage.v1.json actions_runs_cache.v1.json workflow_log_analysis_cache.v1.json fingerprint_quarantine.v1.json validation_history.v1.json operator_bypass_audit.v1.json revalidate_events.v1.json validation_discovery.v1.json workflow_overlay.v1.json; do
  src=".codex-workflow-src/ai-memory/schemas/${sf}"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/ai-memory/schemas/${sf}" ]; then
    src=".codex-workflow-src-main/ai-memory/schemas/${sf}"
  fi
  if [ ! -f "${SUPPORT_AI_MEMORY_DIR}/schemas/${sf}" ] && [ -f "${src}" ]; then
    install -m 0644 "${src}" "${SUPPORT_AI_MEMORY_DIR}/schemas/${sf}"
  fi
done

# WORKFLOW.md overlay is opt-in by file presence; absent file must
# stay a no-op while valid prompt overrides flow through render_prompt.py.
PYTHONDONTWRITEBYTECODE=1 python3 "${SUPPORT_SCRIPTS_DIR}/load_workflow_overlay.py" \
  --repo-root "${workspace_root}" \
  --schema-path "${SUPPORT_AI_MEMORY_DIR}/schemas/workflow_overlay.v1.json" \
  --github-env "${GITHUB_ENV}"

# Mirror the schema install above for the retrieval profiles
# config so consumer repos that do not vendor ai-memory/config/
# still get a workflow-source copy.  The memory library's
# _sync_memory_reference_files reads SUPPORT_AI_MEMORY_DIR as a
# fallback when the consumer-repo source path lacks the file.
retrieval_profiles_src=".codex-workflow-src/ai-memory/config/retrieval_profiles.v1.json"
if [ ! -f "${retrieval_profiles_src}" ] && [ -f ".codex-workflow-src-main/ai-memory/config/retrieval_profiles.v1.json" ]; then
  retrieval_profiles_src=".codex-workflow-src-main/ai-memory/config/retrieval_profiles.v1.json"
fi
mkdir -p "${SUPPORT_AI_MEMORY_DIR}/config"
if [ ! -f "${SUPPORT_AI_MEMORY_DIR}/config/retrieval_profiles.v1.json" ] && [ -f "${retrieval_profiles_src}" ]; then
  install -m 0644 "${retrieval_profiles_src}" "${SUPPORT_AI_MEMORY_DIR}/config/retrieval_profiles.v1.json"
fi

catalog_src=".codex-workflow-src/scripts/codex_model_catalog.json"
if [ ! -f "${catalog_src}" ] && [ -f ".codex-workflow-src-main/scripts/codex_model_catalog.json" ]; then
  catalog_src=".codex-workflow-src-main/scripts/codex_model_catalog.json"
fi
if [ -f "${catalog_src}" ]; then
  install -m 0644 "${catalog_src}" "${SUPPORT_SCRIPTS_DIR}/codex_model_catalog.json"
else
  echo "Model catalog not on ${SCRIPT_REF} yet; using local copy."
fi

if [ ! -f "${SUPPORT_SCRIPTS_DIR}/reviewer_failback_chains.json" ]; then
  failback_src=".codex-workflow-src/scripts/reviewer_failback_chains.json"
  if [ ! -f "${failback_src}" ] && [ -f ".codex-workflow-src-main/scripts/reviewer_failback_chains.json" ]; then
    failback_src=".codex-workflow-src-main/scripts/reviewer_failback_chains.json"
  fi
  if [ -f "${failback_src}" ]; then
    install -m 0644 "${failback_src}" "${SUPPORT_SCRIPTS_DIR}/reviewer_failback_chains.json"
  else
    echo "::warning::reviewer_failback_chains.json not found in checked-out support sources for ${SCRIPT_REF}; reviewer failback will fail open even when REVIEWER_CIRCUIT_BREAKER_ENABLED=true."
    rm -f "${SUPPORT_SCRIPTS_DIR}/reviewer_failback_chains.json"
  fi
fi

for f in unattended_system_instructions.md; do
  target_path="${SUPPORT_ROOT_DIR}/${f}"
  if [ ! -f "${target_path}" ]; then
    src=".codex-workflow-src/${f}"
    if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/${f}" ]; then
      src=".codex-workflow-src-main/${f}"
    fi
    if [ ! -f "${src}" ]; then
      echo "::error::Missing required support file ${f}"
      exit 1
    fi
    install -m 0644 "${src}" "${target_path}"
  fi
done

# Soft install: agents.md is checked by preflight via check_soft_file
# (review_autofix.yml's preflight step), so absence warns but does
# not fail.  Install when available so downstream tooling that reads
# SUPPORT_AGENTS_FILE has the canonical workflow-source copy.
# Note: SUPPORT_AGENTS_FILE is only exported via $GITHUB_ENV, so it
# is not set as a shell variable in this step.  Reference
# ${SUPPORT_ROOT_DIR}/agents.md directly to avoid an unbound
# variable error under set -u.
if [ ! -f "${SUPPORT_ROOT_DIR}/agents.md" ]; then
  src=".codex-workflow-src/agents.md"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/agents.md" ]; then
    src=".codex-workflow-src-main/agents.md"
  fi
  if [ -f "${src}" ]; then
    install -m 0644 "${src}" "${SUPPORT_ROOT_DIR}/agents.md"
  else
    echo "::warning::agents.md not available in checked-out support sources for ${SCRIPT_REF}; downstream features that reference SUPPORT_AGENTS_FILE will see SOFT_MISSING."
  fi
fi

# Soft install: probably_unnecessary_but_read_if_stuck.md holds the
# operator-runbook overflow split out of agents.md (env var ref,
# autofix internals, orchestrator auto-heal, validation self-heal,
# workflow-log-analysis runbook, etc.). Editor inliners point at it
# when included; if absent the pointer line is suppressed.
if [ ! -f probably_unnecessary_but_read_if_stuck.md ]; then
  src=".codex-workflow-src/probably_unnecessary_but_read_if_stuck.md"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/probably_unnecessary_but_read_if_stuck.md" ]; then
    src=".codex-workflow-src-main/probably_unnecessary_but_read_if_stuck.md"
  fi
  if [ -f "${src}" ]; then
    install -m 0644 "${src}" probably_unnecessary_but_read_if_stuck.md
  fi
fi

if [ ! -f "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt" ]; then
  src=".codex-workflow-src/prompts/mode-judge-review-blocked.txt"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/mode-judge-review-blocked.txt" ]; then
    src=".codex-workflow-src-main/prompts/mode-judge-review-blocked.txt"
  fi
  if [ ! -f "${src}" ]; then
    echo "::error::Missing required support file prompts/mode-judge-review-blocked.txt"
    exit 1
  fi
  install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"
fi
if [ ! -f "${SUPPORT_PROMPTS_DIR}/mode-judge-interim.txt" ]; then
  src=".codex-workflow-src/prompts/mode-judge-interim.txt"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/mode-judge-interim.txt" ]; then
    src=".codex-workflow-src-main/prompts/mode-judge-interim.txt"
  fi
  if [ ! -f "${src}" ]; then
    echo "::error::Missing required support file prompts/mode-judge-interim.txt"
    exit 1
  fi
  install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/mode-judge-interim.txt"
fi
if [ ! -f "${SUPPORT_PROMPTS_DIR}/behavioural-smoke-synthesise.txt" ]; then
  src=".codex-workflow-src/prompts/behavioural-smoke-synthesise.txt"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/behavioural-smoke-synthesise.txt" ]; then
    src=".codex-workflow-src-main/prompts/behavioural-smoke-synthesise.txt"
  fi
  if [ ! -f "${src}" ]; then
    echo "::error::Missing required support file prompts/behavioural-smoke-synthesise.txt"
    exit 1
  fi
  install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/behavioural-smoke-synthesise.txt"
fi
if [ ! -f "${SUPPORT_PROMPTS_DIR}/review-consolidator.txt" ]; then
  src=".codex-workflow-src/prompts/review-consolidator.txt"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/review-consolidator.txt" ]; then
    src=".codex-workflow-src-main/prompts/review-consolidator.txt"
  fi
  if [ -f "${src}" ]; then
    install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/review-consolidator.txt"
  else
    echo "::warning::review-consolidator.txt not found in checked-out support sources for ${SCRIPT_REF}; review consolidation will fail open even when REVIEW_CONSOLIDATOR_ENABLED=true."
    rm -f "${SUPPORT_PROMPTS_DIR}/review-consolidator.txt"
  fi
fi
if [ ! -f "${SUPPORT_PROMPTS_DIR}/review-reviewer-checklist.txt" ]; then
  src=".codex-workflow-src/prompts/review-reviewer-checklist.txt"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/review-reviewer-checklist.txt" ]; then
    src=".codex-workflow-src-main/prompts/review-reviewer-checklist.txt"
  fi
  if [ -f "${src}" ]; then
    install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/review-reviewer-checklist.txt"
  else
    echo "::warning::review-reviewer-checklist.txt not found in checked-out support sources for ${SCRIPT_REF}; reviewer prompts will keep the checklist disabled even when REVIEW_REVIEWER_CHECKLIST_ENABLED=true."
    rm -f "${SUPPORT_PROMPTS_DIR}/review-reviewer-checklist.txt"
  fi
fi
if [ ! -f "${SUPPORT_SCRIPTS_DIR}/review_filter_uninteresting_files.sh" ]; then
  src=".codex-workflow-src/scripts/review_filter_uninteresting_files.sh"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/scripts/review_filter_uninteresting_files.sh" ]; then
    src=".codex-workflow-src-main/scripts/review_filter_uninteresting_files.sh"
  fi
  if [ -f "${src}" ]; then
    install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/review_filter_uninteresting_files.sh"
  else
    echo "::warning::review_filter_uninteresting_files.sh not found in checked-out support sources for ${SCRIPT_REF}; reviewer diff filtering will fail open even when REVIEWER_FILTER_UNINTERESTING_ENABLED=true."
    rm -f "${SUPPORT_SCRIPTS_DIR}/review_filter_uninteresting_files.sh"
  fi
fi
if [ ! -f "${SUPPORT_SCRIPTS_DIR}/review_agents_md_materiality.sh" ]; then
  src=".codex-workflow-src/scripts/review_agents_md_materiality.sh"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/scripts/review_agents_md_materiality.sh" ]; then
    src=".codex-workflow-src-main/scripts/review_agents_md_materiality.sh"
  fi
  if [ -f "${src}" ]; then
    install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/review_agents_md_materiality.sh"
  else
    echo "::warning::review_agents_md_materiality.sh not found in checked-out support sources for ${SCRIPT_REF}; AGENTS.md materiality advisories will fail open even when AGENTS_MD_MATERIALITY_ENABLED=true."
    rm -f "${SUPPORT_SCRIPTS_DIR}/review_agents_md_materiality.sh"
  fi
fi
if [ ! -f "${SUPPORT_PROMPTS_DIR}/conflict-resolver.txt" ]; then
  src=".codex-workflow-src/prompts/conflict-resolver.txt"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/conflict-resolver.txt" ]; then
    src=".codex-workflow-src-main/prompts/conflict-resolver.txt"
  fi
  if [ -f "${src}" ]; then
    install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/conflict-resolver.txt"
  else
    echo "::warning::conflict-resolver.txt not found in checked-out support sources for ${SCRIPT_REF}; downstream conflict resolution will fail with a specific missing-template error."
    rm -f "${SUPPORT_PROMPTS_DIR}/conflict-resolver.txt"
  fi
fi
if [ ! -f "${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver.txt" ]; then
  src=".codex-workflow-src/prompts/integration-sync-conflict-resolver.txt"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/integration-sync-conflict-resolver.txt" ]; then
    src=".codex-workflow-src-main/prompts/integration-sync-conflict-resolver.txt"
  fi
  if [ -f "${src}" ]; then
    install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver.txt"
  else
    echo "::warning::integration-sync-conflict-resolver.txt not found in checked-out support sources for ${SCRIPT_REF}; orchestrator/project-* runs will fall back to the generic conflict-resolver template (intent-injection disabled)."
    rm -f "${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver.txt"
  fi
fi
if [ ! -f "${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver-retry-prelude.txt" ]; then
  src=".codex-workflow-src/prompts/integration-sync-conflict-resolver-retry-prelude.txt"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/integration-sync-conflict-resolver-retry-prelude.txt" ]; then
    src=".codex-workflow-src-main/prompts/integration-sync-conflict-resolver-retry-prelude.txt"
  fi
  if [ -f "${src}" ]; then
    install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver-retry-prelude.txt"
  else
    echo "::warning::integration-sync-conflict-resolver-retry-prelude.txt not found in checked-out support sources for ${SCRIPT_REF}; resolver retries on integration-sync runs will use the original prompt verbatim (no reflexion guidance)."
    rm -f "${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver-retry-prelude.txt"
  fi
fi
if [ ! -f "${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver-retry-timeout-prelude.txt" ]; then
  src=".codex-workflow-src/prompts/integration-sync-conflict-resolver-retry-timeout-prelude.txt"
  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/integration-sync-conflict-resolver-retry-timeout-prelude.txt" ]; then
    src=".codex-workflow-src-main/prompts/integration-sync-conflict-resolver-retry-timeout-prelude.txt"
  fi
  if [ -f "${src}" ]; then
    install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver-retry-timeout-prelude.txt"
  else
    echo "::warning::integration-sync-conflict-resolver-retry-timeout-prelude.txt not found in checked-out support sources for ${SCRIPT_REF}; resolver timeout-induced retries on integration-sync runs will fall back to the original prompt verbatim (no timeout-aware reflexion guidance)."
    rm -f "${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver-retry-timeout-prelude.txt"
  fi
fi
}

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

main_validate()
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

if [ "${1:-}" = "validate" ]; then
	main_validate "$@"
else
	stage_review_runtime_support
fi
