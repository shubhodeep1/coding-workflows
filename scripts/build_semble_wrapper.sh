#!/usr/bin/env bash
# build_semble_wrapper.sh — fail-soft Semble BM25 wrapper builder.
#
# Pinned semble (==0.1.3 today) ships the Python indexing/search primitives
# but not the documented `index`/`query` CLI surface that scripts/semble_helpers.sh
# expects. This script bridges the gap: it walks the repo with semble's own
# Python modules, builds a small BM25 index, pickles it to disk, and writes
# a tiny Python wrapper binary that implements `semble query --index ...
# --top-k N --format text` against the pickle so callers can invoke `semble`
# unchanged. Originally inlined in .github/workflows/validate.yml:631-786 and
# extracted here once the same fix-up was needed in workflow-log-analysis.yml.
#
# Inputs (env, all optional):
#   GITHUB_WORKSPACE   — repo root to index (defaults to PWD)
#   SEMBLE_INDEX_PATH  — output pickle path (defaults to ${RUNTIME_DIR:-${RUNNER_TEMP:-${PWD}}}/.semble-index)
#   SEMBLE_WRAPPER_DIR — output wrapper bin dir (defaults to <index_dir>/semble/bin)
#   GITHUB_ENV         — appended SEMBLE_AVAILABLE / SEMBLE_INDEX_AVAILABLE /
#                        SEMBLE_INDEX_PATH / SEMBLE_BIN on outcome
#   GITHUB_PATH        — appended wrapper dir on success
#
# Exit code: always 0 (fail-soft). Callers gate downstream behavior on the
# SEMBLE_*_AVAILABLE env keys this script writes to GITHUB_ENV.

set -euo pipefail

log()
{
	printf 'build_semble_wrapper: %s\n' "$*" >&2
}

write_env_kv()
{
	local key="${1:?write_env_kv: key required}"
	local value="${2:?write_env_kv: value required}"

	if [ -z "${GITHUB_ENV:-}" ]; then
		return 0
	fi
	if ! printf '%s=%s\n' "${key}" "${value}" >> "${GITHUB_ENV}" 2>/dev/null; then
		log "unable to append ${key} to GITHUB_ENV=${GITHUB_ENV}; continuing."
	fi
}

append_path_dir()
{
	local dir="${1:-}"

	if [ -z "${dir}" ] || [ -z "${GITHUB_PATH:-}" ]; then
		return 0
	fi
	if ! printf '%s\n' "${dir}" >> "${GITHUB_PATH}" 2>/dev/null; then
		log "unable to append ${dir} to GITHUB_PATH=${GITHUB_PATH}; continuing."
	fi
}

resolve_paths()
{
	local default_root=""

	repo_root="${GITHUB_WORKSPACE:-${PWD}}"

	if [ -n "${SEMBLE_INDEX_PATH:-}" ]; then
		index_path="${SEMBLE_INDEX_PATH}"
	else
		default_root="${RUNTIME_DIR:-${RUNNER_TEMP:-${PWD}}}"
		index_path="${default_root}/.semble-index"
	fi

	if [ -n "${SEMBLE_WRAPPER_DIR:-}" ]; then
		wrapper_dir="${SEMBLE_WRAPPER_DIR}"
	else
		wrapper_dir="$(dirname "${index_path}")/semble/bin"
	fi
	wrapper_path="${wrapper_dir}/semble"
}

mark_unavailable()
{
	local reason="${1:-unknown}"

	log "Semble wrapper unavailable: ${reason}"
	# Conservative: write SEMBLE_AVAILABLE=false too. install_semble.sh may
	# have written SEMBLE_AVAILABLE=true earlier, but if we land here the
	# wrapper at SEMBLE_BIN never gets installed, so semble_helpers.sh's
	# `semble query` contract can't be honored — disabling SEMBLE_AVAILABLE
	# keeps downstream `[ "${SEMBLE_AVAILABLE}" = "true" ]` gates accurate.
	write_env_kv "SEMBLE_AVAILABLE" "false"
	write_env_kv "SEMBLE_INDEX_AVAILABLE" "false"
	write_env_kv "SEMBLE_INDEX_PATH" "${index_path}"
	exit 0
}

