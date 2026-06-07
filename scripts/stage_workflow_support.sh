#!/usr/bin/env bash
set -euo pipefail

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

REQUIRED_BOOTSTRAP_SCRIPTS="gh_helpers.sh git_ref_health_check.sh generate_symbol_diff_summary.py render_prompt.sh load_workflow_overlay.py tg_helpers.sh label_helpers.sh memory_helpers.sh ai_memory.py ai_memory_lib.py openrouter_prompt_cache.py cost_audit.py codex_heartbeat.sh codex_stall_guard.sh review_run_reviewers.sh review_apply_fixes.sh review_reject_verify.sh review_rb_judge.sh review_run_judge_interim.sh review_synthesise_smoke.sh review_commit_changes.sh review_collect_pr_metadata.sh review_conflict_prepare.sh review_conflict_resolve.sh orchestrate_force_tick.sh check_workflow_script_refs.py check_resolver_diff.sh summarize_reviewer_consensus.sh check_external_branch_advance.sh post_review_comment.sh targeted_file_context.py write_codex_config.sh detect_editor_changes_lost.sh validate_editor_audit.sh workspace_init.sh workspace_safety_check.sh"
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
