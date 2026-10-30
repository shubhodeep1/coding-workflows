#!/usr/bin/env bash
# Reproduce a heal's failing tests at a SHA and its last green SHA in the
# credential-free pre-push gate container (plan P2, tracking #8434).
#
# Source plan: docs/plans/heal-evidence-reproduction-and-trends-plan.md, P2
# step 3 and the step 4 mapping table. The callers are the heal intake
# (scripts/workflow_failure_heal_intake.sh) and the heal editor
# (scripts/heal_isolated_implement.sh), both behind
# WORKFLOW_HEAL_REPRODUCE_ENABLED (default false). This script does not read
# that switch: the callers gate on it.
#
# Usage:
#   heal_reproduce.sh --tests-file FILE --baseline SHA --out FILE
#                     (--sha SHA | --worktree DIR) [--repo DIR] [--remote NAME]
#
#   --tests-file  one `path[::name]` per line (blank lines and `#` comments
#                 skipped), the --tests-file list of
#                 scripts/prepush_test_gate_run.sh. Callers write it from the
#                 `pairs` and `files` of extract_failing_tests
#                 (scripts/workflow_failure_heal.py). Regular file, no
#                 symlink, at most 256 KiB.
#   --baseline    last green commit (7-40 lowercase hex).
#   --sha         commit to test; a detached `git worktree` is added for it
#                 and removed on exit.
#   --worktree    an existing work tree to test as-is (its HEAD is the tested
#                 commit; uncommitted edits are included). Never removed.
#   --repo        repository to fetch into and add the worktree from
#                 (default: the --worktree, else $PWD).
#   --remote      remote to fetch from (default origin).
#
# Steps: resolve both commits and, when the merge-base is missing, fetch them
# from --remote (deepening a shallow clone along the fixed ladder 50/200/1000);
# add the detached worktree; build the sandbox image
# (codex_isolated_exec.sh image); prepare dependencies
# (codex_isolated_exec.sh prepare --deps, fail-open); run
# prepush_test_gate_run.sh --tests-file, which runs the list on the tested
# tree and re-runs its failures on the merge-base; map the gate result.
#
# Output (--out, written atomically), schema heal_repro.v1:
#   {"schema": "heal_repro.v1", "status": "reproduced|not_reproduced|unverified",
#    "reason": "<token>", "failing_at": "<40-hex or \"\">",
#    "passing_at": "<40-hex or \"\">",
#    "tests": [{"test": "<line as listed>", "status": "...", "reason": "<token>"}]}
#   Per test: gate `caused` -> reproduced/regression; `inherited` ->
#   not_reproduced/pre_existing; passed at the tested SHA ->
#   not_reproduced/passes_at_failing_sha; unverified, timed out or no
#   merge-base -> unverified/<gate reason> (no_merge_base, timeout, ...).
#   The run is `reproduced` only when every listed test is; any unverified
#   test makes it `unverified`. A test that passes at the failing SHA is never
#   `reproduced`. Test IDs in the output are untrusted data: escape them
#   before rendering.
#
# Env (each defaulted here):
#   WORKFLOW_HEAL_REPRODUCE_TIMEOUT_MINUTES  wall time, default 10 (1-60).
#   WORKFLOW_HEAL_REPRODUCE_TIMEOUT_SECS     optional seconds override
#                                            (5-3600), default unset.
#   On timeout the result is unverified/timeout.
#
# Security: test code runs only inside the existing gate container
# (--network none, read-only root, all capabilities dropped, no environment,
# results copied back as JSON only). The image, prepare and gate steps run
# under an allowlisted `env -i`, so no token reaches them. Every host git call
# disables hooks and fsmonitor. Support files (gate wrapper, gate module,
# codex_isolated_exec.sh) are read from this script's own directory, never
# from the tested tree. Logs carry fixed tokens and counts only.
#
# Always exits 0. Log prefix HEAL_REPRODUCE; the final line is
#   HEAL_REPRODUCE outcome=<status> reason=<reason> tests=<n>
set -uo pipefail

# Keep the caller's git environment from redirecting -C.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR

heal_repro_self="$(realpath -e "${BASH_SOURCE[0]}" 2>/dev/null)" || heal_repro_self="${BASH_SOURCE[0]}"
heal_repro_support="$(dirname "${heal_repro_self}")"
heal_repro_fetch_depths=(50 200 1000)
heal_repro_tests_file_max=262144
heal_repro_tmp_base="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
heal_repro_marker=".heal_repro_parent"

