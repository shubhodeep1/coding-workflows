#!/usr/bin/env bash
# Run the pre-push affected-test gate in a credential-free container (issue #7137).
#
# Host-side wrapper for workflow-templates/.claude/hooks/prepush_affected_tests.py.
# Plan phase P1 ships it; the implement, review autofix, conflict resolver,
# heal and integration-repair callers (P3-P5) invoke it. It:
#   1. refuses a gate module that lives inside the tested worktree (a PR must
#      not be able to change the code that gates it);
#   2. lists the changed files against the merge-base with --base (plus
#      untracked files) using the host checkout's git;
#   3. exports the merge-base tree with checkout-index from a temporary
#      index (baseline for the inherited-failure classification);
#   4. builds the PR tree from git the same way: the tracked files (HEAD
#      plus staged and unstaged edits and deletions) through a temporary
#      index, and untracked files only when review_untrusted_workspace.py's
#      credential filter admits them (no .env*, keys or credential stores);
#      .git is never copied;
#   5. runs the module in `docker run --network none --read-only --cap-drop ALL
#      --security-opt no-new-privileges` with no environment from the runner,
#      the PR tree read-write and the module, baseline and dependency roots
#      read-only, under `timeout --kill-after=30s`;
#   6. copies back only result.json and summary.md (regular files, 1 MiB cap).
#
# Usage:
#   prepush_test_gate_run.sh --worktree DIR --module FILE --image IMAGE \
#     --base SHA --out-dir DIR [--timeout-minutes N] [--deps-root DIR]... \
#     [--venv DIR] [--shards N] [--large-file-tests N] [--node-modules DIR] \
#     [--nested-node-modules-root DIR] [--tests-only FILE] [--tests-file F] \
#     [--expect-baseline-fail-file F]
#
# --tests-only (issue #8461, the heal reproduction runner) runs an explicit
# list of test files (one `path[::name]` per
# line) instead of the module's affected-test selection; failures are still
# re-run on the merge-base. The file must be a regular file (no symlink) of
# at most 256 KiB. It is copied into the private run root and mounted
# read-only at /gate/tests.txt, and the module validates every line. A bad
# file gives a synthetic tests_file_missing / tests_file_not_regular /
# tests_file_unreadable / tests_file_too_large / tests_file_copy_failed result; it never falls back
# to the selection.
#
# --tests-file and --expect-baseline-fail-file (issue #8427, P3) pass a plan's
# done-check targets (one `path[::name]` per line) to the module's `run`:
# --tests-file adds them to the affected-test selection, and
# --expect-baseline-fail-file also re-runs them on the baseline tree. Each
# must be a regular, non-symlink file of at most 65,536 bytes; it is copied
# into a private inputs directory mounted read-only at /inputs. A rejected
# file is logged (tests_file_rejected) and the run goes ahead without it.
# All three options add an `acceptance` map to the result.
# Without these options the docker argv is unchanged.
#
# --nested-node-modules-root mounts each DIR/<project>/node_modules (the
# nested Node projects that codex_isolated_exec.sh prepare --deps installs,
# at most 6, depth 2-6, hidden directories skipped) read-only at
# /source/<project>/node_modules and /baseline/<project>/node_modules.
#
# --node-modules mounts a prepared node_modules directory (the Node prep root
# of codex_isolated_exec.sh prepare --deps) read-only at /source/node_modules
# and /baseline/node_modules, because the PR tree holds no untracked node_modules and
# Node resolves packages from the project directory upward (issue #8190).
#
# --venv mounts a dependency venv read-only at /opt/codex-venv (where
# codex_isolated_exec.sh prepare --deps builds it, so its absolute shebangs
# work) and puts its bin first on PATH. --shards and --large-file-tests
# forward PREPUSH_TEST_GATE_SHARDS / PREPUSH_TEST_GATE_LARGE_FILE_TESTS to the
# module, because the container gets no environment from the runner
# (issue #8190). Values that are not plain digits are dropped.
#
# Always exits 0: the outcome is in <out-dir>/result.json (schema
# prepush_test_gate.v1). Failures of the wrapper itself produce a synthetic
# {"status": "unverified", "reason": ...} result. Log prefix PREPUSH_TEST_GATE.
set -uo pipefail

