#!/usr/bin/env bash
# shellcheck shell=bash
# implement_step_prepush_test_gate.sh - body of implement.yml's
# "Pre-push affected-test gate" step (issue #8190, plan phase P3 of
# docs/plans/prepush-affected-test-gate-plan.md).
#
# Sourced by the step (no arguments): runs the pre-push affected-test gate on
# the editor's uncommitted change, in the credential-free container of
# scripts/prepush_test_gate_run.sh. In `repair` mode, failures this change
# caused (not `inherited`) get exactly one editor repair turn, through the
# same isolated launch as the post-Codex syntax repair. Edits outside the
# repair allow-list are rolled back, syntax is re-validated (a syntax failure
# reverts the whole repair), and the gate runs once more; there is never a
# second repair. The host writes ${RUNTIME_DIR}/prepush_test_gate_summary.md
# from the final result.json (never from the container's summary.md, which
# tests can write); the PR-body pre-flight step appends it as
# "## Pre-push test gate".
#
# Executed as `implement_step_prepush_test_gate.sh expected-files PLAN ISSUE_BODY`
# (Build implementation context): prints an advisory "EXPECTED FILES" section
# for a standalone issue, from the trusted module's `select --paths-file` over
# the repository paths the approved plan names.
#
# Trust: the gate module, the wrapper and the repair prompt come only from the
# pre-editor run-dir copies (IMPLEMENT_STAGED_SUPPORT_RUN_DIR, staged by "Stage
# workflow support files") or the verified support checkout
# (.codex-workflow-src, copied outside the workspace first), never from the PR
# checkout. Tests run only in the container.
#
# Environment (each defaulted here, unattended section 8):
#   PREPUSH_TEST_GATE_MODE              off | report | repair (default repair;
#                                       unknown values act as repair)
#   PREPUSH_TEST_GATE_TIMEOUT_MINUTES   15
#   PREPUSH_TEST_GATE_LARGE_FILE_TESTS  50
#   PREPUSH_TEST_GATE_SHARDS            4
#
# Never blocks the push: every path, bootstrap errors included, ends with
# status 0. No GitHub API call. Log prefix: PREPUSH_TEST_GATE phase=implement.

implement_prepush_test_gate_log()
{
	echo "PREPUSH_TEST_GATE phase=implement $*" >&2
}

# Embedded host-side helper (trusted, stdlib only). Modes:
#   status RESULT                 -> "<status> <pr_caused> <inherited>"
#   synthetic RESULT REASON       write an "unverified" result
#   failures RESULT               failure text, `::` neutralised, 16 KiB cap
#   allow SELECTION RESULT        affected tests, companions, failing files
#   summary RESULT MODULE OUT MODE REPAIR INITIAL
#   expected SELECTION            advisory expected-files lines
IMPLEMENT_PREPUSH_TEST_GATE_PY='
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
		paths += [e.get("file") for e in entries(result, "failures")]
		for path in paths:
			if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
				continue
			if any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or path.lower().startswith((".claude/", ".github/ai/", ".git/")):
				continue
			print(path)
	elif mode == "expected":
		selection = load(argv[2])
		paths = {e.get("path") for e in entries(selection, "affected_tests")}
		paths |= {p for p in (selection.get("companion_files") or []) if isinstance(p, str)}
		for path in sorted(p for p in paths if isinstance(p, str) and p):
			print("- " + scrub(path))
		for check in entries(selection, "registry_checks"):
			print("- registry check: " + scrub(str(check.get("check"))))
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

implement_prepush_test_gate_py()
{
	PYTHONDONTWRITEBYTECODE=1 python3 -I -B -c "${IMPLEMENT_PREPUSH_TEST_GATE_PY}" "$@"
}

