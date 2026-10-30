#!/usr/bin/env bash
# shellcheck shell=bash
# review_autofix_step_prepush_test_gate.sh - body of review_autofix.yml's
# "Pre-push affected-test gate" step, and the shared gate entry point of the
# conflict resolver and the workflow-heal editor (issue P4 of
# docs/plans/prepush-affected-test-gate-plan.md; mirrors the implement step
# scripts/implement_step_prepush_test_gate.sh, plan phase P3).
#
# Sourced by the step (no arguments): runs the pre-push affected-test gate on
# the review editor's uncommitted change, in the credential-free container of
# scripts/prepush_test_gate_run.sh, with the review sandbox image and its
# dependency venv. In `repair` mode, failures this change caused (not
# `inherited`) get exactly one editor turn through
# `review_apply_fixes.sh --repair-turn` (the editor's own sandbox launch and
# validated transfer). Edits outside the repair allow-list are rolled back,
# syntax is re-validated (a syntax failure reverts the whole repair), and the
# gate runs once more; there is never a second repair. The host writes the
# summary from the final result.json (never from the container's summary.md)
# and appends it to EDITOR_SUMMARY_FILE as "## Pre-push test gate" when that
# file is already non-empty, so "Post editor summary comment" posts it.
#
# Executed with a subcommand (resolver and heal):
#   gate --worktree DIR --base SHA --image IMG --out DIR [--venv DIR] [--phase P]
#        [--image-reason R] [--node-modules DIR] runs the wrapper once; writes DIR/result.json (synthetic when the run
#        wrote none) and prints "<status> <pr_caused> <inherited>".
#   repair-prompt RESULT OUT [ALLOW_FILE]
#        trusted template + "=== CAPTURED TEST FAILURES (UNTRUSTED) ===" block.
#   allow-list WORKTREE CHANGED_FILE RESULT OUT
#        changed files + affected tests + companions + failing files.
#   summary RESULT OUT MODE REPAIR INITIAL
#   mode  prints the normalised mode.
# Subcommands return non-zero only for missing inputs; callers ignore it.
#
# Trust: the gate module, the wrapper and the repair prompt come only from the
# staged support bundle, this script's own (trusted) directory or the
# verified support checkouts (.codex-workflow-src, .codex-workflow-src-main),
# copied outside every tested tree; never from the PR checkout. Tests run
# only in the container. No GitHub API call.
#
# Environment (each defaulted here, unattended section 8):
#   PREPUSH_TEST_GATE_MODE              off | report | repair (default repair;
#                                       unknown values act as repair)
#   PREPUSH_TEST_GATE_TIMEOUT_MINUTES   15 (clamped to 53)
#   PREPUSH_TEST_GATE_LARGE_FILE_TESTS  50
#   PREPUSH_TEST_GATE_SHARDS            4
#
# Never blocks the push: the sourced step ends with status 0 on every path.
# Log prefix: PREPUSH_TEST_GATE phase=review|resolver|heal|integration.
#
# Integration mode (#8210, plan phase P5): sourced with
# PREPUSH_TEST_GATE_STEP_MODE=integration by review_autofix.yml's
# "Integration post-sync test repair" step. Only with POST_SYNC_TEST_REPAIR=true
# on an orchestrator/project-<N> head it writes INTEGRATION_TEST_REPAIR_ACTIVE=true
# to GITHUB_ENV (the reviewer/editor steps skip), gates the PR head against its
# merge-base with BASE_BRANCH and, on failures that head caused, runs
# `review_conflict_resolve.sh --test-repair <result.json>` once.
set -euo pipefail

review_prepush_test_gate_log()
{
	echo "PREPUSH_TEST_GATE phase=${rpg_phase:-review} $*" >&2
}

# Embedded host-side helper (trusted, stdlib only; same contract as P3's).
# Modes: status RESULT | synthetic RESULT REASON | failures RESULT |
#   allow SELECTION RESULT | summary RESULT MODULE OUT MODE REPAIR INITIAL
REVIEW_PREPUSH_TEST_GATE_PY='
import json
import re
import sys
from pathlib import Path

STATUSES = ("pass", "fail", "unverified", "skipped")
WORD_JOINER = "\u2060"
FAILURE_CAP = 16 * 1024
SUMMARY_CAP = 8 * 1024


def load(path):
	try:
		data = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


def entries(result, key):
	value = result.get(key)
	return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def status_of(result):
	value = result.get("status")
	return value if value in STATUSES else "unverified"


def neutralise(text):
	return "\n".join((" " + line) if line.lstrip().startswith("::") else line for line in text.splitlines())


def scrub(text):
	text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
	text = re.sub(r"#(\d)", "#" + WORD_JOINER + r"\1", text)
	text = re.sub(r"@(?=[A-Za-z0-9_-])", "@" + WORD_JOINER, text)
	return text


