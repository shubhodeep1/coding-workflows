#!/usr/bin/env bash
# Validate the output of the AI conflict-resolver step before committing.
#
# Enforces four invariants on the resolver's edits:
#
#   1. Touched-set ⊆ conflicted-set.  The resolver may only modify files
#      that contained merge conflicts (paths from `git ls-files --unmerged`
#      taken before the resolver ran).  Any edit outside that set is
#      treated as a hallucination — the resolver inventing new code under
#      the guise of "merge resolution" — and the run aborts.
#
#   2. Per-file syntax sanity.  For every modified .sh, run `bash -n`.
#      For every modified .py, run `python3 -m py_compile`.  Catches
#      truncated heredocs, missing fi/done, etc., before they reach a
#      consumer repo.
#
#   3. Workflow → script reference integrity.  For every modified
#      .github/workflows/*.yml file, invoke check_workflow_script_refs.py
#      to verify all script paths it references exist in scripts/.  This
#      is what would have caught the build_repo_overview.sh /
#      protected_paths.txt hallucination on PR #912.
#
#   4. No conflicted path left behind.  A conflicted path the resolver did
#      not touch that is still unmerged in the index fails here, named with
#      its index stages (PR #6844: a modify/delete fragment).
#
# Exit code 0 on success.  Exit code 1 with a diagnostic on the first
# failure — the caller (review_autofix.yml) treats this as a hard error
# and skips the [ai-merge-resolve] commit.
#
# Usage:
#   check_resolver_diff.sh \
#     --conflicted-set <path> \
#     --touched-set    <path> \
#     [--repo-root     <path>] \
#     [--extra-allowed-file <repo-relative path>]...
#
# --extra-allowed-file (repeatable) adds a path invariant 1 accepts besides
# the conflicted set: the pre-push gate's affected tests and companions, so
# its one test-repair turn on a resolution can pass the subset check. Values
# that are absolute, contain a `..` segment or control characters are ignored
# with a warning. Invariants 2-4 are unchanged, and invariant 4 still uses the
# conflicted set only. Without the flag the script behaves as before.
#
# All paths are interpreted as relative to --repo-root (default: $PWD).
# Paths starting with "/" are treated as absolute.

set -euo pipefail

CONFLICTED_SET=""
TOUCHED_SET=""
REPO_ROOT="${PWD}"
EXTRA_ALLOWED=()

while [ "$#" -gt 0 ]; do
	case "$1" in
		--conflicted-set)
			CONFLICTED_SET="$2"; shift 2 ;;
		--touched-set)
			TOUCHED_SET="$2"; shift 2 ;;
		--repo-root)
			REPO_ROOT="$2"; shift 2 ;;
		--extra-allowed-file)
			if [ "$#" -lt 2 ]; then
				echo "::error::check_resolver_diff.sh: --extra-allowed-file needs a value" >&2
				exit 2
			fi
			extra_path="$2"
			if [ -z "${extra_path}" ] || [[ "${extra_path}" == /* ]] || [[ "/${extra_path}/" == */../* ]] || \
				[[ "${extra_path}" =~ [[:cntrl:]] ]]; then
				echo "::warning::check_resolver_diff.sh: ignoring unsafe --extra-allowed-file value" >&2
			else
				EXTRA_ALLOWED+=("${extra_path}")
			fi
			shift 2 ;;
		-h|--help)
			sed -n '2,46p' "$0"; exit 0 ;;
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

if [ ! -f "${CONFLICTED_SET}" ]; then
	echo "::error::check_resolver_diff.sh: conflicted-set file not found: ${CONFLICTED_SET}" >&2
	exit 2
fi
if [ ! -f "${TOUCHED_SET}" ]; then
	echo "::error::check_resolver_diff.sh: touched-set file not found: ${TOUCHED_SET}" >&2
	exit 2
fi

cd "${REPO_ROOT}"

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
if [ "${#EXTRA_ALLOWED[@]}" -gt 0 ]; then
	allowed_set="$(mktemp)"
	{ cat "${CONFLICTED_SET}"; printf '%s\n' "${EXTRA_ALLOWED[@]}"; } | sort -u > "${allowed_set}"
	out_of_conflict="$(comm -23 "${TOUCHED_SET}" "${allowed_set}" || true)"
	rm -f "${allowed_set}"
else
	out_of_conflict="$(comm -23 "${TOUCHED_SET}" "${CONFLICTED_SET}" || true)"
fi
if [ -n "${out_of_conflict}" ]; then
	echo "::error::Conflict resolver edited files outside the conflicted set:" >&2
	while IFS= read -r path; do
		printf '  - %s\n' "${path}" >&2
	done <<< "${out_of_conflict}"
	echo "::error::This usually indicates a hallucinated merge resolution. Aborting." >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# (2) Per-file syntax sanity
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
			if ! PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile "${touched}" 2>&1; then
				echo "::error::py_compile failed for ${touched}" >&2
				syntax_failed=$((syntax_failed + 1))
			fi
			;;
		*.json)
			if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "${touched}" 2>&1; then
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
# (3) Workflow → script reference integrity
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
	if ! PYTHONDONTWRITEBYTECODE=1 python3 "${checker}" \
			--repo-root "${REPO_ROOT}" \
			--files "${workflow_files[@]}"; then
		echo "::error::Modified workflow file references nonexistent script(s). Aborting." >&2
		exit 1
	fi
fi

# ---------------------------------------------------------------------------
# (4) No conflicted path left behind
# ---------------------------------------------------------------------------
# The resolver's commit stages only the paths it touched, so a conflicted
# path it never touched stays unmerged and the commit is refused later.
# PR #6844 passed this script ("touched=1 conflicted=2 ... OK") and then
# failed at the index guard on a modify/delete changelog fragment.  Name
# such paths here, with their index stages, instead of reporting OK.
# Outside a merge (no unmerged entries) this check passes.
if git rev-parse --git-dir >/dev/null 2>&1; then
	untouched_unmerged=""
	while IFS= read -r conflicted; do
		[ -z "${conflicted}" ] && continue
		if grep -Fxq -- "${conflicted}" "${TOUCHED_SET}"; then
			continue
		fi
		stages="$(git ls-files -u -- "${conflicted}" | awk '{print $3}' | sort -u | tr '\n' ' ')"
		[ -n "${stages}" ] || continue
		untouched_unmerged="${untouched_unmerged}${conflicted} (index stages: ${stages% })"$'\n'
	done < "${CONFLICTED_SET}"
	if [ -n "${untouched_unmerged}" ]; then
		echo "::error::Conflict resolver left conflicted path(s) untouched; they would stay unmerged and block the [ai-merge-resolve] commit:" >&2
		printf '%s' "${untouched_unmerged}" | sed '/^$/d; s/^/  - /' >&2
		echo "::error::Stages 1 2 or 1 3 mean one side deleted the file: keep or delete it explicitly." >&2
		exit 1
	fi
fi

echo "check_resolver_diff: OK"