implement_prepush_test_gate_mode()
{
	local raw
	raw="$(printf '%s' "${PREPUSH_TEST_GATE_MODE:-repair}" | tr '[:upper:]' '[:lower:]')"
	case "${raw}" in
		off|report|repair) gate_mode="${raw}" ;;
		*)
			implement_prepush_test_gate_log "outcome=mode_invalid fallback=repair"
			gate_mode="repair"
			;;
	esac
	gate_timeout="${PREPUSH_TEST_GATE_TIMEOUT_MINUTES:-15}"
	[[ "${gate_timeout}" =~ ^[0-9]+$ ]] && [ "${gate_timeout}" -ge 1 ] && [ "${gate_timeout}" -le 360 ] || gate_timeout=15
	# Force base 10: bash arithmetic reads a leading zero (08, 030) as octal.
	gate_timeout=$((10#${gate_timeout}))
	# One run (timeout + 120 s) must fit the step's 55-minute budget, or the
	# 60-minute step kill lands before result.json and the summary exist.
	if [ "${gate_timeout}" -gt 53 ]; then
		implement_prepush_test_gate_log "outcome=config_invalid name=PREPUSH_TEST_GATE_TIMEOUT_MINUTES clamped=53"
		gate_timeout=53
	fi
	gate_large="${PREPUSH_TEST_GATE_LARGE_FILE_TESTS:-50}"
	[[ "${gate_large}" =~ ^[0-9]+$ ]] && [ "${gate_large}" -ge 1 ] || gate_large=50
	gate_shards="${PREPUSH_TEST_GATE_SHARDS:-4}"
	[[ "${gate_shards}" =~ ^[0-9]+$ ]] && [ "${gate_shards}" -ge 1 ] && [ "${gate_shards}" -le 64 ] || gate_shards=4
	gate_large=$((10#${gate_large})) gate_shards=$((10#${gate_shards}))
}

# First regular, non-symlink file among the arguments.
implement_prepush_test_gate_pick()
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

# Sets gate_module, gate_wrapper, gate_template from trusted copies only.
implement_prepush_test_gate_locate()
{
	local run_dir="${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}" src="${GITHUB_WORKSPACE:-$PWD}/.codex-workflow-src" fallback="${RUNTIME_DIR}/prepush_gate_support"
	local found
	gate_module="" gate_wrapper="" gate_template=""
	if [ -n "${run_dir}" ]; then
		gate_module="$(implement_prepush_test_gate_pick "${run_dir}/prepush_affected_tests.py")" || gate_module=""
		gate_wrapper="$(implement_prepush_test_gate_pick "${run_dir}/prepush_test_gate_run.sh")" || gate_wrapper=""
		gate_template="$(implement_prepush_test_gate_pick "${run_dir%/scripts}/prompts/mode-implement-repair-tests.txt")" || gate_template=""
	fi
	if [ -z "${gate_module}" ] || [ -z "${gate_wrapper}" ]; then
		# Copy the verified support checkout's files outside the workspace:
		# the wrapper refuses a module inside the tested worktree.
		mkdir -p "${fallback}" 2>/dev/null || return 1
		if found="$(implement_prepush_test_gate_pick "${src}/workflow-templates/.claude/hooks/prepush_affected_tests.py")" \
			&& install -m 0644 "${found}" "${fallback}/prepush_affected_tests.py"; then
			gate_module="${fallback}/prepush_affected_tests.py"
		fi
		if found="$(implement_prepush_test_gate_pick "${src}/scripts/prepush_test_gate_run.sh")" \
			&& install -m 0755 "${found}" "${fallback}/prepush_test_gate_run.sh"; then
			gate_wrapper="${fallback}/prepush_test_gate_run.sh"
			if found="$(implement_prepush_test_gate_pick "${src}/scripts/review_untrusted_workspace.py")"; then
				install -m 0644 "${found}" "${fallback}/review_untrusted_workspace.py" || true
			fi
		fi
	fi
	if [ -z "${gate_template}" ] && found="$(implement_prepush_test_gate_pick "${src}/prompts/mode-implement-repair-tests.txt")"; then
		install -m 0644 "${found}" "${fallback}/mode-implement-repair-tests.txt" 2>/dev/null && gate_template="${fallback}/mode-implement-repair-tests.txt"
	fi
	# Attempt journal rules fragment (issue #8428), located like gate_template.
	gate_journal_rules=""
	if [ "${PERSIST_JOURNAL_ENABLED:-false}" = "true" ]; then
		if [ -n "${run_dir}" ]; then
			gate_journal_rules="$(implement_prepush_test_gate_pick "${run_dir%/scripts}/prompts/mode-persist-journal-rules.txt")" || gate_journal_rules=""
		fi
		if [ -z "${gate_journal_rules}" ] && found="$(implement_prepush_test_gate_pick "${src}/prompts/mode-persist-journal-rules.txt")" \
			&& mkdir -p "${fallback}" 2>/dev/null; then
			install -m 0644 "${found}" "${fallback}/mode-persist-journal-rules.txt" 2>/dev/null && gate_journal_rules="${fallback}/mode-persist-journal-rules.txt"
		fi
	fi
	[ -n "${gate_module}" ] && [ -n "${gate_wrapper}" ]
}

implement_prepush_test_gate_staged_workspace()
{
	local helper="${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}/implement_staged_support_workspace.sh"
	[ -n "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}" ] && [ -f "${helper}" ] || return 0
	bash "${helper}" "$1" || implement_prepush_test_gate_log "outcome=warning reason=staged_support_$1_failed"
}

# Uncommitted change set (tracked edits plus untracked files), one per line.
# Same pathspec exclusions as the commit step's untracked listing.
gate_untracked_excludes=(-- . ':!node_modules' ':!.codex-workflow-src' ':!.codex-workflow-src-main')

implement_prepush_test_gate_changed()
{
	{
		git diff --name-only HEAD 2>/dev/null
		git ls-files --others --exclude-standard "${gate_untracked_excludes[@]}" 2>/dev/null
	} | sed '/^$/d' | sort -u
}

implement_prepush_test_gate_run_once()
{
	local out="$1"
	local args
	rm -rf -- "${out}"
	mkdir -p "${out}"
	if [ -z "${gate_image}" ]; then
		implement_prepush_test_gate_py synthetic "${out}/result.json" "${gate_image_reason:-image_unavailable}"
		return 0
	fi
	args=(--worktree "${gate_worktree}" --module "${gate_module}" --image "${gate_image}" --base "${gate_base}"
		--out-dir "${out}" --timeout-minutes "${gate_timeout}" --shards "${gate_shards}" --large-file-tests "${gate_large}")
	if [ -n "${CODEX_ISOLATED_IMPLEMENT_ROOT:-}" ] && [ -d "${CODEX_ISOLATED_IMPLEMENT_ROOT}/venv/bin" ]; then
		args+=(--venv "${CODEX_ISOLATED_IMPLEMENT_ROOT}/venv")
	fi
	# Node prep root of prepare --deps (the snapshot leaves node_modules out).
	if [ -n "${CODEX_ISOLATED_IMPLEMENT_ROOT:-}" ] && [ -d "${CODEX_ISOLATED_IMPLEMENT_ROOT}/work/node_modules" ] && [ ! -L "${CODEX_ISOLATED_IMPLEMENT_ROOT}/work/node_modules" ]; then
		args+=(--node-modules "${CODEX_ISOLATED_IMPLEMENT_ROOT}/work/node_modules")
	fi
	# Nested Node projects' node_modules (handoff-nested-deps puts them under work/<project>/).
	if [ -n "${CODEX_ISOLATED_IMPLEMENT_ROOT:-}" ] && [ -d "${CODEX_ISOLATED_IMPLEMENT_ROOT}/work" ] && [ ! -L "${CODEX_ISOLATED_IMPLEMENT_ROOT}/work" ]; then
		args+=(--nested-node-modules-root "${CODEX_ISOLATED_IMPLEMENT_ROOT}/work")
	fi
	implement_prepush_test_gate_staged_workspace restore
	bash "${gate_wrapper}" "${args[@]}" || true
	implement_prepush_test_gate_staged_workspace reinstall
	[ -f "${out}/result.json" ] || implement_prepush_test_gate_py synthetic "${out}/result.json" wrapper_no_result
}

# Restore paths changed since the repair baseline. $1 = strays (only paths
# outside the allow-list) or all (every repair edit).
implement_prepush_test_gate_revert()
{
	local which="$1" path baseline_blob current_blob restore_failed=false
	local tracked=() untracked=() restore=() remove=()
	while IFS= read -r -d '' path; do
		[ -n "${path}" ] || continue
		if [ "${which}" = strays ] && grep -Fqx -- "${path}" "${gate_allow_file}"; then
			continue
		fi
		if git cat-file -e "${gate_baseline}:${path}" 2>/dev/null; then
			baseline_blob="$(git rev-parse "${gate_baseline}:${path}" 2>/dev/null)"
		else
			baseline_blob="__MISSING__"
		fi
		if [ -f "${path}" ] || [ -L "${path}" ]; then
			current_blob="$(git hash-object -- "${path}" 2>/dev/null || echo "__MISSING__")"
		else
			current_blob="__MISSING__"
		fi
		[ "${baseline_blob}" = "${current_blob}" ] || tracked+=("${path}")
	done < <({ git diff --name-only -z HEAD 2>/dev/null; git diff --name-only -z HEAD "${gate_baseline}" 2>/dev/null; } | sort -zu)
	while IFS= read -r -d '' path; do
		[ -n "${path}" ] || continue
		if [ "${which}" = strays ] && grep -Fqx -- "${path}" "${gate_allow_file}"; then
			continue
		fi
		grep -Fqx -- "${path}" "${gate_pre_untracked}" || untracked+=("${path}")
	done < <(git ls-files --others --exclude-standard -z "${gate_untracked_excludes[@]}" 2>/dev/null)
	[ "${#tracked[@]}" -gt 0 ] || [ "${#untracked[@]}" -gt 0 ] || [ "${which}" = all ] || return 1
	for path in "${tracked[@]}"; do
		if git cat-file -e "${gate_baseline}:${path}" 2>/dev/null; then
			restore+=("${path}")
		else
			remove+=("${path}")
		fi
	done
	[ "${#restore[@]}" -eq 0 ] || git restore --source="${gate_baseline}" --staged --worktree -- "${restore[@]}" || restore_failed=true
	[ "${#remove[@]}" -eq 0 ] || git rm -r -f -q --ignore-unmatch -- "${remove[@]}" || restore_failed=true
	if [ "${restore_failed}" = true ]; then
		: > "${RUNTIME_DIR}/prepush_gate_restore_failed.flag"
		implement_prepush_test_gate_log "outcome=warning reason=restore_failed mode=${which}"
	fi
	for path in "${untracked[@]}"; do
		case "${path}" in
			/*|../*|*/../*|*/..) implement_prepush_test_gate_log "outcome=warning reason=unsafe_untracked_path" ;;
			*) rm -rf -- "${path}" ;;
		esac
	done
	if [ "${which}" = all ] && [ -s "${gate_untracked_backup}" ]; then
		# Untracked files the editor created before the repair turn.
		tar -xf "${gate_untracked_backup}" 2>/dev/null || implement_prepush_test_gate_log "outcome=warning reason=untracked_restore_failed"
	fi
	return 0
}

# A tracked symlink the repair editor replaced or retargeted goes back to HEAD
# (the "Restore tracked symlinks" step already ran before this step).
implement_prepush_test_gate_restore_symlinks()
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
		if [ -n "${STAGED_SUPPORT_LEDGER:-}" ] && [ -f "${STAGED_SUPPORT_LEDGER}" ] && grep -Fqx -- "${path}" "${STAGED_SUPPORT_LEDGER}"; then
			continue
		fi
		# The backup lives outside the worktree, so a kill can never leave it
		# for "Commit changes" to stage.
		bak_n=$((bak_n + 1))
		bak="${RUNTIME_DIR}/prepush_gate_symlink_bak.${bak_n}"
		rm -rf -- "${bak}"
		if [ -e "./${path}" ] || [ -L "./${path}" ]; then
			mv -- "./${path}" "${bak}" 2>/dev/null || continue
		fi
		if GIT_LITERAL_PATHSPECS=1 git checkout HEAD -- "${path}" 2>/dev/null; then
			rm -rf -- "${bak}"
			implement_prepush_test_gate_log "outcome=warning reason=symlink_restored"
		else
			mv -- "${bak}" "./${path}" 2>/dev/null || true
			implement_prepush_test_gate_log "outcome=warning reason=symlink_restore_failed"
		fi
	done < <(git ls-files -s -z 2>/dev/null)
}