def main(argv):
	mode = argv[1]
	if mode == "status":
		result = load(argv[2])
		print(status_of(result), len(entries(result, "failures")), len(entries(result, "inherited")))
	elif mode == "synthetic":
		reason = re.sub(r"[^A-Za-z0-9_.-]", "_", argv[3])[:80]
		Path(argv[2]).write_text(json.dumps({
			"schema": "prepush_test_gate.v1", "status": "unverified", "reason": reason,
			"ran": [], "failures": [], "inherited": [],
			"unverified": [{"item": "gate", "reason": reason}],
			"changed_assertions": [], "selected": 0, "elapsed_secs": 0,
		}) + "\n", encoding="utf-8")
	elif mode == "failures":
		parts = []
		for entry in entries(load(argv[2]), "failures"):
			names = ", ".join(str(n) for n in (entry.get("names") or [])[:20] if isinstance(n, str))
			parts.append("----- " + str(entry.get("test")) + " (file: " + str(entry.get("file")) + ") -----")
			if names:
				parts.append("Failing tests: " + names)
			parts.append(str(entry.get("tail") or "")[-6000:])
		text = neutralise("\n".join(parts))
		data = text.encode("utf-8")
		if len(data) > FAILURE_CAP:
			text = data[:FAILURE_CAP].decode("utf-8", "ignore") + "\n(truncated at 16 KiB)"
		print(text or "(no failure output captured)")
	elif mode == "allow":
		selection, result = load(argv[2]), load(argv[3])
		paths = [e.get("path") for e in entries(selection, "affected_tests")]
		paths += [p for p in (selection.get("companion_files") or []) if isinstance(p, str)]
		# failures[] comes from the container (tests can rewrite result.json):
		# a failing file counts only when the host selection already has it.
		selected = {p for p in paths if isinstance(p, str)}
		paths += [e.get("file") for e in entries(result, "failures") if e.get("file") in selected]
		for path in paths:
			if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
				continue
			if any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or path.lower().startswith((".claude/", ".github/ai/", ".git/")):
				continue
			print(path)
	elif mode == "summary":
		result_path, module_path, out_path, gate_mode, repair, initial = argv[2:8]
		result = load(result_path)
		clean = {
			"status": status_of(result),
			"ran": [str(r) for r in (result.get("ran") or []) if isinstance(r, (str, int))] if isinstance(result.get("ran"), list) else [],
			"selected": result.get("selected") if isinstance(result.get("selected"), int) else 0,
			"failures": [{"test": str(e.get("test")), "names": [n for n in (e.get("names") or []) if isinstance(n, str)][:10]} for e in entries(result, "failures")],
			"inherited": [{"test": str(e.get("test")), "names": [n for n in (e.get("names") or []) if isinstance(n, str)][:10]} for e in entries(result, "inherited")],
			"unverified": [{"item": str(e.get("item")), "reason": str(e.get("reason")), "names": [n for n in (e.get("names") or []) if isinstance(n, str)][:10]} for e in entries(result, "unverified")],
			"changed_assertions": [
				{"file": str(e.get("file")), "kind": e.get("kind") if e.get("kind") in ("removed_or_modified", "skip_added") else "changed"}
				for e in entries(result, "changed_assertions")
			],
		}
		sys.path.insert(0, str(Path(module_path).resolve().parent))
		import prepush_affected_tests as gate
		# The summary holds test names and paths only: changed assertions are
		# listed by file and kind, never with the assertion source text.
		assertions = clean.pop("changed_assertions")
		lines = gate.write_summary(clean).splitlines()
		if clean["ran"]:
			lines += ["", "### Tests and checks run", ""]
			lines += ["- `" + gate._sanitize_name(r) + "`" for r in clean["ran"][:50]]
			if len(clean["ran"]) > 50:
				lines.append("- (" + str(len(clean["ran"]) - 50) + " more)")
		if assertions:
			lines += ["", "### Changed pre-existing assertions", ""]
			lines += ["- `" + gate._sanitize_name(a["file"]) + "` (" + a["kind"] + ")" for a in assertions]
		extra = ["- Mode: `" + gate_mode + "`", "- Repair turn: " + repair]
		if initial and initial != clean["status"]:
			extra.append("- Status before the repair turn: **" + initial + "**")
		if repair.startswith("ran (re-run skipped"):
			extra.append("- The result above predates the repair turn: the re-run was skipped.")
		lines = lines[:3] + extra + lines[3:]
		text = scrub("\n".join(lines))
		data = text.encode("utf-8")
		if len(data) > SUMMARY_CAP:
			text = data[:SUMMARY_CAP].decode("utf-8", "ignore") + "\n\n(summary truncated)"
		Path(out_path).write_text(text.rstrip("\n") + "\n", encoding="utf-8")
	return 0


sys.exit(main(sys.argv))
'

review_prepush_test_gate_py()
{
	PYTHONDONTWRITEBYTECODE=1 python3 -I -B -c "${REVIEW_PREPUSH_TEST_GATE_PY}" "$@"
}

review_prepush_test_gate_py_module()
{
	PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${rpg_module}" "$@" 2>/dev/null
}

