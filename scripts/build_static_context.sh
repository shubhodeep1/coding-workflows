#!/usr/bin/env bash
# Build the cacheable "static prefix" portion of the AI prompt.
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
# Phase-specific trimming (extracted from the previous inline implementations
# in clarify.yml / plan.yml / implement.yml). plan and implement outputs are
# byte-identical to their inline predecessors. clarify trims one fewer
# section after a pre-existing inline-awk bug was fixed in this extraction
# (the `^# Phase 2` anchor never matched the level-2 `## Phase 2` heading);
# the rest of the clarify pipeline is unchanged from the inline form.
#
#   clarify   — codex sections 0-2 (through Ask-First) + FINAL REMINDER;
#               ai_pipeline.md Phase 1 only;
#               README.md trimmed before "### 2. Create wrapper workflows".
#   plan      — codex sections 0-6 + FINAL REMINDER;
#               ai_pipeline.md trimmed before Phase 3;
#               README.md trimmed before "### 2. Create wrapper workflows".
#   implement — codex sections 0-6 + section 9 (Code Style) + FINAL REMINDER;
#               ai_pipeline.md Shared Runtime + Phase 3 only;
#               agents.md canonical + repo-local; README.md NOT included.
#
# Usage:
#   build_static_context.sh <phase> <output-file>
#
# Inputs (read from CWD): codex_system_instructions.md, ai_pipeline.md,
# agents.md, README.md. For the implement phase, also reads
# "${RUNTIME_DIR}/agents_canonical.md" if present.

set -euo pipefail

if [ "$#" -ne 2 ]; then
	echo "Usage: $0 <phase> <output-file>" >&2
	echo "  phase: clarify | plan | implement" >&2
	exit 2
fi

phase="$1"
output="$2"

if [ ! -f codex_system_instructions.md ] || [ ! -f ai_pipeline.md ]; then
	echo "Missing required codex input file(s): codex_system_instructions.md and/or ai_pipeline.md" >&2
	exit 1
fi