worktree=""
module=""
image=""
base=""
out_dir=""
timeout_minutes="${PREPUSH_TEST_GATE_TIMEOUT_MINUTES:-15}"
deps_roots=()
venv=""
node_modules=""
nested_nm_root=""
shards=""
large_file_tests=""
# tests_file / tests_file_given / tests_file_real: the --tests-only list.
tests_file=""
tests_file_given=0
done_check_tests_file=""
done_check_tests_file_given=0
baseline_fail_file=""
baseline_fail_file_given=0

gate_log()
{
	echo "PREPUSH_TEST_GATE phase=wrapper $*" >&2
}

synthetic_result()
{
	local reason="$1"
	gate_log "outcome=unverified reason=${reason}"
	[ -n "${out_dir}" ] || return 0
	mkdir -p "${out_dir}" 2>/dev/null || return 0
	printf '{"schema": "prepush_test_gate.v1", "status": "unverified", "reason": "%s", "ran": [], "failures": [], "inherited": [], "unverified": [{"item": "gate", "reason": "%s"}], "changed_assertions": [], "selected": 0, "elapsed_secs": 0}\n' "${reason}" "${reason}" > "${out_dir}/result.json"
	printf '## Pre-push test gate\n\n- Status: **unverified** (%s)\n' "${reason}" > "${out_dir}/summary.md"
}

while [ "$#" -gt 0 ]; do
	case "$1" in
		--worktree) worktree="${2:-}"; shift 2 ;;
		--module) module="${2:-}"; shift 2 ;;
		--image) image="${2:-}"; shift 2 ;;
		--base) base="${2:-}"; shift 2 ;;
		--out-dir) out_dir="${2:-}"; shift 2 ;;
		--timeout-minutes) timeout_minutes="${2:-}"; shift 2 ;;
		--deps-root) deps_roots+=("${2:-}"); shift 2 ;;
		--venv) venv="${2:-}"; shift 2 ;;
		--node-modules) node_modules="${2:-}"; shift 2 ;;
		--nested-node-modules-root) nested_nm_root="${2:-}"; shift 2 ;;
		--shards) shards="${2:-}"; shift 2 ;;
		--large-file-tests) large_file_tests="${2:-}"; shift 2 ;;
		--tests-only) [ "$#" -ge 2 ] || { synthetic_result usage_error; exit 0; }; tests_file="${2:-}"; tests_file_given=1; shift 2 ;;
		--tests-file) [ "$#" -ge 2 ] || { synthetic_result usage_error; exit 0; }; done_check_tests_file="${2:-}"; done_check_tests_file_given=1; shift 2 ;;
		--expect-baseline-fail-file) [ "$#" -ge 2 ] || { synthetic_result usage_error; exit 0; }; baseline_fail_file="${2:-}"; baseline_fail_file_given=1; shift 2 ;;
		*) gate_log "outcome=error reason=unknown_argument"; synthetic_result usage_error; exit 0 ;;
	esac
done

if [ -z "${worktree}" ] || [ -z "${module}" ] || [ -z "${image}" ] || [ -z "${base}" ] || [ -z "${out_dir}" ]; then
	synthetic_result usage_error
	exit 0
fi
if ! [[ "${base}" =~ ^[0-9a-f]{7,40}$ ]]; then
	synthetic_result invalid_base
	exit 0
fi
if ! [[ "${timeout_minutes}" =~ ^[0-9]+$ ]] || [ "${timeout_minutes}" -lt 1 ] || [ "${timeout_minutes}" -gt 360 ]; then
	gate_log "outcome=config_invalid name=PREPUSH_TEST_GATE_TIMEOUT_MINUTES fallback=15"
	timeout_minutes=15