build_index()
{
	rm -f "${index_path}"
	mkdir -p "${wrapper_dir}"

	if ! "${semble_python_path}" - "${repo_root}" "${index_path}" <<'PY'
import pickle
import sys
from pathlib import Path

import bm25s
from semble.index.chunker import chunk_source
from semble.index.file_walker import filter_extensions, language_for_path, walk_files
from semble.index.sparse import enrich_for_bm25
from semble.tokens import tokenize
from semble.version import __version__ as semble_version

repo_root = Path(sys.argv[1]).resolve()
index_path = Path(sys.argv[2])
extensions = filter_extensions(None, include_text_files=True)
MAX_INDEX_FILES = 5000
chunks = []
indexed_files = 0

for file_path in walk_files(repo_root, extensions, None):
    indexed_files += 1
    if indexed_files > MAX_INDEX_FILES:
        raise SystemExit(
            f"Semble indexing skipped after {MAX_INDEX_FILES} files; continuing without Semble."
        )
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    rel_path = file_path.resolve().relative_to(repo_root)
    chunks.extend(chunk_source(source, str(rel_path), language_for_path(file_path)))

if not chunks:
    raise SystemExit("No supported files found for Semble indexing.")

bm25_index = bm25s.BM25()
bm25_index.index([tokenize(enrich_for_bm25(chunk)) for chunk in chunks], show_progress=False)

index_path.parent.mkdir(parents=True, exist_ok=True)
with index_path.open("wb") as handle:
    pickle.dump(
        {
            "version": semble_version,
            "chunks": chunks,
            "bm25_index": bm25_index,
        },
        handle,
        protocol=pickle.HIGHEST_PROTOCOL,
    )
PY
	then
		rm -f "${index_path}"
		mark_unavailable "index-build-failed"
	fi
}

write_wrapper()
{
	# Shebang must match the interpreter that has semble + bm25s installed.
	# install_semble.sh honors SEMBLE_PYTHON_BIN (default: python3); using
	# `#!/usr/bin/env python3` here would silently pick the wrong python on
	# runners where install ran under a non-default interpreter (e.g.
	# python3.11), so `import semble` would fail at query time. Resolve the
	# absolute path now and bake it into the shebang.
	{
		printf '#!%s\n' "${semble_python_path}"
		cat <<'PY'
import argparse
import os
import pickle
import sys
from pathlib import Path


def _default_index_path() -> Path:
    return Path(__file__).resolve().parents[2] / ".semble-index"


def _print_version() -> int:
    index_path = Path(os.environ.get("SEMBLE_INDEX_PATH", str(_default_index_path())))
    try:
        with index_path.open("rb") as handle:
            payload = pickle.load(handle)
        print(f"semble {payload.get('version', 'unknown')}")
    except Exception:
        print("semble unknown")
    return 0


def _query(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="semble query")
    parser.add_argument("query")
    parser.add_argument("--index", required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--format", default="text")
    args, _extra = parser.parse_known_args(argv)

    if args.format != "text":
        print("semble wrapper supports only --format text", file=sys.stderr)
        return 2

    try:
        from semble.search import search_bm25
        with open(args.index, "rb") as handle:
            payload = pickle.load(handle)
    except Exception as exc:
        print(f"failed to load Semble index: {exc}", file=sys.stderr)
        return 1

    results = search_bm25(args.query, payload["bm25_index"], payload["chunks"], args.top_k, selector=None)
    for idx, result in enumerate(results, start=1):
        chunk = result.chunk
        if idx > 1:
            print()
        print(f"[{idx}] {chunk.file_path}:{chunk.start_line}-{chunk.end_line}")
        print(chunk.content.rstrip("\n"))
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--version":
        return _print_version()
    if len(sys.argv) >= 2 and sys.argv[1] == "query":
        return _query(sys.argv[2:])
    print("semble wrapper supports only `query` and `--version`.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
PY
	} > "${wrapper_path}"
	chmod +x "${wrapper_path}"
}

main()
{
	local repo_root=""
	local index_path=""
	local wrapper_dir=""
	local wrapper_path=""
	local semble_python_bin=""
	local semble_python_path=""

	resolve_paths

	# Resolve the python interpreter once so build_index() and the generated
	# wrapper agree with whatever install_semble.sh used. SEMBLE_PYTHON_BIN
	# is the contract there (defaults to python3); honor the same default
	# here so callers see consistent behavior across the trio of scripts.
	semble_python_bin="${SEMBLE_PYTHON_BIN:-python3}"
	semble_python_path="$(command -v "${semble_python_bin}" 2>/dev/null || true)"
	if [ -z "${semble_python_path}" ]; then
		mark_unavailable "python-interpreter-missing:${semble_python_bin}"
	fi

	if ! "${semble_python_path}" -c "import semble" >/dev/null 2>&1; then
		mark_unavailable "semble-python-module-missing"
	fi
	if ! "${semble_python_path}" -c "import bm25s" >/dev/null 2>&1; then
		mark_unavailable "bm25s-missing"
	fi

	build_index
	write_wrapper

	append_path_dir "${wrapper_dir}"
	write_env_kv "SEMBLE_AVAILABLE" "true"
	write_env_kv "SEMBLE_INDEX_AVAILABLE" "true"
	write_env_kv "SEMBLE_INDEX_PATH" "${index_path}"
	write_env_kv "SEMBLE_BIN" "${wrapper_path}"
	log "Semble wrapper ready at ${wrapper_path} (index=${index_path})."
}

main "$@"