# Attempt journal blocks of the repair prompt (issue #8428, P1). Only with
# PERSIST_JOURNAL_ENABLED=true (default false); otherwise prints nothing, so
# the prompt is byte-identical with the switch off. Prints the removed lines
# of `git diff HEAD` for the allow-list files (each line UNTRUSTED_DATA-
# prefixed, at most 4 KiB), the journal rendered for this run's editor prompt
# (${RUNTIME_DIR}/attempt_journal.txt, already fenced; no API call) and the
# trusted rules fragment.
implement_prepush_test_gate_journal_blocks()
{
	local allow="$1" rules="$2" path removed
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
	if [ -n "${rules}" ] && [ -f "${rules}" ]; then
		echo
		# Body only: the fragment's tier and Role/Goal metadata lines stay out.
		sed -n '/^=== ATTEMPT JOURNAL REPAIR RULES ===$/,$p' "${rules}" || true
	else
		echo "PERSIST journal outcome=skipped reason=rules_absent" >&2
	fi
	return 0
}

implement_prepush_test_gate_build_prompt()
{
	local result="$1" out="$2" path
	{
		cat "${gate_template}"
		echo
		echo "=== CAPTURED TEST FAILURES (UNTRUSTED) ==="
		# Every line is prefixed so test output cannot imitate the trusted
		# section headers below; the END marker closes the untrusted block.
		implement_prepush_test_gate_py failures "${result}" | sed 's/^/UNTRUSTED_DATA: /'
		echo "=== END CAPTURED TEST FAILURES (UNTRUSTED) ==="
		echo
		echo "=== CHANGED FILES ==="
		cat "${gate_changed_file}"
		echo
		echo "=== EFFECTIVE REPAIR ALLOW-LIST ==="
		cat "${gate_allow_file}"
		echo
		echo "=== CURRENT DIFF FOR ALLOW-LIST FILES ==="
		while IFS= read -r path; do
			[ -n "${path}" ] || continue
			echo "----- ${path} -----"
			if git ls-files --error-unmatch -- "${path}" >/dev/null 2>&1; then
				git diff HEAD -- "${path}" || true
			elif [ -f "${path}" ] && [ ! -L "${path}" ]; then
				git diff --no-index -- /dev/null "${path}" || true
			else
				echo "(file not found in working tree)"
			fi
		done < "${gate_allow_file}"
		implement_prepush_test_gate_journal_blocks "${gate_allow_file}" "${gate_journal_rules:-}"
	} > "${out}"
	if [ -n "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}" ] && [ -f "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/gh_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/gh_helpers.sh" 2>/dev/null || true
	fi
	if command -v sanitize_codex_prompt_file >/dev/null 2>&1; then
		sanitize_codex_prompt_file "${out}" || true
	fi
}

# Narrow the repair allow-list to what the commit-time guards accept, so a
# compliant repair can never get the commit refused as out of scope. Changed
# files stay (they are already in the commit); other paths must pass the
# issue's files_touched scope, and automation paths must be exact entries.
implement_prepush_test_gate_narrow_allow_list()
{
	local guard="${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}/files_touched_scope_guard.py"
	local extras="${RUNTIME_DIR}/prepush_gate_allow_extras.txt" entries="${RUNTIME_DIR}/prepush_gate_allow_entries.txt"
	local denied="${RUNTIME_DIR}/prepush_gate_allow_denied.txt" rc=0 path before after
	local auto="${RUNTIME_DIR}/prepush_gate_allow_auto.txt" auto_denied="${RUNTIME_DIR}/prepush_gate_allow_auto_denied.txt" auto_rc=127
	grep -Fxv -f "${gate_changed_file}" "${gate_allow_file}" > "${extras}" || true
	[ -s "${extras}" ] || return 0
	before="$(wc -l < "${extras}")"
	: > "${entries}"
	: > "${denied}"
	: > "${auto}"
	: > "${auto_denied}"
	if [ ! -f "${guard}" ]; then
		cp -f "${extras}" "${denied}"
	elif [ "${ENFORCE_FILES_TOUCHED:-true}" = "true" ] && [ "${ALLOW_OUT_OF_SCOPE_FILES:-false}" != "true" ]; then
		PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${guard}" --issue-body-file "${ISSUE_BODY_FILE:-/dev/null}" --issue-number "${ISSUE_NUMBER:-}" \
			--staged-file "${extras}" --allowlist-out "${entries}" > "${denied}" 2>/dev/null || rc=$?
		case "${rc}" in 0) : > "${denied}" ;; 10|20) ;; *) cp -f "${extras}" "${denied}" ;; esac
	fi
	while IFS= read -r path; do
		[ -n "${path}" ] || continue
		grep -Fqx -- "${path}" "${denied}" && continue
		case "$(printf '%s' "${path}" | tr '[:upper:]' '[:lower:]')" in
			.github/*|.claude/*|scripts/*|prompts/*|workflow-templates/*) printf '%s\n' "${path}" >> "${auto}"; continue ;;
		esac
		printf '%s\n' "${path}"
	done < "${extras}" > "${extras}.kept"
	# Automation paths go through the same grant check "Commit changes" runs
	# (ALLOW_WORKFLOW_EDITS, trusted issue-bound grant, open pipeline grant).
	if [ -s "${auto}" ] && [ -f "${guard}" ]; then
		auto_rc=0
		PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${guard}" --check-automation-paths --staged-file "${auto}" \
			--automation-grant-file "${AUTOMATION_PATH_GRANT_FILE:-${RUNTIME_DIR}/automation_path_grant.json}" \
			--allow-workflow-edits "${ALLOW_WORKFLOW_EDITS:-false}" --issue-number "${ISSUE_NUMBER:-}" > "${auto_denied}" 2>/dev/null || auto_rc=$?
	fi
	case "${auto_rc}" in
		0) cat "${auto}" >> "${extras}.kept" ;;
		30) [ ! -s "${auto_denied}" ] || grep -Fxv -f "${auto_denied}" "${auto}" >> "${extras}.kept" || true ;;
	esac
	cat "${gate_changed_file}" "${extras}.kept" | sed '/^$/d' | sort -u > "${gate_allow_file}"
	after="$(wc -l < "${extras}.kept")"
	[ "${after}" -eq "${before}" ] || implement_prepush_test_gate_log "outcome=warning reason=allow_list_narrowed dropped=$(( before - after ))"
	rm -f "${extras}" "${extras}.kept" "${entries}" "${denied}" "${auto}" "${auto_denied}"
}

# One isolated editor turn. Sets gate_repair to ran | skipped (...) | reverted (syntax).
implement_prepush_test_gate_repair()
{
	local run_dir="${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}" result="$1" prompt rc reasoning editor_timeout
	local isolated_exec="${run_dir}/codex_isolated_exec.sh" isolated_root="${CODEX_ISOLATED_IMPLEMENT_ROOT:-}" prepared_root=""
	if [ -z "${run_dir}" ] || [ ! -f "${isolated_exec}" ] || [ ! -f "${run_dir}/codex_thread_reuse.sh" ]; then
		implement_prepush_test_gate_log "outcome=repair_skipped reason=isolation_unavailable"
		gate_repair="skipped (isolation unavailable)"
		return 0
	fi
	gate_allow_file="${RUNTIME_DIR}/prepush_gate_allow_list.txt"
	{
		cat "${gate_changed_file}"
		if implement_prepush_test_gate_py_module select --repo-root "${gate_worktree}" --paths-file "${gate_changed_file}" --out "${RUNTIME_DIR}/prepush_gate_selection.json"; then
			implement_prepush_test_gate_py allow "${RUNTIME_DIR}/prepush_gate_selection.json" "${result}"
		fi
	} | sed '/^$/d' | sort -u > "${gate_allow_file}"
	if [ "${ALLOW_WORKFLOW_EDITS:-false}" != "true" ]; then
		grep -v '^\.github/workflows/' "${gate_allow_file}" > "${gate_allow_file}.tmp" || true
		mv -f "${gate_allow_file}.tmp" "${gate_allow_file}"
	fi
	implement_prepush_test_gate_narrow_allow_list
	if [ ! -s "${gate_allow_file}" ]; then
		implement_prepush_test_gate_log "outcome=repair_skipped reason=empty_allow_list"
		gate_repair="skipped (empty allow-list)"
		return 0
	fi
	# The baseline is the pre-repair tree. HEAD is only correct when no tracked
	# file differs from it; otherwise a failed stash would make `revert all`
	# undo the whole implementation, so the repair is skipped instead.
	if ! gate_baseline="$(git stash create "prepush-gate-repair" 2>/dev/null)"; then
		gate_baseline=""
	fi
	if [ -z "${gate_baseline}" ]; then
		if git diff --quiet HEAD -- 2>/dev/null; then
			gate_baseline="HEAD"
		else
			implement_prepush_test_gate_log "outcome=repair_skipped reason=baseline_unavailable"
			gate_repair="skipped (baseline unavailable)"
			return 0
		fi
	fi
	gate_pre_untracked="${RUNTIME_DIR}/prepush_gate_pre_untracked.txt"
	gate_untracked_backup="${RUNTIME_DIR}/prepush_gate_pre_untracked.tar"
	git ls-files --others --exclude-standard "${gate_untracked_excludes[@]}" 2>/dev/null | sort -u > "${gate_pre_untracked}"
	rm -f "${gate_untracked_backup}"
	if [ -s "${gate_pre_untracked}" ]; then
		git ls-files --others --exclude-standard -z "${gate_untracked_excludes[@]}" | tar --null -T - -cf "${gate_untracked_backup}" 2>/dev/null || rm -f "${gate_untracked_backup}"
	fi
	prompt="${RUNTIME_DIR}/mode-implement-repair-tests.txt"
	implement_prepush_test_gate_build_prompt "${result}" "${prompt}"
	if ! bash "${run_dir}/workspace_safety_check.sh"; then
		implement_prepush_test_gate_log "outcome=repair_skipped reason=workspace_unsafe"
		gate_repair="skipped (workspace safety check failed)"
		return 0
	fi
	if [ -z "${isolated_root}" ] || [ ! -d "${isolated_root}" ]; then
		if ! isolated_root="$(CODEX_ISOLATED_IMAGE_BUILD_ATTEMPTS=3 bash "${isolated_exec}" prepare --workdir "$(pwd)" --deps)"; then
			implement_prepush_test_gate_log "outcome=repair_skipped reason=isolation_unavailable"
			gate_repair="skipped (isolation unavailable)"
			return 0
		fi
		prepared_root="${isolated_root}"
	fi
	# Bound the turn so the step's 60-minute kill can never land before the
	# rollback and syntax re-validation below (step_budget, gate_started and
	# run_wall are implement_prepush_test_gate_main's locals).
	editor_timeout=$(( ${step_budget:-3300} - (SECONDS - ${gate_started:-${SECONDS}}) - ${run_wall:-1020} ))
	if [ "${editor_timeout}" -lt 300 ]; then
		[ -z "${prepared_root}" ] || bash "${isolated_exec}" cleanup --root "${prepared_root}" >/dev/null 2>&1 || true
		implement_prepush_test_gate_log "outcome=repair_skipped reason=step_budget"
		gate_repair="skipped (step time budget)"
		return 0
	fi
	reasoning="${MODEL_REPAIR_REASONING_EFFORT:-high}"
	case "${reasoning}" in xhigh|high|medium|low|none) ;; *) reasoning="high" ;; esac
	implement_prepush_test_gate_staged_workspace restore
	rc=0
	CODEX_ISOLATED_EXEC="${isolated_exec}" CODEX_ISOLATED_ROOT="${isolated_root}" CODEX_ISOLATED_MODE="workspace" \
		CODEX_THREAD_REUSE_STATE_KEY="implement-repair-tests" \
		CODEX_THREAD_REUSE_TIMEOUT_SECS="${editor_timeout}" \
		CODEX_THREAD_REUSE_ENGINE="${AI_ENGINE_RESOLVED_IMPLEMENT_REPAIR:-codex}" \
		CODEX_THREAD_REUSE_ENGINE_ROLE="IMPLEMENT_REPAIR" \
		AI_ENGINE_EFFORT_HINT="${reasoning}" \
		CODEX_THREAD_REUSE_PROMPT_FILE="${prompt}" \
		CODEX_THREAD_REUSE_OUTPUT_FILE="${RUNTIME_DIR}/prepush_gate_repair_output.txt" \
		CODEX_THREAD_REUSE_PHASE="implement_repair_tests" \
		CODEX_THREAD_REUSE_MODEL="${MODEL_EDITOR:-}" \
		CODEX_THREAD_REUSE_SKIP_GIT_REPO_CHECK="true" \
		CODEX_THREAD_REUSE_LOG_FILE="${RUNTIME_DIR}/prepush_gate_repair_log.txt" \
		CODEX_THREAD_REUSE_CONTINUATION_FILE="" \
		CODEX_THREAD_REUSE_TRANSFORM_MODE="replace-prefix" \
		CODEX_THREAD_REUSE_MARKER_START="=== CAPTURED TEST FAILURES (UNTRUSTED) ===" \
		env -u STAGED_SUPPORT_LEDGER -u STAGED_SUPPORT_BASE_DIR -u STAGED_SUPPORT_EDITOR_HEAD_LEDGER -u IMPLEMENT_STAGED_SUPPORT_RUN_DIR \
		bash "${run_dir}/codex_thread_reuse.sh" direct-run || rc=$?
	if [ -n "${prepared_root}" ]; then
		bash "${isolated_exec}" cleanup --root "${prepared_root}" >/dev/null 2>&1 \
			|| implement_prepush_test_gate_log "outcome=warning reason=isolated_root_cleanup_failed"
	fi
	implement_prepush_test_gate_staged_workspace reinstall
	if implement_prepush_test_gate_revert strays; then
		implement_prepush_test_gate_log "outcome=warning reason=stray_edits_rolled_back"
	fi
	implement_prepush_test_gate_restore_symlinks
	if [ "${rc}" -ne 0 ]; then
		# The isolated runner writes edits back even after a failed attempt;
		# unvalidated partial edits must not reach "Commit changes".
		implement_prepush_test_gate_revert all || true
		implement_prepush_test_gate_log "outcome=repair_skipped reason=editor_rc_${rc}"
		gate_repair="skipped (editor exit ${rc})"
		[ "${rc}" -ne 80 ] || gate_repair="skipped (Claude capacity wait)"
		return 0
	fi
	if ! CAPTURE_FILE="${RUNTIME_DIR}/prepush_gate_syntax_errors.txt" RUNTIME_DIR="${RUNTIME_DIR}" bash "${run_dir}/validate_changed_files_syntax.sh"; then
		implement_prepush_test_gate_revert all || true
		implement_prepush_test_gate_log "outcome=repair_reverted reason=syntax"
		gate_repair="reverted (syntax)"
		return 0
	fi
	gate_repair="ran"
	return 0
}

implement_prepush_test_gate_py_module()
{
	PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${gate_module}" "$@" 2>/dev/null
}

implement_prepush_test_gate_main()
{
	set +eu
	set +o pipefail
	local final initial pr_caused inherited status_line run1 run2 summary run_wall gate_started="${SECONDS}"
	# The step has timeout-minutes: 60; keep a margin so a kill can never land
	# inside the repair turn (before its rollback and syntax re-validation).
	local step_budget=3300 editor_reserve=900
	gate_repair="not needed"
	implement_prepush_test_gate_mode
	if [ "${gate_mode}" = off ]; then
		implement_prepush_test_gate_log "outcome=skipped reason=mode_off"
		return 0
	fi
	if [ -z "${RUNTIME_DIR:-}" ] || [ ! -d "${RUNTIME_DIR}" ]; then
		implement_prepush_test_gate_log "outcome=skipped reason=runtime_dir_missing"
		return 0
	fi
	if ! implement_prepush_test_gate_locate; then
		implement_prepush_test_gate_log "outcome=skipped reason=module_absent"
		return 0
	fi
	if [ "${gate_mode}" = repair ] && [ -z "${gate_template}" ]; then
		implement_prepush_test_gate_log "outcome=mode_report reason=repair_template_absent"
		gate_mode="report"
	fi
	gate_worktree="$(git rev-parse --show-toplevel 2>/dev/null)" || gate_worktree=""
	gate_base="$(git rev-parse HEAD 2>/dev/null)" || gate_base=""
	if [ -z "${gate_worktree}" ] || ! [[ "${gate_base}" =~ ^[0-9a-f]{40}$ ]]; then
		implement_prepush_test_gate_log "outcome=skipped reason=no_git_checkout"
		return 0
	fi
	cd "${gate_worktree}" || return 0
	gate_changed_file="${RUNTIME_DIR}/prepush_gate_changed.txt"
	implement_prepush_test_gate_staged_workspace restore
	implement_prepush_test_gate_changed > "${gate_changed_file}"
	implement_prepush_test_gate_staged_workspace reinstall
	if [ ! -s "${gate_changed_file}" ]; then
		implement_prepush_test_gate_log "outcome=skipped reason=no_changes"
		return 0
	fi
	gate_image="" gate_image_reason=""
	if [ -n "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}" ] && [ -f "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/codex_isolated_exec.sh" ]; then
		gate_image="$(bash "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/codex_isolated_exec.sh" image | tail -n 1)" || gate_image=""
		[[ "${gate_image}" =~ ^sha256:[0-9a-f]{64}$ ]] || gate_image=""
		if [ -z "${gate_image}" ] && ! grep -qF 'image-context|image)' "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/codex_isolated_exec.sh"; then
			# The staged support copy predates the image action.
			gate_image_reason="support_image_action_missing"
		fi
	fi
	# Image build and setup already used part of the step budget: one run
	# (timeout + 120 s) must still fit what is left of it.
	local fit_minutes=$(( (step_budget - (SECONDS - gate_started) - 120) / 60 ))
	if [ "${fit_minutes}" -lt 1 ]; then
		# Not even a 1-minute run fits: record UNVERIFIED instead of starting
		# a container the 60-minute step kill would cut off before the summary.
		implement_prepush_test_gate_log "outcome=skipped reason=step_budget"
		gate_image="" gate_image_reason="step_budget" fit_minutes=1
	elif [ "${gate_timeout}" -gt "${fit_minutes}" ]; then
		implement_prepush_test_gate_log "outcome=warning reason=step_budget timeout_clamped=${fit_minutes}"
		gate_timeout="${fit_minutes}"
	fi
	run1="${RUNTIME_DIR}/prepush_gate/run1"
	implement_prepush_test_gate_run_once "${run1}"
	final="${run1}/result.json"
	status_line="$(implement_prepush_test_gate_py status "${final}")" || status_line="unverified 0 0"
	read -r initial pr_caused inherited <<< "${status_line}"
	if [ "${initial}" = fail ] && [ "${pr_caused:-0}" -gt 0 ] 2>/dev/null; then
		run_wall=$(( gate_timeout * 60 + 120 ))
		if [ "${gate_mode}" = repair ] && [ $(( SECONDS - gate_started + editor_reserve + run_wall )) -gt "${step_budget}" ]; then
			implement_prepush_test_gate_log "outcome=repair_skipped reason=step_budget"
			gate_repair="skipped (step time budget)"
		elif [ "${gate_mode}" = repair ]; then
			rm -f "${RUNTIME_DIR}/prepush_gate_restore_failed.flag"
			implement_prepush_test_gate_repair "${final}"
			# A reverted repair leaves the tree run 1 already tested: no re-run.
			if [ "${gate_repair}" = ran ] && [ $(( SECONDS - gate_started + run_wall )) -gt "${step_budget}" ]; then
				implement_prepush_test_gate_log "outcome=warning reason=step_budget rerun=skipped"
				gate_repair="ran (re-run skipped: step time budget)"
			elif [ "${gate_repair}" = ran ]; then
				run2="${RUNTIME_DIR}/prepush_gate/run2"
				implement_prepush_test_gate_run_once "${run2}"
				final="${run2}/result.json"
			fi
		else
			gate_repair="not run (report mode)"
		fi
	fi
	if [ -f "${RUNTIME_DIR}/prepush_gate_restore_failed.flag" ]; then
		gate_repair="${gate_repair}; rollback incomplete"
	fi
	summary="${RUNTIME_DIR}/prepush_test_gate_summary.md"
	if ! implement_prepush_test_gate_py summary "${final}" "${gate_module}" "${summary}" "${gate_mode}" "${gate_repair}" "${initial}"; then
		rm -f "${summary}"
		implement_prepush_test_gate_log "outcome=warning reason=summary_failed"
	elif [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
		cat "${summary}" >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
	fi
	status_line="$(implement_prepush_test_gate_py status "${final}")" || status_line="unverified 0 0"
	read -r final pr_caused inherited <<< "${status_line}"
	implement_prepush_test_gate_log "outcome=${final} initial=${initial} failures=${pr_caused} inherited=${inherited} mode=${gate_mode} repair=$(printf '%s' "${gate_repair}" | tr ' ()' '_  ' | tr -d ' ')"
	return 0
}

# expected-files PLAN_FILE ISSUE_BODY_FILE: advisory section for standalone issues.
implement_prepush_test_gate_expected_files()
{
	set +eu
	local plan="${1:-}" body="${2:-}" tmp lines
	implement_prepush_test_gate_mode
	[ "${gate_mode}" != off ] || return 0
	[ -f "${plan}" ] || return 0
	if [ -f "${body}" ] && grep -qF '**Orchestrator metadata**' "${body}"; then
		return 0
	fi
	RUNTIME_DIR="${RUNTIME_DIR:-$(mktemp -d)}"
	implement_prepush_test_gate_locate || return 0
	[ -f "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}/targeted_file_context.py" ] || return 0
	tmp="$(mktemp -d)" || return 0
	PYTHONDONTWRITEBYTECODE=1 python3 -I -B - "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}" "${plan}" > "${tmp}/candidates.txt" 2>/dev/null <<'PY' || true
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import targeted_file_context
for path in targeted_file_context.extract_paths_from_plan(Path(sys.argv[2]).read_text(encoding="utf-8", errors="replace")):
	print(path)
PY
	if [ -s "${tmp}/candidates.txt" ]; then
		tr '\n' '\0' < "${tmp}/candidates.txt" | xargs -0 git ls-files -- 2>/dev/null | sort -u > "${tmp}/paths.txt"
	fi
	if [ -s "${tmp}/paths.txt" ] \
		&& implement_prepush_test_gate_py_module select --repo-root "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" --paths-file "${tmp}/paths.txt" --out "${tmp}/selection.json"; then
		lines="$(implement_prepush_test_gate_py expected "${tmp}/selection.json")"
		if [ -n "${lines}" ]; then
			echo "EXPECTED FILES (ADVISORY)"
			echo "Tests and registries the pre-push gate expects this change to affect. This is not a scope."
			printf '%s\n' "${lines}"
			echo
		fi
	fi
	rm -rf -- "${tmp}"
	return 0
}

if [ "${BASH_SOURCE[0]}" != "$0" ]; then
	( implement_prepush_test_gate_main ) || true
	return 0
fi
case "${1:-run}" in
	run) ( implement_prepush_test_gate_main ) || true ;;
	expected-files) shift; implement_prepush_test_gate_expected_files "$@" || true ;;
	*) implement_prepush_test_gate_log "outcome=error reason=unknown_subcommand" ;;
esac
exit 0