fi
# Force base 10: bash arithmetic reads a leading zero (08, 030) as octal.
timeout_minutes=$((10#${timeout_minutes}))
if [ -n "${shards}" ] && { ! [[ "${shards}" =~ ^[0-9]+$ ]] || [ "${shards}" -lt 1 ] || [ "${shards}" -gt 64 ]; }; then
	gate_log "outcome=config_invalid name=PREPUSH_TEST_GATE_SHARDS fallback=module_default"
	shards=""
fi
if [ -n "${large_file_tests}" ] && { ! [[ "${large_file_tests}" =~ ^[0-9]+$ ]] || [ "${large_file_tests}" -lt 1 ] || [ "${large_file_tests}" -gt 100000 ]; }; then
	gate_log "outcome=config_invalid name=PREPUSH_TEST_GATE_LARGE_FILE_TESTS fallback=module_default"
	large_file_tests=""
fi
mkdir -p "${out_dir}" || { gate_log "outcome=error reason=out_dir_unwritable"; exit 0; }

worktree_real="$(realpath -e "${worktree}" 2>/dev/null)" || { synthetic_result worktree_missing; exit 0; }
module_real="$(realpath -e "${module}" 2>/dev/null)" || { synthetic_result module_absent; exit 0; }
case "${module_real}/" in
	"${worktree_real}/"*)
		gate_log "outcome=error reason=module_untrusted"
		synthetic_result module_untrusted
		exit 0
		;;
esac
if [ ! -f "${module_real}" ] || [ -L "${module}" ]; then
	synthetic_result module_not_regular
	exit 0
fi
tests_file_real=""
if [ "${tests_file_given}" -eq 1 ]; then
	# An explicit test list never falls back to the affected-test selection.
	if [ -z "${tests_file}" ]; then
		synthetic_result usage_error
		exit 0
	fi
	if [ -L "${tests_file}" ]; then
		synthetic_result tests_file_not_regular
		exit 0
	fi
	tests_file_real="$(realpath -e "${tests_file}" 2>/dev/null)" || { synthetic_result tests_file_missing; exit 0; }
	if [ ! -f "${tests_file_real}" ]; then
		synthetic_result tests_file_not_regular
		exit 0
	fi
	tests_file_size="$(stat -c %s "${tests_file_real}" 2>/dev/null)" || tests_file_size=""
	if ! [[ "${tests_file_size}" =~ ^[0-9]+$ ]]; then
		synthetic_result tests_file_unreadable
		exit 0
	fi
	if [ "${tests_file_size}" -gt 262144 ]; then
		synthetic_result tests_file_too_large
		exit 0
	fi
fi
support="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "${support}/review_untrusted_workspace.py" ]; then
	synthetic_result support_missing
	exit 0
fi
if ! command -v docker >/dev/null 2>&1; then
	synthetic_result docker_unavailable
	exit 0
fi

root="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/prepush-gate.XXXXXXXX")" || { synthetic_result tmpdir_failed; exit 0; }
container="prepush-gate-${GITHUB_RUN_ID:-local}-$$"
cleanup()
{
	env -i PATH="${PATH}" docker rm -f "${container}" >/dev/null 2>&1 || true
	rm -rf -- "${root}"
}
trap cleanup EXIT
mkdir -m 0700 "${root}/results" "${root}/baseline"
mkdir -m 0755 "${root}/source"
# Copy the test list into the private root: the content is frozen after
# validation and caller path characters never reach the CSV --mount value.
if [ -n "${tests_file_real}" ] && ! install -m 0444 "${tests_file_real}" "${root}/tests.txt" 2>/dev/null; then
	synthetic_result tests_file_copy_failed
	exit 0
fi

git_safe=(git -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "${worktree_real}")
merge_base="$("${git_safe[@]}" merge-base "${base}" HEAD 2>/dev/null)" || merge_base=""
if ! [[ "${merge_base}" =~ ^[0-9a-f]{40}$ ]]; then
	synthetic_result no_merge_base
	exit 0
