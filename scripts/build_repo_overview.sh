#!/usr/bin/env bash

set -u

export LC_ALL=C

GENERATOR_VERSION="repo-overview/v1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="${REPO_OVERVIEW_ROOT:-${DEFAULT_ROOT}}"
OUTPUT_FILE="${REPO_ROOT}/repo_overview.txt"
TMP_FILE="${OUTPUT_FILE}.tmp"

trim_and_sort_unique()
{
	sed '/^$/d' | LC_ALL=C sort -u
}

safe_git_ls_files()
{
	if command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		git -C "${REPO_ROOT}" ls-files -- "$@" 2>/dev/null
		return 0
	fi
	return 1
}

collect_input_files()
{
	if safe_git_ls_files \
		':(glob)**/package.json' \
		':(glob)**/pyproject.toml' \
		':(glob)**/go.mod' \
		':(glob)**/Cargo.toml' \
		'.pre-commit-config.yaml' \
		':(glob)**/Dockerfile' \
		':(glob).github/workflows/*.yml'; then
		return 0
	fi

	(
		cd "${REPO_ROOT}" || exit 0
		find . \
			\( -path './.git' -o -path './node_modules' -o -path './.serena' -o -path './validation' \) -prune -o \
			-type f \
			\( -name 'package.json' -o -name 'pyproject.toml' -o -name 'go.mod' -o -name 'Cargo.toml' -o -name 'Dockerfile' \) -print
		[ -f .pre-commit-config.yaml ] && echo '.pre-commit-config.yaml'
		find .github/workflows -maxdepth 1 -type f -name '*.yml' -print 2>/dev/null
	) | sed 's#^\./##'
}

compute_inputs_hash()
{
	local hasher
	hasher="$(command -v sha256sum || true)"
	if [ -z "${hasher}" ] && command -v shasum >/dev/null 2>&1; then
		hasher="shasum -a 256"
	fi

	if [ -z "${hasher}" ]; then
		echo "unavailable"
		return 0
	fi

	local files
	files="$(collect_input_files | trim_and_sort_unique)"

	{
		if [ -f "${BASH_SOURCE[0]}" ]; then
			printf '%s  %s\n' "$(${hasher} < "${BASH_SOURCE[0]}" | awk '{print $1}')" "__build_repo_overview_script__"
		fi

		while IFS= read -r rel; do
			[ -n "${rel}" ] || continue
			if [ -f "${REPO_ROOT}/${rel}" ]; then
				printf '%s  %s\n' "$(${hasher} < "${REPO_ROOT}/${rel}" | awk '{print $1}')" "${rel}"
			fi
		done <<< "${files}"
	} | ${hasher} | awk '{print $1}'
}

print_tree_section()
{
	if command -v tree >/dev/null 2>&1; then
		(
			cd "${REPO_ROOT}" || exit 0
			tree -d -L 2 --noreport -I '.git|node_modules|.serena|validation|.venv|__pycache__' 2>/dev/null
		)
		return 0
	fi

	(
		cd "${REPO_ROOT}" || exit 0
		find . -maxdepth 2 \
			\( -path './.git' -o -path './node_modules' -o -path './.serena' -o -path './validation' -o -path './.venv' -o -path './__pycache__' \) -prune -o \
			-type d -print | sed 's#^\./##' | trim_and_sort_unique
	)
}

print_languages_section()
{
	local file_list
	if ! file_list="$(safe_git_ls_files 2>/dev/null)"; then
		file_list="$(
			cd "${REPO_ROOT}" || exit 0
			find . \
				\( -path './.git' -o -path './node_modules' -o -path './.serena' -o -path './validation' \) -prune -o \
				-type f -print | sed 's#^\./##'
		)"
	fi

	if [ -z "${file_list}" ]; then
		echo "(none)"
		return 0
	fi

	{
		echo "${file_list}" | grep -E '\.py$' >/dev/null 2>&1 && echo "Python"
		echo "${file_list}" | grep -E '\.(sh|bash)$' >/dev/null 2>&1 && echo "Shell"
		echo "${file_list}" | grep -E '\.(js|cjs|mjs)$' >/dev/null 2>&1 && echo "JavaScript"
		echo "${file_list}" | grep -E '\.(ts|tsx)$' >/dev/null 2>&1 && echo "TypeScript"
		echo "${file_list}" | grep -E '\.go$' >/dev/null 2>&1 && echo "Go"
		echo "${file_list}" | grep -E '\.rs$' >/dev/null 2>&1 && echo "Rust"
		echo "${file_list}" | grep -E '\.(yml|yaml)$' >/dev/null 2>&1 && echo "YAML"
		echo "${file_list}" | grep -E '\.md$' >/dev/null 2>&1 && echo "Markdown"
		echo "${file_list}" | grep -E '\.json$' >/dev/null 2>&1 && echo "JSON"
	} | trim_and_sort_unique | awk '{print "- " $0}'
}

print_package_files_section()
{
	local files
	files="$({
		safe_git_ls_files ':(glob)**/package.json' ':(glob)**/pyproject.toml' ':(glob)**/go.mod' ':(glob)**/Cargo.toml' || true
	} | trim_and_sort_unique)"

	if [ -z "${files}" ]; then
		echo "(none)"
		return 0
	fi

	echo "${files}" | awk '{print "- " $0}'
}

