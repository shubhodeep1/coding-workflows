#!/usr/bin/env bash
# Validate the output of the AI conflict-resolver step before committing.
#
# Enforces three invariants on the resolver's edits:
#
#   1. Touched-set ⊆ conflicted-set.  The resolver may only modify files
#      that contained merge conflicts (paths from `git ls-files --unmerged`
#      taken before the resolver ran).  Any edit outside that set is
#      treated as a hallucination — the resolver inventing new code under
#      the guise of "merge resolution" — and the run aborts.
#
#   2. Per-file syntax sanity.  For every modified .sh, run `bash -n`.
#      For every modified .py, parse with `ast.parse` without writing
#      bytecode. Catches truncated heredocs, missing fi/done, etc., before
#      they reach a consumer repo.
#
#   3. Workflow → script reference integrity.  For every modified
#      .github/workflows/*.yml file, invoke check_workflow_script_refs.py
#      to verify all script paths it references exist in scripts/.  This
#      is what would have caught the build_repo_overview.sh /
#      protected_paths.txt hallucination on PR #912.
#
# Exit code 0 on success.  Exit code 1 with a diagnostic on the first
# failure — the caller (review_autofix.yml) treats this as a hard error
# and skips the [ai-merge-resolve] commit.
#
# Usage:
#   check_resolver_diff.sh \
#     --conflicted-set <path> \
#     --touched-set    <path> \
#     [--conflict-spans <json>] \
#     [--clean-manifest <tsv>] \
#     [--repo-root     <path>]
#
# All paths are interpreted as relative to --repo-root (default: $PWD).
# Paths starting with "/" are treated as absolute.

set -euo pipefail

CONFLICTED_SET=""
TOUCHED_SET=""
CONFLICT_SPANS=""
CLEAN_MANIFEST=""
STRICT_MANIFESTS=false
REPO_ROOT="${PWD}"

while [ "$#" -gt 0 ]; do
	case "$1" in
		--conflicted-set)
			CONFLICTED_SET="$2"; shift 2 ;;
		--touched-set)
			TOUCHED_SET="$2"; shift 2 ;;
		--conflict-spans)
			CONFLICT_SPANS="$2"; shift 2 ;;
		--clean-manifest)
			CLEAN_MANIFEST="$2"; shift 2 ;;
		--strict-manifests)
			STRICT_MANIFESTS=true; shift ;;
		--repo-root)
			REPO_ROOT="$2"; shift 2 ;;
		-h|--help)
			sed -n '2,30p' "$0"; exit 0 ;;
		*)
			echo "::error::check_resolver_diff.sh: unknown argument: $1" >&2
			exit 2 ;;
	esac
done

if [ -z "${CONFLICTED_SET}" ] || [ -z "${TOUCHED_SET}" ]; then
	echo "::error::check_resolver_diff.sh: --conflicted-set and --touched-set are required" >&2
	exit 2
fi

if ! REPO_ROOT="$(cd "${REPO_ROOT}" 2>/dev/null && pwd)"; then
	echo "::error::check_resolver_diff.sh: repo-root directory not found: ${REPO_ROOT}" >&2
	exit 2
fi