heal_repro_tests_file=""
heal_repro_baseline=""
heal_repro_out=""
heal_repro_sha=""
heal_repro_worktree=""
heal_repro_repo=""
heal_repro_remote="origin"
heal_repro_inner_root=""
heal_repro_inner_deadline=""
heal_repro_usage_error=""

heal_repro_log()
{
	echo "HEAL_REPRODUCE $*" >&2
}

heal_repro_git()
{
	local dir="$1"
	shift
	git -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "${dir}" "$@"
}

# Allowlisted environment for the image, prepare and gate steps (no tokens).
heal_repro_clean_env=(env -i "PATH=${PATH}" "HOME=${HOME:-/tmp}" PYTHONDONTWRITEBYTECODE=1)
for heal_repro_name in RUNNER_TEMP TMPDIR DOCKER_HOST DOCKER_CONFIG SANDBOX_IMAGE_REGISTRY SANDBOX_IMAGE_PULL_TIMEOUT_SECS \
	CODEX_ISOLATED_IMAGE_BUILD_ATTEMPTS CODEX_ISOLATED_IMAGE_BUILD_RETRY_SLEEP_SECS CODEX_VERSION DEPENDENCY_PROXY_ALLOWED_HOSTS; do
	if [ -n "${!heal_repro_name:-}" ]; then
		heal_repro_clean_env+=("${heal_repro_name}=${!heal_repro_name}")
	fi
done

# Python helpers: count, map, check, summary. No repo imports, no bytecode.
IFS= read -r -d '' heal_repro_py <<'PY' || true
import json
import os
import re
import sys