review_prepush_test_gate_mode()
{
	local raw
	raw="$(printf '%s' "${PREPUSH_TEST_GATE_MODE:-repair}" | tr '[:upper:]' '[:lower:]')"
	case "${raw}" in
		off|report|repair) rpg_mode="${raw}" ;;
		*)
			review_prepush_test_gate_log "outcome=mode_invalid fallback=repair"
			rpg_mode="repair"
			;;
	esac
	rpg_timeout="${PREPUSH_TEST_GATE_TIMEOUT_MINUTES:-15}"
	[[ "${rpg_timeout}" =~ ^[0-9]+$ ]] && [ "$((10#${rpg_timeout}))" -ge 1 ] && [ "$((10#${rpg_timeout}))" -le 360 ] || rpg_timeout=15
	# Force base 10: bash arithmetic reads a leading zero (08, 030) as octal.
	rpg_timeout=$((10#${rpg_timeout}))
	if [ "${rpg_timeout}" -gt 53 ]; then
		review_prepush_test_gate_log "outcome=config_invalid name=PREPUSH_TEST_GATE_TIMEOUT_MINUTES clamped=53"
		rpg_timeout=53
	fi
	rpg_large="${PREPUSH_TEST_GATE_LARGE_FILE_TESTS:-50}"
	[[ "${rpg_large}" =~ ^[0-9]+$ ]] && [ "$((10#${rpg_large}))" -ge 1 ] || rpg_large=50
	rpg_shards="${PREPUSH_TEST_GATE_SHARDS:-4}"
	[[ "${rpg_shards}" =~ ^[0-9]+$ ]] && [ "$((10#${rpg_shards}))" -ge 1 ] && [ "$((10#${rpg_shards}))" -le 64 ] || rpg_shards=4
	rpg_large=$((10#${rpg_large})) rpg_shards=$((10#${rpg_shards}))
}

# Absolute support directories only: a relative value (the `scripts`
# default) would resolve inside the tested checkout.
review_prepush_test_gate_abs()
{
	case "${1:-}" in /*) printf '%s\n' "$1" ;; esac
}

# First regular, non-symlink file among the arguments.
review_prepush_test_gate_pick()
{
	local candidate
	for candidate in "$@"; do
		if [ -n "${candidate}" ] && [ -f "${candidate}" ] && [ ! -L "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done
	return 1
}

# Sets rpg_module, rpg_wrapper, rpg_template from trusted copies only, each
# copied into a private directory outside every tested tree.
review_prepush_test_gate_locate()
{
	local ws="${GITHUB_WORKSPACE:-$PWD}" dir="${rpg_script_dir:-}" found helper
	local copy_dir="${RUNTIME_DIR:-}" sd pd
	sd="$(review_prepush_test_gate_abs "${SUPPORT_SCRIPTS_DIR:-}")"
	pd="$(review_prepush_test_gate_abs "${SUPPORT_PROMPTS_DIR:-}")"
	rpg_module="" rpg_wrapper="" rpg_template=""
	if [ -n "${copy_dir}" ] && [ -d "${copy_dir}" ]; then
		copy_dir="${copy_dir}/prepush_gate_support"
		mkdir -p "${copy_dir}" 2>/dev/null || return 1
	else
		copy_dir="$(mktemp -d "${RUNNER_TEMP:-/tmp}/prepush-gate-support.XXXXXX")" || return 1
	fi
	if found="$(review_prepush_test_gate_pick \
		"${dir:+${dir}/prepush_affected_tests.py}" \
		"${dir:+${dir}/../workflow-templates/.claude/hooks/prepush_affected_tests.py}" \
		"${ws}/.codex-workflow-src/workflow-templates/.claude/hooks/prepush_affected_tests.py" \
		"${ws}/.codex-workflow-src-main/workflow-templates/.claude/hooks/prepush_affected_tests.py")" \
		&& install -m 0644 "${found}" "${copy_dir}/prepush_affected_tests.py"; then
		rpg_module="${copy_dir}/prepush_affected_tests.py"
	fi
	if found="$(review_prepush_test_gate_pick \
		"${sd:+${sd}/prepush_test_gate_run.sh}" \
		"${dir:+${dir}/prepush_test_gate_run.sh}" \
		"${ws}/.codex-workflow-src/scripts/prepush_test_gate_run.sh" \
		"${ws}/.codex-workflow-src-main/scripts/prepush_test_gate_run.sh")" \
		&& install -m 0755 "${found}" "${copy_dir}/prepush_test_gate_run.sh"; then
		rpg_wrapper="${copy_dir}/prepush_test_gate_run.sh"
		# The wrapper snapshots through its sibling review_untrusted_workspace.py.
		if helper="$(review_prepush_test_gate_pick "$(dirname -- "${found}")/review_untrusted_workspace.py" \
			"${sd:+${sd}/review_untrusted_workspace.py}")"; then
			install -m 0644 "${helper}" "${copy_dir}/review_untrusted_workspace.py" || true
		fi
	fi
	if found="$(review_prepush_test_gate_pick \
		"${pd:+${pd}/mode-implement-repair-tests.txt}" \
		"${dir:+${dir}/../prompts/mode-implement-repair-tests.txt}" \
		"${ws}/.codex-workflow-src/prompts/mode-implement-repair-tests.txt" \
		"${ws}/.codex-workflow-src-main/prompts/mode-implement-repair-tests.txt")" \
		&& install -m 0644 "${found}" "${copy_dir}/mode-implement-repair-tests.txt"; then
		rpg_template="${copy_dir}/mode-implement-repair-tests.txt"
	fi
	# Attempt journal rules fragment (issue #8428), located like rpg_template.
	rpg_journal_rules=""
	if [ "${PERSIST_JOURNAL_ENABLED:-false}" = "true" ] && found="$(review_prepush_test_gate_pick \
		"${pd:+${pd}/mode-persist-journal-rules.txt}" \
		"${dir:+${dir}/../prompts/mode-persist-journal-rules.txt}" \
		"${ws}/.codex-workflow-src/prompts/mode-persist-journal-rules.txt" \
		"${ws}/.codex-workflow-src-main/prompts/mode-persist-journal-rules.txt")" \
		&& install -m 0644 "${found}" "${copy_dir}/mode-persist-journal-rules.txt"; then
		rpg_journal_rules="${copy_dir}/mode-persist-journal-rules.txt"
	fi
	[ -n "${rpg_module}" ] && [ -n "${rpg_wrapper}" ]
}

rpg_untracked_excludes=(-- . ':!node_modules' ':!.codex-workflow-src' ':!.codex-workflow-src-main')

# Uncommitted change set (tracked edits plus untracked files), one per line.
review_prepush_test_gate_changed()
{
	{
		git diff --name-only HEAD 2>/dev/null
		git ls-files --others --exclude-standard "${rpg_untracked_excludes[@]}" 2>/dev/null
	} | sed '/^$/d' | sort -u
}

# Docker image ids and registry references only; anything else is refused.
review_prepush_test_gate_image_ok()
{
	[[ "${1:-}" =~ ^sha256:[0-9a-f]{64}$ ]] || [[ "${1:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$ ]]
}

# One wrapper run into $1 (worktree, base, image, venv from rpg_* globals).
review_prepush_test_gate_run_once()
{
	local out="$1"
	local args
	rm -rf -- "${out}"
	mkdir -p "${out}" || return 0
	if [ -z "${rpg_image}" ] || ! review_prepush_test_gate_image_ok "${rpg_image}"; then
		review_prepush_test_gate_py synthetic "${out}/result.json" "${rpg_image_reason:-image_unavailable}"
		return 0
	fi
	args=(--worktree "${rpg_worktree}" --module "${rpg_module}" --image "${rpg_image}" --base "${rpg_base}"
		--out-dir "${out}" --timeout-minutes "${rpg_timeout}" --shards "${rpg_shards}" --large-file-tests "${rpg_large}")
	if [ -n "${rpg_venv:-}" ] && [ -d "${rpg_venv}/bin" ] && [ ! -L "${rpg_venv}" ]; then
		args+=(--venv "${rpg_venv}")
	fi
	# Node dependencies the sandbox prepare installed (the snapshot leaves node_modules out).
	if [ -n "${rpg_node_modules:-}" ] && [ -d "${rpg_node_modules}" ] && [ ! -L "${rpg_node_modules}" ]; then
		args+=(--node-modules "${rpg_node_modules}")
	fi
	bash "${rpg_wrapper}" "${args[@]}" || true
	[ -f "${out}/result.json" ] || review_prepush_test_gate_py synthetic "${out}/result.json" wrapper_no_result
}

# Allow-list: changed files + module selection (affected tests, companions)
# + failing files; .github/workflows/ only with ALLOW_WORKFLOW_EDITS=true.
review_prepush_test_gate_allow_list()
{
	local worktree="$1" changed="$2" result="$3" out="$4" selection
	selection="$(mktemp "${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/prepush_gate_selection.XXXXXX")" || return 1
	{
		cat "${changed}"
		if review_prepush_test_gate_py_module select --repo-root "${worktree}" --paths-file "${changed}" --out "${selection}"; then
			review_prepush_test_gate_py allow "${selection}" "${result}"
		else
			# No selection: the container's failing files are not trusted.
			printf '{}' > "${selection}"
			review_prepush_test_gate_py allow "${selection}" "${result}"
		fi
	} | sed '/^$/d' | sort -u > "${out}"
	rm -f -- "${selection}"
	if [ "${ALLOW_WORKFLOW_EDITS:-false}" != "true" ]; then
		grep -v '^\.github/workflows/' "${out}" > "${out}.tmp" || true
		mv -f "${out}.tmp" "${out}"
	fi
	return 0
}

# Attempt journal blocks of the repair prompt (issue #8428, P1). Only with
# PERSIST_JOURNAL_ENABLED=true (default false); otherwise prints nothing, so
# the prompt is byte-identical with the switch off. Prints the removed lines
# of `git diff HEAD` for the allow-list files (each line UNTRUSTED_DATA-
# prefixed, at most 4 KiB), the journal rendered for this run's editor prompt
# (${RUNTIME_DIR}/attempt_journal.txt, already fenced; no API call) and the
# trusted rules fragment.
review_prepush_test_gate_journal_blocks()
{
	local allow="$1" rules="$2" path removed rules_body=""
	[ "${PERSIST_JOURNAL_ENABLED:-false}" = "true" ] || return 0
	[ -n "${allow}" ] && [ -f "${allow}" ] || return 0
	git rev-parse --git-dir >/dev/null 2>&1 || return 0
	echo
	echo "=== EDITOR DELETIONS THIS ROUND ==="
	{
		while IFS= read -r path; do
			[ -n "${path}" ] || continue
			git ls-files --error-unmatch -- "${path}" >/dev/null 2>&1 || continue
			removed="$(git diff --src-prefix=a/ --dst-prefix=b/ HEAD -- "${path}" 2>/dev/null | grep -v -e '^--- a/' -e '^--- /dev/null' | grep '^-' || true)"
			[ -n "${removed}" ] || continue
			printf 'UNTRUSTED_DATA: (file) %s\n' "${path}"
			printf '%s\n' "${removed}" | sed 's/^/UNTRUSTED_DATA: /'
		done < "${allow}"
	} | head -c 4096 || true
	echo
	echo "=== END EDITOR DELETIONS THIS ROUND ==="
	if [ -s "${RUNTIME_DIR:-}/attempt_journal.txt" ]; then
		echo
		cat "${RUNTIME_DIR}/attempt_journal.txt"
	fi
	# Body only: the fragment's tier and Role/Goal metadata lines stay out. A
	# file without the marker line counts as absent, so the drop is logged.
	if [ -n "${rules}" ] && [ -f "${rules}" ]; then
		rules_body="$(sed -n '/^=== ATTEMPT JOURNAL REPAIR RULES ===$/,$p' "${rules}" 2>/dev/null || true)"
	fi
	if [ -n "${rules_body}" ]; then
		echo
		printf '%s\n' "${rules_body}"
	else
		echo "PERSIST journal outcome=skipped reason=rules_absent" >&2
	fi
	return 0
}

review_prepush_test_gate_build_prompt()
{
	local result="$1" out="$2" allow="${3:-}" changed="${4:-}" path
	[ -n "${rpg_template}" ] || return 1
	{
		cat "${rpg_template}"
		echo
		echo "=== CAPTURED TEST FAILURES (UNTRUSTED) ==="
		# Every line is prefixed so test output cannot imitate the trusted
		# section headers below; the END marker closes the untrusted block.
		review_prepush_test_gate_py failures "${result}" | sed 's/^/UNTRUSTED_DATA: /'
		echo "=== END CAPTURED TEST FAILURES (UNTRUSTED) ==="
		if [ -n "${changed}" ] && [ -f "${changed}" ]; then
			echo
			echo "=== CHANGED FILES ==="
			cat "${changed}"
		fi
		if [ -n "${allow}" ] && [ -f "${allow}" ]; then
			echo
			echo "=== EFFECTIVE REPAIR ALLOW-LIST ==="
			cat "${allow}"
			if git rev-parse --git-dir >/dev/null 2>&1; then
				echo
				echo "=== CURRENT DIFF FOR ALLOW-LIST FILES ==="
				while IFS= read -r path; do
					[ -n "${path}" ] || continue
					echo "----- ${path} -----"
					if git ls-files --error-unmatch -- "${path}" >/dev/null 2>&1; then
						git diff HEAD -- "${path}" 2>/dev/null || true
					elif [ -f "${path}" ] && [ ! -L "${path}" ]; then
						git diff --no-index -- /dev/null "${path}" 2>/dev/null || true
					else
						echo "(file not found in working tree)"
					fi
				done < "${allow}"
			fi
		fi
		review_prepush_test_gate_journal_blocks "${allow}" "${rpg_journal_rules:-}"
	} > "${out}"
}

# Restore paths changed since the repair baseline. $1 = strays (paths outside
# the allow-list only) or all (every repair edit).
review_prepush_test_gate_revert()
{
	local which="$1" path baseline_blob current_blob restore_failed=false
	local tracked=() untracked=() restore=() remove=()
	while IFS= read -r -d '' path; do
		[ -n "${path}" ] || continue
		if [ "${which}" = strays ] && grep -Fqx -- "${path}" "${rpg_allow_file}"; then
			continue
		fi
		if git cat-file -e "${rpg_baseline}:${path}" 2>/dev/null; then
			baseline_blob="$(git rev-parse "${rpg_baseline}:${path}" 2>/dev/null)"
		else
			baseline_blob="__MISSING__"
		fi
		if [ -f "${path}" ] || [ -L "${path}" ]; then
			current_blob="$(git hash-object -- "${path}" 2>/dev/null || echo "__MISSING__")"
		else
			current_blob="__MISSING__"
		fi
		[ "${baseline_blob}" = "${current_blob}" ] || tracked+=("${path}")
	done < <({ git diff --name-only -z HEAD 2>/dev/null; git diff --name-only -z HEAD "${rpg_baseline}" 2>/dev/null; } | sort -zu)
	while IFS= read -r -d '' path; do
		[ -n "${path}" ] || continue
		if [ "${which}" = strays ] && grep -Fqx -- "${path}" "${rpg_allow_file}"; then
			continue
		fi
		grep -Fqx -- "${path}" "${rpg_pre_untracked}" || untracked+=("${path}")
	done < <(git ls-files --others --exclude-standard -z "${rpg_untracked_excludes[@]}" 2>/dev/null)
	[ "${#tracked[@]}" -gt 0 ] || [ "${#untracked[@]}" -gt 0 ] || [ "${which}" = all ] || return 1
	for path in "${tracked[@]}"; do
		if git cat-file -e "${rpg_baseline}:${path}" 2>/dev/null; then
			restore+=("${path}")
		else
			remove+=("${path}")
		fi
	done
	[ "${#restore[@]}" -eq 0 ] || git restore --source="${rpg_baseline}" --worktree -- "${restore[@]}" || restore_failed=true
	[ "${#remove[@]}" -eq 0 ] || git rm -r -f -q --ignore-unmatch -- "${remove[@]}" || restore_failed=true
	if [ "${restore_failed}" = true ]; then
		: > "${RUNTIME_DIR}/prepush_gate_restore_failed.flag"
		review_prepush_test_gate_log "outcome=warning reason=restore_failed mode=${which}"
	fi
	for path in "${untracked[@]}"; do
		case "${path}" in
			/*|../*|*/../*|*/..) review_prepush_test_gate_log "outcome=warning reason=unsafe_untracked_path" ;;
			*) rm -rf -- "${path}" ;;
		esac
	done
	if [ "${which}" = all ] && [ -s "${rpg_untracked_backup}" ]; then
		tar -xf "${rpg_untracked_backup}" 2>/dev/null || review_prepush_test_gate_log "outcome=warning reason=untracked_restore_failed"
	fi
	return 0
}

# A tracked symlink the repair editor replaced or retargeted goes back to HEAD.
review_prepush_test_gate_restore_symlinks()
{
	local entry meta path expected bak bak_n=0
	while IFS= read -r -d '' entry; do
		meta="${entry%%$'\t'*}"
		path="${entry#*$'\t'}"
		case "${meta}" in 120000\ *\ 0) ;; *) continue ;; esac
		expected="$(git cat-file -p "HEAD:${path}" 2>/dev/null)" || continue
		if [ -L "${path}" ] && [ "$(readlink -- "${path}")" = "${expected}" ]; then
			continue
		fi
		case "/${path}/" in */../*|*/./*|*/.git/*) continue ;; esac
		bak_n=$((bak_n + 1))
		bak="${RUNTIME_DIR}/prepush_gate_symlink_bak.${bak_n}"
		rm -rf -- "${bak}"
		if [ -e "./${path}" ] || [ -L "./${path}" ]; then
			mv -- "./${path}" "${bak}" 2>/dev/null || continue
		fi
		if GIT_LITERAL_PATHSPECS=1 git checkout HEAD -- "${path}" 2>/dev/null; then
			rm -rf -- "${bak}"
			review_prepush_test_gate_log "outcome=warning reason=symlink_restored"
		else
			mv -- "${bak}" "./${path}" 2>/dev/null || true
			review_prepush_test_gate_log "outcome=warning reason=symlink_restore_failed"
		fi
	done < <(git ls-files -s -z 2>/dev/null)
}

# One review editor turn. Sets rpg_repair to ran | skipped (...) | reverted (syntax).
review_prepush_test_gate_repair()
{
	local result="$1" prompt rc editor editor_timeout validator sd
	sd="$(review_prepush_test_gate_abs "${SUPPORT_SCRIPTS_DIR:-}")"
	editor="$(review_prepush_test_gate_pick "${sd:+${sd}/review_apply_fixes.sh}")" || editor=""
	if [ -z "${editor}" ] || ! grep -qxF '# supports: --repair-turn' "${editor}"; then
		review_prepush_test_gate_log "outcome=repair_skipped reason=editor_hook_missing"
		rpg_repair="skipped (editor hook missing)"
		return 0
	fi
	rpg_allow_file="${RUNTIME_DIR}/prepush_gate_allow_list.txt"
	review_prepush_test_gate_allow_list "${rpg_worktree}" "${rpg_changed_file}" "${result}" "${rpg_allow_file}"
	if [ ! -s "${rpg_allow_file}" ]; then
		review_prepush_test_gate_log "outcome=repair_skipped reason=empty_allow_list"
		rpg_repair="skipped (empty allow-list)"
		return 0
	fi
	# The baseline is the pre-repair tree; HEAD only when nothing tracked
	# differs from it, so a failed stash can never undo the editor's work.
	if ! rpg_baseline="$(git stash create "prepush-gate-repair" 2>/dev/null)"; then
		rpg_baseline=""
	fi
	if [ -z "${rpg_baseline}" ]; then
		if git diff --quiet HEAD -- 2>/dev/null; then
			rpg_baseline="HEAD"
		else
			review_prepush_test_gate_log "outcome=repair_skipped reason=baseline_unavailable"
			rpg_repair="skipped (baseline unavailable)"
			return 0
		fi
	fi
	rpg_pre_untracked="${RUNTIME_DIR}/prepush_gate_pre_untracked.txt"
	rpg_untracked_backup="${RUNTIME_DIR}/prepush_gate_pre_untracked.tar"
	git ls-files --others --exclude-standard "${rpg_untracked_excludes[@]}" 2>/dev/null | sort -u > "${rpg_pre_untracked}"
	rm -f "${rpg_untracked_backup}"
	if [ -s "${rpg_pre_untracked}" ]; then
		git ls-files --others --exclude-standard -z "${rpg_untracked_excludes[@]}" | tar --null -T - -cf "${rpg_untracked_backup}" 2>/dev/null || rm -f "${rpg_untracked_backup}"
	fi
	prompt="${RUNTIME_DIR}/mode-implement-repair-tests.txt"
	review_prepush_test_gate_build_prompt "${result}" "${prompt}" "${rpg_allow_file}" "${rpg_changed_file}" || {
		review_prepush_test_gate_log "outcome=repair_skipped reason=repair_template_absent"
		rpg_repair="skipped (repair template absent)"
		return 0
	}
	editor_timeout=$(( ${rpg_step_budget:-3300} - (SECONDS - ${rpg_started:-${SECONDS}}) - ${rpg_run_wall:-1020} ))
	if [ "${editor_timeout}" -lt 300 ]; then
		review_prepush_test_gate_log "outcome=repair_skipped reason=step_budget"
		rpg_repair="skipped (step time budget)"
		return 0
	fi
	rc=0
	timeout --kill-after=30s "${editor_timeout}" bash "${editor}" --repair-turn "${prompt}" "${RUNTIME_DIR}/prepush_gate_repair_output.txt" || rc=$?
	if review_prepush_test_gate_revert strays; then
		review_prepush_test_gate_log "outcome=warning reason=stray_edits_rolled_back"
	fi
	review_prepush_test_gate_restore_symlinks
	if [ "${rc}" -ne 0 ]; then
		# The sandbox writes edits back even after a failed attempt;
		# unvalidated partial edits must not reach "Commit changes".
		review_prepush_test_gate_revert all || true
		review_prepush_test_gate_log "outcome=repair_skipped reason=editor_rc_${rc}"
		rpg_repair="skipped (editor exit ${rc})"
		[ "${rc}" -ne 80 ] || rpg_repair="skipped (Claude capacity wait)"
		return 0
	fi
	validator="$(review_prepush_test_gate_pick "${sd:+${sd}/validate_changed_files_syntax.sh}")" || validator=""
	if [ -z "${validator}" ]; then
		review_prepush_test_gate_log "outcome=warning reason=syntax_validator_missing"
	elif ! CAPTURE_FILE="${RUNTIME_DIR}/prepush_gate_syntax_errors.txt" bash "${validator}" >/dev/null 2>&1; then
		review_prepush_test_gate_revert all || true
		review_prepush_test_gate_log "outcome=repair_reverted reason=syntax"
		rpg_repair="reverted (syntax)"
		return 0
	fi
	rpg_repair="ran"
	return 0
}

# Append the host-written summary to the editor summary (only when non-empty,
# so the post step's no-op and fallback logic is unchanged). The "results:"
# line ends a preceding "PR comment audit:" section for its parser.
review_prepush_test_gate_append_summary()
{
	local summary="$1" target="${EDITOR_SUMMARY_FILE:-}"
	[ -n "${target}" ] && [ -s "${target}" ] && [ ! -L "${target}" ] || return 0
	{
		printf '\n## Pre-push test gate\n\nPre-push test gate results:\n'
		sed '1{/^## Pre-push test gate$/d}' "${summary}"
	} >> "${target}" 2>/dev/null || review_prepush_test_gate_log "outcome=warning reason=summary_append_failed"
}

review_prepush_test_gate_main()
{
	set +eu
	set +o pipefail
	local final initial pr_caused inherited status_line run1 run2 summary root
	local rpg_started="${SECONDS}" rpg_step_budget=3300 rpg_editor_reserve=900 rpg_run_wall
	rpg_phase=review
	rpg_repair="not needed"
	review_prepush_test_gate_mode
	if [ "${rpg_mode}" = off ]; then
		review_prepush_test_gate_log "outcome=skipped reason=mode_off"
		return 0
	fi
	if [ -z "${RUNTIME_DIR:-}" ] || [ ! -d "${RUNTIME_DIR}" ]; then
		review_prepush_test_gate_log "outcome=skipped reason=runtime_dir_missing"
		return 0
	fi
	if [ "${AUTOFIX_PARTIAL_FINALIZE_REQUESTED:-false}" = "true" ]; then
		review_prepush_test_gate_log "outcome=skipped reason=partial_finalize"
		return 0
	fi
	if ! review_prepush_test_gate_locate; then
		review_prepush_test_gate_log "outcome=skipped reason=module_absent"
		return 0
	fi
	if [ "${rpg_mode}" = repair ] && [ -z "${rpg_template}" ]; then
		review_prepush_test_gate_log "outcome=mode_report reason=repair_template_absent"
		rpg_mode="report"
	fi
	cd "${WORKSPACE_PATH:-${GITHUB_WORKSPACE:-$PWD}}" 2>/dev/null || return 0
	rpg_worktree="$(git rev-parse --show-toplevel 2>/dev/null)" || rpg_worktree=""
	rpg_base="$(git rev-parse HEAD 2>/dev/null)" || rpg_base=""
	if [ -z "${rpg_worktree}" ] || ! [[ "${rpg_base}" =~ ^[0-9a-f]{40}$ ]]; then
		review_prepush_test_gate_log "outcome=skipped reason=no_git_checkout"
		return 0
	fi
	cd "${rpg_worktree}" || return 0
	rpg_changed_file="${RUNTIME_DIR}/prepush_gate_changed.txt"
	review_prepush_test_gate_changed > "${rpg_changed_file}"
	if [ ! -s "${rpg_changed_file}" ]; then
		review_prepush_test_gate_log "outcome=skipped reason=no_changes"
		return 0
	fi
	rpg_image="" rpg_image_reason="review_image_unavailable" rpg_venv="" rpg_node_modules=""
	root="${REVIEW_SANDBOX_ROOT:-}"
	if [ -n "${root}" ] && [ -f "${root}/image" ] && [ ! -L "${root}/image" ]; then
		rpg_image="$(head -n 1 "${root}/image" 2>/dev/null | tr -d '[:space:]')"
		review_prepush_test_gate_image_ok "${rpg_image}" || rpg_image=""
	fi
	if [ -n "${root}" ] && [ -d "${root}/source/.review-venv/bin" ] && [ ! -L "${root}/source/.review-venv" ]; then
		rpg_venv="${root}/source/.review-venv"
	elif [ -n "${root}" ]; then
		review_prepush_test_gate_log "outcome=warning reason=venv_absent"
	fi
	if [ -n "${root}" ] && [ -d "${root}/source/node_modules" ] && [ ! -L "${root}/source/node_modules" ]; then
		rpg_node_modules="${root}/source/node_modules"
	fi
	local fit_minutes=$(( (rpg_step_budget - (SECONDS - rpg_started) - 120) / 60 ))
	if [ "${fit_minutes}" -lt 1 ]; then
		review_prepush_test_gate_log "outcome=skipped reason=step_budget"
		rpg_image="" rpg_image_reason="step_budget"
	elif [ "${rpg_timeout}" -gt "${fit_minutes}" ]; then
		review_prepush_test_gate_log "outcome=warning reason=step_budget timeout_clamped=${fit_minutes}"
		rpg_timeout="${fit_minutes}"
	fi
	run1="${RUNTIME_DIR}/prepush_gate/run1"
	review_prepush_test_gate_run_once "${run1}"
	final="${run1}/result.json"
	status_line="$(review_prepush_test_gate_py status "${final}")" || status_line="unverified 0 0"
	read -r initial pr_caused inherited <<< "${status_line}"
	if [ "${initial}" = fail ] && [ "${pr_caused:-0}" -gt 0 ] 2>/dev/null; then
		rpg_run_wall=$(( rpg_timeout * 60 + 120 ))
		if [ "${rpg_mode}" = repair ] && [ $(( SECONDS - rpg_started + rpg_editor_reserve + rpg_run_wall )) -gt "${rpg_step_budget}" ]; then
			review_prepush_test_gate_log "outcome=repair_skipped reason=step_budget"
			rpg_repair="skipped (step time budget)"
		elif [ "${rpg_mode}" = repair ]; then
			rm -f "${RUNTIME_DIR}/prepush_gate_restore_failed.flag"
			review_prepush_test_gate_repair "${final}"
			if [ "${rpg_repair}" = ran ] && [ $(( SECONDS - rpg_started + rpg_run_wall )) -gt "${rpg_step_budget}" ]; then
				review_prepush_test_gate_log "outcome=warning reason=step_budget rerun=skipped"
				rpg_repair="ran (re-run skipped: step time budget)"
			elif [ "${rpg_repair}" = ran ]; then
				run2="${RUNTIME_DIR}/prepush_gate/run2"
				review_prepush_test_gate_run_once "${run2}"
				final="${run2}/result.json"
			fi
		else
			rpg_repair="not run (report mode)"
		fi
	fi
	if [ -f "${RUNTIME_DIR}/prepush_gate_restore_failed.flag" ]; then
		rpg_repair="${rpg_repair}; rollback incomplete"
	fi
	summary="${RUNTIME_DIR}/prepush_test_gate_summary.md"
	if ! review_prepush_test_gate_py summary "${final}" "${rpg_module}" "${summary}" "${rpg_mode}" "${rpg_repair}" "${initial}"; then
		rm -f "${summary}"
		review_prepush_test_gate_log "outcome=warning reason=summary_failed"
	else
		review_prepush_test_gate_append_summary "${summary}"
		if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
			cat "${summary}" >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
		fi
	fi
	status_line="$(review_prepush_test_gate_py status "${final}")" || status_line="unverified 0 0"
	read -r final pr_caused inherited <<< "${status_line}"
	review_prepush_test_gate_log "outcome=${final} initial=${initial} failures=${pr_caused} inherited=${inherited} mode=${rpg_mode} repair=$(printf '%s' "${rpg_repair}" | tr ' ()' '_  ' | tr -d ' ')"
	return 0
}

# Integration mode (#8210): see the header. Always returns 0.
review_prepush_test_gate_integration_main()
{
	set +eu
	set +o pipefail
	local branch="${TARGET_BRANCH:-}" base_branch="${BASE_BRANCH:-}" base="" sd resolver
	local gate_dir root="" image="" prep_env="" venv_args=() status_line status="" pr_caused=0 inherited=0 repair="not needed" rc=0
	# The step has timeout-minutes 120; keep 300 s for the push and summary.
	local step_started="${SECONDS}" step_budget=6900 left
	rpg_phase=integration
	if [ "${POST_SYNC_TEST_REPAIR:-false}" != "true" ] || ! [[ "${branch}" =~ ^orchestrator/project-[1-9][0-9]*$ ]]; then
		review_prepush_test_gate_log "outcome=skipped reason=not_requested"
		return 0
	fi
	# First, so the reviewer/editor steps skip even when the gate cannot run.
	if [ -n "${GITHUB_ENV:-}" ]; then
		echo "INTEGRATION_TEST_REPAIR_ACTIVE=true" >> "${GITHUB_ENV}"
	fi
	review_prepush_test_gate_mode
	if [ "${rpg_mode}" = off ]; then
		review_prepush_test_gate_log "outcome=skipped reason=mode_off"
		return 0
	fi
	if [ "${AUTOFIX_PRE_REVIEW_RESOLVE:-false}" = "true" ] || [ "${MERGE_CONFLICT:-false}" = "true" ]; then
		review_prepush_test_gate_log "outcome=skipped reason=conflict_path"
		return 0
	fi
	if [ -z "${RUNTIME_DIR:-}" ] || [ ! -d "${RUNTIME_DIR}" ]; then
		review_prepush_test_gate_log "outcome=skipped reason=runtime_dir_missing"
		return 0
	fi
	cd "${WORKSPACE_PATH:-${GITHUB_WORKSPACE:-$PWD}}" 2>/dev/null || return 0
	rpg_worktree="$(git rev-parse --show-toplevel 2>/dev/null)" || rpg_worktree=""
	[ -n "${rpg_worktree}" ] && cd "${rpg_worktree}" || { review_prepush_test_gate_log "outcome=skipped reason=no_git_checkout"; return 0; }
	if [[ "${base_branch}" =~ ^[A-Za-z0-9._/-]+$ ]]; then
		git rev-parse --verify --quiet "refs/remotes/origin/${base_branch}" >/dev/null 2>&1 \
			|| git fetch --no-tags --quiet origin "+refs/heads/${base_branch}:refs/remotes/origin/${base_branch}" 2>/dev/null || true
		base="$(git merge-base HEAD "refs/remotes/origin/${base_branch}" 2>/dev/null || true)"
	fi
	if ! [[ "${base}" =~ ^[0-9a-f]{40}$ ]]; then
		review_prepush_test_gate_log "outcome=skipped reason=merge_base_unresolved"
		return 0
	fi
	sd="$(review_prepush_test_gate_abs "${SUPPORT_SCRIPTS_DIR:-}")"
	gate_dir="${RUNTIME_DIR}/prepush_gate_integration"
	mkdir -p "${gate_dir}" 2>/dev/null || return 0
	if [ -n "${sd}" ] && [ -f "${sd}/review_untrusted_sandbox.sh" ]; then
		# A full prepare also builds the dependency venv with pytest, which the
		# gate needs to fail (not just mark unverified) pytest suites. prepare
		# reports its root only through GITHUB_ENV, so give it a private file.
		prep_env="${gate_dir}/prepare_github_env"
		: > "${prep_env}" 2>/dev/null || prep_env=""
		if [ -n "${prep_env}" ] && GITHUB_ENV="${prep_env}" timeout --kill-after=30s 1200 \
			bash "${sd}/review_untrusted_sandbox.sh" prepare codex >/dev/null 2>&1; then
			root="$(sed -n 's/^REVIEW_SANDBOX_ROOT=//p' "${prep_env}" | tail -n 1)"
		fi
		case "${root}" in "${RUNNER_TEMP:-/tmp}"/review-isolated-*) ;; *) root="" ;; esac
		[ -z "${root}" ] || [ -d "${root}" ] || root=""
		if [ -z "${root}" ]; then
			review_prepush_test_gate_log "outcome=warning reason=prepare_failed fallback=image_only"
			root="$(timeout --kill-after=30s 600 bash "${sd}/review_untrusted_sandbox.sh" prepare-ephemeral codex 2>/dev/null)" || root=""
		fi
		if [ -n "${root}" ] && [ -f "${root}/image" ] && [ ! -L "${root}/image" ]; then
			image="$(head -n 1 "${root}/image" 2>/dev/null | tr -d '[:space:]')"
		fi
		if [ -n "${root}" ] && [ -d "${root}/source/.review-venv/bin" ] && [ ! -L "${root}/source/.review-venv" ]; then
			venv_args=(--venv "${root}/source/.review-venv")
		elif [ -n "${root}" ]; then
			review_prepush_test_gate_log "outcome=warning reason=venv_absent"
		fi
		if [ -n "${root}" ] && [ -d "${root}/source/node_modules" ] && [ ! -L "${root}/source/node_modules" ]; then
			venv_args+=(--node-modules "${root}/source/node_modules")
		fi
	fi
	status_line="$(review_prepush_test_gate_cmd_gate --phase integration --worktree "${rpg_worktree}" --base "${base}" \
		--image "${image}" --image-reason integration_image_unavailable ${venv_args[@]+"${venv_args[@]}"} --out "${gate_dir}/run1" | tail -n 1)" || status_line=""
	rpg_phase=integration
	read -r status pr_caused inherited <<< "${status_line:-unverified 0 0}"
	if [ "${status}" = fail ] && [ "${pr_caused:-0}" -gt 0 ] 2>/dev/null; then
		resolver="$(review_prepush_test_gate_pick "${sd:+${sd}/review_conflict_resolve.sh}")" || resolver=""
		if [ "${rpg_mode}" != repair ]; then
			repair="not run (report mode)"
		elif [ -z "${resolver}" ]; then
			repair="skipped (resolver absent)"
		else
			rc=0
			left=$(( step_budget - (SECONDS - step_started) ))
			[ "${left}" -gt 0 ] || left=0
			RESOLVER_TEST_REPAIR_BUDGET_SECS="${left}" bash "${resolver}" --test-repair "${gate_dir}/run1/result.json" || rc=$?
			repair="resolver exit ${rc}"
			if [ -n "${GITHUB_ENV:-}" ] && grep -qx 'INTEGRATION_TEST_REPAIRED=true' "${GITHUB_ENV}" 2>/dev/null; then
				repair="committed"
				# Re-run the gate on the repaired head; a repair that leaves
				# PR-caused failures is not pushed (the later env line wins).
				left=$(( step_budget - (SECONDS - step_started) ))
				if [ "${left}" -lt $(( ${rpg_timeout:-15} * 60 + 120 )) ]; then
					repair="committed (re-run skipped: step time budget)"
				else
					status_line="$(review_prepush_test_gate_cmd_gate --phase integration --worktree "${rpg_worktree}" --base "${base}" \
						--image "${image}" --image-reason integration_image_unavailable ${venv_args[@]+"${venv_args[@]}"} --out "${gate_dir}/run2" | tail -n 1)" || status_line=""
					rpg_phase=integration
					read -r status pr_caused inherited <<< "${status_line:-unverified 0 0}"
					if [ "${status}" = fail ] && [ "${pr_caused:-0}" -gt 0 ] 2>/dev/null; then
						echo "INTEGRATION_TEST_REPAIRED=false" >> "${GITHUB_ENV}"
						repair="withheld (failures remain after repair)"
					fi
				fi
			fi
		fi
	fi
	if [ -n "${root}" ]; then
		REVIEW_SANDBOX_ROOT="${root}" bash "${sd}/review_untrusted_sandbox.sh" cleanup >/dev/null 2>&1 || true
	fi
	review_prepush_test_gate_log "outcome=${status:-unverified} failures=${pr_caused:-0} inherited=${inherited:-0} mode=${rpg_mode} repair=$(printf '%s' "${repair}" | tr ' ()' '_  ' | tr -d ' ')"
	return 0
}

# gate --worktree DIR --base SHA --image IMG --out DIR [--venv DIR] [--phase P]
#      [--image-reason R] [--node-modules DIR]
review_prepush_test_gate_cmd_gate()
{
	set +e
	local out=""
	rpg_worktree="" rpg_base="" rpg_image="" rpg_venv="" rpg_node_modules="" rpg_image_reason="image_unavailable"
	while [ "$#" -gt 0 ]; do
		case "$1" in
			--worktree) rpg_worktree="${2:-}"; shift 2 ;;
			--base) rpg_base="${2:-}"; shift 2 ;;
			--image) rpg_image="${2:-}"; shift 2 ;;
			--image-reason) rpg_image_reason="$(printf '%s' "${2:-}" | tr -dc 'a-z0-9_')"; shift 2 ;;
			--out) out="${2:-}"; shift 2 ;;
			--venv) rpg_venv="${2:-}"; shift 2 ;;
			--node-modules) rpg_node_modules="${2:-}"; shift 2 ;;
			--phase)
				case "${2:-}" in resolver|heal|review|integration) rpg_phase="$2" ;; esac
				shift 2
				;;
			*) review_prepush_test_gate_log "outcome=error reason=unknown_argument"; return 2 ;;
		esac
	done
	[ -n "${out}" ] && [ -n "${rpg_worktree}" ] || { review_prepush_test_gate_log "outcome=error reason=missing_argument"; return 2; }
	mkdir -p "${out}" 2>/dev/null || return 2
	review_prepush_test_gate_mode
	if [ "${rpg_mode}" = off ]; then
		review_prepush_test_gate_log "outcome=skipped reason=mode_off"
		echo "skipped 0 0"
		return 0
	fi
	if ! review_prepush_test_gate_locate; then
		review_prepush_test_gate_log "outcome=skipped reason=module_absent"
		echo "skipped 0 0"
		return 0
	fi
	if ! [[ "${rpg_base}" =~ ^[0-9a-f]{40}$ ]]; then
		rpg_image="" rpg_image_reason="base_unresolved"
	fi
	review_prepush_test_gate_run_once "${out}"
	review_prepush_test_gate_py status "${out}/result.json" || echo "unverified 0 0"
	return 0
}

rpg_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)" || rpg_script_dir=""

if [ "${BASH_SOURCE[0]}" != "$0" ]; then
	if [ "${PREPUSH_TEST_GATE_STEP_MODE:-}" = "integration" ]; then
		( review_prepush_test_gate_integration_main ) || true
	else
		( review_prepush_test_gate_main ) || true
	fi
	return 0
fi
rpg_phase="${rpg_phase:-review}"
rpg_cmd="${1:-}"
shift || true
case "${rpg_cmd}" in
	gate) review_prepush_test_gate_cmd_gate "$@"; exit $? ;;
	repair-prompt)
		# repair-prompt RESULT OUT [ALLOW_FILE] [TEMPLATE] [CHANGED_FILE]: a
		# regular, non-symlink TEMPLATE replaces the implement repair template
		# (the integration test repair passes its rendered prompt, #8210).
		[ -f "${1:-}" ] && [ -n "${2:-}" ] || exit 2
		review_prepush_test_gate_locate || exit 1
		if [ -n "${4:-}" ]; then
			[ -f "$4" ] && [ ! -L "$4" ] || exit 2
			rpg_template="$4"
		fi
		review_prepush_test_gate_build_prompt "$1" "$2" "${3:-}" "${5:-}" || exit 1
		exit 0
		;;
	allow-list)
		[ -d "${1:-}" ] && [ -f "${2:-}" ] && [ -f "${3:-}" ] && [ -n "${4:-}" ] || exit 2
		review_prepush_test_gate_locate || exit 1
		review_prepush_test_gate_allow_list "$1" "$2" "$3" "$4"
		exit $?
		;;
	summary)
		[ -f "${1:-}" ] && [ -n "${2:-}" ] || exit 2
		review_prepush_test_gate_locate || exit 1
		review_prepush_test_gate_py summary "$1" "${rpg_module}" "$2" "${3:-repair}" "${4:-not needed}" "${5:-}"
		exit $?
		;;
	mode) review_prepush_test_gate_mode; echo "${rpg_mode}"; exit 0 ;;
	*) review_prepush_test_gate_log "outcome=error reason=unknown_subcommand"; exit 2 ;;
esac