print_test_runner_section()
{
	local runners=""
	local files
	files="$(safe_git_ls_files 2>/dev/null || true)"

	echo "${files}" | grep -E '(^|/)package.json$' >/dev/null 2>&1 && runners="${runners}\nnpm test"
	echo "${files}" | grep -E '(^|/)pyproject.toml$|(^|/)pytest\.ini$|(^|/)conftest\.py$' >/dev/null 2>&1 && runners="${runners}\npytest"
	echo "${files}" | grep -E '(^|/)go.mod$' >/dev/null 2>&1 && runners="${runners}\ngo test ./..."
	echo "${files}" | grep -E '(^|/)Cargo.toml$' >/dev/null 2>&1 && runners="${runners}\ncargo test"

	if [ -z "${runners}" ]; then
		echo "(none detected)"
		return 0
	fi

	printf '%s\n' "${runners}" | trim_and_sort_unique | awk '{print "- " $0}'
}

print_lint_format_section()
{
	local tools=""
	[ -f "${REPO_ROOT}/.pre-commit-config.yaml" ] && tools="${tools}\npre-commit"

	local files
	files="$(safe_git_ls_files 2>/dev/null || true)"
	echo "${files}" | grep -E '(^|/)package.json$' >/dev/null 2>&1 && tools="${tools}\nnpm run lint (if defined)\nnpm run format (if defined)"
	echo "${files}" | grep -E '(^|/)pyproject.toml$' >/dev/null 2>&1 && tools="${tools}\nruff/black (pyproject.toml)"

	if [ -z "${tools}" ]; then
		echo "(none detected)"
		return 0
	fi

	printf '%s\n' "${tools}" | trim_and_sort_unique | awk '{print "- " $0}'
}

print_containers_section()
{
	local files
	files="$(safe_git_ls_files ':(glob)**/Dockerfile' 2>/dev/null | trim_and_sort_unique || true)"
	if [ -z "${files}" ]; then
		echo "(none)"
		return 0
	fi
	echo "${files}" | awk '{print "- " $0}'
}

print_ci_workflows_section()
{
	local files
	files="$(safe_git_ls_files ':(glob).github/workflows/*.yml' 2>/dev/null | trim_and_sort_unique || true)"
	if [ -z "${files}" ]; then
		echo "(none)"
		return 0
	fi
	echo "${files}" | awk '{print "- " $0}'
}

print_db_contracts_section()
{
	local files
	files="$(
		cd "${REPO_ROOT}" || exit 0
		find db/contracts -maxdepth 1 -type f \( -name '*.yml' -o -name '*.json' \) -print 2>/dev/null | sed 's#^\./##' | trim_and_sort_unique
	)"
	if [ -z "${files}" ]; then
		echo "(none)"
		return 0
	fi
	echo "${files}" | awk '{print "- " $0}'
}

print_protected_paths_section()
{
	cat <<'PATHS'
- .git/**
- .serena/**
- node_modules/**
- repo_overview.txt
- pre_assembled_static.txt
- scripts/** (consumer repos)
- prompts/** (consumer repos)
- .github/prompts/**
- .github/scripts/**
- ai-memory/** (consumer repos)
PATHS
}

mkdir -p "${REPO_ROOT}" 2>/dev/null || true

inputs_hash="$(compute_inputs_hash)"
current_hash=""
if [ -f "${OUTPUT_FILE}" ]; then
	current_hash="$(sed -n 's/^inputs_hash:[[:space:]]*//p' "${OUTPUT_FILE}" | head -n 1)"
fi

if [ -n "${current_hash}" ] && [ "${current_hash}" = "${inputs_hash}" ]; then
	exit 0
fi

generated_at="$(git -C "${REPO_ROOT}" log -1 --format=%cI 2>/dev/null || true)"
if [ -z "${generated_at}" ]; then
	generated_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
fi

{
	echo "=== REPO OVERVIEW ==="
	echo "generated_at: ${generated_at}"
	echo "generator_version: ${GENERATOR_VERSION}"
	echo "inputs_hash: ${inputs_hash}"
	echo
	echo "=== TREE (depth 2, pruned) ==="
	print_tree_section
	echo
	echo "=== LANGUAGES ==="
	print_languages_section
	echo
	echo "=== PACKAGE FILES ==="
	print_package_files_section
	echo
	echo "=== TEST RUNNER ==="
	print_test_runner_section
	echo
	echo "=== LINT/FORMAT ==="
	print_lint_format_section
	echo
	echo "=== CONTAINERS ==="
	print_containers_section
	echo
	echo "=== CI WORKFLOWS ==="
	print_ci_workflows_section
	echo
	echo "=== DB CONTRACTS ==="
	print_db_contracts_section
	echo
	echo "=== PROTECTED PATHS ==="
	print_protected_paths_section
} > "${TMP_FILE}" 2>/dev/null || {
	rm -f "${TMP_FILE}" 2>/dev/null || true
	exit 0
}

mv "${TMP_FILE}" "${OUTPUT_FILE}" 2>/dev/null || {
	rm -f "${TMP_FILE}" 2>/dev/null || true
	exit 0
}

exit 0