case "${phase}" in
	clarify)
		{
			echo "=== SYSTEM INSTRUCTIONS (clarification-trimmed) ==="
			# Extract: header through end of section 2 (Ask-First Mode / question
			# format). This includes the Prime Directive, Core Priorities, and
			# the mandatory Q/A format — everything needed for clarification.
			# Stop at section 3 ("## 3.") which begins execution-phase rules.
			awk '/^## 3\. /{exit} {print}' codex_system_instructions.md
			# Append the FINAL REMINDER since it reinforces ask-first behavior
			echo
			awk '/^## FINAL REMINDER$/,0{print}' codex_system_instructions.md
			echo
			echo "=== AI PIPELINE (Phase 1 — Clarification) ==="
			# Extract only the relevant sections: Overview, Commands, Phase 1,
			# and Shared Runtime Behavior. Stop before the level-2 Phase 2
			# heading. ai_pipeline.md uses "## Phase 2 — Planning"; the
			# previous pattern `^# Phase 2` never matched the level-2
			# heading, so this trim was a no-op and the clarify static
			# context silently included Phase 2/3 content.
			awk '/^## Phase 2/{exit} {print}' ai_pipeline.md
			echo
			if [ -f agents.md ]; then
				echo "=== AGENTS.MD ==="
				cat agents.md
				echo
			fi
			if [ -f README.md ]; then
				echo "=== README.MD (trimmed) ==="
				# Include the overview/setup/conventions section but exclude
				# the wrapper-workflow + automation runbooks that follow, which
				# are irrelevant to clarification. The previous anchors
				# (`### GitHub AI Automation` / `### Decimal128 switchover`) do
				# not exist in the current README, so the awk ran to EOF and
				# the trim was a no-op — the entire 1748-line README was
				# bundled into the clarify static context. Switch to the same
				# anchor the plan phase already uses.
				awk '/^### 2\. Create wrapper workflows/{exit} {print}' README.md
				echo
			fi
			if [ -f probably_unnecessary_but_read_if_stuck.md ]; then
				echo "=== OVERFLOW REFERENCE ==="
				echo "If you cannot make progress without operator-runbook details (env var reference, autofix retrigger/dedup internals, orchestrator integration-sync auto-heal, validation self-healing, workflow log analysis pipeline, semantic cache scope, wrapper pin policy), read ./probably_unnecessary_but_read_if_stuck.md from the working tree before bailing."
				echo
			fi
		} > "${output}"
		;;
	plan)
		{
			echo "=== SYSTEM INSTRUCTIONS (planning-trimmed) ==="
			# Include sections 0-6 + FINAL REMINDER; skip section 7+.
			awk '/^## 7\./{exit} {print}' codex_system_instructions.md
			echo
			awk '/^## FINAL REMINDER$/,0{print}' codex_system_instructions.md
			echo

			echo "=== AI PIPELINE (planning-trimmed) ==="
			# Keep shared runtime + clarify/planning context; stop before phase 3.
			awk '/^## Phase 3/{exit} {print}' ai_pipeline.md
			echo

			if [ -f agents.md ]; then
				echo "=== AGENTS.MD ==="
				cat agents.md
				echo
			fi
			if [ -f README.md ]; then
				echo "=== README.MD (trimmed) ==="
				# Keep overview/variables; exclude wrapper/automation docs.
				awk '/^### 2\. Create wrapper workflows/{exit} {print}' README.md
				echo
			fi
			if [ -f probably_unnecessary_but_read_if_stuck.md ]; then
				echo "=== OVERFLOW REFERENCE ==="
				echo "If you cannot make progress without operator-runbook details (env var reference, autofix retrigger/dedup internals, orchestrator integration-sync auto-heal, validation self-healing, workflow log analysis pipeline, semantic cache scope, wrapper pin policy), read ./probably_unnecessary_but_read_if_stuck.md from the working tree before bailing."
				echo
			fi
		} > "${output}"
		;;
	implement)
		{
			echo "=== SYSTEM INSTRUCTIONS (implementation-trimmed) ==="
			# Include sections 0-6 + code style + FINAL REMINDER.
			awk '/^## 7\./{exit} {print}' codex_system_instructions.md
			echo
			awk '/^## 9\. Code Style/,/^## 10\./{if ($0 ~ /^## 10\./) exit; print}' codex_system_instructions.md
			echo
			awk '/^## FINAL REMINDER$/,0{print}' codex_system_instructions.md
			echo

			echo "=== AI PIPELINE (implementation-trimmed) ==="
			# Keep shared runtime behavior and implementation phase details only.
			awk '
				/^## Shared Runtime Behavior/{in_shared=1}
				/^## Phase 1/{in_shared=0}
				/^## Phase 3/{in_impl=1}
				/^## Failure Handling/{in_impl=0}
				{ if (in_shared || in_impl) print }
			' ai_pipeline.md
			echo

			# Include agents.md context: canonical from coding-workflows plus
			# the caller repo's own agents.md (if present). Both are included
			# so the model has the full picture. Guard RUNTIME_DIR explicitly:
			# a bare "${RUNTIME_DIR:-}/agents_canonical.md" would test
			# "/agents_canonical.md" at filesystem root when unset, and the
			# subsequent cat would trip set -u without a default.
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
			if [ -f probably_unnecessary_but_read_if_stuck.md ]; then
				echo "=== OVERFLOW REFERENCE ==="
				echo "If you cannot make progress without operator-runbook details (env var reference, autofix retrigger/dedup internals, orchestrator integration-sync auto-heal, validation self-healing, workflow log analysis pipeline, semantic cache scope, wrapper pin policy), read ./probably_unnecessary_but_read_if_stuck.md from the working tree before bailing."
				echo
			fi
		} > "${output}"
		;;
	*)
		echo "Unknown phase: ${phase} (expected clarify | plan | implement)" >&2
		exit 2
		;;
esac