TOKEN_RE = re.compile(r"^[a-z0-9_:.-]{1,64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STATUSES = ("reproduced", "not_reproduced", "unverified")
# Gate limits (workflow-templates/.claude/hooks/prepush_affected_tests.py):
# failing_names keeps at most 50 names from a tail of at most 4096 bytes. A
# tail this close to the cap may have been truncated.
GATE_NAMES_MAX = 50
GATE_TAIL_COMPLETE_BYTES = 4000


def token(value, fallback="invalid_reason"):
	return value if isinstance(value, str) and TOKEN_RE.match(value) else fallback


def entries(path):
	out = []
	try:
		with open(path, "rb") as handle:
			text = handle.read().decode("utf-8", "replace")
	except OSError:
		return out
	for number, raw in enumerate(text.split("\n"), start=1):
		line = raw.strip()
		if not line or line.startswith("#"):
			continue
		test_path, separator, name = line.partition("::")
		out.append((number, line, test_path, name if separator else None))
	return out


def read_sha(path):
	try:
		with open(path, encoding="utf-8") as handle:
			value = handle.read().strip()
	except OSError:
		return ""
	return value if SHA_RE.match(value) else ""


def load_gate(path):
	if not path:
		return None
	try:
		with open(path, encoding="utf-8") as handle:
			data = json.load(handle)
	except (OSError, ValueError):
		return None
	if not isinstance(data, dict) or data.get("status") not in ("pass", "fail", "unverified", "skipped"):
		return None
	return data


def items(data, key):
	value = data.get(key)
	return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def map_entry(data, number, test_path, name):
	unverified = items(data, "unverified")
	for item in unverified:
		if item.get("item") == f"tests_file:line{number}":
			return "unverified", token(item.get("reason"))
	for item in unverified:
		if item.get("item") == test_path:
			return "unverified", token(item.get("reason"))
	for item in items(data, "failures"):
		if item.get("test") == test_path:
			names = [n for n in (item.get("names") or []) if isinstance(n, str)]
			if name is not None and names:
				node = f"{test_path}::{name}"
				if not any(n == node or n.startswith(node + "[") for n in names):
					# Absence from the gate's parsed names proves a pass only when
					# that list is complete: the gate keeps the last TAIL_BYTES
					# (4096) of output, at most 50 names, and matches only FAILED
					# lines, so an `ERROR <node>` setup/teardown error is missed.
					tail = item.get("tail") if isinstance(item.get("tail"), str) else ""
					if re.search(r"\bERROR\s+" + re.escape(node) + r"(?:\[|\s|$)", tail, re.M):
						return "reproduced", "regression"
					if not tail or len(names) >= GATE_NAMES_MAX or len(tail.encode("utf-8")) >= GATE_TAIL_COMPLETE_BYTES:
						return "unverified", "names_incomplete"
					# The named test passed at the failing SHA (a sibling failed).
					return "not_reproduced", "passes_at_failing_sha"
			return "reproduced", "regression"
	for item in items(data, "inherited"):
		if item.get("test") == test_path:
			# Inherited entries carry no output tail, so a name missing from
			# their parsed names proves nothing; keep the unit-level verdict.
			return "not_reproduced", "pre_existing"
	ran = data.get("ran") if isinstance(data.get("ran"), list) else []
	if test_path in ran:
		return "not_reproduced", "passes_at_failing_sha"
	for item in unverified:
		# Whole-list rejection (tests_file_unreadable, _too_large, _too_many_entries).
		if item.get("item") == "tests_file":
			return "unverified", token(item.get("reason"))
	if data.get("reason"):
		return "unverified", token(data.get("reason"))
	return "unverified", "not_run"


def cmd_map(tests_file, gate_file, forced, failing_file, passing_file, out):
	listed = entries(tests_file)
	tests = []
	if forced:
		forced = token(forced)
		tests = [{"test": line, "status": "unverified", "reason": forced} for _, line, _, _ in listed]
	else:
		data = load_gate(gate_file)
		gate_item = None
		if data is not None:
			for item in items(data, "unverified"):
				if item.get("item") in ("gate", "merge_base"):
					gate_item = item
					break
		for number, line, test_path, name in listed:
			if data is None:
				status, reason = "unverified", "gate_result_invalid"
			elif gate_item is not None:
				status, reason = "unverified", token(gate_item.get("reason"))
			else:
				status, reason = map_entry(data, number, test_path, name)
			tests.append({"test": line, "status": status, "reason": reason})
	if forced or not tests:
		status, reason = "unverified", forced or "no_tests"
	else:
		bad = [t for t in tests if t["status"] == "unverified"]
		if bad:
			status, reason = "unverified", bad[0]["reason"]
		elif all(t["status"] == "reproduced" for t in tests):
			status, reason = "reproduced", "regression"
		else:
			first = next(t for t in tests if t["status"] != "reproduced")
			status, reason = "not_reproduced", first["reason"]
	result = {"schema": "heal_repro.v1", "status": status, "reason": reason,
		"failing_at": read_sha(failing_file), "passing_at": read_sha(passing_file), "tests": tests}
	tmp = out + ".tmp"
	with open(tmp, "w", encoding="utf-8") as handle:
		json.dump(result, handle)
		handle.write("\n")
	os.replace(tmp, out)


def load_result(path):
	try:
		with open(path, encoding="utf-8") as handle:
			data = json.load(handle)
	except (OSError, ValueError):
		return None
	if not isinstance(data, dict) or data.get("schema") != "heal_repro.v1" or data.get("status") not in STATUSES:
		return None
	if not isinstance(data.get("tests"), list):
		return None
	return data


def main(argv):
	command = argv[0] if argv else ""
	if command == "count":
		print(len(entries(argv[1])))
	elif command == "map":
		cmd_map(*argv[1:7])
	elif command == "check":
		return 0 if load_result(argv[1]) is not None else 1
	elif command == "summary":
		data = load_result(argv[1])
		if data is None:
			print("unverified result_invalid 0")
		else:
			print(data["status"], token(data.get("reason")), len(data["tests"]))
	else:
		return 2
	return 0


sys.exit(main(sys.argv[1:]))
PY

heal_repro_py_run()
{
	PYTHONDONTWRITEBYTECODE=1 python3 -I -c "${heal_repro_py}" "$@"
}

while [ "$#" -gt 0 ]; do
	case "$1" in
		--tests-file|--baseline|--out|--sha|--worktree|--repo|--remote|--inner-root|--inner-deadline)
			if [ "$#" -lt 2 ]; then
				heal_repro_usage_error="usage_error"
				shift
				continue
			fi
			case "$1" in
				--tests-file) heal_repro_tests_file="$2" ;;
				--baseline) heal_repro_baseline="$2" ;;
				--out) heal_repro_out="$2" ;;
				--sha) heal_repro_sha="$2" ;;
				--worktree) heal_repro_worktree="$2" ;;
				--repo) heal_repro_repo="$2" ;;
				--remote) heal_repro_remote="$2" ;;
				--inner-root) heal_repro_inner_root="$2" ;;
				--inner-deadline) heal_repro_inner_deadline="$2" ;;
			esac
			shift 2
			;;
		*) heal_repro_usage_error="usage_error"; shift ;;
	esac