fi
(
	# Worktree against the merge-base: committed, staged and unstaged edits
	# (and deletions) of tracked files all drive selection.
	"${git_safe[@]}" diff --name-status -M "${merge_base}" || exit 1
	"${git_safe[@]}" ls-files --others --exclude-standard | sed 's/^/A\t/'
) > "${root}/results/changed.txt" 2>/dev/null || { synthetic_result git_diff_failed; exit 0; }
host_git_dir="$("${git_safe[@]}" rev-parse --absolute-git-dir 2>/dev/null)" || host_git_dir=""
# The baseline is written by checkout-index from a temporary index, like the
# PR tree below, not by git archive: archive honours export-ignore, so a file
# marked export-ignore at the merge-base would be missing from /baseline only,
# and a failure in it could never be classified as inherited. read-tree and
# checkout-index write no objects; the real index is never touched.
base_git=(env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_INDEX_FILE="${root}/base.index"
	git -c core.fsmonitor=false -c core.hooksPath=/dev/null -c core.splitIndex=false
	-c filter.lfs.clean= -c filter.lfs.smudge= -c filter.lfs.process= -c filter.lfs.required=false
	--git-dir="${host_git_dir}" --work-tree="${worktree_real}" -C "${worktree_real}")
if [ -z "${host_git_dir}" ] || ! "${base_git[@]}" read-tree "${merge_base}" >/dev/null 2>&1 \
	|| ! "${base_git[@]}" checkout-index -a --prefix="${root}/baseline/" >/dev/null 2>&1; then
	synthetic_result baseline_export_failed
	exit 0
