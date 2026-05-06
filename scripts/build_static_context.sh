#!/usr/bin/env bash
# Build the cacheable "static prefix" portion of the AI prompt for unattended
# codex-cli phases.
#
# Splits prompt context into static (constant across runs) and dynamic
# (per-issue) files. The static prefix is identical across all runs of a
# given phase, so the LLM provider can cache it and charge reduced rates
# for subsequent requests. Bundling everything into one file defeats prefix
# caching because the dynamic issue context changes every run.
#
# This file is meant to be inlined directly into the Codex prompt (not read
# via a tool call) to avoid wasting a tool call round-trip and the token
# overhead of a tool response envelope.
#
# All three phases share the same shape: the full
# unattended_system_instructions.md (already trimmed for unattended codex
# use — no STOP-and-ASK rules, no FINAL REMINDER), plus a phase-trimmed
# slice of ai_pipeline.md, plus agents.md, plus a README.md trim for
# non-implement phases.
#
# agents.md handling differs by phase:
#   - clarify, plan: emit only the consumer repo's local agents.md
#     (when present).
#   - implement: emit the canonical agents_canonical.md staged from
#     .codex-workflow-src (when "${RUNTIME_DIR}/agents_canonical.md" is
#     present, i.e. the consumer-repo flow), followed by the consumer's
#     local agents.md (when present). Both are emitted because the
#     implement phase is the one that actually edits files and benefits
#     from the canonical repo-architecture facts on top of the consumer's
#     own.
#
# Phase-specific ai_pipeline.md trims:
#   clarify   — Phase 1 only (stop at "## Phase 2").
#   plan      — Phases 1+2 (stop at "## Phase 3").
#   implement — Shared Runtime + Phase 3 (skip Failure Handling).
#
# Usage:
#   build_static_context.sh <phase> <output-file>
#
# Inputs (read from CWD): unattended_system_instructions.md, ai_pipeline.md,
# agents.md, README.md. For the implement phase, also reads
# "${RUNTIME_DIR}/agents_canonical.md" if present (consumer-repo flow).

set -euo pipefail

if [ "$#" -ne 2 ]; then
	echo "Usage: $0 <phase> <output-file>" >&2
	echo "  phase: clarify | plan | implement" >&2
	exit 2
fi

phase="$1"
output="$2"

if [ ! -f unattended_system_instructions.md ] || [ ! -f ai_pipeline.md ]; then
	echo "Missing required input file(s): unattended_system_instructions.md and/or ai_pipeline.md" >&2
	exit 1
fi

emit_system_instructions() {
	echo "=== SYSTEM INSTRUCTIONS (unattended) ==="
	cat unattended_system_instructions.md
	echo
}

emit_agents_md() {
	# Implement phase: include canonical-from-coding-workflows agents_canonical.md
	# (staged from .codex-workflow-src) followed by the consumer repo's own
	# agents.md if present. Other phases just emit the local agents.md.
	# Guard RUNTIME_DIR explicitly under set -u.
	case "${phase}" in
		implement)
			if { [ -n "${RUNTIME_DIR:-}" ] && [ -f "${RUNTIME_DIR}/agents_canonical.md" ]; } || [ -f agents.md ]; then
				echo "=== AGENTS.MD ==="
				if [ -n "${RUNTIME_DIR:-}" ] && [ -f "${RUNTIME_DIR}/agents_canonical.md" ]; then
					cat "${RUNTIME_DIR}/agents_canonical.md"
					echo
				fi
				if [ -f agents.md ]; then
					echo "=== REPO-SPECIFIC AGENTS.MD ==="
					cat agents.md
					echo
				fi
			fi
			;;
		*)
			if [ -f agents.md ]; then
				echo "=== AGENTS.MD ==="
				cat agents.md
				echo
			fi
			;;
	esac
}

emit_readme_trimmed() {
	if [ -f README.md ]; then
		echo "=== README.MD (trimmed) ==="
		# Keep overview/setup/conventions; exclude wrapper-workflow + automation runbooks.
		awk '/^### 2\. Create wrapper workflows/{exit} {print}' README.md
		echo
	fi
}

emit_overflow_pointer() {
	if [ -f probably_unnecessary_but_read_if_stuck.md ]; then
		echo "=== OVERFLOW REFERENCE ==="
		echo "If you cannot make progress without operator-runbook details (env var reference, autofix retrigger/dedup internals, orchestrator integration-sync auto-heal, validation self-healing, workflow log analysis pipeline, semantic cache scope, wrapper pin policy), read ./probably_unnecessary_but_read_if_stuck.md from the working tree before bailing."
		echo
	fi
}

case "${phase}" in
	clarify)
		{
			emit_system_instructions
			echo "=== AI PIPELINE (Phase 1 — Clarification) ==="
			awk '/^## Phase 2/{exit} {print}' ai_pipeline.md
			echo
			emit_agents_md
			emit_readme_trimmed
			emit_overflow_pointer
		} > "${output}"
		;;
	plan)
		{
			emit_system_instructions
			echo "=== AI PIPELINE (planning-trimmed) ==="
			awk '/^## Phase 3/{exit} {print}' ai_pipeline.md
			echo
			emit_agents_md
			emit_readme_trimmed
			emit_overflow_pointer
		} > "${output}"
		;;
	implement)
		{
			emit_system_instructions
			echo "=== AI PIPELINE (implementation-trimmed) ==="
			awk '
				/^## Shared Runtime Behavior/{in_shared=1}
				/^## Phase 1/{in_shared=0}
				/^## Phase 3/{in_impl=1}
				/^## Failure Handling/{in_impl=0}
				{ if (in_shared || in_impl) print }
			' ai_pipeline.md
			echo
			emit_agents_md
			emit_overflow_pointer
		} > "${output}"
		;;
	*)
		echo "Unknown phase: ${phase} (expected clarify | plan | implement)" >&2
		exit 2
		;;
esac