done

if [ -z "${heal_repro_repo}" ]; then
	heal_repro_repo="${heal_repro_worktree:-${PWD}}"
fi

# ---------------------------------------------------------------------------
# Inner pass: runs under the parent's `timeout`; writes ROOT/repro.json.
# ---------------------------------------------------------------------------
heal_repro_inner_finish()
{
	local reason="$1" gate_file="${2:-}"
	heal_repro_py_run map "${heal_repro_inner_root}/tests.txt" "${gate_file}" "${reason}" \
		"${heal_repro_inner_root}/failing_at" "${heal_repro_inner_root}/passing_at" "${heal_repro_inner_root}/repro.json" \
		|| heal_repro_log "phase=map outcome=failed"
	exit 0
}

heal_repro_resolve()
{
	heal_repro_git "$1" rev-parse --verify --quiet "$2^{commit}" 2>/dev/null
}

heal_repro_inner()
{
	local root="${heal_repro_inner_root}" root_real base_real tested="" passing="" mb="" depth rc
	local tree image prep="" module="" wrapper="${heal_repro_support}/prepush_test_gate_run.sh"
	local exec_helper="${heal_repro_support}/codex_isolated_exec.sh" now minutes gate_status
	local fetch_args shallow depths
	root_real="$(realpath -e "${root}" 2>/dev/null)" || exit 0
	base_real="$(realpath -e "${heal_repro_tmp_base}" 2>/dev/null)" || exit 0
	case "${root_real}" in
		"${base_real}"/heal-repro.*) ;;
		*) heal_repro_log "outcome=error reason=inner_root_refused"; exit 0 ;;
	esac
	if [ ! -f "${root_real}/${heal_repro_marker}" ] || [ -L "${root_real}/${heal_repro_marker}" ] || [ ! -O "${root_real}/${heal_repro_marker}" ]; then
		heal_repro_log "outcome=error reason=inner_root_refused"
		exit 0
	fi
	heal_repro_inner_root="${root_real}"
	root="${root_real}"
	if ! [[ "${heal_repro_inner_deadline}" =~ ^[0-9]{1,12}$ ]]; then
		heal_repro_inner_deadline=$(( $(date +%s) + 600 ))
	fi

	# 1. Support files come from this script's directory only.
	if [ -f "${heal_repro_support}/prepush_affected_tests.py" ]; then
		module="${heal_repro_support}/prepush_affected_tests.py"
	elif [ -f "${heal_repro_support}/../workflow-templates/.claude/hooks/prepush_affected_tests.py" ]; then
		module="$(realpath -e "${heal_repro_support}/../workflow-templates/.claude/hooks/prepush_affected_tests.py" 2>/dev/null)" || module=""
	fi
	if [ -z "${module}" ] || [ ! -f "${wrapper}" ] || [ ! -f "${exec_helper}" ]; then
		heal_repro_inner_finish support_missing
	fi

	# 2. Resolve both commits; fetch only when the merge-base is missing.
	if [ -n "${heal_repro_worktree}" ]; then
		if ! heal_repro_git "${heal_repro_worktree}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
			heal_repro_inner_finish worktree_missing
		fi
	fi
	heal_repro_tested_commit()
	{
		if [ -n "${heal_repro_worktree}" ]; then
			heal_repro_git "${heal_repro_worktree}" rev-parse --verify --quiet "HEAD^{commit}" 2>/dev/null
		else
			heal_repro_resolve "${heal_repro_repo}" "${heal_repro_sha}"
		fi
	}
	heal_repro_try_merge_base()
	{
		tested="$(heal_repro_tested_commit)" || tested=""
		passing="$(heal_repro_resolve "${heal_repro_repo}" "${heal_repro_baseline}")" || passing=""
		mb=""
		if [ -n "${tested}" ] && [ -n "${passing}" ]; then
			mb="$(heal_repro_git "${heal_repro_repo}" merge-base "${passing}" "${tested}" 2>/dev/null)" || mb=""
		fi
		[[ "${mb}" =~ ^[0-9a-f]{40}$ ]]
	}
	if ! heal_repro_try_merge_base; then
		shallow="$(heal_repro_git "${heal_repro_repo}" rev-parse --is-shallow-repository 2>/dev/null)" || shallow=""
		if [ "${shallow}" = true ]; then
			depths=("${heal_repro_fetch_depths[@]}")
		else
			# Never pass --depth to a complete clone: it would make it shallow.
			depths=(full)
		fi
		for depth in "${depths[@]}"; do
			fetch_args=(fetch --quiet --no-tags)
			[ "${depth}" = full ] || fetch_args+=(--depth "${depth}")
			fetch_args+=(-- "${heal_repro_remote}")
			[ -n "${heal_repro_worktree}" ] || fetch_args+=("${heal_repro_sha}")
			fetch_args+=("${heal_repro_baseline}")
			rc=0
			timeout --kill-after=10s 120 git -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "${heal_repro_repo}" \
				"${fetch_args[@]}" </dev/null >/dev/null 2>&1 || rc=$?
			if [ "${rc}" -eq 0 ]; then
				heal_repro_log "phase=fetch outcome=ok depth=${depth}"
			else
				heal_repro_log "phase=fetch outcome=failed depth=${depth}"
			fi
			heal_repro_try_merge_base && break
		done
	fi
	[[ "${tested}" =~ ^[0-9a-f]{40}$ ]] && printf '%s\n' "${tested}" > "${root}/failing_at"
	[[ "${passing}" =~ ^[0-9a-f]{40}$ ]] && printf '%s\n' "${passing}" > "${root}/passing_at"
	if [ -z "${tested}" ] || [ -z "${passing}" ]; then
		heal_repro_inner_finish fetch_failed
	fi
	if ! [[ "${mb}" =~ ^[0-9a-f]{40}$ ]]; then
		heal_repro_inner_finish no_merge_base
	fi

	# 3. Detached worktree (recorded first, so the parent removes it even
	# after a SIGKILL of this pass).
	if [ -n "${heal_repro_worktree}" ]; then
		tree="$(realpath -e "${heal_repro_worktree}" 2>/dev/null)" || heal_repro_inner_finish worktree_missing
	else
		tree="${root}/tree"
		printf '%s\n' "${tree}" > "${root}/worktree_added"
		if ! heal_repro_git "${heal_repro_repo}" worktree add --quiet --detach "${tree}" "${tested}" </dev/null >/dev/null 2>&1; then
			heal_repro_log "phase=worktree outcome=failed"
			heal_repro_inner_finish worktree_failed
		fi
		heal_repro_log "phase=worktree outcome=ok"
	fi

	# 4. Sandbox image.
	image="$("${heal_repro_clean_env[@]}" bash "${exec_helper}" image </dev/null | tail -n 1)" || image=""
	if ! [[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
		heal_repro_log "phase=image outcome=unavailable"
		heal_repro_inner_finish image_unavailable
	fi

	# 5. Dependencies (fail-open: missing pytest makes tests unverified).
	prep="$("${heal_repro_clean_env[@]}" bash "${exec_helper}" prepare --workdir "${tree}" --deps </dev/null | tail -n 1)" || prep=""
	if [[ "${prep}" == /* ]] && [ -d "${prep}" ]; then
		printf '%s\n' "${prep}" > "${root}/prepare_root"
		heal_repro_log "phase=deps outcome=ok"
	else
		prep=""
		heal_repro_log "phase=deps outcome=unavailable"
	fi

	# 6. Gate: leave 180 s for the wrapper's overhead, kill grace and slack.
	now="$(date +%s)"
	minutes=$(( (heal_repro_inner_deadline - now - 180) / 60 ))
	[ "${minutes}" -ge 1 ] || minutes=1
	local gate_args=(--worktree "${tree}" --module "${module}" --image "${image}" --base "${passing}"
		--out-dir "${root}/gate" --timeout-minutes "${minutes}" --tests-file "${root}/tests.txt")
	if [ -n "${prep}" ] && [ -d "${prep}/venv/bin" ]; then
		gate_args+=(--venv "${prep}/venv")
	fi
	if [ -n "${prep}" ] && [ -d "${prep}/work/node_modules" ] && [ ! -L "${prep}/work/node_modules" ]; then
		gate_args+=(--node-modules "${prep}/work/node_modules")
	fi
	if [ -n "${prep}" ] && [ -d "${prep}/work" ] && [ ! -L "${prep}/work" ]; then
		gate_args+=(--nested-node-modules-root "${prep}/work")
	fi
	"${heal_repro_clean_env[@]}" bash "${wrapper}" "${gate_args[@]}" </dev/null || true
	gate_status="$(python3 -I -c 'import json,sys
try:
	d = json.load(open(sys.argv[1]))
	s = d.get("status") if isinstance(d, dict) else None
except Exception:
	s = None
print(s if s in ("pass", "fail", "unverified", "skipped") else "invalid")' "${root}/gate/result.json" 2>/dev/null)" || gate_status="invalid"
	heal_repro_log "phase=gate outcome=${gate_status}"

	# 7. Map.
	heal_repro_inner_finish "" "${root}/gate/result.json"
}

if [ -n "${heal_repro_inner_root}" ]; then
	heal_repro_inner
	exit 0
fi

# ---------------------------------------------------------------------------
# Parent: validate, private root, cleanup trap, outer timeout, final output.
# ---------------------------------------------------------------------------
heal_repro_root=""
heal_repro_child=""

heal_repro_cleanup()
{
	local tree prep added=""
	[ -n "${heal_repro_root}" ] && [ -d "${heal_repro_root}" ] || return 0
	if [ -f "${heal_repro_root}/worktree_added" ]; then
		added=1
		tree="$(head -n 1 "${heal_repro_root}/worktree_added")"
		if [ "${tree}" = "${heal_repro_root}/tree" ] && [ -e "${tree}" ]; then
			heal_repro_git "${heal_repro_repo}" worktree remove --force "${tree}" >/dev/null 2>&1 \
				|| heal_repro_log "phase=cleanup outcome=failed step=worktree"
		fi
	fi
	if [ -f "${heal_repro_root}/prepare_root" ]; then
		prep="$(head -n 1 "${heal_repro_root}/prepare_root")"
		if [[ "${prep}" == /* ]] && [ -d "${prep}" ]; then
			"${heal_repro_clean_env[@]}" bash "${heal_repro_support}/codex_isolated_exec.sh" cleanup --root "${prep}" </dev/null >/dev/null 2>&1 \
				|| heal_repro_log "phase=cleanup outcome=failed step=prepare"
		fi
	fi
	# Only this run's private temp root is removed.
	rm -rf -- "${heal_repro_root}" 2>/dev/null || heal_repro_log "phase=cleanup outcome=failed step=root"
	# Prune only when this run added a worktree (never touch unrelated entries).
	if [ -n "${added}" ] && [ -n "${heal_repro_repo}" ] && [ -d "${heal_repro_repo}" ]; then
		heal_repro_git "${heal_repro_repo}" worktree prune >/dev/null 2>&1 || heal_repro_log "phase=cleanup outcome=failed step=prune"
	fi
	heal_repro_root=""
}

heal_repro_on_signal()
{
	if [ -n "${heal_repro_child}" ]; then
		kill -TERM "${heal_repro_child}" 2>/dev/null || true
		wait "${heal_repro_child}" 2>/dev/null || true
	fi
	exit 143
}

# Write --out atomically from a mapped result file.
heal_repro_publish()
{
	local src="$1" out_dir tmp summary status reason count
	out_dir="$(dirname -- "${heal_repro_out}")"
	mkdir -p -- "${out_dir}" 2>/dev/null || true
	tmp="$(mktemp "${out_dir}/.heal_repro.XXXXXXXX" 2>/dev/null)" || tmp=""
	if [ -n "${tmp}" ] && cat -- "${src}" > "${tmp}" && mv -f -- "${tmp}" "${heal_repro_out}"; then
		:
	else
		[ -z "${tmp}" ] || rm -f -- "${tmp}"
		heal_repro_log "outcome=error reason=out_unwritable"
	fi
	summary="$(heal_repro_py_run summary "${src}" 2>/dev/null)" || summary="unverified result_invalid 0"
	read -r status reason count <<<"${summary}"
	heal_repro_log "outcome=${status:-unverified} reason=${reason:-result_invalid} tests=${count:-0}"
}

# Result for a run that never reached the inner pass (no private root).
heal_repro_publish_early()
{
	local reason="$1" scratch
	if [ -z "${heal_repro_out}" ]; then
		heal_repro_log "outcome=unverified reason=${reason} tests=0"
		return 0
	fi
	scratch="$(mktemp -d "${heal_repro_tmp_base}/heal-repro-early.XXXXXXXX" 2>/dev/null)" || {
		heal_repro_log "outcome=unverified reason=${reason} tests=0"
		return 0
	}
	: > "${scratch}/tests.txt"
	if [ -f "${heal_repro_tests_file}" ] && [ ! -L "${heal_repro_tests_file}" ] \
		&& [ "$(stat -c %s "${heal_repro_tests_file}" 2>/dev/null || echo 0)" -le "${heal_repro_tests_file_max}" ]; then
		cp -- "${heal_repro_tests_file}" "${scratch}/tests.txt" 2>/dev/null || : > "${scratch}/tests.txt"
	fi
	heal_repro_py_run map "${scratch}/tests.txt" "" "${reason}" "${scratch}/none" "${scratch}/none" "${scratch}/repro.json" \
		&& heal_repro_publish "${scratch}/repro.json"
	rm -rf -- "${scratch}"
}

if [ -z "${heal_repro_out}" ]; then
	heal_repro_usage_error="usage_error"
fi
if [ -z "${heal_repro_usage_error}" ]; then
	if [ -z "${heal_repro_tests_file}" ] || [ -z "${heal_repro_baseline}" ]; then
		heal_repro_usage_error="usage_error"
	elif [ -n "${heal_repro_sha}" ] && [ -n "${heal_repro_worktree}" ]; then
		heal_repro_usage_error="usage_error"
	elif [ -z "${heal_repro_sha}" ] && [ -z "${heal_repro_worktree}" ]; then
		heal_repro_usage_error="usage_error"
	elif ! [[ "${heal_repro_baseline}" =~ ^[0-9a-f]{7,40}$ ]]; then
		heal_repro_usage_error="invalid_sha"
	elif [ -n "${heal_repro_sha}" ] && ! [[ "${heal_repro_sha}" =~ ^[0-9a-f]{7,40}$ ]]; then
		heal_repro_usage_error="invalid_sha"
	elif ! [[ "${heal_repro_remote}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$ ]]; then
		heal_repro_usage_error="invalid_remote"
	elif [ ! -d "${heal_repro_repo}" ]; then
		heal_repro_usage_error="repo_missing"
	fi
fi
if [ -n "${heal_repro_usage_error}" ]; then
	heal_repro_log "phase=config outcome=${heal_repro_usage_error}"
	heal_repro_publish_early "${heal_repro_usage_error}"
	exit 0
fi

heal_repro_minutes="${WORKFLOW_HEAL_REPRODUCE_TIMEOUT_MINUTES:-10}"
if ! [[ "${heal_repro_minutes}" =~ ^[0-9]{1,4}$ ]] || [ "$((10#${heal_repro_minutes}))" -lt 1 ] || [ "$((10#${heal_repro_minutes}))" -gt 60 ]; then
	heal_repro_log "phase=config outcome=config_invalid name=WORKFLOW_HEAL_REPRODUCE_TIMEOUT_MINUTES fallback=10"
	heal_repro_minutes=10
fi
# Force base 10: bash arithmetic reads a leading zero (08) as octal.
heal_repro_wall_secs=$(( 10#${heal_repro_minutes} * 60 ))
heal_repro_secs="${WORKFLOW_HEAL_REPRODUCE_TIMEOUT_SECS:-}"
if [ -n "${heal_repro_secs}" ]; then
	if [[ "${heal_repro_secs}" =~ ^[0-9]{1,5}$ ]] && [ "$((10#${heal_repro_secs}))" -ge 5 ] && [ "$((10#${heal_repro_secs}))" -le 3600 ]; then
		heal_repro_wall_secs=$((10#${heal_repro_secs}))
	else
		heal_repro_log "phase=config outcome=config_invalid name=WORKFLOW_HEAL_REPRODUCE_TIMEOUT_SECS fallback=minutes"
	fi
fi

heal_repro_root="$(mktemp -d "${heal_repro_tmp_base}/heal-repro.XXXXXXXX" 2>/dev/null)" || heal_repro_root=""
if [ -z "${heal_repro_root}" ]; then
	heal_repro_publish_early tmpdir_failed
	exit 0
fi
heal_repro_root="$(realpath -e "${heal_repro_root}")"
trap heal_repro_cleanup EXIT
trap heal_repro_on_signal INT TERM
chmod 0700 "${heal_repro_root}"
: > "${heal_repro_root}/${heal_repro_marker}"

# Freeze the test list in the private root (same limits as the gate wrapper).
heal_repro_list_reason=""
if [ -L "${heal_repro_tests_file}" ]; then
	heal_repro_list_reason="tests_file_not_regular"
elif [ ! -e "${heal_repro_tests_file}" ]; then
	heal_repro_list_reason="tests_file_missing"
elif [ ! -f "${heal_repro_tests_file}" ]; then
	heal_repro_list_reason="tests_file_not_regular"
elif [ "$(stat -c %s "${heal_repro_tests_file}" 2>/dev/null || echo 0)" -gt "${heal_repro_tests_file_max}" ]; then
	heal_repro_list_reason="tests_file_too_large"
elif ! cp -- "${heal_repro_tests_file}" "${heal_repro_root}/tests.txt" 2>/dev/null; then
	heal_repro_list_reason="tests_file_unreadable"
elif [ "$(stat -c %s "${heal_repro_root}/tests.txt" 2>/dev/null || echo 999999999)" -gt "${heal_repro_tests_file_max}" ]; then
	heal_repro_list_reason="tests_file_too_large"
fi
if [ -n "${heal_repro_list_reason}" ]; then
	: > "${heal_repro_root}/tests.txt"
	heal_repro_py_run map "${heal_repro_root}/tests.txt" "" "${heal_repro_list_reason}" \
		"${heal_repro_root}/failing_at" "${heal_repro_root}/passing_at" "${heal_repro_root}/final.json"
	heal_repro_publish "${heal_repro_root}/final.json"
	exit 0
fi
chmod 0444 "${heal_repro_root}/tests.txt"
heal_repro_count="$(heal_repro_py_run count "${heal_repro_root}/tests.txt" 2>/dev/null)" || heal_repro_count=0
if [ "${heal_repro_count}" = 0 ]; then
	heal_repro_py_run map "${heal_repro_root}/tests.txt" "" "" \
		"${heal_repro_root}/failing_at" "${heal_repro_root}/passing_at" "${heal_repro_root}/final.json"
	heal_repro_publish "${heal_repro_root}/final.json"
	exit 0
fi

heal_repro_deadline=$(( $(date +%s) + heal_repro_wall_secs ))
heal_repro_inner_args=(--inner-root "${heal_repro_root}" --inner-deadline "${heal_repro_deadline}"
	--baseline "${heal_repro_baseline}" --repo "${heal_repro_repo}" --remote "${heal_repro_remote}"
	--tests-file "${heal_repro_root}/tests.txt" --out "${heal_repro_root}/repro.json")
if [ -n "${heal_repro_worktree}" ]; then
	heal_repro_inner_args+=(--worktree "${heal_repro_worktree}")
else
	heal_repro_inner_args+=(--sha "${heal_repro_sha}")
fi
heal_repro_rc=0
# `timeout` signals the inner pass's whole process group on expiry; the
# parent stays outside it so its EXIT trap always cleans up.
timeout --kill-after=30s "${heal_repro_wall_secs}" bash "${heal_repro_self}" "${heal_repro_inner_args[@]}" </dev/null &
heal_repro_child=$!
wait "${heal_repro_child}" || heal_repro_rc=$?
heal_repro_child=""

if [ "${heal_repro_rc}" -eq 124 ] || [ "${heal_repro_rc}" -eq 137 ]; then
	heal_repro_log "phase=wall outcome=timeout secs=${heal_repro_wall_secs}"
	heal_repro_py_run map "${heal_repro_root}/tests.txt" "" timeout \
		"${heal_repro_root}/failing_at" "${heal_repro_root}/passing_at" "${heal_repro_root}/final.json"
elif heal_repro_py_run check "${heal_repro_root}/repro.json" 2>/dev/null; then
	cp -- "${heal_repro_root}/repro.json" "${heal_repro_root}/final.json"
else
	heal_repro_py_run map "${heal_repro_root}/tests.txt" "" inner_failed \
		"${heal_repro_root}/failing_at" "${heal_repro_root}/passing_at" "${heal_repro_root}/final.json"
fi
heal_repro_publish "${heal_repro_root}/final.json"
exit 0
