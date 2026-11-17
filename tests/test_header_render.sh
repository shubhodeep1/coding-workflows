#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

rendered_file="${tmpdir}/header.txt"

PYTHONDONTWRITEBYTECODE=1 \
PROMPT_PERSONA_PREFIX_ENABLED=false \
REPO_LEARNINGS='' \
bash "${REPO_ROOT}/scripts/render_prompt.sh" "${REPO_ROOT}/prompts/header.txt" > "${rendered_file}"

PYTHONDONTWRITEBYTECODE=1 python3 - "${rendered_file}" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path


text = Path(sys.argv[1]).read_text(encoding="utf-8")
expected = (
	"<compaction-rules>\n"
	"If you compact context:\n"
	"- Preserve the latest file-read result for every file still likely to be edited in this run.\n"
	"- Preserve the exact structured-output contract, including required section headings and JSON/Q-ID schemas.\n"
	"- When `UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true`, trust the host-side `.transcripts/<sanitized-run_id>-<sanitized-phase>-<ts>.json` archive instead of re-emitting raw transcript or tool-call history.\n"
	"</compaction-rules>\n"
)

assert text.startswith(
	"Role: AI pipeline phase agent. Goal: produce the artefact described below.\n"
), text
assert expected in text, text
assert text.count(expected) == 1, text
PY

echo "test_header_render.sh: PASS"