fi
# The PR tree is built from git, like the baseline, so a test that reads a
# tracked dotfile or .github/ai/*.json sees it on both trees. Before, /source
# was the review sandbox's filtered snapshot, which drops those files, so
# such tests failed only on the PR side and were reported as caused by the
# change. Tracked files are the real index's paths (HEAD plus staged adds,
# minus staged deletions) with their worktree content (after Git's built-in
# eol/ident conversions, as a commit and checkout would apply them), written
# through a temporary index and object directory in the private run root: the
# real index and the checkout's .git are only read. Global and system Git
# config are left out and the git-lfs driver is switched off, so a PR's
# .gitattributes cannot run a filter driver (git-lfs would write into
# .git/lfs and fetch over the network); repository-local config is the
# runner's own.
# checkout-index (not git archive) writes both trees, so export-ignore cannot
# hide a file from the gate. .git itself is never copied.
toplevel="$("${git_safe[@]}" rev-parse --show-toplevel 2>/dev/null)" || toplevel=""
objects_dir="$("${git_safe[@]}" rev-parse --path-format=absolute --git-path objects 2>/dev/null)" || objects_dir=""
# GIT_ALTERNATE_OBJECT_DIRECTORIES is a colon-separated list.
if [ -z "${host_git_dir}" ] || [ "${toplevel}" != "${worktree_real}" ] || [ ! -d "${objects_dir}" ] || [[ "${objects_dir}" == *[:\"]* ]]; then
	synthetic_result source_export_failed
	exit 0
fi
mkdir -m 0700 "${root}/objects" "${root}/objects/pack" "${root}/objects/info" "${root}/filtered"
pr_git=(env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_INDEX_FILE="${root}/pr.index"
	GIT_OBJECT_DIRECTORY="${root}/objects" GIT_ALTERNATE_OBJECT_DIRECTORIES="${objects_dir}"
	git -c core.fsmonitor=false -c core.hooksPath=/dev/null -c core.splitIndex=false -c core.untrackedCache=false
	-c filter.lfs.clean= -c filter.lfs.smudge= -c filter.lfs.process= -c filter.lfs.required=false
	--git-dir="${host_git_dir}" --work-tree="${worktree_real}" -C "${worktree_real}")
# Staged additions and deletions relative to HEAD, read from the real index.
if ! "${git_safe[@]}" diff-index --cached --no-renames --name-only --diff-filter=A -z HEAD > "${root}/staged_added" 2>/dev/null \
	|| ! "${git_safe[@]}" diff-index --cached --no-renames --name-only --diff-filter=D -z HEAD > "${root}/staged_deleted" 2>/dev/null \
	|| ! "${pr_git[@]}" read-tree HEAD >/dev/null 2>&1 \
	|| ! "${pr_git[@]}" update-index --force-remove -z --stdin < "${root}/staged_deleted" >/dev/null 2>&1 \
	|| ! "${pr_git[@]}" add -u -- :/ >/dev/null 2>&1 \
	|| ! "${pr_git[@]}" update-index --add --remove -z --stdin < "${root}/staged_added" >/dev/null 2>&1 \
	|| ! "${pr_git[@]}" checkout-index -a --prefix="${root}/source/" >/dev/null 2>&1; then
	synthetic_result source_export_failed
	exit 0
fi
# Untracked, non-ignored files enter only through the review sandbox's
# credential filter: the snapshot keeps the ones allowed() admits (no .env*,
# keys, credential stores or secret-named paths), and only those are copied.
if ! PYTHONDONTWRITEBYTECODE=1 python3 "${support}/review_untrusted_workspace.py" snapshot "${worktree_real}" "${root}/filtered" "${root}/manifest.json" "${host_git_dir}" >/dev/null 2>&1; then
	synthetic_result snapshot_failed
	exit 0
fi
"${git_safe[@]}" ls-files --others --exclude-standard -z > "${root}/untracked" 2>/dev/null || { synthetic_result source_export_failed; exit 0; }
source_real="$(realpath -e "${root}/source")"
while IFS= read -r -d '' untracked; do
	[ -f "${root}/filtered/${untracked}" ] && [ ! -L "${root}/filtered/${untracked}" ] || continue
	# Never write through a symlink of the PR tree.
	if [ "$(realpath -m "${source_real}/${untracked}")" != "${source_real}/${untracked}" ]; then
		gate_log "outcome=warning reason=untracked_copy_skipped detail=symlink_in_path"
		continue
	fi
	if ! mkdir -p "$(dirname "${source_real}/${untracked}")" || ! cp -p -- "${root}/filtered/${untracked}" "${source_real}/${untracked}"; then
		synthetic_result snapshot_failed
		exit 0
	fi
done < "${root}/untracked"
rm -rf -- "${root}/filtered"
# The gate reads Makefile and .ai/validate.yml to resolve test commands. A
# tracked copy is already in the PR tree; the credential filter leaves out an
# untracked one, so copy those two regular files in from the worktree. The PR
# tree can hold a tracked symlink at either path: never install through it.
for overlay in Makefile .ai/validate.yml; do
	if [ -f "${worktree_real}/${overlay}" ] && [ ! -L "${worktree_real}/${overlay}" ] && [ ! -L "${worktree_real}/$(dirname "${overlay}")" ] \
		&& [ "$(realpath -m "${source_real}/${overlay}")" = "${source_real}/${overlay}" ]; then
		mkdir -p "${root}/source/$(dirname "${overlay}")" && install -m 0644 "${worktree_real}/${overlay}" "${root}/source/${overlay}" || { synthetic_result snapshot_failed; exit 0; }
	fi
done
# The PR tree gets a fresh, local, one-commit Git repository, as the old
# filtered snapshot had (review_untrusted_workspace.py snapshot): tests that
# run git at /source keep passing there instead of failing on both trees and
# being listed as inherited. It is built from /source's own files with no
# runner environment, no global or system config and no hooks; the checkout's
# .git is never copied. `add --force`: /source holds only the files the gate
# should see (tracked ones, and untracked ones the filter admitted), and a
# tracked .gitignore now present in it must not drop a tracked file that one
# of its rules also matches from the index the module lists tests from. On
# failure the run is unverified (snapshot_failed), as with the old snapshot,
# because without the repository git-dependent tests would fail on both trees
# and be classified as inherited.
source_git=(env -i PATH="${PATH}" HOME="${root}" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
	git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c commit.gpgsign=false -C "${source_real}")
if ! { "${source_git[@]}" init -q && printf '.review-venv/\nnode_modules/\n.venv/\n__pycache__/\n*.egg-info/\n*.dist-info/\n.pytest_cache/\n' > "${source_real}/.git/info/exclude" \
	&& "${source_git[@]}" add --all --force && "${source_git[@]}" -c user.name=isolated -c user.email=isolated@invalid commit --allow-empty -qm snapshot; } >/dev/null 2>&1; then
	gate_log "outcome=error reason=source_git_init_failed"
	synthetic_result snapshot_failed
	exit 0
fi

mounts=(
	--mount "type=bind,src=${root}/source,dst=/source"
	--mount "type=bind,src=${root}/baseline,dst=/baseline,readonly"
	--mount "type=bind,src=${module_real},dst=/gate/prepush_affected_tests.py,readonly"
	--mount "type=bind,src=${root}/results,dst=/results"
)
for deps_root in "${deps_roots[@]}"; do
	deps_real="$(realpath -e "${deps_root}" 2>/dev/null)" || { gate_log "outcome=warning reason=deps_root_missing"; continue; }
	mounts+=(--mount "type=bind,src=${deps_real},dst=${deps_real},readonly")
done

extra_env=()
run_extra=()
if [ -n "${venv}" ]; then
	if venv_real="$(realpath -e "${venv}" 2>/dev/null)" && [ -d "${venv_real}" ]; then
		mounts+=(--mount "type=bind,src=${venv_real},dst=/opt/codex-venv,readonly")
		extra_env+=(--env "PATH=/opt/codex-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" --env HOME=/tmp)
	else
		gate_log "outcome=warning reason=venv_missing"
	fi
fi
if [ -n "${node_modules}" ]; then
	# The PR tree can hold a tracked node_modules symlink: never mkdir through it.
	if nm_real="$(realpath -e "${node_modules}" 2>/dev/null)" && [ -d "${nm_real}" ] && [ ! -L "${node_modules}" ] \
		&& [ ! -L "${root}/source/node_modules" ] && [ ! -L "${root}/baseline/node_modules" ] \
		&& mkdir -p "${root}/source/node_modules" "${root}/baseline/node_modules"; then
		mounts+=(--mount "type=bind,src=${nm_real},dst=/source/node_modules,readonly")
		mounts+=(--mount "type=bind,src=${nm_real},dst=/baseline/node_modules,readonly")
	else
		gate_log "outcome=warning reason=node_modules_missing"
	fi
fi
if [ -n "${nested_nm_root}" ] && nested_real="$(realpath -e "${nested_nm_root}" 2>/dev/null)" && [ -d "${nested_real}" ]; then
	root_real="$(realpath -e "${root}")"
	nested_count=0
	while IFS= read -r -d '' nm; do
		[ "${nm}" != "${nested_real}/node_modules" ] || continue
		rel="${nm#"${nested_real}"/}"
		rel="${rel%/node_modules}"
		# Docker parses --mount as CSV: only a comma, a quote or a control
		# character breaks it. Paths with spaces are mounted like any other.
		if [[ "${rel}" == *[,\"]* || "${rel}" =~ [[:cntrl:]] ]]; then
			gate_log "outcome=warning reason=nested_node_modules_unsafe_path"
			continue
		fi
		case "/${rel}/" in */../*|*/./*|*//*) gate_log "outcome=warning reason=nested_node_modules_unsafe_path"; continue ;; esac
		if [ "${nested_count}" -ge 6 ]; then
			gate_log "outcome=warning reason=nested_node_modules_cap"
			break
		fi
		# Never create mount points through a symlink in the copied trees.
		if [ "$(realpath -m "${root_real}/source/${rel}")" != "${root_real}/source/${rel}" ] \
			|| [ "$(realpath -m "${root_real}/baseline/${rel}")" != "${root_real}/baseline/${rel}" ]; then
			gate_log "outcome=warning reason=nested_node_modules_mount_skipped detail=symlink_in_path"
			continue
		fi
		if ! mkdir -p "${root_real}/source/${rel}/node_modules" "${root_real}/baseline/${rel}/node_modules" 2>/dev/null; then
			gate_log "outcome=warning reason=nested_node_modules_mount_skipped detail=mkdir_failed"
			continue
		fi
		mounts+=(--mount "type=bind,src=${nm},dst=/source/${rel}/node_modules,readonly")
		mounts+=(--mount "type=bind,src=${nm},dst=/baseline/${rel}/node_modules,readonly")
		nested_count=$((nested_count + 1))
	done < <(find "${nested_real}" -mindepth 1 -maxdepth 6 -type d \( -name node_modules -prune -print0 -o -name '.*' -prune \) 2>/dev/null | sort -z)
fi
[ -z "${large_file_tests}" ] || extra_env+=(--env "PREPUSH_TEST_GATE_LARGE_FILE_TESTS=${large_file_tests}")
[ -z "${shards}" ] || run_extra+=(--shards "${shards}")
if [ -n "${tests_file_real}" ]; then
	mounts+=(--mount "type=bind,src=${root}/tests.txt,dst=/gate/tests.txt,readonly")
	run_extra+=(--tests-only /gate/tests.txt)
fi
# Done-check target files (issue #8427, P3): host copies, mounted read-only.
# The --tests-only list is validated and mounted above.
inputs_used=false
gate_input()
{
	local src="$1" name="$2" option="$3" size
	if [ -f "${src}" ] && [ ! -L "${src}" ] && size="$(stat -c %s "${src}" 2>/dev/null)" && [ "${size}" -le 65536 ] \
		&& mkdir -p -m 0700 "${root}/inputs" && install -m 0644 "${src}" "${root}/inputs/${name}"; then
		run_extra+=("${option}" "/inputs/${name}")
		inputs_used=true
	else
		gate_log "outcome=warning reason=tests_file_rejected"
	fi
}
# An explicitly empty value is logged as rejected, not treated as absent.
[ "${done_check_tests_file_given}" != 1 ] || gate_input "${done_check_tests_file}" tests.txt --tests-file
[ "${baseline_fail_file_given}" != 1 ] || gate_input "${baseline_fail_file}" baseline_fail.txt --expect-baseline-fail-file
[ "${inputs_used}" != true ] || mounts+=(--mount "type=bind,src=${root}/inputs,dst=/inputs,readonly")

wall=$(( timeout_minutes * 60 + 120 ))
rc=0
# /tmp is mounted `exec`: Docker's --tmpfs defaults to noexec, and many shell
# tests write a stub script to a temp dir and run it. Under noexec those tests
# failed or hung on both trees, so the gate reported noise (PR #8371 loop).
# The tests already run arbitrary code here; exec adds no capability.
env -i PATH="${PATH}" timeout --kill-after=30s "${wall}" docker run --rm --init --name "${container}" \
	--user "$(id -u):$(id -g)" --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
	--pids-limit 512 --memory 4g --tmpfs /tmp:rw,exec,nosuid,nodev,size=512m \
	"${mounts[@]}" ${extra_env[@]+"${extra_env[@]}"} \
	--workdir /source "${image}" \
	python3 /gate/prepush_affected_tests.py run --repo-root /source \
	--changed-files-file /results/changed.txt --baseline-tree /baseline \
	--timeout-minutes "${timeout_minutes}" ${run_extra[@]+"${run_extra[@]}"} --out /results/result.json --summary /results/summary.md \
	> "${root}/container.log" 2>&1 || rc=$?
env -i PATH="${PATH}" docker rm -f "${container}" >/dev/null 2>&1 || true
if [ -n "$(env -i PATH="${PATH}" docker ps -aq --filter "name=^/${container}$" 2>/dev/null)" ]; then
	synthetic_result container_survived
	exit 0
fi

# The gate exits 0 for every outcome it records (fail included). A non-zero
# exit means it crashed or was killed, for example because a test made
# /results/result.json unwritable after forging it, so never trust the file.
if [ "${rc}" -ne 0 ]; then
	synthetic_result "container_rc_${rc}"
	exit 0
fi
copied=0
for name in result.json summary.md; do
	src="${root}/results/${name}"
	if [ -f "${src}" ] && [ ! -L "${src}" ] && [ "$(stat -c %s "${src}")" -le 1048576 ]; then
		install -m 0644 "${src}" "${out_dir}/${name}" && copied=$((copied + 1))
	fi
done
if [ ! -f "${out_dir}/result.json" ] || [ "${copied}" -eq 0 ]; then
	synthetic_result "container_no_result_rc_${rc}"
	exit 0
fi
status="$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); s=d.get("status"); print(s if s in ("pass","fail","unverified","skipped") else "invalid")' "${out_dir}/result.json" 2>/dev/null)" || status="invalid"
if [ "${status}" = invalid ]; then
	synthetic_result result_invalid
	exit 0
fi
gate_log "outcome=${status} reason=container_rc_${rc}"
exit 0