resolve_path() {
	local path="$1"
	if [[ "${path}" == /* ]]; then
		printf '%s\n' "${path}"
	else
		printf '%s\n' "${REPO_ROOT}/${path}"
	fi
}

CONFLICTED_SET="$(resolve_path "${CONFLICTED_SET}")"
TOUCHED_SET="$(resolve_path "${TOUCHED_SET}")"
[ -z "${CONFLICT_SPANS}" ] || CONFLICT_SPANS="$(resolve_path "${CONFLICT_SPANS}")"
[ -z "${CLEAN_MANIFEST}" ] || CLEAN_MANIFEST="$(resolve_path "${CLEAN_MANIFEST}")"

if [ ! -f "${CONFLICTED_SET}" ]; then
	echo "::error::check_resolver_diff.sh: conflicted-set file not found: ${CONFLICTED_SET}" >&2
	exit 2
fi
if [ ! -f "${TOUCHED_SET}" ]; then
	echo "::error::check_resolver_diff.sh: touched-set file not found: ${TOUCHED_SET}" >&2
	exit 2
fi
if [ -n "${CONFLICT_SPANS}" ] && [ ! -f "${CONFLICT_SPANS}" ]; then
	echo "::error::check_resolver_diff.sh: conflict-spans file not found: ${CONFLICT_SPANS}" >&2
	exit 2
fi
if [ -n "${CLEAN_MANIFEST}" ] && [ ! -f "${CLEAN_MANIFEST}" ]; then
	echo "::error::check_resolver_diff.sh: clean-manifest file not found: ${CLEAN_MANIFEST}" >&2
	exit 2
fi
if [ "${STRICT_MANIFESTS}" = true ] && { [ -z "${CONFLICT_SPANS}" ] || [ -z "${CLEAN_MANIFEST}" ]; }; then
	echo "::error::check_resolver_diff.sh: strict mode requires --conflict-spans and --clean-manifest" >&2
	exit 2
fi

cd "${REPO_ROOT}"

run_isolated_validator_python() {
	local sandbox_helper="${POST_AGENT_VALIDATION_SANDBOX:-}"
	local validation_runtime_dir="${POST_AGENT_VALIDATION_RUNTIME_DIR:-${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}}"
	if [ -n "${sandbox_helper}" ]; then
		[ -x "${sandbox_helper}" ] || {
			echo "::error::check_resolver_diff.sh: isolated validator sandbox is unavailable" >&2
			return 1
		}
		bash "${sandbox_helper}" \
			--role validator --workspace "${REPO_ROOT}" --runtime-dir "${validation_runtime_dir}" \
			-- /usr/bin/python3 -I -S "$@"
		return $?
	fi
	(
		cd "${TMPDIR:-/tmp}"
		env -i HOME="${TMPDIR:-/tmp}" PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
			PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I -S "$@"
	)
}

# Sort+dedupe both sets in place so comm sees consistent ordering.
sort -u -o "${CONFLICTED_SET}" "${CONFLICTED_SET}"
sort -u -o "${TOUCHED_SET}"    "${TOUCHED_SET}"

touched_count="$(wc -l < "${TOUCHED_SET}" | tr -d '[:space:]')"
conflicted_count="$(wc -l < "${CONFLICTED_SET}" | tr -d '[:space:]')"
echo "check_resolver_diff: touched=${touched_count} conflicted=${conflicted_count}"

# ---------------------------------------------------------------------------
# (1) Touched ⊆ Conflicted
# ---------------------------------------------------------------------------
# `comm -23 a b` prints lines in a not in b.  A non-empty result means the
# resolver edited paths that did NOT contain merge conflicts.
out_of_conflict="$(comm -23 "${TOUCHED_SET}" "${CONFLICTED_SET}" || true)"
if [ -n "${out_of_conflict}" ]; then
	echo "::error::Conflict resolver edited files outside the conflicted set:" >&2
	while IFS= read -r path; do
		printf '  - %s\n' "${path}" >&2
	done <<< "${out_of_conflict}"
	echo "::error::This usually indicates a hallucinated merge resolution. Aborting." >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# (2) Conflict anchors and deterministically merged clean paths
# ---------------------------------------------------------------------------
# The optional manifests are produced before the resolver runs. Conflict
# anchors preserve every byte outside the original marker spans; clean paths
# are compared by mode and Git blob ID against an independent merge-tree.
if [ -n "${CONFLICT_SPANS}" ] || [ -n "${CLEAN_MANIFEST}" ]; then
	run_isolated_validator_python - \
		"${REPO_ROOT}" "${CONFLICTED_SET}" "${CONFLICT_SPANS}" "${CLEAN_MANIFEST}" <<'PY'
import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

repo = Path(sys.argv[1]).resolve()
conflicted_path = Path(sys.argv[2])
spans_path = Path(sys.argv[3]) if sys.argv[3] else None
clean_manifest_path = Path(sys.argv[4]) if sys.argv[4] else None


def fail(message: str) -> None:
	print(f"::error::check_resolver_diff.sh: {message}", file=sys.stderr)
	raise SystemExit(1)


def safe_path(value: str) -> Path:
	path = PurePosixPath(value)
	if path.is_absolute() or ".." in path.parts or not value or any(ord(char) < 32 for char in value):
		fail("manifest contains an unsafe path")
	return repo / path


conflicted = {line for line in conflicted_path.read_text(encoding="utf-8").splitlines() if line}
if spans_path is not None:
	try:
		spans = json.loads(spans_path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError):
		fail("conflict-spans JSON is invalid")
	if not isinstance(spans, dict) or set(spans) != conflicted:
		fail("conflict-spans paths do not exactly match the conflicted set")
	for path_name, row in spans.items():
		if not isinstance(row, dict) or not isinstance(row.get("anchors"), list):
			fail(f"conflict-spans entry is invalid for {path_name}")
		allow_delete = row.get("allow_delete", False)
		if not isinstance(allow_delete, bool):
			fail(f"conflict-spans deletion authorization is invalid for {path_name}")
		expected_mode = row.get("mode")
		allowed_modes = {"100644", "100755", "120000", "160000"} if allow_delete else {"100644", "100755"}
		if expected_mode not in allowed_modes:
			fail(f"conflict-spans mode is invalid for {path_name}")
		resolved_path = safe_path(path_name)
		if allow_delete:
			if row["anchors"] or resolved_path.exists() or resolved_path.is_symlink():
				fail(f"fingerprint-authorized deletion was not preserved for {path_name}")
			continue
		try:
			anchors = [base64.b64decode(item, validate=True) for item in row["anchors"]]
		except (TypeError, ValueError):
			fail(f"conflict-spans anchors are invalid for {path_name}")
		if len(anchors) < 2:
			fail(f"conflict-spans entry has no bounded conflict for {path_name}")
		if not resolved_path.is_file() or resolved_path.is_symlink():
			fail(f"conflicted path is not a regular resolved file: {path_name}")
		actual_mode = "100755" if os.lstat(resolved_path).st_mode & stat.S_IXUSR else "100644"
		if actual_mode != expected_mode:
			fail(f"resolver changed conflicted path mode: {path_name}")
		resolved = resolved_path.read_bytes()
		if not resolved.startswith(anchors[0]) or not resolved.endswith(anchors[-1]):
			fail(f"resolver changed content outside conflict spans in {path_name}")
		cursor = len(anchors[0])
		for anchor in anchors[1:-1]:
			anchor_offset = resolved.find(anchor, cursor)
			if anchor_offset < 0:
				fail(f"resolver changed content outside conflict spans in {path_name}")
			cursor = anchor_offset + len(anchor)

if clean_manifest_path is not None:
	for line_number, line in enumerate(clean_manifest_path.read_text(encoding="utf-8").splitlines(), 1):
		if not line:
			continue
		parts = line.split("\t", 2)
		if len(parts) != 3:
			fail(f"clean manifest row {line_number} is malformed")
		expected_mode, expected_blob, path_name = parts
		clean_path = safe_path(path_name)
		if expected_mode == "000000" and expected_blob == "-":
			if clean_path.exists() or clean_path.is_symlink():
				fail(f"resolver recreated deterministically deleted content: {path_name}")
			continue
		if not clean_path.exists() and not clean_path.is_symlink():
			fail(f"deterministically merged path is missing: {path_name}")
		path_stat = os.lstat(clean_path)
		if stat.S_ISLNK(path_stat.st_mode):
			actual_mode = "120000"
		elif stat.S_ISREG(path_stat.st_mode):
			actual_mode = "100755" if path_stat.st_mode & stat.S_IXUSR else "100644"
		else:
			fail(f"deterministically merged path has unsupported type: {path_name}")
		blob_result = subprocess.run(
			["git", "hash-object", "--", path_name], cwd=repo,
			capture_output=True, text=True, check=False,
		)
		actual_blob = blob_result.stdout.strip()
		if blob_result.returncode != 0 or actual_mode != expected_mode or actual_blob != expected_blob:
			fail(f"resolver changed deterministically merged content: {path_name}")
PY
fi

# ---------------------------------------------------------------------------
# (3) Per-file syntax sanity
# ---------------------------------------------------------------------------
# Read touched paths line-by-line (preserves whitespace) and run language-
# appropriate syntax checks.  Skip files the resolver deleted.
syntax_failed=0
while IFS= read -r touched; do
	[ -z "${touched}" ] && continue
	[ -e "${touched}" ] || continue
	case "${touched}" in
		*.sh)
			if ! bash -n -- "${touched}" 2>&1; then
				echo "::error::bash -n failed for ${touched}" >&2
				syntax_failed=$((syntax_failed + 1))
			fi
			;;
		*.py)
			if ! run_isolated_validator_python -c \
				'import ast,pathlib,sys; source=pathlib.Path(sys.argv[1]); ast.parse(source.read_text(encoding="utf-8"), filename=str(source))' \
				"${REPO_ROOT}/${touched}" 2>&1; then
				echo "::error::Python syntax validation failed for ${touched}" >&2
				syntax_failed=$((syntax_failed + 1))
			fi
			;;
		*.json)
			if ! run_isolated_validator_python -c "import json,sys; json.load(open(sys.argv[1]))" "${REPO_ROOT}/${touched}" 2>&1; then
				echo "::error::JSON parse failed for ${touched}" >&2
				syntax_failed=$((syntax_failed + 1))
			fi
			;;
	esac
done < "${TOUCHED_SET}"

if [ "${syntax_failed}" -gt 0 ]; then
	echo "::error::${syntax_failed} touched file(s) failed syntax check. Aborting." >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# (4) Workflow → script reference integrity
# ---------------------------------------------------------------------------
# For every modified .github/workflows/*.yml, verify the script paths it
# references all exist.  Uses the canonical checker so CI and the inline
# guard share identical logic.
workflow_files=()
while IFS= read -r touched; do
	[ -z "${touched}" ] && continue
	[ -e "${touched}" ] || continue
	case "${touched}" in
		.github/workflows/*.yml|.github/workflows/*.yaml)
			workflow_files+=("${touched}") ;;
	esac
done < "${TOUCHED_SET}"

if [ "${#workflow_files[@]}" -gt 0 ]; then
	checker="${REPO_ROOT}/scripts/check_workflow_script_refs.py"
	if [ ! -f "${checker}" ]; then
		echo "::error::check_workflow_script_refs.py not found at ${checker}" >&2
		exit 1
	fi
	if ! run_isolated_validator_python "${checker}" \
			--repo-root "${REPO_ROOT}" \
			--files "${workflow_files[@]}"; then
		echo "::error::Modified workflow file references nonexistent script(s). Aborting." >&2
		exit 1
	fi
fi

echo "check_resolver_diff: OK"
